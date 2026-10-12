import datetime
import logging
import os
from unittest.mock import patch

from odoo.exceptions import AccessError
from odoo.tests import new_test_user, tagged
from odoo.tools import mute_logger

from ..engine.cipher import ENV_SESSION_KEY, SessionCipher
from ..engine.session import DbSession
from ..models.telegram_account import PARAM_API_ID, PARAM_MAX_CODES
from .common import USER_GROUP, TelegramTestCommon, errors, types

_logger = logging.getLogger(__name__)


@tagged('post_install', '-at_install')
class TestTelegramEngine(TelegramTestCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user_new = new_test_user(cls.env, login='tg_user_new', groups=USER_GROUP)

    def setUp(self):
        super().setUp()
        # Existing accounts hold valid sessions, so the engine keeps them connected.
        for account in (self.account_a, self.account_b, self.account_c):
            self.give_session(account)

    def _accounts(self, user):
        return self.env['telegram.account'].with_user(user)

    def _account_of(self, user):
        return self.env['telegram.account'].search([('user_id', '=', user.id)])

    def _commands(self, account):
        return self.env['telegram.command'].search([('account_id', '=', account.id)])

    def test_phone_login_with_two_step_password(self):
        accounts = self._accounts(self.user_new)
        self.assertEqual(accounts._tg_auth_start('phone')['state'], 'waiting_phone')
        account = self._account_of(self.user_new)

        status = accounts._tg_auth_submit('phone', '+20 123 456 7845')
        self.assertTrue(status['pending'])
        command = self._commands(account)
        self.assertEqual(command.action, 'send_code')
        self.assertNotIn('7845', command.payload, "Queued values must be encrypted.")

        self.run_engine()
        self.assertFalse(self._commands(account), "Commands are deleted once read.")
        self.assertEqual(self.telegram.calls[-1], ('send_code', '+20 123 456 7845'))
        status = accounts._tg_auth_status()
        self.assertEqual(
            (status['state'], status['code_delivery'], status['phone_masked'], status['pending']),
            ('waiting_code', 'telegram_app', '+201*******45', False))

        self.telegram.password_required = True
        accounts._tg_auth_submit('code', ' 24680 ')
        self.run_engine()
        self.assertIn(('sign_in_code', '+20 123 456 7845', '24680', 'hash-123'), self.telegram.calls)
        status = accounts._tg_auth_status()
        self.assertEqual((status['state'], status['password_hint']), ('waiting_password', 'pet name'))

        accounts._tg_auth_submit('password', ' pass with spaces ')
        self.run_engine()
        status = accounts._tg_auth_status()
        self.assertEqual((status['state'], status['tg_username'], status['tg_name']),
                         ('connected', 'ada', 'Ada Lovelace'))
        self.assertTrue(account.connected_since)
        self.assertFalse(account.password_hint)

        data = SessionCipher(self.env).decrypt(account.session_data)
        self.assertTrue(data['session']['auth_key'])
        self.assertEqual(data['login'], {}, "Login context (phone, code hash) is dropped once connected.")

    def test_wrong_code_keeps_the_step(self):
        accounts = self._accounts(self.user_new)
        accounts._tg_auth_start('phone')
        accounts._tg_auth_submit('phone', '+201234567845')
        self.run_engine()
        self.telegram.failures['sign_in_code'] = errors.PhoneCodeInvalidError(request=None)
        accounts._tg_auth_submit('code', '11111')
        self.run_engine()
        status = accounts._tg_auth_status()
        self.assertEqual(status['state'], 'waiting_code')
        self.assertIn('Wrong code', status['last_error'])

    def test_flood_wait_is_reported(self):
        accounts = self._accounts(self.user_new)
        accounts._tg_auth_start('phone')
        self.telegram.failures['send_code'] = errors.FloodWaitError(request=None, capture=42)
        accounts._tg_auth_submit('phone', '+201234567845')
        self.run_engine()
        status = accounts._tg_auth_status()
        self.assertEqual(status['state'], 'waiting_phone')
        self.assertIn('42 seconds', status['last_error'])

    def test_code_requests_are_capped(self):
        self.env['ir.config_parameter'].set_param(PARAM_MAX_CODES, 2)
        accounts = self._accounts(self.user_new)
        accounts._tg_auth_start('phone')
        for _attempt in range(2):
            self.assertFalse(accounts._tg_auth_submit('phone', '+201234567845')['error'])
            self.run_engine()
        # Refused in the web request already: no connection to Telegram is opened.
        self.assertIn('Too many attempts', accounts._tg_auth_submit('phone', '+201234567845')['error'])
        self.run_engine()
        self.assertEqual(self.telegram.names().count('send_code'), 2)
        # Logging out does not reset the cap.
        accounts._tg_auth_logout()
        self.run_engine()
        accounts._tg_auth_start('phone')
        self.assertIn('Too many attempts', accounts._tg_auth_submit('phone', '+201234567845')['error'])
        self.run_engine()
        self.assertEqual(self.telegram.names().count('send_code'), 2)

    def test_qr_login(self):
        accounts = self._accounts(self.user_new)
        accounts._tg_auth_start('qr')
        self.run_engine()
        account = self._account_of(self.user_new)
        status = accounts._tg_auth_status()
        self.assertEqual(status['state'], 'waiting_qr')
        self.assertTrue(status['qr_image'].startswith('data:image/png;base64,'))
        self.assertTrue(account.qr_link.startswith('tg://login?token='))
        with self.assertRaises(AccessError):
            account.with_user(self.user_new).read(['qr_link'])

        # The user scans the code: the next window confirms the login.
        self.telegram.qr_accepted = True
        self.run_engine()
        self.assertEqual(account.state, 'connected')
        self.assertFalse(account.qr_link)

    def test_qr_login_with_two_step_password(self):
        accounts = self._accounts(self.user_new)
        accounts._tg_auth_start('qr')
        self.run_engine()
        self.telegram.qr_accepted = self.telegram.password_required = True
        self.run_engine()
        self.assertEqual(self._account_of(self.user_new).state, 'waiting_password')

    def test_unscanned_qr_code_expires(self):
        accounts = self._accounts(self.user_new)
        accounts._tg_auth_start('qr')
        self.run_engine()
        account = self._account_of(self.user_new)
        cipher = SessionCipher(self.env)
        data = cipher.decrypt(account.session_data)
        data['login']['qr_started'] -= 3600
        account.session_data = cipher.encrypt(data)
        self.run_engine()
        self.assertEqual((account.state, account.last_error), ('disconnected', 'QR_EXPIRED'))
        self.assertFalse(account.session_data)

    def test_session_ended_from_the_phone(self):
        self.telegram.authorized.clear()
        self.run_engine()
        self.assertEqual((self.account_a.state, self.account_a.last_error), ('disconnected', 'SESSION_REVOKED'))
        self.assertFalse(self.chat_a.exists() or self.message_a.exists() or self.contact_a.exists())
        self.assertFalse(self.account_a.session_data)

    def test_logout(self):
        status = self._accounts(self.user_a)._tg_auth_logout()
        self.assertTrue(status['pending'])
        self.run_engine()
        self.assertIn('log_out', self.telegram.names())
        self.assertEqual(self.account_a.state, 'disconnected')
        self.assertFalse(self.account_a.session_data or self.account_a.tg_username)
        self.assertFalse(self.chat_a.exists() or self.message_a.exists() or self.contact_a.exists())
        self.assertTrue(self.chat_b.exists(), "Other users' data must be untouched.")
        self.assertEqual(self.account_b.state, 'connected')

    def test_network_failure_answers_the_user(self):
        accounts = self._accounts(self.user_new)
        accounts._tg_auth_start('phone')
        accounts._tg_auth_submit('phone', '+201234567845')
        self.telegram.network_down = True
        with mute_logger('odoo.addons.telegram_personal_client.engine.runner'):
            self.run_engine()
        account = self._account_of(self.user_new)
        self.assertFalse(self._commands(account))
        status = accounts._tg_auth_status()
        self.assertEqual(status['state'], 'waiting_phone')
        self.assertIn('cannot reach Telegram', status['last_error'])

    def test_online_status_follows_the_open_screen(self):
        self.run_engine()
        self.assertNotIn('UpdateStatusRequest', self.telegram.names(),
                         "No status is sent while nobody has the screen open.")
        accounts = self._accounts(self.user_a)
        accounts._tg_heartbeat()
        accounts._tg_heartbeat()
        self.assertEqual(len(self._commands(self.account_a)), 1, "Heartbeats do not pile up.")
        self.run_engine(ticks=2)
        status_calls = [call for call in self.telegram.calls if call[0] == 'UpdateStatusRequest']
        self.assertEqual(status_calls, [('UpdateStatusRequest', False)])

    def test_session_encrypted_with_an_unknown_key_is_dropped(self):
        with patch.dict(os.environ, {ENV_SESSION_KEY: 'a-different-server-key-0123456789'}):
            foreign = SessionCipher(self.env)._fernets[0].encrypt(b'{}').decode()
        self.account_a.session_data = foreign
        self.run_engine()
        self.assertEqual((self.account_a.state, self.account_a.last_error), ('disconnected', 'SESSION_KEY_MISMATCH'))

    def test_secrets_never_reach_the_logs(self):
        accounts = self._accounts(self.user_new)
        accounts._tg_auth_start('phone')
        self.telegram.password_required = True
        with self.assertLogs(level='DEBUG') as logs:
            _logger.debug("capturing every log record")
            accounts._tg_auth_submit('phone', '+201234567845')
            self.run_engine()
            accounts._tg_auth_submit('code', '97531')
            self.run_engine()
            self.telegram.failures['sign_in_password'] = errors.PasswordHashInvalidError(request=None)
            accounts._tg_auth_submit('password', 'Sup3r-Secret!')
            self.run_engine()
        self.assertIn('Wrong password', accounts._tg_auth_status()['last_error'])
        output = '\n'.join(logs.output)
        for secret in ('201234567845', '97531', 'Sup3r-Secret!'):
            self.assertNotIn(secret, output)

    def test_engine_waits_for_configuration(self):
        self.env['ir.config_parameter'].set_param(PARAM_API_ID, False)
        self.assertFalse(self.run_engine())
        self.assertFalse(self.telegram.calls)


@tagged('post_install', '-at_install')
class TestTelegramEngineParts(TelegramTestCommon):

    def test_session_round_trip(self):
        from .common import AuthKey
        session = DbSession()
        session.set_dc(2, '149.154.167.40', 443)
        session.auth_key = AuthKey(b'k' * 256)
        date = datetime.datetime(2026, 1, 2, tzinfo=datetime.timezone.utc)
        session.set_update_state(0, types.updates.State(pts=10, qts=2, date=date, seq=3, unread_count=0))
        session.set_update_state(1234, types.updates.State(pts=99, qts=0, date=date, seq=0, unread_count=0))
        session._entities = {(42, 777, 'ada', '201234567845', 'Ada')}

        restored = DbSession.from_dict(session.to_dict())
        self.assertEqual((restored.dc_id, restored.server_address, restored.port), (2, '149.154.167.40', 443))
        self.assertEqual(restored.auth_key.key, b'k' * 256)
        self.assertEqual(restored.get_update_state(0).pts, 10)
        self.assertEqual(restored.get_update_state(1234).pts, 99)
        self.assertEqual(restored.get_update_state(0).date, date)
        self.assertEqual(restored._entities, session._entities)

    def test_cipher_accepts_both_keys(self):
        old = SessionCipher(self.env).encrypt({'x': 1})
        with patch.dict(os.environ, {ENV_SESSION_KEY: 'environment-session-key-0123456789'}):
            cipher = SessionCipher(self.env)
            new = cipher.encrypt({'x': 2})
            self.assertEqual(cipher.decrypt(old), {'x': 1}, "Sessions saved before the key was set still open.")
            self.assertEqual(cipher.decrypt(new), {'x': 2})
        self.assertIsNone(SessionCipher(self.env).decrypt(new), "Without the key, its sessions cannot be read.")

    def test_engine_window_respects_odoo_time_limits(self):
        engine = self.env['telegram.engine']
        for cron_limit, real_limit, expected in [(-1, 120, 45), (-1, 60, 24), (0, 120, 45), (30, 120, 12), (-1, 0, 45)]:
            config = {'limit_time_real_cron': cron_limit, 'limit_time_real': real_limit}
            with self.subTest(**config), patch('odoo.addons.telegram_personal_client.models.telegram_engine.config', config):
                self.assertEqual(engine._run_window_seconds(), expected)
