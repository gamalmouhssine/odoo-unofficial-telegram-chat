from odoo import fields, models

COMMAND_ACTIONS = [
    ('send_code', 'Send Login Code'),
    ('check_code', 'Check Login Code'),
    ('check_password', 'Check Two-Step Password'),
    ('start_qr', 'Start QR Login'),
    ('logout', 'Log Out'),
    ('heartbeat', 'Screen Open'),
    ('send_message', 'Send Message'),
    ('load_history', 'Load Older Messages'),
    ('mark_read', 'Mark Chat as Read'),
    ('open_chat', 'Open Chat with Contact'),
]


class TelegramCommand(models.Model):
    """Work queued by web requests for the engine (engine/runner.py).

    Web requests never connect to Telegram themselves: a session must only
    ever be used from one connection, which the engine owns. Commands are
    deleted as soon as the engine reads them; values (codes, passwords) are
    stored encrypted until then.
    """
    _name = 'telegram.command'
    _description = 'Telegram Command'
    _order = 'id'

    account_id = fields.Many2one('telegram.account', required=True, index=True, ondelete='cascade')
    action = fields.Selection(COMMAND_ACTIONS, required=True)
    payload = fields.Text(groups=fields.NO_ACCESS, help="Encrypted value for the command, if any.")
    # Record the command is about (chat, message or contact), in clear so
    # duplicate requests can be skipped. Never sensitive.
    res_id = fields.Integer()
