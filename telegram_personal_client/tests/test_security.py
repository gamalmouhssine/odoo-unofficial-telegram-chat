from psycopg2 import IntegrityError

from odoo.exceptions import AccessError, ValidationError
from odoo.tests import HttpCase, JsonRpcException, tagged
from odoo.tools import mute_logger

from .common import TelegramTestCommon

TELEGRAM_MODELS = ('telegram.account', 'telegram.chat', 'telegram.message', 'telegram.contact')


@tagged('post_install', '-at_install')
class TestTelegramSecurity(TelegramTestCommon):

    def test_user_sees_only_own_records(self):
        own_records = {
            'telegram.account': self.account_a,
            'telegram.chat': self.chat_a,
            'telegram.message': self.message_a,
            'telegram.contact': self.contact_a,
        }
        for model, own in own_records.items():
            with self.subTest(model=model):
                self.assertEqual(self.env[model].with_user(self.user_a).search([]), own)

    def test_user_cannot_read_other_users_records_by_id(self):
        for record in (self.account_b, self.chat_b, self.message_b, self.contact_b):
            with self.subTest(model=record._name), self.assertRaises(AccessError):
                record.with_user(self.user_a).read(['display_name'])

    def test_user_cannot_modify_system_managed_records(self):
        with self.assertRaises(AccessError):
            self.account_a.with_user(self.user_a).write({'state': 'connected'})
        with self.assertRaises(AccessError):
            self.env['telegram.account'].with_user(self.user_a).create({'user_id': self.user_a.id})
        with self.assertRaises(AccessError):
            self.account_a.with_user(self.user_a).unlink()
        with self.assertRaises(AccessError):
            self.chat_a.with_user(self.user_a).write({'title': 'Renamed'})
        with self.assertRaises(AccessError):
            self.message_a.with_user(self.user_a).write({'body': 'tampered'})
        with self.assertRaises(AccessError):
            self.message_a.with_user(self.user_a).unlink()
        with self.assertRaises(AccessError):
            self.contact_a.with_user(self.user_a).unlink()

    def test_contact_only_partner_link_is_editable(self):
        partner = self.env['res.partner'].create({'name': 'Linked Partner'})
        self.contact_a.with_user(self.user_a).write({'partner_id': partner.id})
        self.assertEqual(self.contact_a.partner_id, partner)
        with self.assertRaises(AccessError):
            self.contact_a.with_user(self.user_a).write({'first_name': 'Renamed'})
        with self.assertRaises(AccessError):
            self.contact_b.with_user(self.user_a).write({'partner_id': partner.id})

    def test_session_fields_hidden_from_non_system_users(self):
        for user in (self.user_a, self.manager):
            for field in ('session_data', 'qr_link', 'login_attempts'):
                with self.subTest(user=user.login, field=field), self.assertRaises(AccessError):
                    self.account_a.with_user(user).read([field])

    def test_manager_sees_account_status_but_not_conversations(self):
        accounts = self.env['telegram.account'].with_user(self.manager).search([])
        self.assertIn(self.account_a, accounts)
        self.assertIn(self.account_b, accounts)
        self.assertNotIn(self.account_c, accounts, "Accounts of other companies must stay hidden.")
        for model in ('telegram.chat', 'telegram.message', 'telegram.contact'):
            with self.subTest(model=model):
                self.assertFalse(self.env[model].with_user(self.manager).search([]))
        with self.assertRaises(AccessError):
            self.message_a.with_user(self.manager).read(['body'])

    def test_user_without_telegram_group_has_no_access(self):
        for model in TELEGRAM_MODELS:
            with self.subTest(model=model), self.assertRaises(AccessError):
                self.env[model].with_user(self.outsider).search([])

    def test_one_account_per_user(self):
        with mute_logger('odoo.sql_db'), self.assertRaises(IntegrityError):
            self.env['telegram.account'].sudo().create({'user_id': self.user_a.id})

    def test_chat_contact_must_belong_to_same_account(self):
        with self.assertRaises(ValidationError):
            self.chat_a.sudo().write({'contact_id': self.contact_b.id})

    def test_auth_status_returns_only_own_account(self):
        status = self.env['telegram.account'].with_user(self.user_a)._tg_auth_status()
        self.assertEqual(status['tg_username'], self.user_a.login)
        self.assertFalse(status['is_admin'])
        self.assertNotIn('session_data', status)
        with self.assertRaises(AccessError):
            self.env['telegram.account'].with_user(self.outsider)._tg_auth_status()


@tagged('post_install', '-at_install')
class TestTelegramRpcSecurity(TelegramTestCommon, HttpCase):
    """Same guarantees through the web client's RPC endpoint, as an attacker
    tampering with record ids in the browser would use it."""

    def _call_kw(self, model, method, args):
        return self.make_jsonrpc_request(f'/web/dataset/call_kw/{model}/{method}', {
            'model': model, 'method': method, 'args': args, 'kwargs': {},
        })

    def test_rpc_read_with_forged_ids_is_denied(self):
        self.authenticate('tg_user_a', 'tg_user_a')
        own = self._call_kw('telegram.message', 'read', [[self.message_a.id], ['body']])
        self.assertEqual(own[0]['body'], self.message_a.body)
        for record in (self.account_b, self.chat_b, self.message_b, self.contact_b):
            with self.subTest(model=record._name), mute_logger('odoo.http'), \
                    self.assertRaises(JsonRpcException) as catcher:
                self._call_kw(record._name, 'read', [[record.id], ['display_name']])
            self.assertEqual(str(catcher.exception), 'odoo.exceptions.AccessError')

    def test_rpc_cannot_reach_internal_methods(self):
        """The login and webhook helpers must only run through their routes,
        which pin them to the caller's own account or a signed request."""
        self.authenticate('tg_user_a', 'tg_user_a')
        for model, method, args in [
            ('telegram.account', '_tg_auth_start', ['qr']),
            ('telegram.account', '_tg_write_status', [[self.account_a.id], {'state': 'disconnected'}]),
            ('telegram.account', '_tg_reset', [[self.account_b.id], False]),
            ('telegram.engine', '_cron_run', []),
        ]:
            with self.subTest(method=method), mute_logger('odoo.http'), \
                    self.assertRaises(JsonRpcException) as catcher:
                self._call_kw(model, method, args)
            self.assertEqual(str(catcher.exception), 'odoo.exceptions.AccessError')
        self.assertEqual(self.account_b.state, 'connected')
        self.assertFalse(self.env['telegram.command'].search([]))

    def test_session_fields_are_hidden(self):
        self.authenticate('tg_user_a', 'tg_user_a')
        for field in ('session_data', 'qr_link', 'login_attempts'):
            with self.subTest(field=field), mute_logger('odoo.http'), \
                    self.assertRaises(JsonRpcException) as catcher:
                self._call_kw('telegram.account', 'read', [[self.account_a.id], [field]])
            self.assertEqual(str(catcher.exception), 'odoo.exceptions.AccessError')
        with mute_logger('odoo.http'), self.assertRaises(JsonRpcException):
            self._call_kw('telegram.command', 'search_read', [[], ['payload']])

    def test_rpc_search_read_returns_only_own_messages(self):
        self.authenticate('tg_user_a', 'tg_user_a')
        rows = self._call_kw('telegram.message', 'search_read', [[], ['body']])
        self.assertEqual([row['id'] for row in rows], self.message_a.ids)
