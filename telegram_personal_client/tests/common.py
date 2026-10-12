import datetime
import os
from types import SimpleNamespace
from unittest.mock import patch

from odoo import Command
from odoo.tests import TransactionCase, new_test_user

from ..engine import runner
from ..engine.cipher import ENV_SESSION_KEY, SessionCipher
from ..engine.runner import Engine
from ..engine.session import DbSession
from ..engine.vendor import ensure_libraries
from ..models.telegram_account import ENV_API_HASH, ENV_API_ID, PARAM_API_HASH, PARAM_API_ID

ensure_libraries()
from telethon import errors, types, utils  # noqa: E402
from telethon.crypto import AuthKey  # noqa: E402

USER_GROUP = 'telegram_personal_client.group_telegram_user'
MANAGER_GROUP = 'telegram_personal_client.group_telegram_manager'
API_ID = '1234567'
API_HASH = '0123456789abcdef0123456789abcdef'


class FakeTelegram:
    """Plays Telegram's side for the engine, so tests never touch the network.
    Its clients mimic the parts of Telethon's TelegramClient the engine uses."""

    def __init__(self):
        self.calls = []
        self.authorized = set()
        self.failures = {}
        self.password_required = False
        self.qr_accepted = False
        self.network_down = False
        self.user = types.User(
            id=777000123456, first_name='Ada', last_name='Lovelace', username='ada', phone='201234567845')
        # Messaging: entities by marked id, dialog list, history per chat
        # (newest first), contacts, updates delivered right after connecting.
        self.entities = {}
        self.dialogs = []
        self.history = {}
        self.contacts = []
        self.updates = []
        self.photo = b''
        self._next_message_id = 5000

    def client(self, session, catch_up):
        return FakeClient(self, session)

    def names(self):
        return [call[0] for call in self.calls]

    def add_entity(self, entity):
        self.entities[utils.get_peer_id(entity)] = entity
        return entity

    def add_dialog(self, entity, top_message=None, unread=0, pinned=False, read_outbox=0):
        self.add_entity(entity)
        self.dialogs.append(SimpleNamespace(
            entity=entity, message=top_message, unread_count=unread, pinned=pinned,
            dialog=SimpleNamespace(read_inbox_max_id=0, read_outbox_max_id=read_outbox)))

    def next_message_id(self):
        self._next_message_id += 1
        return self._next_message_id


def make_message(message_id, peer, text='', out=False, from_id=None, sender=None, minutes_ago=0, **extra):
    """A Telegram message as Telethon returns it."""
    date = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0) - datetime.timedelta(minutes=minutes_ago)
    message = types.Message(id=message_id, peer_id=peer, date=date, message=text, out=out, from_id=from_id, **extra)
    if sender is not None:
        message._sender = sender  # what Telethon fills for messages from get_dialogs / get_messages
    return message


class FakeClient:
    def __init__(self, server, session):
        self.server = server
        self.session = session
        self.handlers = []

    @property
    def key(self):
        return self.session.auth_key.key

    def _record(self, name, *details):
        self.server.calls.append((name, *details))
        error = self.server.failures.get(name)
        if error:
            raise error

    async def connect(self):
        if self.server.network_down:
            raise ConnectionError("network down")
        if self.session.auth_key is None:
            self.session.auth_key = AuthKey(os.urandom(256))
        if self.key in self.server.authorized:
            await self._deliver_updates()

    async def disconnect(self):
        pass

    async def is_user_authorized(self):
        return self.key in self.server.authorized

    async def send_code_request(self, phone):
        self._record('send_code', phone)
        return types.auth.SentCode(type=types.auth.SentCodeTypeApp(length=5), phone_code_hash='hash-123')

    async def sign_in(self, phone=None, code=None, *, password=None, phone_code_hash=None):
        if password is None:
            self._record('sign_in_code', phone, code, phone_code_hash)
            if self.server.password_required:
                raise errors.SessionPasswordNeededError(request=None)
        else:
            self._record('sign_in_password')
        self.server.authorized.add(self.key)
        return self.server.user

    async def log_out(self):
        self._record('log_out')
        self.server.authorized.discard(self.key)
        return True

    def add_event_handler(self, callback, event):
        self.handlers.append((callback, event))

    async def _deliver_updates(self):
        """Like Telethon's catch-up: pending updates arrive after connecting."""
        updates, self.server.updates = self.server.updates, []
        for update in updates:
            for callback, event in self.handlers:
                if event.types and isinstance(update, event.types):
                    await callback(update)

    async def get_dialogs(self, limit=None, archived=None):
        self._record('get_dialogs')
        return list(self.server.dialogs)[:limit]

    async def get_entity(self, peer):
        key = peer if isinstance(peer, int) else utils.get_peer_id(peer)
        self._record('get_entity', key)
        if key not in self.server.entities:
            raise ValueError(f"Could not find the input entity for {key}")
        return self.server.entities[key]

    async def get_input_entity(self, peer):
        if peer not in self.server.entities:
            raise ValueError(f"Could not find the input entity for {peer}")
        return peer

    async def get_messages(self, entity, limit=None, offset_id=0):
        self._record('get_messages', entity, offset_id)
        history = self.server.history.get(entity, [])
        older = [message for message in history if not offset_id or message.id < offset_id]
        return older[:limit]

    async def send_message(self, entity, text, parse_mode=()):
        self._record('send_message', entity, text, parse_mode)
        peer = utils.get_peer(entity)
        return make_message(self.server.next_message_id(), peer, text, out=True)

    async def send_read_acknowledge(self, entity, max_id=None):
        self._record('read_ack', entity, max_id)
        return True

    async def download_profile_photo(self, entity, file=None, download_big=True):
        self._record('download_photo', entity)
        return self.server.photo

    async def _switch_dc(self, dc_id):
        self._record('switch_dc', dc_id)

    async def _on_login(self, user):
        pass

    async def __call__(self, request):
        name = type(request).__name__
        self._record(name, getattr(request, 'offline', None))
        if name == 'ExportLoginTokenRequest':
            if self.server.qr_accepted:
                if self.server.password_required:
                    raise errors.SessionPasswordNeededError(request=None)
                self.server.authorized.add(self.key)
                return types.auth.LoginTokenSuccess(authorization=types.auth.Authorization(user=self.server.user))
            expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=30)
            return types.auth.LoginToken(expires=expires, token=b'qr-token-bytes')
        if name == 'GetPasswordRequest':
            return SimpleNamespace(hint='pet name')
        if name == 'UpdateStatusRequest':
            return True
        if name == 'GetContactsRequest':
            return SimpleNamespace(users=list(self.server.contacts))
        raise NotImplementedError(name)


