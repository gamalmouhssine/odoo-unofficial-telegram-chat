from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .tools import changed_values

CHAT_TYPES = [
    ('private', 'Private'),
    ('basic_group', 'Group'),
    ('supergroup', 'Supergroup'),
    # Kept for later: showing channel content requires sponsored-message
    # support (Telegram API terms, section 3.3), so v1 never creates them.
    ('channel', 'Channel'),
]


class TelegramChat(models.Model):
    """Cached entry of an account's chat list."""
    _name = 'telegram.chat'
    _description = 'Telegram Chat'
    _order = 'is_pinned desc, last_message_date desc, id desc'
    _rec_name = 'title'

    account_id = fields.Many2one('telegram.account', required=True, index=True, ondelete='cascade')
    user_id = fields.Many2one(related='account_id.user_id', store=True, index=True)
    # Telegram ids exceed PostgreSQL int4, so they are stored as text.
    tg_chat_id = fields.Char('Telegram Chat ID', required=True)
    chat_type = fields.Selection(CHAT_TYPES, required=True, default='private')
    title = fields.Char()
    contact_id = fields.Many2one(
        'telegram.contact', string='Contact', index='btree_not_null', ondelete='set null')
    avatar_128 = fields.Image('Avatar', max_width=128, max_height=128)
    photo_available = fields.Boolean(help="Telegram has a profile photo for this chat.")
    has_avatar = fields.Boolean()
    unread_count = fields.Integer()
    last_message_preview = fields.Char()
    last_message_date = fields.Datetime(index=True)
    # TDLib/Telegram message ids are below 2^53, which float8 stores exactly.
    last_read_inbox_key = fields.Float()
    last_read_outbox_key = fields.Float()
    oldest_cached_key = fields.Float()
    is_pinned = fields.Boolean()
    can_send = fields.Boolean(default=True)
    history_loaded = fields.Boolean(help="Older messages were loaded at least once (the chat was opened).")
    history_complete = fields.Boolean(help="The whole history is cached: nothing older on Telegram.")
    message_ids = fields.One2many('telegram.message', 'chat_id', string='Messages')

    _account_tg_chat_uniq = models.Constraint(
        'UNIQUE(account_id, tg_chat_id)', 'This Telegram chat already exists for this account.')

    @api.constrains('account_id', 'contact_id')
    def _check_contact_account(self):
        for chat in self:
            if chat.contact_id and chat.contact_id.account_id != chat.account_id:
                raise ValidationError(self.env._(
                    "A chat can only be linked to a contact of the same Telegram account."))

    # Browser payloads ------------------------------------------------------------

    def _tg_payload(self):
        return [{
            'id': chat.id,
            'title': chat.title or '',
            'chat_type': chat.chat_type,
            'unread_count': chat.unread_count,
            'last_message_preview': chat.last_message_preview or '',
            'last_message_date': fields.Datetime.to_string(chat.last_message_date) if chat.last_message_date else False,
            'is_pinned': chat.is_pinned,
            'can_send': chat.can_send,
            'last_read_outbox_key': chat.last_read_outbox_key,
            'history_complete': chat.history_complete,
            'history_loaded': chat.history_loaded,
            'has_avatar': chat.has_avatar,
            'avatar_unique': fields.Datetime.to_string(chat.write_date) if chat.write_date else '',
            'contact_id': chat.contact_id.id or False,
        } for chat in self]

    # Requests from the current user (controllers/main.py) --------------------------

    @api.model
    def _tg_list(self):
        account = self.env['telegram.account']._tg_own_account()
        chats = self.search([('account_id', '=', account.id)], limit=500) if account else self.browse()
        return {'chats': chats._tg_payload()}

    @api.model
    def _tg_own_chat(self, chat_id):
        """The caller's chat, or an error. The search applies record rules,
        and missing and foreign ids get the same answer, so ids cannot be
        probed."""
        chat = self.search([('id', '=', int(chat_id))], limit=1)
        if not chat:
            raise AccessError(self.env._("This chat is not available."))
        return chat

    @api.model
    def _tg_request_history(self, chat_id):
        chat = self._tg_own_chat(chat_id)
        if not chat.history_complete:
            chat.account_id._tg_queue_once('load_history', chat.id)
        return True

    @api.model
    def _tg_mark_read(self, chat_id):
        chat = self._tg_own_chat(chat_id)
        if chat.unread_count:
            chat.account_id._tg_queue_once('mark_read', chat.id)
        return True

    # Engine callbacks (superuser; engine/runner.py) ---------------------------------

    @api.model
    def _tg_upsert(self, account_id, rows):
        """Create or update chats from engine rows. Idempotent."""
        account = self.env['telegram.account'].browse(account_id)
        keys = [row['tg_chat_id'] for row in rows]
        existing = {chat.tg_chat_id: chat for chat in self.search(
            [('account_id', '=', account_id), ('tg_chat_id', 'in', keys)])}
        contact_ids = {}
        user_keys = [row['contact_tg_user_id'] for row in rows if row.get('contact_tg_user_id')]
        if user_keys:
            contact_ids = {contact.tg_user_id: contact.id for contact in self.env['telegram.contact'].search(
                [('account_id', '=', account_id), ('tg_user_id', 'in', user_keys)])}
        changed = self.browse()
        for row in rows:
            vals = dict(row)
            user_key = vals.pop('contact_tg_user_id', None)
            if user_key and user_key in contact_ids:
                vals['contact_id'] = contact_ids[user_key]
            chat = existing.get(vals['tg_chat_id'])
            if chat:
                diff = changed_values(chat, vals)
                if diff:
                    chat.write(diff)
                    changed |= chat
            else:
                chat = existing[vals['tg_chat_id']] = self.create({'account_id': account_id, **vals})
                changed |= chat
        if changed:
            account._tg_notify({'chats': changed._tg_payload()})
        return True

    @api.model
    def _tg_apply_read(self, account_id, tg_chat_id, inbox_key=None, unread=None, outbox_key=None):
        chat = self.search([('account_id', '=', account_id), ('tg_chat_id', '=', tg_chat_id)], limit=1)
        if not chat:
            return
        vals = {}
        if inbox_key is not None and inbox_key >= chat.last_read_inbox_key:
            vals['last_read_inbox_key'] = inbox_key
        if unread is not None:
            vals['unread_count'] = max(0, unread)
        if outbox_key is not None and outbox_key > chat.last_read_outbox_key:
            vals['last_read_outbox_key'] = outbox_key
        vals = changed_values(chat, vals)
        if vals:
            chat.write(vals)
            chat.account_id._tg_notify({'chats': chat._tg_payload()})

    @api.model
    def _tg_history_loaded(self, chat_id, complete):
        chat = self.browse(chat_id).exists()
        if not chat:
            return
        oldest = self.env['telegram.message'].search(
            [('chat_id', '=', chat.id), ('tg_message_id', '!=', False)], order='sort_key asc', limit=1)
        chat.write({'history_loaded': True, 'history_complete': complete, 'oldest_cached_key': oldest.sort_key})
        chat.account_id._tg_notify({'chats': chat._tg_payload(), 'history': chat.id})

    @api.model
    def _tg_notify_open(self, account_id, tg_chat_id):
        """A chat requested from the contact list is ready: show it."""
        chat = self.search([('account_id', '=', account_id), ('tg_chat_id', '=', tg_chat_id)], limit=1)
        if chat:
            chat.account_id._tg_notify({'chats': chat._tg_payload(), 'open_chat': chat.id})

    @api.model
    def _tg_set_avatar(self, chat_id, image_base64):
        chat = self.browse(chat_id).exists()
        if not chat:
            return
        if image_base64:
            chat.write({'avatar_128': image_base64, 'has_avatar': True})
        else:
            chat.write({'photo_available': False})
        chat.account_id._tg_notify({'chats': chat._tg_payload()})
