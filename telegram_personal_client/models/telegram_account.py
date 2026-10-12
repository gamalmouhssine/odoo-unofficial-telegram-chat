import base64
import io
import os
import platform
import re
import time

import qrcode

from odoo import api, fields, models
from odoo.exceptions import AccessError
from odoo.tools import str2bool

from ..engine.cipher import SessionCipher

PARAM_API_ID = 'telegram_personal_client.api_id'
PARAM_API_HASH = 'telegram_personal_client.api_hash'
PARAM_TEST_DC = 'telegram_personal_client.use_test_dc'
PARAM_MAX_CODES = 'telegram_personal_client.max_code_requests_per_day'
# Optional overrides from the Odoo server's environment (keep the api_hash
# out of the database).
ENV_API_ID = 'TELEGRAM_API_ID'
ENV_API_HASH = 'TELEGRAM_API_HASH'

ACCOUNT_STATES = [
    ('disconnected', 'Disconnected'),
    ('waiting_phone', 'Waiting for Phone Number'),
    ('waiting_qr', 'Waiting for QR Scan'),
    ('waiting_code', 'Waiting for Code'),
    ('waiting_password', 'Waiting for Password'),
    ('connected', 'Connected'),
]

# step: (command, maximum length, states in which the step is accepted)
SUBMIT_STEPS = {
    'phone': ('send_code', 32, ('waiting_phone', 'waiting_code')),
    'code': ('check_code', 64, ('waiting_code',)),
    'password': ('check_password', 256, ('waiting_password',)),
}
PHONE_RE = re.compile(r'\+?[0-9][0-9 ()\-]{5,30}')
# Login commands: at most one waiting per account, each capped (see _tg_login_limits).
LOGIN_COMMANDS = ('send_code', 'check_code', 'check_password', 'start_qr')
LOGIN_LIMIT_KEYS = {
    'send_code': 'send_code', 'start_qr': 'request_qr',
    'check_code': 'check_code', 'check_password': 'check_password',
}
HOUR = 3600
DAY = 24 * HOUR
# Changes that the user's open Telegram screen must reflect.
NOTIFY_FIELDS = {'state', 'last_error', 'qr_link', 'code_delivery', 'password_hint', 'tg_name', 'tg_username'}
ENGINE_CRON = 'telegram_personal_client.ir_cron_telegram_engine'


