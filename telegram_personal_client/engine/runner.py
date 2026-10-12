"""The Telegram engine.

Runs inside Odoo's cron worker for a bounded window (models/telegram_engine.py)
and owns every live Telegram connection of the database:

* one Telethon client per account that is connected or logging in;
* commands queued by the web client (login steps, logout, heartbeat) are
  executed in order, about twice a second;
* session data (authorization key, update state) is saved encrypted on the
  account, so the next window resumes without logging in again.

Odoo locks a cron job while it runs, so there is never more than one engine
per database: a Telegram session is never used from two connections at once,
which Telegram punishes by ending the session (AUTH_KEY_DUPLICATED).
"""
import asyncio
import base64
import contextlib
import json
import logging
import re
import time
from datetime import datetime, timezone

import psycopg2

from odoo import fields

from .cipher import SessionCipher
from .errors import FATAL_CODES, LoginRefused, error_code
from .messaging import MessagingMixin
from .session import DbSession
from .vendor import ensure_libraries

ensure_libraries()
from telethon import TelegramClient, errors, events, functions, types  # noqa: E402

_logger = logging.getLogger(__name__)

TICK_SECONDS = 0.5
IDLE_EXIT_SECONDS = 5
SAVE_INTERVAL = 30
RECONNECT_DELAY = 15
QR_MAX_SECONDS = 300
ONLINE_REFRESH_SECONDS = 120
HEARTBEAT_SECONDS = 70
OFFBOARD_INTERVAL = 30
TEST_DC = (2, '149.154.167.40', 443)

# States that need a live connection.
LIVE_STATES = ('waiting_qr', 'waiting_code', 'waiting_password', 'connected')

CODE_DELIVERY = {
    'SentCodeTypeApp': 'telegram_app',
    'SentCodeTypeSms': 'sms',
    'SentCodeTypeSmsWord': 'sms',
    'SentCodeTypeSmsPhrase': 'sms',
    'SentCodeTypeFirebaseSms': 'sms',
    'SentCodeTypeCall': 'call',
    'SentCodeTypeFlashCall': 'call',
    'SentCodeTypeMissedCall': 'call',
    'SentCodeTypeFragmentSms': 'fragment',
    'SentCodeTypeEmailCode': 'email',
}

_CLEAR_ERROR = {'last_error': False, 'last_error_detail': False, 'last_error_retry': 0}


def mask_phone(phone):
    """'201234567845' -> '+201*******45'. Enough to recognise, not to reuse."""
    digits = re.sub(r'\D', '', phone or '')
    if len(digits) < 6:
        return False
    return f"+{digits[:3]}{'*' * (len(digits) - 5)}{digits[-2:]}"


class AccountRuntime:
    """In-memory state of one account's live connection."""

    def __init__(self, account_id, client, session, login):
        self.account_id = account_id
        self.client = client
        self.session = session
        # Login context kept with the session: phone, phone_code_hash, QR start time.
        self.login = login
        self.qr_expires = None
        self.qr_scanned = False
        # A session counts as offline until it says otherwise. Starting from
        # False means no "offline" is ever sent without a prior "online"
        # (each status update also refreshes the user's "last seen").
        self.online = False
        self.online_sent_at = 0.0
        self.online_until = 0.0
        self.saved_snapshot = None
        self.saved_at = time.monotonic()
        # Chats/messages state, once the account is connected (engine/messaging.py).
        self.messaging = None

    async def on_login_token(self, _update):
        self.qr_scanned = True


