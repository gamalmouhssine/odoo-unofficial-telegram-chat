from odoo import api, fields, models
from odoo.exceptions import AccessError

from .tools import changed_values


class TelegramContact(models.Model):
    """A Telegram user known to an account: an address-book contact or the
    other side of a private chat."""
    _name = 'telegram.contact'
    _description = 'Telegram Contact'
    _order = 'name, id'

    # Everything else is synchronized from Telegram by the engine.
    _USER_WRITABLE_FIELDS = frozenset({'partner_id'})

    account_id = fields.Many2one('telegram.account', required=True, index=True, ondelete='cascade')
    user_id = fields.Many2one(related='account_id.user_id', store=True, index=True)
    tg_user_id = fields.Char('Telegram User ID', required=True)
    first_name = fields.Char()
    last_name = fields.Char()
    name = fields.Char(compute='_compute_name', store=True)
    username = fields.Char()
    phone = fields.Char(help="Only filled when the person's Telegram privacy settings share it.")
    avatar_128 = fields.Image('Avatar', max_width=128, max_height=128)
    is_contact = fields.Boolean('In Address Book')
    partner_id = fields.Many2one(
        'res.partner', string='Odoo Contact', index='btree_not_null', ondelete='set null')

    _account_tg_user_uniq = models.Constraint(
        'UNIQUE(account_id, tg_user_id)', 'This Telegram user already exists for this account.')

    @api.depends('first_name', 'last_name', 'username', 'tg_user_id')
    def _compute_name(self):
        for contact in self:
            full_name = ' '.join(filter(None, [contact.first_name, contact.last_name]))
            username = contact.username and f'@{contact.username}'
            contact.name = full_name or username or contact.tg_user_id

    def write(self, vals):
        if not self.env.su:
            if not vals.keys() <= self._USER_WRITABLE_FIELDS:
                raise AccessError(self.env._(
                    "Telegram contact details come from Telegram and cannot be edited in Odoo."))
            if vals.get('partner_id'):
                # Only link Odoo contacts the user may see (otherwise reading
                # the link back would reveal names of hidden partners).
                self.env['res.partner'].browse(vals['partner_id']).check_access('read')
        return super().write(vals)

    def _tg_payload(self):
        return [{
            'id': contact.id,
            'name': contact.name or '',
            'username': contact.username or '',
            'phone': contact.phone or '',
            'partner_name': contact.partner_id.display_name
            if contact.partner_id and contact.partner_id.has_access('read') else '',
        } for contact in self]

    # Requests from the current user (controllers/main.py) --------------------------

    @api.model
    def _tg_list(self, search=''):
        account = self.env['telegram.account']._tg_own_account()
        if not account:
            return {'contacts': []}
        domain = [('account_id', '=', account.id), ('is_contact', '=', True)]
        search = (search or '')[:64]
        if search:
            domain += ['|', '|', ('name', 'ilike', search), ('username', 'ilike', search), ('phone', 'ilike', search)]
        return {'contacts': self.search(domain, limit=100)._tg_payload()}

    @api.model
    def _tg_open_chat(self, contact_id):
        """Open the private chat with a contact, creating it on Telegram's side if needed."""
        contact = self.search([('id', '=', int(contact_id))], limit=1)
        if not contact:
            raise AccessError(self.env._("This contact is not available."))
        chat =self.env['telegram.chat'].search([('contact_id', '=', contact.id)], limit=1)
        if chat:
            return {'chat_id': chat.id}
        contact.account_id._tg_queue_once('open_chat', contact.id)
        return {'chat_id': False}

    # Engine callbacks (superuser; engine/runner.py) ---------------------------------

    @api.model
    def _tg_upsert(self, account_id, rows):
        existing = {contact.tg_user_id: contact for contact in self.search(
            [('account_id', '=', account_id), ('tg_user_id', 'in', [row['tg_user_id'] for row in rows])])}
        for row in rows:
            contact = existing.get(row['tg_user_id'])
            if contact:
                diff = changed_values(contact, row)
                if diff:
                    contact.write(diff)
            else:
                existing[row['tg_user_id']] = self.create({'account_id': account_id, **row})
        return True
