"""Chats, messages and contacts of connected accounts (mixed into Engine).

Live updates are only collected by Telethon's event handler (no database
work there); each engine tick drains them in order, together with queued
commands, so every database change goes through Engine._call and can be
re-applied after a failed commit.
"""
import base64
import collections
import logging
import time
from datetime import timedelta

from odoo import fields

from .errors import FATAL_CODES, error_code
from .sync import SYNC_UPDATES, chat_row, contact_row, message_row, naive_utc, peer_key
from .vendor import ensure_libraries

ensure_libraries()
from telethon import events, functions, types, utils  # noqa: E402

_logger = logging.getLogger(__name__)

DIALOG_LIMIT = 100
HISTORY_PAGE = 50
AVATARS_PER_TICK = 2
DIALOG_RESYNC = timedelta(hours=6)
CONTACT_RESYNC = timedelta(hours=24)
ERROR_PAUSE_SECONDS = 15
# Human pace, well under Telegram's limits (anti-ban rule C.11): (count, seconds).
SEND_LIMIT = (20, 60)
HISTORY_LIMIT = (30, 60)
HISTORY_PER_CHAT = (1, 2)
OPEN_CHAT_LIMIT = (20, 60)

_NEW = (types.UpdateNewMessage, types.UpdateNewChannelMessage)
_EDIT = (types.UpdateEditMessage, types.UpdateEditChannelMessage)


class MessagingState:
    """Per-account messaging state kept on the runtime for one window."""

    def __init__(self):
        self.inbox = []
        self.chat_types = {}   # tg_chat_id -> chat_type, for chats Odoo shows
        self.titles = {}       # tg_chat_id -> title
        self.skipped = set()   # chats Odoo does not show (broadcast channels)
        self.names = {}        # Telegram peer id -> display name
        self.names_source_size = -1
        self.rate = collections.defaultdict(collections.deque)
        self.sync_dialogs = False
        self.sync_contacts = False
        self.paused_until = 0.0

    async def on_update(self, update):
        self.inbox.append(update)


