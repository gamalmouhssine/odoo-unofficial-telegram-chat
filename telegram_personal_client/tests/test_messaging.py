import base64
import io
from datetime import timedelta

from PIL import Image

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import tagged

from ..models.telegram_message import PARAM_RETENTION_DAYS
from .common import TelegramTestCommon, errors, make_message, types

BOB_ID = 1001
GROUP_ID = 2002
SUPERGROUP_ID = 3003
NEWS_ID = 4004


def _png():
    buffer = io.BytesIO()
    Image.new('RGB', (8, 8), (120, 30, 200)).save(buffer, format='PNG')
    return buffer.getvalue()


@tagged('post_install', '-at_install')
class TestTelegramMessaging(TelegramTestCommon):

    def setUp(self):
        super().setUp()
        # Only account A is live here; B and C hold no session.
        self.account_b.state = self.account_c.state = 'disconnected'
        self.give_session(self.account_a)
        self.account_a._tg_purge_cached_data()
        tg = self.telegram
        tg.photo = _png()
        self.bob = types.User(
            id=BOB_ID, first_name='Bob', last_name='Builder', username='bob', phone='201111111111',
            contact=True, photo=types.UserProfilePhoto(photo_id=1, dc_id=2))
        self.carol = tg.add_entity(types.User(id=1002, first_name='Carol', contact=True))
        group = types.Chat(id=GROUP_ID, title='Team', photo=types.ChatPhotoEmpty(),
                           participants_count=3, date=None, version=1)
        supergroup = types.Channel(id=SUPERGROUP_ID, title='Big Group', photo=types.ChatPhotoEmpty(),
                                   date=None, megagroup=True)
        news = types.Channel(id=NEWS_ID, title='News', photo=types.ChatPhotoEmpty(), date=None, broadcast=True)
        self.bob_peer = types.PeerUser(BOB_ID)
        self.group_peer = types.PeerChat(GROUP_ID)
        self.supergroup_peer = types.PeerChannel(SUPERGROUP_ID)
        tg.add_dialog(self.bob, make_message(200, self.bob_peer, 'Hello', minutes_ago=5), unread=2)
        tg.add_dialog(group, make_message(20, self.group_peer, 'Team meeting at 3',
                                          from_id=self.bob_peer, sender=self.bob, minutes_ago=10))
        tg.add_dialog(supergroup, make_message(30, self.supergroup_peer, 'Welcome', minutes_ago=20))
        tg.add_dialog(news, make_message(40, types.PeerChannel(NEWS_ID), 'Breaking', minutes_ago=1))
        tg.contacts = [self.bob, self.carol]
        self.Chat = self.env['telegram.chat']
        self.Message = self.env['telegram.message']

    def _chat(self, tg_chat_id):
        return self.Chat.search([('account_id', '=', self.account_a.id), ('tg_chat_id', '=', str(tg_chat_id))])

    def _sync(self, ticks=2):
        self.run_engine(ticks=ticks)

    def _bob_chat(self):
        self._sync()
        return self._chat(BOB_ID)

    def test_chat_list_contacts_and_previews(self):
        self._sync()
        bob = self._chat(BOB_ID)
        self.assertEqual((bob.title, bob.chat_type, bob.unread_count), ('Bob Builder', 'private', 2))
        self.assertEqual(bob.last_message_preview, 'Hello')
        self.assertEqual(bob.contact_id.tg_user_id, str(BOB_ID))
        self.assertTrue(bob.has_avatar, "Profile photos are fetched in the background.")
        group = self._chat(-GROUP_ID)
        self.assertEqual((group.title, group.chat_type), ('Team', 'basic_group'))
        top = group.message_ids
        self.assertEqual((top.body, top.sender_name), ('Team meeting at 3', 'Bob Builder'))
        self.assertEqual(self._chat(-1000000000000 - SUPERGROUP_ID).chat_type, 'supergroup')
        self.assertFalse(self._chat(-1000000000000 - NEWS_ID), "Broadcast channels are not shown in v1.")
        contacts = self.env['telegram.contact'].search([('account_id', '=', self.account_a.id)])
        self.assertEqual(set(contacts.mapped('name')), {'Bob Builder', 'Carol'})
        self.assertTrue(self.account_a.dialogs_synced_at and self.account_a.contacts_synced_at)
        # Unchanged data is not re-synchronized at every window.
        self._sync()
        self.assertEqual(self.telegram.names().count('get_dialogs'), 1)

    def test_live_messages_are_stored_once(self):
        bob = self._bob_chat()
        update = types.UpdateNewMessage(make_message(201, self.bob_peer, 'How are you?'), pts=1, pts_count=1)
        self.telegram.updates = [update]
        self._sync(ticks=1)
        self.assertEqual(bob.last_message_preview, 'How are you?')
        self.assertEqual(bob.unread_count, 3)
        self.telegram.updates = [update]  # delivered again (e.g. after a reconnect)
        self._sync(ticks=1)
        self.assertEqual(self.Message.search_count([('chat_id', '=', bob.id), ('tg_message_id', '=', '201')]), 1)
        self.assertEqual(bob.unread_count, 3)

    def test_large_groups_are_cached_only_once_opened(self):
        self._sync()
        supergroup = self._chat(-1000000000000 - SUPERGROUP_ID)
        new = types.UpdateNewChannelMessage(make_message(31, self.supergroup_peer, 'Hi all'), pts=1, pts_count=1)
        self.telegram.updates = [new]
        self._sync(ticks=1)
        self.assertEqual(supergroup.last_message_preview, 'Hi all')
        self.assertFalse(supergroup.message_ids.filtered(lambda m: m.tg_message_id == '31'))
        # Opening the chat loads its history; from then on new messages are cached.
        marked = -1000000000000 - SUPERGROUP_ID
        self.telegram.history[marked] = [make_message(31, self.supergroup_peer, 'Hi all'),
                                         make_message(30, self.supergroup_peer, 'Welcome')]
        self.Chat.with_user(self.user_a)._tg_request_history(supergroup.id)
        self._sync(ticks=1)
        self.assertTrue(supergroup.history_loaded and supergroup.history_complete)
        self.telegram.updates = [types.UpdateNewChannelMessage(
            make_message(32, self.supergroup_peer, 'Later'), pts=2, pts_count=1)]
        self._sync(ticks=1)
        self.assertEqual(sorted(supergroup.message_ids.mapped('tg_message_id')), ['30', '31', '32'])

    def test_send_message(self):
        bob = self._bob_chat()
        messages = self.Message.with_user(self.user_a)
        payload = messages._tg_send(bob.id, '*not markdown*', 'local-0001')['message']
        self.assertEqual(payload['state'], 'pending')
        self.assertEqual(messages._tg_send(bob.id, '*not markdown*', 'local-0001')['message']['id'], payload['id'],
                         "The same local id never sends twice.")
        self._sync(ticks=1)
        message = self.Message.browse(payload['id'])
        self.assertEqual(message.state, 'sent')
        self.assertTrue(message.tg_message_id)
        sent = [call for call in self.telegram.calls if call[0] == 'send_message']
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][2:], ('*not markdown*', None), "Text is sent as typed (parse_mode=None).")
        self.assertEqual(bob.last_message_preview, '*not markdown*')
        # Telegram echoes our own message as an update: no duplicate.
        echo = make_message(int(message.tg_message_id), self.bob_peer, '*not markdown*', out=True)
        self.telegram.updates = [types.UpdateNewMessage(echo, pts=2, pts_count=1)]
        self._sync(ticks=1)
        self.assertEqual(self.Message.search_count([('chat_id', '=', bob.id), ('body', '=', '*not markdown*')]), 1)

    def test_failed_send_and_retry(self):
        bob = self._bob_chat()
        messages = self.Message.with_user(self.user_a)
        payload = messages._tg_send(bob.id, 'Hello?', 'local-0002')['message']
        self.telegram.failures['send_message'] = errors.ChatWriteForbiddenError(request=None)
        self._sync(ticks=1)
        message = self.Message.browse(payload['id'])
        self.assertEqual(message.state, 'failed')
        self.assertIn('cannot send', message.error_message)
        self.assertFalse(bob.can_send)
        del self.telegram.failures['send_message']
        bob.can_send = True
        messages._tg_retry(message.id)
        self._sync(ticks=1)
        self.assertEqual(message.state, 'sent')

    def test_sending_is_paced(self):
        bob = self._bob_chat()
        messages = self.Message.with_user(self.user_a)
        ids = [messages._tg_send(bob.id, f'message {n}', f'local-pace-{n:03d}')['message']['id'] for n in range(21)]
        self._sync(ticks=1)
        states = self.Message.browse(ids).mapped('state')
        self.assertEqual(states.count('sent'), 20)
        self.assertEqual(self.Message.browse(ids[-1]).state, 'failed')

    def test_history_is_loaded_page_by_page(self):
        bob = self._bob_chat()
        # 120 messages, newest (id 200) first, as Telegram returns them.
        self.telegram.history[BOB_ID] = [
            make_message(n, self.bob_peer, f'message {n}', minutes_ago=205 - n) for n in range(200, 80, -1)]
        chats = self.Chat.with_user(self.user_a)
        for expected_count, complete in ((50, False), (100, False), (120, True)):
            chats._tg_request_history(bob.id)
            self._sync(ticks=1)
            self.assertEqual(len(bob.message_ids), expected_count)
            self.assertEqual(bob.history_complete, complete)
        offsets = [call[2] for call in self.telegram.calls if call[0] == 'get_messages']
        self.assertEqual(offsets, [0, 151, 101], "First page from the newest, then below the oldest cached.")
        listing = self.Message.with_user(self.user_a)._tg_list(bob.id, limit=50)
        self.assertTrue(listing['has_more'])
        self.assertEqual(listing['messages'][-1]['tg_message_id'], '200', "Newest last.")

    def test_read_receipts(self):
        bob = self._bob_chat()
        self.Chat.with_user(self.user_a)._tg_mark_read(bob.id)
        self._sync(ticks=1)
        self.assertIn(('read_ack', BOB_ID, 200), self.telegram.calls)
        self.assertEqual(bob.unread_count, 0)
        self.telegram.updates = [
            types.UpdateReadHistoryOutbox(peer=self.bob_peer, max_id=200, pts=3, pts_count=1),
            types.UpdateReadHistoryInbox(peer=self.bob_peer, max_id=200, still_unread_count=0, pts=4, pts_count=1),
        ]
        self._sync(ticks=1)
        self.assertEqual(bob.last_read_outbox_key, 200)

    def test_edits_and_deletions(self):
        bob = self._bob_chat()
        edited = make_message(200, self.bob_peer, 'Hello (edited)', edit_date=fields.Datetime.now())
        self.telegram.updates = [types.UpdateEditMessage(edited, pts=5, pts_count=1)]
        self._sync(ticks=1)
        message = bob.message_ids.filtered(lambda m: m.tg_message_id == '200')
        self.assertEqual((message.body, message.is_edited), ('Hello (edited)', True))
        self.telegram.updates = [types.UpdateDeleteMessages(messages=[200], pts=6, pts_count=1)]
        self._sync(ticks=1)
        self.assertFalse(message.exists())

    def test_open_chat_from_contacts(self):
        self._sync()
        carol = self.env['telegram.contact'].search([('account_id', '=', self.account_a.id), ('tg_user_id', '=', '1002')])
        contacts = self.env['telegram.contact'].with_user(self.user_a)
        self.assertEqual(contacts._tg_open_chat(carol.id), {'chat_id': False})
        self._sync(ticks=1)
        chat = self._chat(1002)
        self.assertEqual((chat.title, chat.contact_id), ('Carol', carol))
        self.assertEqual(contacts._tg_open_chat(carol.id), {'chat_id': chat.id})
        self.assertIn('Carol', [c['name'] for c in contacts._tg_list('car')['contacts']])

    def test_other_users_cannot_touch_this_account(self):
        bob = self._bob_chat()
        message = bob.message_ids[:1]
        carol = self.env['telegram.contact'].search([('account_id', '=', self.account_a.id), ('tg_user_id', '=', '1002')])
        as_b = lambda model: self.env[model].with_user(self.user_b)  # noqa: E731
        for call in (
            lambda: as_b('telegram.message')._tg_list(bob.id),
            lambda: as_b('telegram.message')._tg_send(bob.id, 'hi', 'local-evil-01'),
            lambda: as_b('telegram.message')._tg_retry(message.id),
            lambda: as_b('telegram.chat')._tg_request_history(bob.id),
            lambda: as_b('telegram.chat')._tg_mark_read(bob.id),
            lambda: as_b('telegram.contact')._tg_open_chat(carol.id),
        ):
            with self.assertRaises(AccessError):
                call()
        self.assertNotIn(bob.id, [chat['id'] for chat in as_b('telegram.chat')._tg_list()['chats']])
        self.assertNotIn(carol.id, [contact['id'] for contact in as_b('telegram.contact')._tg_list()['contacts']])

    def test_retention(self):
        bob = self._bob_chat()
        old = self.Message.create({'chat_id': bob.id, 'tg_message_id': '1', 'sort_key': 1,
                                   'date': fields.Datetime.now() - timedelta(days=40), 'body': 'old'})
        self.Message._cron_purge_old_messages()
        self.assertTrue(old.exists(), "0 days keeps everything.")
        self.env['ir.config_parameter'].set_param(PARAM_RETENTION_DAYS, 30)
        self.Message._cron_purge_old_messages()
        self.assertFalse(old.exists())
        self.assertTrue(bob.message_ids)

    def test_avatar_is_a_real_image(self):
        bob = self._bob_chat()
        self.assertTrue(base64.b64decode(bob.avatar_128).startswith(b'\x89PNG'))
