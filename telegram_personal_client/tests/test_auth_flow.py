from odoo.exceptions import AccessError
from odoo.tests import HttpCase, new_test_user, tagged

from ..models.telegram_account import PARAM_API_HASH
from .common import USER_GROUP, TelegramTestCommon


@tagged('post_install', '-at_install')
class TestTelegramAuthRequests(TelegramTestCommon):
    """What web requests accept and queue, before the engine runs."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user_new = new_test_user(cls.env, login='tg_user_new', groups=USER_GROUP)

    def _accounts(self, user):
        return self.env['telegram.account'].with_user(user)

    def _commands(self):
        return self.env['telegram.command'].search([])

    def test_invalid_input_is_never_queued(self):
        accounts = self._accounts(self.user_new)
        accounts._tg_auth_start('phone')
        for step, value in [
            ('phone', 'not a phone'), ('phone', '+1 23'), ('phone', '   '), ('phone', '+' + '1' * 40),
            ('unknown', 'x'), ('phone', 12345),
        ]:
            with self.subTest(step=step, value=value):
                self.assertTrue(accounts._tg_auth_submit(step, value)['error'])
        self.assertFalse(self._commands())

    def test_steps_out_of_order_are_refused(self):
        accounts = self._accounts(self.user_new)
        self.assertIn('expired', accounts._tg_auth_submit('code', '12345')['error'])
        accounts._tg_auth_start('phone')
        self.assertIn('expired', accounts._tg_auth_submit('password', 'secret')['error'])
        self.assertFalse(self._commands())

    def test_not_configured(self):
        self.env['ir.config_parameter'].set_param(PARAM_API_HASH, False)
        status = self._accounts(self.user_new)._tg_auth_start('qr')
        self.assertFalse(status['configured'])
        self.assertIn('not configured', status['error'])
        self.assertFalse(self._commands())

    def test_already_connected(self):
        self.assertIn('already connected', self._accounts(self.user_a)._tg_auth_start('qr')['error'])

    def test_disconnect_without_a_session_resets_directly(self):
        # Nothing to close on Telegram's side: no engine round-trip needed.
        status = self._accounts(self.user_a)._tg_auth_logout()
        self.assertEqual((status['state'], status['pending']), ('disconnected', False))
        self.assertFalse(self.chat_a.exists())
        self.assertTrue(self.chat_b.exists())

    def test_force_disconnect(self):
        with self.assertRaises(AccessError):
            self.account_a.with_user(self.user_a).action_force_disconnect()
        with self.assertRaises(AccessError):
            self.account_c.with_user(self.manager).action_force_disconnect()
        self.account_b.with_user(self.manager).action_force_disconnect()
        self.assertEqual(self.account_b.state, 'disconnected')
        self.assertFalse(self.chat_b.exists())
        self.assertTrue(self.chat_a.exists())

    def test_users_without_telegram_access_cannot_start(self):
        with self.assertRaises(AccessError):
            self._accounts(self.outsider)._tg_auth_start('qr')
        self.assertFalse(self._commands())

    def test_settings_hide_the_api_hash(self):
        settings = self.env['res.config.settings'].create({})
        self.assertFalse(settings.telegram_api_hash)
        self.assertEqual(settings.telegram_api_hash_source, 'param')


@tagged('post_install', '-at_install')
class TestTelegramRoutes(TelegramTestCommon, HttpCase):

    def test_routes_act_on_the_callers_account_only(self):
        self.authenticate('tg_user_a', 'tg_user_a')
        status = self.make_jsonrpc_request('/telegram/auth/status')
        self.assertEqual((status['state'], status['tg_username']), ('connected', 'tg_user_a'))
        self.assertTrue(self.make_jsonrpc_request('/telegram/heartbeat'))
        commands = self.env['telegram.command'].search([])
        self.assertEqual((commands.account_id, commands.action), (self.account_a, 'heartbeat'))