class TelegramTestCommon(TransactionCase):
    """Two Telegram users in the main company, one in a second company, a
    Telegram administrator, and an internal user without Telegram access.
    Each Telegram user owns a connected account with one contact, chat and
    message. The Telegram API is configured; FakeTelegram plays Telegram."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Ignore secrets set in the test server's environment.
        cls.startClassPatcher(patch.dict(os.environ))
        for name in (ENV_SESSION_KEY, ENV_API_ID, ENV_API_HASH):
            os.environ.pop(name, None)
        cls.startClassPatcher(patch.object(runner, 'TICK_SECONDS', 0))
        params = cls.env['ir.config_parameter']
        params.set_param(PARAM_API_ID, API_ID)
        params.set_param(PARAM_API_HASH, API_HASH)

        cls.company_2 = cls.env['res.company'].create({'name': 'Telegram Test Company 2'})

        cls.user_a = new_test_user(cls.env, login='tg_user_a', groups=USER_GROUP)
        cls.user_b = new_test_user(cls.env, login='tg_user_b', groups=USER_GROUP)
        cls.user_c = new_test_user(
            cls.env, login='tg_user_c', groups=USER_GROUP,
            company_id=cls.company_2.id, company_ids=[Command.set(cls.company_2.ids)])
        cls.manager = new_test_user(cls.env, login='tg_manager', groups=MANAGER_GROUP)
        cls.outsider = new_test_user(cls.env, login='tg_outsider', groups='base.group_user')

        cls.account_a, cls.contact_a, cls.chat_a, cls.message_a = cls._create_account_data(cls.user_a)
        cls.account_b, cls.contact_b, cls.chat_b, cls.message_b = cls._create_account_data(cls.user_b)
        cls.account_c, cls.contact_c, cls.chat_c, cls.message_c = cls._create_account_data(cls.user_c)

    def setUp(self):
        super().setUp()
        self.telegram = FakeTelegram()

    def run_engine(self, ticks=1):
        engine = Engine(self.env, window=30, commit=lambda: None, client_factory=self.telegram.client)
        return engine.run(max_ticks=ticks)

    def give_session(self, account):
        """Give an account a valid session on the fake Telegram."""
        session = DbSession()
        session.auth_key = AuthKey(os.urandom(256))
        self.telegram.authorized.add(session.auth_key.key)
        account.sudo().session_data = SessionCipher(self.env).encrypt({'session': session.to_dict(), 'login': {}})

    @classmethod
    def _create_account_data(cls, user):
        # sudo(): these records are system-managed (written by the engine in
        # production); users have read-only access to them.
        account = cls.env['telegram.account'].sudo().create({
            'user_id': user.id,
            'company_id': user.company_id.id,
            'state': 'connected',
            'tg_user_id': str(10 ** 12 + user.id),
            'tg_username': user.login,
            'tg_name': user.name,
        })
        # Same Telegram ids on every account on purpose: ids are only unique per account.
        contact = cls.env['telegram.contact'].sudo().create({
            'account_id': account.id,
            'tg_user_id': '7000000001',
            'first_name': 'Peer',
            'is_contact': True,
        })
        chat = cls.env['telegram.chat'].sudo().create({
            'account_id': account.id,
            'tg_chat_id': '7000000001',
            'chat_type': 'private',
            'title': 'Peer',
            'contact_id': contact.id,
        })
        message = cls.env['telegram.message'].sudo().create({
            'chat_id': chat.id,
            'tg_message_id': '1048576',
            'sort_key': 1048576,
            'body': f'private text of {user.login}',
        })
        return account, contact, chat, message