class Engine(MessagingMixin):
    def __init__(self, env, window, commit, client_factory=None):
        self.env = env
        self.window = window
        self._commit_cursor = commit
        self._client_factory = client_factory or self._make_client
        self.settings = env['telegram.account']._tg_engine_settings()
        self.cipher = SessionCipher(env)
        self.runtimes = {}
        self._retry_at = {}
        self._offboard_at = 0.0
        self._done_commands = set()
        # Database changes since the last commit, replayed if the commit fails.
        self._replay = []

    # Window -------------------------------------------------------------------

    def run(self, max_ticks=None):
        """Run one window (or at most max_ticks iterations). Returns True
        while accounts need a live connection, so the caller can start the
        next window right away."""
        if not (self.settings['api_id'] and self.settings['api_hash']):
            return False
        return asyncio.run(self._run(max_ticks))

    async def _run(self, max_ticks):
        deadline = time.monotonic() + self.window
        idle_since = None
        live = False
        ticks = 0
        try:
            while time.monotonic() < deadline and (max_ticks is None or ticks < max_ticks):
                ticks += 1
                live = await self._tick()
                self._commit()
                if live:
                    idle_since = None
                elif idle_since is None:
                    idle_since = time.monotonic()
                elif time.monotonic() - idle_since > IDLE_EXIT_SECONDS:
                    break
                await asyncio.sleep(TICK_SECONDS)
        finally:
            for account_id in list(self.runtimes):
                await self._close(account_id)
            self._commit()
        return live

    async def _tick(self):
        accounts_model = self.env['telegram.account'].sudo()
        if time.monotonic() >= self._offboard_at:
            # Archived users, or users who lost Telegram access, are logged out.
            self._offboard_at = time.monotonic() + OFFBOARD_INTERVAL
            accounts_model._tg_offboard()
        # Snapshot the queue first: commands are deleted as they are executed.
        commands = self.env['telegram.command'].sudo().search_read(
            [('id', 'not in', list(self._done_commands))], ['account_id', 'action', 'payload'], order='id')
        queued = {}
        for command in commands:
            queued.setdefault(command['account_id'][0], []).append(command)
        accounts = accounts_model.search([('state', 'in', LIVE_STATES)]) | accounts_model.browse(list(queued))
        for account_id in set(self.runtimes) - set(accounts.ids):
            await self._close(account_id)
        for account in accounts:
            try:
                await self._process(account, queued.get(account.id, []))
            except Exception:
                _logger.exception("Telegram engine: unexpected error on account %s", account.id)
        return bool(accounts)

    async def _process(self, account, commands):
        runtime = self.runtimes.get(account.id) or await self._open(account)
        if runtime is None:
            if commands and account.exists() and account.state != 'disconnected':
                # Telegram unreachable: answer the waiting user instead of
                # leaving the request pending.
                for command in commands:
                    self._done_commands.add(command['id'])
                    self._mutate(self._unlink_command, command['id'])
                if any(command['action'] != 'heartbeat' for command in commands):
                    self._update(account, {'last_error': 'NETWORK_ERROR', 'last_error_detail': False,
                                           'last_error_retry': 0})
            return
        for command in commands:
            await self._execute(account, runtime, command)
            if account.id not in self.runtimes:
                return  # logged out or refused: remaining commands were dropped
        await self._maintain(account, runtime)

    # Connections -----------------------------------------------------------------

    def _make_client(self, session, catch_up):
        settings = self.settings
        return TelegramClient(
            session, settings['api_id'], settings['api_hash'],
            # Honest device information; never impersonate official apps.
            device_model=settings['device_model'],
            system_version=settings['system_version'],
            app_version=settings['app_version'],
            lang_code=settings['lang_code'],
            system_lang_code=settings['lang_code'],
            # Report flood waits instead of sleeping inside the engine.
            flood_sleep_threshold=0,
            connection_retries=1,
            request_retries=2,
            timeout=10,
            receive_updates=True,
            catch_up=catch_up,
        )

    async def _open(self, account):
        if time.monotonic() < self._retry_at.get(account.id, 0):
            return None
        data = {}
        if account.session_data:
            data = self.cipher.decrypt(account.session_data)
            if data is None:
                # Encrypted with a key this server no longer has.
                self._reset(account, 'SESSION_KEY_MISMATCH')
                return None
        session = DbSession.from_dict(data.get('session'))
        if session.auth_key is None and self.settings['test_dc']:
            session.set_dc(*TEST_DC)
        connected_account = account.state == 'connected'
        client = self._client_factory(session, connected_account)
        runtime = AccountRuntime(account.id, client, session, data.get('login') or {})
        # Handlers first: the catch-up of missed updates starts on connect.
        client.add_event_handler(runtime.on_login_token, events.Raw(types.UpdateLoginToken))
        if connected_account:
            self._prepare_messaging(account, runtime)
        try:
            await client.connect()
            authorized = await client.is_user_authorized() if connected_account else None
        except Exception as error:
            code, _retry, _detail = error_code(error)
            _logger.warning("Telegram engine: cannot connect account %s (%s)", account.id, code)
            with contextlib.suppress(Exception):
                await client.disconnect()
            if code == 'SESSION_REVOKED':
                self._reset(account, code)
            else:
                self._retry_at[account.id] = time.monotonic() + RECONNECT_DELAY
            return None
        self.runtimes[account.id] = runtime
        if connected_account and not authorized:
            # The user ended this session from another device.
            await self._drop(account.id)
            self._reset(account, 'SESSION_REVOKED')
            return None
        return runtime

    async def _close(self, account_id):
        """End a window for an account, keeping its session."""
        runtime = self.runtimes.pop(account_id, None)
        if runtime is None:
            return
        with contextlib.suppress(Exception):
            # Telethon stores the update state in the session on disconnect.
            await runtime.client.disconnect()
        account = self.env['telegram.account'].sudo().browse(account_id).exists()
        if account and account.state != 'disconnected':
            self._save_session(account, runtime)

    async def _drop(self, account_id):
        """Disconnect without saving: the session is being discarded."""
        runtime = self.runtimes.pop(account_id, None)
        if runtime is not None:
            with contextlib.suppress(Exception):
                await runtime.client.disconnect()

    # Commands ----------------------------------------------------------------------

    async def _execute(self, account, runtime, command):
        self._done_commands.add(command['id'])
        action = command['action']
        value = None
        if command['payload']:
            value = (self.cipher.decrypt(command['payload']) or {}).get('value')
        # Consumed before running: a command (and any code or password in it)
        # is never kept, even if running it fails.
        self._mutate(self._unlink_command, command['id'])
        handler = {
            'send_code': self._send_code,
            'check_code': self._check_code,
            'check_password': self._check_password,
            'start_qr': self._start_qr,
            'logout': self._logout,
            'heartbeat': self._heartbeat,
            'send_message': self._send_message,
            'load_history': self._load_history,
            'mark_read': self._mark_read,
            'open_chat': self._open_chat,
        }[action]
        await self._guard(account, handler(account, runtime, value))

    async def _guard(self, account, coroutine):
        try:
            await coroutine
        except Exception as error:
            code, retry_after, detail = error_code(error)
            if code == 'UNEXPECTED_ERROR':
                _logger.exception("Telegram engine: unexpected error on account %s", account.id)
            elif code != 'NETWORK_ERROR':
                _logger.info("Telegram engine: account %s: %s", account.id, code)
            if code in FATAL_CODES:
                await self._drop(account.id)
                self._reset(account, code)
            else:
                self._update(account, {
                    'last_error': code, 'last_error_detail': detail or False,
                    'last_error_retry': retry_after or 0,
                })

    async def _send_code(self, account, runtime, phone):
        self._hit_limit(account, 'send_code')
        sent = await runtime.client.send_code_request(phone)
        kind = type(sent.type).__name__
        if kind == 'SentCodeTypeSetUpEmailRequired':
            raise LoginRefused('EMAIL_SETUP_REQUIRED')
        runtime.login = {'phone': phone, 'phone_code_hash': sent.phone_code_hash}
        self._update(account, {
            'state': 'waiting_code', 'code_delivery': CODE_DELIVERY.get(kind, 'other'),
            'phone_masked': mask_phone(phone), 'password_hint': False, 'qr_link': False, **_CLEAR_ERROR,
        })
        self._save_session(account, runtime, force=True)

    async def _check_code(self, account, runtime, code):
        login = runtime.login
        if not login.get('phone_code_hash'):
            raise LoginRefused('NOT_STARTED')
        self._hit_limit(account, 'check_code')
        try:
            user = await runtime.client.sign_in(login['phone'], code, phone_code_hash=login['phone_code_hash'])
        except errors.SessionPasswordNeededError:
            await self._ask_password(account, runtime)
            return
        await self._authorized(account, runtime, user)

    async def _check_password(self, account, runtime, password):
        self._hit_limit(account, 'check_password')
        user = await runtime.client.sign_in(password=password)
        await self._authorized(account, runtime, user)

    async def _start_qr(self, account, runtime, _value):
        self._hit_limit(account, 'request_qr')
        runtime.login = {'qr_started': time.time()}
        await self._export_qr(account, runtime)

    async def _logout(self, account, runtime, _value):
        if await runtime.client.is_user_authorized():
            # Removes the session from the user's list of devices.
            await runtime.client.log_out()
        self.runtimes.pop(account.id, None)
        with contextlib.suppress(Exception):
            await runtime.client.disconnect()
        self._reset(account, False)

    async def _heartbeat(self, account, runtime, _value):
        runtime.online_until = time.monotonic() + HEARTBEAT_SECONDS

    # Login helpers -------------------------------------------------------------------

    async def _ask_password(self, account, runtime):
        password = await runtime.client(functions.account.GetPasswordRequest())
        self._update(account, {
            'state': 'waiting_password', 'password_hint': password.hint or False,
            'code_delivery': False, 'qr_link': False, **_CLEAR_ERROR,
        })
        self._save_session(account, runtime, force=True)

    async def _export_qr(self, account, runtime):
        client = runtime.client
        try:
            result = await client(functions.auth.ExportLoginTokenRequest(
                self.settings['api_id'], self.settings['api_hash'], except_ids=[]))
            if isinstance(result, types.auth.LoginTokenMigrateTo):
                await client._switch_dc(result.dc_id)
                result = await client(functions.auth.ImportLoginTokenRequest(result.token))
        except errors.SessionPasswordNeededError:
            await self._ask_password(account, runtime)
            return
        if isinstance(result, types.auth.LoginTokenSuccess):
            user = result.authorization.user
            await client._on_login(user)
            await self._authorized(account, runtime, user)
        elif isinstance(result, types.auth.LoginToken):
            runtime.qr_expires = result.expires
            token = base64.urlsafe_b64encode(result.token).decode().rstrip('=')
            self._update(account, {'state': 'waiting_qr', 'qr_link': f'tg://login?token={token}', **_CLEAR_ERROR})
            self._save_session(account, runtime, force=True)

    async def _authorized(self, account, runtime, user):
        runtime.login = {}
        runtime.qr_expires = None
        usernames = [u.username for u in (getattr(user, 'usernames', None) or []) if getattr(u, 'active', False)]
        self._update(account, {
            'state': 'connected',
            'tg_user_id': str(user.id),
            'tg_username': getattr(user, 'username', None) or (usernames[0] if usernames else False),
            'tg_name': ' '.join(filter(None, [user.first_name, user.last_name])) or False,
            'phone_masked': mask_phone(getattr(user, 'phone', None)) or account.phone_masked,
            'connected_since': fields.Datetime.now(),
            'code_delivery': False, 'password_hint': False, 'qr_link': False, **_CLEAR_ERROR,
        })
        self._save_session(account, runtime, force=True)
        # Start synchronizing chats right away, in this same window.
        self._prepare_messaging(account, runtime)

    def _hit_limit(self, account, action):
        """Caps login attempts below Telegram's own limits (persisted, so
        neither a restart nor a logout resets them). Web requests check the
        same caps before queueing (telegram.account._tg_queue_login)."""
        now = time.time()
        retry_after = account._tg_login_retry_after(action, now)
        if retry_after:
            raise LoginRefused('RATE_LIMITED', retry_after=retry_after)
        window = account._tg_login_limits()[action][1]
        attempts = dict(account.login_attempts or {})
        attempts[action] = [at for at in attempts.get(action, []) if at > now - window] + [now]
        self._update(account, {'login_attempts': attempts})

    # Periodic work -------------------------------------------------------------------

    async def _maintain(self, account, runtime):
        if account.state == 'waiting_qr':
            await self._maintain_qr(account, runtime)
        elif account.state == 'connected':
            try:
                await self._maintain_messaging(account, runtime)
            except Exception as error:
                # Only fatal errors get here (session ended, account banned…).
                await self._drop(account.id)
                self._reset(account, error_code(error)[0])
                return
            await self._sync_online(runtime)
            if time.monotonic() - runtime.saved_at > SAVE_INTERVAL:
                self._save_session(account, runtime)

    async def _maintain_qr(self, account, runtime):
        if runtime.qr_scanned:
            runtime.qr_scanned = False
            await self._guard(account, self._export_qr(account, runtime))
            return
        if runtime.qr_expires is None or datetime.now(timezone.utc) >= runtime.qr_expires:
            started = runtime.login.get('qr_started') or time.time()
            if time.time() - started > QR_MAX_SECONDS:
                await self._drop(account.id)
                self._reset(account, 'QR_EXPIRED')
                return
            # New token: also confirms a scan that happened while disconnected.
            await self._guard(account, self._export_qr(account, runtime))

    async def _sync_online(self, runtime):
        """Online only while the user has Odoo's Telegram screen open, like
        Telegram Desktop with a focused window (never forced on 24/7)."""
        now = time.monotonic()
        online = now < runtime.online_until
        if online == runtime.online and not (online and now - runtime.online_sent_at > ONLINE_REFRESH_SECONDS):
            return
        try:
            await runtime.client(functions.account.UpdateStatusRequest(offline=not online))
        except Exception as error:
            _logger.debug("Telegram engine: status update failed: %s", error)
            return
        runtime.online = online
        runtime.online_sent_at = now

    # Database --------------------------------------------------------------------------

    def _save_session(self, account, runtime, force=False):
        data = {'session': runtime.session.to_dict(), 'login': runtime.login}
        snapshot = json.dumps(data, sort_keys=True)
        runtime.saved_at = time.monotonic()
        if not force and snapshot == runtime.saved_snapshot:
            return
        runtime.saved_snapshot = snapshot
        self._update(account, {'session_data': self.cipher.encrypt(data)})

    def _update(self, account, vals):
        self._mutate(self._apply_update, account.id, vals)

    def _reset(self, account, error):
        self._mutate(self._apply_reset, account.id, error)

    def _apply_update(self, account_id, vals):
        account = self.env['telegram.account'].sudo().browse(account_id).exists()
        if account:
            account._tg_write_status(vals)

    def _apply_reset(self, account_id, error):
        account = self.env['telegram.account'].sudo().browse(account_id).exists()
        if account:
            account._tg_reset(error)

    def _unlink_command(self, command_id):
        self.env['telegram.command'].sudo().browse(command_id).exists().unlink()

    def _mutate(self, function, *args):
        function(*args)
        self._replay.append((function, args))

    def _commit(self):
        try:
            self._commit_cursor()
        except psycopg2.Error as error:
            # Typically a concurrent update from a web request: start a new
            # transaction and re-apply what the engine had written.
            _logger.info("Telegram engine: commit failed (%s), retrying", type(error).__name__)
            self.env.cr.rollback()
            self.env.invalidate_all()
            for function, args in self._replay:
                try:
                    function(*args)
                except Exception:
                    _logger.exception("Telegram engine: could not re-apply a change")
            return
        self._replay = []
