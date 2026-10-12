import re

from odoo import api, fields, models
from odoo.exceptions import UserError

from ..engine.cipher import env_session_key
from .telegram_account import ENGINE_CRON, PARAM_API_HASH, PARAM_API_ID, PARAM_TEST_DC
from .telegram_message import PARAM_RETENTION_DAYS


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    telegram_api_id = fields.Char(
        'Telegram API ID', config_parameter=PARAM_API_ID,
        help="From https://my.telegram.org, under API development tools.")
    # Write-only: computed empty and not stored, so the api_hash is never
    # sent back to the browser nor kept in the transient settings table.
    telegram_api_hash = fields.Char(
        'New API Hash', compute='_compute_telegram_api_hash', inverse='_inverse_telegram_api_hash')
    telegram_api_hash_source = fields.Selection(
        [('env', 'Set by environment variable'), ('param', 'Stored in Odoo'), ('missing', 'Not set')],
        string='API Hash Status', compute='_compute_telegram_api_hash_source')
    telegram_use_test_dc = fields.Boolean(
        'Use Telegram Test Servers', config_parameter=PARAM_TEST_DC,
        help="For development only: test servers use separate test accounts (numbers 99966XYYYY).")
    telegram_engine_lastcall = fields.Datetime(
        'Last Engine Run', compute='_compute_telegram_engine_lastcall')
    telegram_session_key_source = fields.Selection(
        [('env', 'Server environment (TELEGRAM_SESSION_KEY)'), ('database', 'Database secret (weaker)')],
        string='Session Encryption Key', compute='_compute_telegram_session_key_source')
    telegram_message_retention_days = fields.Integer(
        'Keep Cached Messages (days)', config_parameter=PARAM_RETENTION_DAYS,
        help="Cached messages older than this are deleted from Odoo (0 keeps them). "
             "They stay available in Telegram.")

    def _compute_telegram_api_hash(self):
        self.telegram_api_hash = False

    def _inverse_telegram_api_hash(self):
        for settings in self:
            api_hash = (settings.telegram_api_hash or '').strip().lower()
            if not api_hash:
                continue
            if not re.fullmatch(r'[0-9a-f]{32}', api_hash):
                raise UserError(self.env._("The API hash is the 32-character value shown on my.telegram.org."))
            self.env['ir.config_parameter'].set_param(PARAM_API_HASH, api_hash)

    @api.depends('telegram_api_hash')
    def _compute_telegram_api_hash_source(self):
        settings = self.env['telegram.account']._tg_engine_settings()
        self.telegram_api_hash_source = settings['api_hash_source'] if settings['api_hash'] else 'missing'

    def _compute_telegram_engine_lastcall(self):
        self.telegram_engine_lastcall = self.env.ref(ENGINE_CRON).sudo().lastcall

    def _compute_telegram_session_key_source(self):
        self.telegram_session_key_source = 'env' if env_session_key() else 'database'

    def set_values(self):
        api_id = (self.telegram_api_id or '').strip()
        if api_id and not api_id.isdigit():
            raise UserError(self.env._("The API ID is the number shown on my.telegram.org."))
        self.telegram_api_id = api_id
        super().set_values()