class MessagingMixin:

    # Setup ------------------------------------------------------------------------

    def _prepare_messaging(self, account, runtime):
        """Register the update handler (before connecting, so the catch-up of
        missed updates is not lost) and decide what to synchronize."""
        state = runtime.messaging = MessagingState()
        runtime.client.add_event_handler(state.on_update, events.Raw(SYNC_UPDATES))
        for chat in account.chat_ids:
            state.chat_types[chat.tg_chat_id] = chat.chat_type
            state.titles[chat.tg_chat_id] = chat.title or ''
        now = fields.Datetime.now()
        state.sync_dialogs = not account.dialogs_synced_at or account.dialogs_synced_at < now - DIALOG_RESYNC
        state.sync_contacts = not account.contacts_synced_at or account.contacts_synced_at < now - CONTACT_RESYNC

    async def _maintain_messaging(self, account, runtime):
        state = runtime.messaging
        if state is None or time.monotonic() < state.paused_until:
            return
        try:
            if state.sync_dialogs:
                state.sync_dialogs = False
                await self._sync_dialogs(account, runtime)
            if state.sync_contacts:
                state.sync_contacts = False
                await self._sync_contacts(account, runtime)
            await self._drain_inbox(account, runtime)
            await self._fetch_avatars(account, runtime)
        except Exception as error:
            code, retry_after, _detail = error_code(error)
            if code in FATAL_CODES:
                raise
            if code == 'UNEXPECTED_ERROR':
                _logger.exception("Telegram engine: synchronization failed for account %s", account.id)
            else:
                _logger.info("Telegram engine: synchronization paused for account %s (%s)", account.id, code)
            state.paused_until = time.monotonic() + (retry_after or ERROR_PAUSE_SECONDS)

    # Synchronization ------------------------------------------------------------------

    async def _sync_dialogs(self, account, runtime):
        """The chat list: once per connection, then kept current by updates."""
        state = runtime.messaging
        dialogs = await runtime.client.get_dialogs(limit=DIALOG_LIMIT, archived=False)
        contacts, chats, tops = [], [], []
        for dialog in dialogs:
            row = chat_row(dialog.entity, dialog)
            if row is None:
                state.skipped.add(peer_key(dialog.entity))
                continue
            if isinstance(dialog.entity, types.User):
                contacts.append(contact_row(dialog.entity))
            chats.append(row)
            state.chat_types[row['tg_chat_id']] = row['chat_type']
            state.titles[row['tg_chat_id']] = row['title']
            if isinstance(dialog.message, types.Message):
                tops.append(dialog.message)
        if contacts:
            self._call('telegram.contact', '_tg_upsert', account.id, contacts)
        if chats:
            self._call('telegram.chat', '_tg_upsert', account.id, chats)
        rows = [row for row in (self._message_row(account, runtime, message) for message in tops) if row]
        if rows:
            self._call('telegram.message', '_tg_upsert', account.id, rows, 'dialog')
        self._update(account, {'dialogs_synced_at': fields.Datetime.now()})

    async def _sync_contacts(self, account, runtime):
        result = await runtime.client(functions.contacts.GetContactsRequest(hash=0))
        rows = [contact_row(user) for user in getattr(result, 'users', []) if isinstance(user, types.User)]
        if rows:
            self._call('telegram.contact', '_tg_upsert', account.id, rows)
        self._update(account, {'contacts_synced_at': fields.Datetime.now()})

    async def _drain_inbox(self, account, runtime):
        state = runtime.messaging
        if not state.inbox:
            return
        updates, state.inbox = state.inbox, []
        new_rows, edited_rows = [], []
        for update in updates:
            if isinstance(update, _NEW + _EDIT):
                message = update.message
                if not isinstance(message, types.Message):
                    continue  # service messages (joins, title changes…) are not shown in v1
                tg_chat_id = peer_key(message.peer_id)
                if tg_chat_id in state.skipped:
                    continue
                if tg_chat_id not in state.chat_types and not await self._ensure_chat(account, runtime, message.peer_id):
                    continue
                row = self._message_row(account, runtime, message)
                (edited_rows if isinstance(update, _EDIT) else new_rows).append(row)
            elif isinstance(update, types.UpdateDeleteMessages):
                self._call('telegram.message', '_tg_delete', account.id, False, [str(i) for i in update.messages])
            elif isinstance(update, types.UpdateDeleteChannelMessages):
                self._call('telegram.message', '_tg_delete', account.id,
                           peer_key(types.PeerChannel(update.channel_id)), [str(i) for i in update.messages])
            elif isinstance(update, types.UpdateReadHistoryInbox):
                self._call('telegram.chat', '_tg_apply_read', account.id, peer_key(update.peer),
                           float(update.max_id), update.still_unread_count, None)
            elif isinstance(update, types.UpdateReadChannelInbox):
                self._call('telegram.chat', '_tg_apply_read', account.id, peer_key(types.PeerChannel(update.channel_id)),
                           float(update.max_id), update.still_unread_count, None)
            elif isinstance(update, types.UpdateReadHistoryOutbox):
                self._call('telegram.chat', '_tg_apply_read', account.id, peer_key(update.peer),
                           None, None, float(update.max_id))
            elif isinstance(update, types.UpdateReadChannelOutbox):
                self._call('telegram.chat', '_tg_apply_read', account.id, peer_key(types.PeerChannel(update.channel_id)),
                           None, None, float(update.max_id))
        if new_rows:
            self._call('telegram.message', '_tg_upsert', account.id, new_rows, 'live')
        if edited_rows:
            self._call('telegram.message', '_tg_upsert', account.id, edited_rows, 'edit')

    async def _ensure_chat(self, account, runtime, peer):
        """A message arrived in a chat Odoo does not know yet: add it."""
        state = runtime.messaging
        try:
            entity = await runtime.client.get_entity(peer)
        except Exception as error:
            _logger.info("Telegram engine: cannot resolve a new chat for account %s (%s)",
                         account.id, error_code(error)[0])
            return False
        row = chat_row(entity)
        if row is None:
            state.skipped.add(peer_key(peer))
            return False
        if isinstance(entity, types.User):
            self._call('telegram.contact', '_tg_upsert', account.id, [contact_row(entity)])
        self._call('telegram.chat', '_tg_upsert', account.id, [row])
        state.chat_types[row['tg_chat_id']] = row['chat_type']
        state.titles[row['tg_chat_id']] = row['title']
        return True

    async def _fetch_avatars(self, account, runtime):
        """Small profile photos, a couple per tick, never retried in a loop."""
        chats = self.env['telegram.chat'].sudo().search([
            ('account_id', '=', account.id), ('photo_available', '=', True), ('has_avatar', '=', False),
        ], limit=AVATARS_PER_TICK)
        for chat in chats:
            data = None
            try:
                entity = await runtime.client.get_input_entity(int(chat.tg_chat_id))
                data = await runtime.client.download_profile_photo(entity, file=bytes, download_big=False)
            except Exception as error:
                if error_code(error)[0] in FATAL_CODES | {'FLOOD_WAIT'}:
                    raise
                _logger.debug("Telegram engine: no avatar for chat %s: %s", chat.id, error)
            self._call('telegram.chat', '_tg_set_avatar', chat.id, base64.b64encode(data).decode() if data else False)

    def _message_row(self, account, runtime, message):
        tg_chat_id = peer_key(message.peer_id)
        chat_type = runtime.messaging.chat_types.get(tg_chat_id)
        if chat_type is None:
            return None
        return message_row(message, tg_chat_id, self._sender_name(account, runtime, message, chat_type, tg_chat_id))

    def _sender_name(self, account, runtime, message, chat_type, tg_chat_id):
        state = runtime.messaging
        if message.out:
            return account.tg_name or ''
        if chat_type == 'private' or message.from_id is None:
            return state.titles.get(tg_chat_id, '')
        try:
            sender = message.sender  # filled for messages from get_dialogs / get_messages
        except AttributeError:
            sender = None
        if sender is not None:
            return utils.get_display_name(sender)
        return self._cached_name(runtime, utils.get_peer_id(message.from_id))

    def _cached_name(self, runtime, peer_id):
        """Names from the entities Telethon caches in the session (users that
        appear in updates), so group senders never cost a network call."""
        state = runtime.messaging
        entities = runtime.session._entities
        if peer_id not in state.names and len(entities) != state.names_source_size:
            state.names = {row[0]: row[4] for row in entities if len(row) > 4 and row[4]}
            state.names_source_size = len(entities)
        return state.names.get(peer_id, '')

    # Commands from the web client --------------------------------------------------------

    async def _send_message(self, account, runtime, message_id):
        message = self.env['telegram.message'].sudo().browse(message_id).exists()
        if not message or message.state != 'pending' or message.account_id != account:
            return
        if runtime.messaging is None or not self._allow(runtime, 'send', *SEND_LIMIT):
            self._call('telegram.message', '_tg_send_result', message.id,
                       {'state': 'failed', 'error_message': account._tg_error_message('SEND_LIMIT')})
            return
        try:
            entity = await runtime.client.get_input_entity(int(message.chat_id.tg_chat_id))
            # parse_mode=None: send exactly what the user typed (no Markdown).
            sent = await runtime.client.send_message(entity, message.body, parse_mode=None)
        except Exception as error:
            code, retry_after, detail = error_code(error)
            if code in FATAL_CODES:
                raise
            vals = {'state': 'failed', 'error_message': account._tg_error_message(code, retry_after, detail)}
            if code == 'CHAT_WRITE_FORBIDDEN':
                vals['can_send'] = False
            self._call('telegram.message', '_tg_send_result', message.id, vals)
            return
        self._call('telegram.message', '_tg_send_result', message.id, {
            'state': 'sent', 'tg_message_id': str(sent.id), 'sort_key': float(sent.id),
            'date': naive_utc(sent.date), 'error_message': False,
        })

    async def _load_history(self, account, runtime, chat_id):
        chat = self.env['telegram.chat'].sudo().browse(chat_id).exists()
        if not chat or chat.account_id != account or runtime.messaging is None:
            return
        if not (self._allow(runtime, f'history:{chat.id}', *HISTORY_PER_CHAT)
                and self._allow(runtime, 'history', *HISTORY_LIMIT)):
            self._call('telegram.chat', '_tg_history_loaded', chat.id, chat.history_complete)
            return
        state = runtime.messaging
        state.chat_types.setdefault(chat.tg_chat_id, chat.chat_type)
        state.titles.setdefault(chat.tg_chat_id, chat.title or '')
        # First load: start from the newest message, since only scattered
        # messages may be cached (chat-list previews, live updates). After
        # that, each page continues below the oldest cached message.
        offset_id = 0
        if chat.history_loaded:
            oldest = self.env['telegram.message'].sudo().search(
                [('chat_id', '=', chat.id), ('tg_message_id', '!=', False)], order='sort_key asc', limit=1)
            offset_id = int(oldest.sort_key) if oldest else 0
        try:
            entity = await runtime.client.get_input_entity(int(chat.tg_chat_id))
            messages = await runtime.client.get_messages(entity, limit=HISTORY_PAGE, offset_id=offset_id)
        except Exception as error:
            if error_code(error)[0] in FATAL_CODES:
                raise
            _logger.info("Telegram engine: history not loaded for chat %s (%s)", chat.id, error_code(error)[0])
            self._call('telegram.chat', '_tg_history_loaded', chat.id, chat.history_complete)
            return
        rows = [row for row in (self._message_row(account, runtime, message)
                                for message in messages if isinstance(message, types.Message)) if row]
        if rows:
            self._call('telegram.message', '_tg_upsert', account.id, rows, 'history')
        self._call('telegram.chat', '_tg_history_loaded', chat.id, len(messages) < HISTORY_PAGE)

    async def _mark_read(self, account, runtime, chat_id):
        """Read receipts are sent only when the user has the chat open."""
        chat = self.env['telegram.chat'].sudo().browse(chat_id).exists()
        if not chat or chat.account_id != account or runtime.messaging is None:
            return
        newest = self.env['telegram.message'].sudo().search(
            [('chat_id', '=', chat.id), ('tg_message_id', '!=', False)], order='sort_key desc', limit=1)
        max_id = int(max(newest.sort_key, chat.last_read_inbox_key)) if newest else None
        try:
            entity = await runtime.client.get_input_entity(int(chat.tg_chat_id))
            await runtime.client.send_read_acknowledge(entity, max_id=max_id)
        except Exception as error:
            if error_code(error)[0] in FATAL_CODES:
                raise
            _logger.info("Telegram engine: could not mark chat %s as read (%s)", chat.id, error_code(error)[0])
            return
        self._call('telegram.chat', '_tg_apply_read', account.id, chat.tg_chat_id,
                   float(max_id) if max_id else None, 0, None)

    async def _open_chat(self, account, runtime, contact_id):
        contact = self.env['telegram.contact'].sudo().browse(contact_id).exists()
        if not contact or contact.account_id != account or runtime.messaging is None:
            return
        if not self._allow(runtime, 'open_chat', *OPEN_CHAT_LIMIT):
            return
        try:
            entity = await runtime.client.get_entity(int(contact.tg_user_id))
        except Exception as error:
            if error_code(error)[0] in FATAL_CODES:
                raise
            _logger.info("Telegram engine: cannot open chat with contact %s (%s)", contact.id, error_code(error)[0])
            return
        row = chat_row(entity)
        if row is None:
            return
        self._call('telegram.contact', '_tg_upsert', account.id, [contact_row(entity)])
        self._call('telegram.chat', '_tg_upsert', account.id, [row])
        self._call('telegram.chat', '_tg_notify_open', account.id, row['tg_chat_id'])
        runtime.messaging.chat_types[row['tg_chat_id']] = row['chat_type']
        runtime.messaging.titles[row['tg_chat_id']] = row['title']

    # Helpers ------------------------------------------------------------------------------

    def _allow(self, runtime, key, maximum, window):
        """In-memory sliding-window limiter for one window of the engine."""
        if runtime.messaging is None:
            return False
        events_ = runtime.messaging.rate[key]
        now = time.monotonic()
        while events_ and events_[0] <= now - window:
            events_.popleft()
        if len(events_) >= maximum:
            return False
        events_.append(now)
        return True

    def _call(self, model, method, *args):
        self._mutate(self._apply_model, model, method, args)

    def _apply_model(self, model, method, args):
        getattr(self.env[model].sudo(), method)(*args)