class TelegramAccount(models.Model):
    """A user's personal Telegram account.

    Users can read their own account but never write it: every field mirrors
    the engine's view (engine/runner.py). The encrypted session and the
    short-lived QR link are restricted to the system.
    """
    _name = 'telegram.account'
    _description = 'Telegram Account'
    _order = 'id'

    user_id = fields.Many2one(
        'res.users', string='User', required=True, index=True, ondelete='cascade',
        default=lambda self: self.env.user)
    company_id = fields.Many2one(
        'res.company', string='Company', required=True, index=True,
        default=lambda self: self.env.company)
    state = fields.Selection(ACCOUNT_STATES, required=True, default='disconnected', readonly=True)
    phone_masked = fields.Char('Phone', readonly=True)
    tg_user_id = fields.Char('Telegram User ID', readonly=True)
    tg_username = fields.Char('Username', readonly=True)
    tg_name = fields.Char('Telegram Name', readonly=True)
    # NO_ACCESS fields: readable by no one through Odoo (not even
    # administrators); the engine and the owner's status use sudo().
    password_hint = fields.Char(readonly=True, groups=fields.NO_ACCESS)
    code_delivery = fields.Char('Code Sent Via', readonly=True)
    last_error = fields.Char(readonly=True)
    last_error_detail = fields.Char(readonly=True, groups=fields.NO_ACCESS)
    last_error_retry = fields.Integer(readonly=True)
    connected_since = fields.Datetime(readonly=True)
    session_data = fields.Text(
        groups=fields.NO_ACCESS, copy=False, prefetch=False,
        help="Telegram session (authorization key and update state), encrypted.")
    qr_link = fields.Char(groups=fields.NO_ACCESS, copy=False)
    login_attempts = fields.Json(groups=fields.NO_ACCESS, copy=False)
    dialogs_synced_at = fields.Datetime('Chat List Synchronized', readonly=True, copy=False)
    contacts_synced_at = fields.Datetime('Contacts Synchronized', readonly=True, copy=False)
    chat_ids = fields.One2many('telegram.chat', 'account_id', string='Chats')
    contact_ids = fields.One2many('telegram.contact', 'account_id', string='Contacts')

    _user_uniq = models.Constraint(
        'UNIQUE(user_id)', 'Each user can connect only one Telegram account.')

    @api.depends('user_id', 'tg_username')
    def _compute_display_name(self):
        for account in self:
            name = account.user_id.name or ''
            if account.tg_username:
                name = f"{name} (@{account.tg_username})"
            account.display_name = name

    # Configuration -------------------------------------------------------------

    @api.model
    def _tg_engine_settings(self):
        """Telegram API credentials and client identity. sudo(): system
        parameters are admin-only; nothing here is returned to the browser."""
        params = self.env['ir.config_parameter'].sudo()
        api_id = (os.environ.get(ENV_API_ID) or params.get_param(PARAM_API_ID) or '').strip()
        api_hash = (os.environ.get(ENV_API_HASH) or params.get_param(PARAM_API_HASH) or '').strip().lower()
        module = self.env['ir.module.module'].sudo().search([('name', '=', 'telegram_personal_client')], limit=1)
        return {
            'api_id': int(api_id) if api_id.isdigit() else 0,
            'api_hash': api_hash if re.fullmatch(r'[0-9a-f]{32}', api_hash) else '',
            'api_hash_source': 'env' if os.environ.get(ENV_API_HASH) else 'param',
            'test_dc': str2bool(params.get_param(PARAM_TEST_DC) or 'False'),
            'max_code_requests_per_day': int(params.get_param(PARAM_MAX_CODES) or 3),
            # Honest device information shown in the user's Telegram "Devices" list.
            'device_model': 'Odoo',
            # Operating system name only: the kernel version would tell
            # Telegram (and the user's device list) more about the server than needed.
            'system_version': platform.system() or 'Server',
            'app_version': f"Unofficial Telegram Chat {module.latest_version or ''}".strip(),
            'lang_code': 'en',
        }

    @api.model
    def _tg_is_configured(self):
        settings = self._tg_engine_settings()
        return bool(settings['api_id'] and settings['api_hash'])

    # Login flow for the current user (controllers/main.py) -----------------------
    # These methods always act on the caller's own account: no parameter
    # selects the account. They only queue work for the engine, which talks
    # to Telegram; results arrive on the bus ("telegram/auth_state").

    @api.model
    def _tg_own_account(self, create=False):
        account = self.search([('user_id', '=', self.env.uid)], limit=1)
        if not account and create:
            # sudo(): accounts are system-managed, users cannot create them.
            # The search above already required the Telegram user group.
            account = self.sudo().create({
                'user_id': self.env.uid,
                'company_id': self.env.company.id,
            }).sudo(False)
        return account

    @api.model
    def _tg_auth_status(self, error=None):
        account = self._tg_own_account()
        pending = bool(account) and bool(self.env['telegram.command'].sudo().search_count(
            [('account_id', '=', account.id), ('action', '!=', 'heartbeat')]))
        qr_link = account.sudo().qr_link if account.state == 'waiting_qr' else False
        return {
            'configured': self._tg_is_configured(),
            'is_admin': self.env.user.has_group('base.group_system'),
            'state': account.state or 'disconnected',
            'pending': pending,
            'code_delivery': account.code_delivery or '',
            # sudo(): the hint is hidden from everyone else (managers included).
            'password_hint': account.sudo().password_hint or '',
            'phone_masked': account.phone_masked or '',
            'tg_name': account.tg_name or '',
            'tg_username': account.tg_username or '',
            'qr_image': self._tg_qr_data_uri(qr_link) if qr_link else False,
            'last_error': '' if pending else account._tg_last_error_message(),
            'error': error or '',
        }

    @api.model
    def _tg_auth_start(self, method):
        if method not in ('qr', 'phone'):
            return self._tg_auth_status(error=self.env._("Unknown login method."))
        if not self._tg_is_configured():
            return self._tg_auth_status(error=self._tg_error_message('NOT_CONFIGURED'))
        account = self._tg_own_account(create=True)
        if account.state == 'connected':
            return self._tg_auth_status(error=self.env._("Your Telegram account is already connected."))
        if method == 'phone':
            # sudo(): system-managed record; no Telegram call is needed yet.
            account.sudo().write({'state': 'waiting_phone', 'last_error': False, 'last_error_detail': False})
            return self._tg_auth_status()
        return self._tg_auth_status(error=account._tg_queue_login('start_qr'))

    @api.model
    def _tg_auth_submit(self, step, value):
        if step not in SUBMIT_STEPS or not isinstance(value, str):
            return self._tg_auth_status(error=self.env._("Invalid request."))
        action, max_length, states = SUBMIT_STEPS[step]
        if step != 'password':
            value = value.strip()
        account = self._tg_own_account()
        error = None
        if not value or len(value) > max_length:
            error = self.env._("Please enter a valid value.")
        elif step == 'phone' and not (
                PHONE_RE.fullmatch(value) and 7 <= len(re.sub(r'\D', '', value)) <= 15):
            error = self.env._(
                "Enter the phone number in international format, for example +20 123 456 7890.")
        elif not account or account.state not in states:
            error = self.env._("This login step has expired. Please start again.")
        if not error:
            error = account._tg_queue_login(action, value)
        return self._tg_auth_status(error=error)

    @api.model
    def _tg_auth_logout(self):
        account = self._tg_own_account()
        if account:
            account._tg_request_disconnect()
        return self._tg_auth_status()

    @api.model
    def _tg_heartbeat(self):
        """The user has the Telegram screen open: show them online."""
        account = self._tg_own_account()
        commands = self.env['telegram.command'].sudo()
        if account.state == 'connected' and not commands.search_count(
                [('account_id', '=', account.id), ('action', '=', 'heartbeat')]):
            account._tg_queue('heartbeat')
        return True

    def action_force_disconnect(self):
        """Administrators: log an account out of Telegram (e.g. when an employee leaves)."""
        if not self.env.user.has_group('telegram_personal_client.group_telegram_manager'):
            raise AccessError(self.env._("Only Telegram administrators can disconnect other users' accounts."))
        # Record rules: administrators only act on accounts they are allowed to see.
        self.check_access('read')
        for account in self:
            account._tg_request_disconnect()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': self.env._("Telegram"),
                'message': self.env._("The account is being disconnected."),
                'type': 'success',
                'next': {'type': 'ir.actions.act_window_close'},
            },
        }

    def _tg_request_disconnect(self):
        self.ensure_one()
        if self.sudo().session_data and self._tg_is_configured():
            # Log out on Telegram too, so the session leaves the user's devices.
            self._tg_queue_once('logout', self.id)
        else:
            # Nothing on Telegram's side to close.
            self.sudo()._tg_reset(False)

    def _tg_queue_login(self, action, value=None):
        """Queue a login step, unless one is already waiting or the attempt
        cap is reached (checked here too, so blocked attempts never open a
        connection to Telegram). Returns an error message, or None."""
        self.ensure_one()
        if self.env['telegram.command'].sudo().search_count(
                [('account_id', '=', self.id), ('action', 'in', LOGIN_COMMANDS)]):
            return None  # the previous step is still being processed
        retry_after = self._tg_login_retry_after(LOGIN_LIMIT_KEYS.get(action))
        if retry_after:
            return self._tg_error_message('RATE_LIMITED', retry_after)
        self._tg_queue(action, value)
        return None

    def _tg_login_limits(self):
        maximum_codes = self._tg_engine_settings()['max_code_requests_per_day']
        return {
            'send_code': (maximum_codes, DAY),
            'request_qr': (10, HOUR),
            'check_code': (5, HOUR),
            'check_password': (5, HOUR),
        }

    def _tg_login_retry_after(self, limit_key, now=None):
        """Seconds until another attempt is allowed (0 when allowed). Caps sit
        below Telegram's own limits and survive restarts and logouts."""
        self.ensure_one()
        if not limit_key:
            return 0
        maximum, window = self._tg_login_limits()[limit_key]
        now = time.time() if now is None else now
        # sudo(): attempt history is hidden from everyone through Odoo.
        recent = [at for at in (self.sudo().login_attempts or {}).get(limit_key, []) if at > now - window]
        return int(recent[0] + window - now) + 1 if len(recent) >= maximum else 0

    @api.model
    def _tg_offboard(self):
        """Disconnect accounts whose user was archived or lost Telegram access,
        so their private messages stop being synchronized."""
        accounts = self.with_context(active_test=False).search([('state', '!=', 'disconnected')])
        for account in accounts:
            user = account.user_id
            if not user.active or not user.has_group('telegram_personal_client.group_telegram_user'):
                account._tg_request_disconnect()

    def _tg_queue(self, action, value=None, res_id=False):
        """Queue work for the engine and wake it up. Values (codes, passwords)
        are encrypted and deleted by the engine as soon as it reads them."""
        self.ensure_one()
        payload = SessionCipher(self.env).encrypt({'value': value}) if value is not None else False
        # sudo(): commands are system records, invisible to users.
        self.env['telegram.command'].sudo().create({
            'account_id': self.id, 'action': action, 'payload': payload, 'res_id': res_id,
        })
        # One pending wake-up is enough: the engine drains the whole queue.
        cron = self.env.ref(ENGINE_CRON).sudo()
        if not self.env['ir.cron.trigger'].sudo().search_count([('cron_id', '=', cron.id)], limit=1):
            cron._trigger()

    def _tg_queue_once(self, action, res_id):
        """Queue an idempotent command unless the same one is already waiting."""
        self.ensure_one()
        if not self.env['telegram.command'].sudo().search_count(
                [('account_id', '=', self.id), ('action', '=', action), ('res_id', '=', res_id)]):
            self._tg_queue(action, res_id, res_id=res_id)

    def _tg_notify(self, payload):
        """Push chat/message changes to the owner's open Telegram screens."""
        self.ensure_one()
        self.user_id._bus_send('telegram/update', payload)

    # Engine callbacks (engine/runner.py, running as superuser) --------------------

    def _tg_write_status(self, vals):
        self.ensure_one()
        changed = {name: value for name, value in vals.items() if self[name] != value}
        if not changed:
            return
        self.write(changed)
        if changed.keys() & NOTIFY_FIELDS:
            self.user_id._bus_send('telegram/auth_state', {'state': self.state})

    def _tg_reset(self, error):
        """Back to disconnected: forget the session and everything cached."""
        self.ensure_one()
        self._tg_purge_cached_data()
        self.env['telegram.command'].search([('account_id', '=', self.id)]).unlink()
        self.write({
            'state': 'disconnected', 'session_data': False, 'qr_link': False,
            'tg_user_id': False, 'tg_username': False, 'tg_name': False, 'phone_masked': False,
            'code_delivery': False, 'password_hint': False, 'connected_since': False,
            'dialogs_synced_at': False, 'contacts_synced_at': False,
            'last_error': error or False, 'last_error_detail': False, 'last_error_retry': 0,
        })
        self.user_id._bus_send('telegram/auth_state', {'state': 'disconnected'})

    def _tg_purge_cached_data(self):
        self.chat_ids.unlink()  # messages are deleted in cascade
        self.contact_ids.unlink()

    # Helpers ----------------------------------------------------------------------

    @api.model
    def _tg_qr_data_uri(self, link):
        image = qrcode.make(link, box_size=8, border=2)
        buffer = io.BytesIO()
        image.save(buffer, format='PNG')
        return 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode()

    def _tg_last_error_message(self):
        if not self or not self.last_error:
            return ''
        # sudo(): the detail is hidden from managers; this is the owner's own status.
        return self._tg_error_message(self.last_error, self.last_error_retry, self.sudo().last_error_detail)

    @api.model
    def _tg_error_message(self, code, retry_after=0, detail=''):
        _ = self.env._
        if code == 'FLOOD_WAIT':
            return _("Telegram asked to wait %(seconds)s seconds before trying again.",
                     seconds=retry_after or 60)
        if code == 'RATE_LIMITED':
            return _("Too many attempts. To protect your account, try again in about %(minutes)s minutes.",
                     minutes=max(1, round((retry_after or 60) / 60)))
        messages = {
            'NOT_CONFIGURED': _("The Telegram API credentials are not configured yet. Please ask your administrator."),
            'NETWORK_ERROR': _("Odoo cannot reach Telegram right now. Please try again in a moment."),
            'NOT_STARTED': _("This login step has expired. Please start again."),
            'QR_EXPIRED': _("The QR code was not scanned in time. Please start again."),
            'PHONE_NUMBER_INVALID': _("This phone number is not valid. Use the international format, for example +20 123 456 7890."),
            'PHONE_NUMBER_BANNED': _("Telegram has banned this phone number."),
            'PHONE_NUMBER_FLOOD': _("Telegram blocked new codes for this number for now. Please try again later."),
            'PHONE_CODE_INVALID': _("Wrong code. Check the code Telegram sent you and try again."),
            'PHONE_CODE_EMPTY': _("Please enter the code Telegram sent you."),
            'PHONE_CODE_EXPIRED': _("This code has expired. Please request a new one."),
            'PASSWORD_INVALID': _("Wrong password."),
            'API_ID_INVALID': _("The Telegram API credentials are invalid. Please contact your administrator."),
            'API_ID_PUBLISHED_FLOOD': _("These Telegram API credentials are public sample credentials. Please contact your administrator."),
            'ACCOUNT_FROZEN': _("Telegram has frozen this account."),
            'ACCOUNT_NOT_FOUND': _("There is no Telegram account for this phone number. Create it in the official Telegram app first."),
            'EMAIL_SETUP_REQUIRED': _("Telegram requires a login email for this account. Set it up in the official Telegram app first, then try again."),
            'SESSION_REVOKED': _("This Telegram session was ended, for example from your phone. Please connect again."),
            'SESSION_KEY_MISMATCH': _("The saved Telegram session could not be decrypted (the server's session key changed). Please connect again."),
            'UNEXPECTED_ERROR': _("Something went wrong while talking to Telegram. Please try again."),
            'CHAT_WRITE_FORBIDDEN': _("You cannot send messages in this chat."),
            'USER_BLOCKED': _("This person has blocked you or you blocked them."),
            'PEER_FLOOD': _("Telegram limited this account for sending too many messages to people who are not contacts. Check @SpamBot in Telegram."),
            'CHAT_NOT_FOUND': _("Telegram could not find this chat."),
            'SEND_LIMIT': _("You are sending messages too fast. Wait a moment and retry."),
        }
        if code in messages:
            return messages[code]
        if detail:
            return _("Telegram returned an error: %(message)s", message=detail)
        return _("Telegram returned an unexpected error (%(code)s).", code=code)
