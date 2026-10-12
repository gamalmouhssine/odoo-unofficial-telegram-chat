import importlib.util
import time
from pathlib import Path

from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user, tagged
from odoo.tools import file_open

from ..engine.cipher import SessionCipher
from .common import USER_GROUP, TelegramTestCommon


@tagged('post_install', '-at_install')
class TestTelegramHardening(TelegramTestCommon):
    """Fixes from the pre-release security review."""

    def test_secrets_are_hidden_from_administrators_too(self):
        admin = new_test_user(self.env, login='tg_sysadmin', groups='base.group_system,' + USER_GROUP)
        for user in (admin, self.manager, self.user_a):
            for field in ('session_data', 'qr_link', 'login_attempts', 'password_hint', 'last_error_detail'):
                with self.subTest(user=user.login, field=field), self.assertRaises(AccessError):
                    self.account_a.with_user(user).read([field])
        with self.assertRaises(AccessError):
            self.env['telegram.command'].with_user(admin).search([])
        # The owner still gets their own hint through their status.
        self.account_a.sudo().write({'state': 'waiting_password', 'password_hint': 'cat'})
        self.assertEqual(self.env['telegram.account'].with_user(self.user_a)._tg_auth_status()['password_hint'], 'cat')

    def test_sessions_do_not_open_in_a_database_copy(self):
        cipher = SessionCipher(self.env)
        token = cipher.encrypt({'session': 'x'})
        self.env['ir.config_parameter'].set_param('database.uuid', 'uuid-of-a-copied-database')
        self.assertIsNone(SessionCipher(self.env).decrypt(token))

    def test_upgrade_re_encrypts_old_sessions(self):
        legacy_token = SessionCipher(self.env, legacy=True).encrypt({'session': {'auth_key': 'k'}, 'login': {}})
        self.account_a.sudo().session_data = legacy_token
        path = Path(__file__).parent.parent / 'migrations' / '19.0.4.0.0' / 'post-migrate.py'
        spec = importlib.util.spec_from_file_location('telegram_post_migrate_4', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.migrate(self.env.cr, '19.0.3.0.0')
        self.env.invalidate_all()
        data = SessionCipher(self.env).decrypt(self.account_a.sudo().session_data)
        self.assertEqual(data['session'], {'auth_key': 'k'})

    def test_neutralized_copies_drop_sessions(self):
        self.give_session(self.account_a)
        self.account_a._tg_queue('heartbeat')
        self.env.flush_all()  # neutralization runs on stored data
        with file_open('telegram_personal_client/data/neutralize.sql') as script:
            self.env.cr.execute(script.read())
        self.env.invalidate_all()
        self.assertEqual(self.account_a.state, 'disconnected')
        self.assertFalse(self.account_a.sudo().session_data)
        self.assertFalse(self.env['telegram.command'].search_count([]))

    def test_login_steps_are_queued_once_and_capped_before_queueing(self):
        user = new_test_user(self.env, login='tg_user_q', groups=USER_GROUP)
        accounts = self.env['telegram.account'].with_user(user)
        accounts._tg_auth_start('qr')
        accounts._tg_auth_start('qr')
        commands = self.env['telegram.command'].search([('action', '=', 'start_qr')])
        self.assertEqual(len(commands), 1, "Repeated clicks do not pile up login requests.")
        commands.unlink()
        account = accounts._tg_own_account().sudo()
        account.login_attempts = {'request_qr': [time.time()] * 10}
        status = accounts._tg_auth_start('qr')
        self.assertIn('Too many attempts', status['error'])
        self.assertFalse(self.env['telegram.command'].search_count([]), "Blocked before reaching Telegram.")

    def test_wake_ups_are_not_duplicated(self):
        cron = self.env.ref('telegram_personal_client.ir_cron_telegram_engine')
        triggers = self.env['ir.cron.trigger']
        triggers.search([('cron_id', '=', cron.id)]).unlink()
        self.account_a._tg_queue('heartbeat')
        self.account_b._tg_queue('heartbeat')
        self.assertEqual(triggers.search_count([('cron_id', '=', cron.id)]), 1)

    def test_archived_users_are_disconnected(self):
        self.give_session(self.account_a)
        self.user_a.active = False
        self.run_engine()
        self.assertIn('log_out', self.telegram.names())
        self.assertEqual(self.account_a.state, 'disconnected')
        self.assertFalse(self.chat_a.exists())

    def test_users_who_lose_access_are_disconnected(self):
        self.give_session(self.account_b)
        self.user_b.group_ids = [(3, self.env.ref(USER_GROUP).id)]
        self.run_engine()
        self.assertEqual(self.account_b.state, 'disconnected')

    def test_missing_and_foreign_ids_get_the_same_answer(self):
        chats = self.env['telegram.chat'].with_user(self.user_a)
        messages = self.env['telegram.message'].with_user(self.user_a)
        for call in (lambda: chats._tg_request_history(self.chat_b.id), lambda: chats._tg_request_history(987654321),
                     lambda: messages._tg_retry(self.message_b.id), lambda: messages._tg_retry(987654321)):
            with self.assertRaisesRegex(AccessError, 'not available'):
                call()

    def test_contacts_link_only_visible_partners(self):
        hidden = self.env['res.partner'].create({'name': 'Hidden Partner', 'company_id': self.company_2.id})
        visible = self.env['res.partner'].create({'name': 'Visible Partner'})
        contact = self.contact_a.with_user(self.user_a)
        with self.assertRaises(AccessError):
            contact.write({'partner_id': hidden.id})
        contact.write({'partner_id': visible.id})
        self.assertEqual(contact._tg_payload()[0]['partner_name'], 'Visible Partner')

    def test_pending_messages_are_capped(self):
        messages = self.env['telegram.message'].with_user(self.user_a)
        for index in range(30):
            messages._tg_send(self.chat_a.id, f'm{index}', f'local-cap-{index:04d}')
        with self.assertRaisesRegex(UserError, 'Too many messages'):
            messages._tg_send(self.chat_a.id, 'one more', 'local-cap-9999')

    def test_malformed_paging_is_rejected(self):
        messages = self.env['telegram.message'].with_user(self.user_a)
        for before in ('not-a-dict', {'date': 5, 'id': 1}, {'date': '2026-01-01 00:00:00', 'id': 'x'}):
            with self.subTest(before=before), self.assertRaises(UserError):
                messages._tg_list(self.chat_a.id, before=before)

    def test_device_info_does_not_reveal_the_kernel(self):
        version = self.env['telegram.account']._tg_engine_settings()['system_version']
        self.assertNotRegex(version, r'\d')
