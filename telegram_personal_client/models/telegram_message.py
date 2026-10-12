import re
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .tools import changed_values

CONTENT_TYPES = [
    ('text', 'Text'),
    ('photo', 'Photo'),
    ('video', 'Video'),
    ('document', 'Document'),
    ('voice', 'Voice Message'),
    ('sticker', 'Sticker'),
    ('other', 'Other'),
]

MESSAGE_STATES = [
    ('pending', 'Sending'),
    ('sent', 'Sent'),
    ('failed', 'Failed'),
    ('received', 'Received'),
]

MAX_TEXT_LENGTH = 4096  # Telegram's limit for one text message
MAX_PENDING = 30  # messages waiting to be sent, per account
PARAM_RETENTION_DAYS = 'telegram_personal_client.message_retention_days'
_UUID_RE = re.compile(r'[0-9a-zA-Z-]{8,64}')


class TelegramMessage(models.Model):
    """Cached Telegram message.

    Only messages the user has loaded or received are cached; Telegram stays
    the source of truth. Bodies are plain text and must be rendered escaped.
    """
    _name = 'telegram.message'
    _description = 'Telegram Message'
    _order = 'date desc, sort_key desc, id desc'

    chat_id = fields.Many2one('telegram.chat', required=True, index=True, ondelete='cascade')
    account_id = fields.Many2one(related='chat_id.account_id', store=True, index=True)
    user_id = fields.Many2one(related='chat_id.user_id', store=True, index=True)
    # Empty while a sent message waits for Telegram to assign its id.
    tg_message_id = fields.Char('Telegram Message ID')
    # Numeric copy of the Telegram message id (below 2^53, exact in float8) for ordering.
    sort_key = fields.Float()
    date = fields.Datetime(required=True, default=fields.Datetime.now)
    is_outgoing = fields.Boolean()
    sender_tg_id = fields.Char('Sender Telegram ID')
    sender_name = fields.Char()
    body = fields.Text()
    content_type = fields.Selection(CONTENT_TYPES, required=True, default='text')
    # Summary of non-text content (label shown in the UI), ready for the media phase.
    content_meta = fields.Json()
    state = fields.Selection(MESSAGE_STATES, required=True, default='received')
    error_message = fields.Char()
    # Client-generated key making sends idempotent across retries.
    local_uuid = fields.Char(copy=False)
    is_edited = fields.Boolean()
    is_deleted = fields.Boolean()

    _chat_tg_message_uniq = models.Constraint(
        'UNIQUE(chat_id, tg_message_id)', 'This Telegram message already exists in this chat.')
    _account_local_uuid_uniq = models.Constraint(
        'UNIQUE(account_id, local_uuid)', 'This message was already sent.')
    _chat_sort_idx = models.Index('(chat_id, sort_key)')

    # Browser payloads ------------------------------------------------------------

    def _tg_payload(self):
        return [{
            'id': message.id,
            'chat_id': message.chat_id.id,
            'tg_message_id': message.tg_message_id or False,
            'sort_key': message.sort_key,
            'date': fields.Datetime.to_string(message.date),
            'is_outgoing': message.is_outgoing,
            'sender_name': message.sender_name or '',
            'body': message.body or '',
            'content_type': message.content_type,
            'content_label': (message.content_meta or {}).get('label', '') if message.content_meta else '',
            'state': message.state,
            'error_message': message.error_message or '',
            'is_edited': message.is_edited,
            'local_uuid': message.local_uuid or False,
        } for message in self]

    # Requests from the current user (controllers/main.py) --------------------------

    @api.model
    def _tg_list(self, chat_id, before=None, limit=50):
        chat = self.env['telegram.chat']._tg_own_chat(chat_id)
        limit = max(1, min(int(limit), 100))
        domain = [('chat_id', '=', chat.id)]
        if before:
            if not (isinstance(before, dict) and isinstance(before.get('date'), str) and str(before.get('id', '')).isdigit()):
                raise UserError(self.env._("Invalid request."))
            date = fields.Datetime.to_datetime(before['date'])
            domain += ['|', ('date', '<', date), '&', ('date', '=', date), ('id', '<', int(before['id']))]
        messages = self.search(domain, limit=limit)
        return {
            'messages': list(reversed(messages._tg_payload())),
            'has_more': len(messages) == limit,
            'history_complete': chat.history_complete,
            'history_loaded': chat.history_loaded,
        }

    @api.model
    def _tg_send(self, chat_id, text, local_uuid):
        chat = self.env['telegram.chat']._tg_own_chat(chat_id)
        if not isinstance(text, str) or not text.strip():
            raise UserError(self.env._("The message is empty."))
        if len(text) > MAX_TEXT_LENGTH:
            raise UserError(self.env._("Telegram messages are limited to %(max)s characters.", max=MAX_TEXT_LENGTH))
        if not isinstance(local_uuid, str) or not _UUID_RE.fullmatch(local_uuid):
            raise UserError(self.env._("Invalid request."))
        account = chat.account_id
        if account.state != 'connected':
            raise UserError(self.env._("Your Telegram account is not connected."))
        if not chat.can_send:
            raise UserError(self.env._("You cannot send messages in this chat."))
        # sudo(): messages are system-managed; ownership was checked above.
        messages = self.sudo()
        existing = messages.search([('account_id', '=', account.id), ('local_uuid', '=', local_uuid)], limit=1)
        if existing:
            return {'message': existing._tg_payload()[0]}
        if messages.search_count([('account_id', '=', account.id), ('state', '=', 'pending')]) >= MAX_PENDING:
            raise UserError(self.env._("Too many messages are waiting to be sent. Please wait a moment."))
        message = messages.create({
            'chat_id': chat.id,
            'is_outgoing': True,
            'body': text,
            'state': 'pending',
            'local_uuid': local_uuid,
            'sender_name': account.tg_name or False,
        })
        account._tg_queue('send_message', message.id, res_id=message.id)
        return {'message': message._tg_payload()[0]}

    @api.model
    def _tg_retry(self, message_id):
        message = self.search([('id', '=', int(message_id))], limit=1)
        if not message:
            raise AccessError(self.env._("This message is not available."))
        if not (message.is_outgoing and message.state == 'failed'):
            raise UserError(self.env._("Only failed messages can be sent again."))
        # sudo(): system-managed record; ownership was checked above.
        message.sudo().write({'state': 'pending', 'error_message': False})
        message.account_id._tg_queue('send_message', message.id, res_id=message.id)
        return {'message': message._tg_payload()[0]}

    # Engine callbacks (superuser; engine/runner.py) ---------------------------------

    @api.model
    def _tg_upsert(self, account_id, rows, mode):
        """Store messages from engine rows. Idempotent.

        mode 'live': new messages as they arrive (counts unread, matches our
        own sends, and caches large groups only once the user opened them);
        'history' / 'dialog': loaded pages and chat-list top messages;
        'edit': updates messages already cached, never creates.
        """
        account = self.env['telegram.account'].browse(account_id)
        chats = {chat.tg_chat_id: chat for chat in self.env['telegram.chat'].search(
            [('account_id', '=', account_id), ('tg_chat_id', 'in', list({row['tg_chat_id'] for row in rows}))])}
        existing = {(message.chat_id.id, message.tg_message_id): message for message in self.search([
            ('chat_id', 'in', [chat.id for chat in chats.values()]),
            ('tg_message_id', 'in', [row['tg_message_id'] for row in rows]),
        ])}
        touched = self.browse()
        chat_vals = {}
        for row in sorted(rows, key=lambda r: r['sort_key']):
            chat = chats.get(row['tg_chat_id'])
            if not chat:
                continue
            vals = {name: value for name, value in row.items() if name != 'tg_chat_id'}
            message = existing.get((chat.id, row['tg_message_id']))
            is_new = not message
            if is_new and mode == 'live' and row['is_outgoing']:
                # Our own send whose result is not recorded yet.
                message = self.search([
                    ('chat_id', '=', chat.id), ('state', '=', 'pending'),
                    ('tg_message_id', '=', False), ('body', '=', row['body']),
                ], order='id', limit=1)
                is_new = False if message else is_new
            if message:
                if not message.tg_message_id or mode != 'dialog':
                    diff = changed_values(message, vals)
                    if diff:
                        message.write(diff)
                        touched |= message
            elif mode != 'edit' and (mode != 'live' or chat.chat_type != 'supergroup' or chat.history_loaded):
                message = self.create({'chat_id': chat.id, **vals})
                existing[(chat.id, row['tg_message_id'])] = message
                touched |= message
            if mode == 'edit':
                continue
            pending = chat_vals.setdefault(chat, {})
            last_date = pending.get('last_message_date', chat.last_message_date)
            if not last_date or row['date'] >= last_date:
                pending['last_message_date'] = row['date']
                pending['last_message_preview'] = self._tg_preview(row)
            if mode == 'live' and is_new and not row['is_outgoing'] and row['sort_key'] > chat.last_read_inbox_key:
                pending['unread_count'] = pending.get('unread_count', chat.unread_count) + 1
        changed_chats = self.env['telegram.chat']
        for chat, vals in chat_vals.items():
            diff = changed_values(chat, vals)
            if diff:
                chat.write(diff)
                changed_chats |= chat
        if touched or changed_chats:
            account._tg_notify({'messages': touched._tg_payload(), 'chats': changed_chats._tg_payload()})
        return True

    @api.model
    def _tg_preview(self, row):
        body = (row.get('body') or '').strip().replace('\n', ' ')
        if body:
            return body[:120]
        label = (row.get('content_meta') or {}).get('label') if row.get('content_meta') else ''
        return f"[{label}]" if label else False

    @api.model
    def _tg_delete(self, account_id, tg_chat_id, tg_message_ids):
        """Messages deleted on Telegram. Outside supergroups, message ids are
        unique per account, so Telegram does not say which chat they were in."""
        domain = [('account_id', '=', account_id), ('tg_message_id', 'in', tg_message_ids)]
        if tg_chat_id:
            domain.append(('chat_id.tg_chat_id', '=', tg_chat_id))
        else:
            domain.append(('chat_id.chat_type', '!=', 'supergroup'))
        messages = self.search(domain)
        if not messages:
            return
        deleted = [{'chat_id': message.chat_id.id, 'id': message.id} for message in messages]
        messages.unlink()
        self.env['telegram.account'].browse(account_id)._tg_notify({'deleted': deleted})

    @api.model
    def _tg_send_result(self, message_id, vals):
        message = self.browse(message_id).exists()
        if not message:
            return
        vals = dict(vals)
        can_send = vals.pop('can_send', None)
        if vals.get('tg_message_id'):
            # The live update for this message may have been stored first.
            duplicate = self.search([
                ('chat_id', '=', message.chat_id.id), ('tg_message_id', '=', vals['tg_message_id']),
                ('id', '!=', message.id)])
            if duplicate:
                deleted = [{'chat_id': message.chat_id.id, 'id': record.id} for record in duplicate]
                duplicate.unlink()
                message.account_id._tg_notify({'deleted': deleted})
        message.write(vals)
        chat = message.chat_id
        chat_vals = {}
        if vals.get('state') == 'sent' and (not chat.last_message_date or message.date >= chat.last_message_date):
            chat_vals = {'last_message_date': message.date, 'last_message_preview': self._tg_preview(
                {'body': message.body, 'content_meta': message.content_meta})}
        if can_send is False:
            chat_vals['can_send'] = False
        if chat_vals:
            chat.write(chat_vals)
        message.account_id._tg_notify({'messages': message._tg_payload(), 'chats': chat._tg_payload()})

    @api.model
    def _cron_purge_old_messages(self):
        """Retention setting: drop cached messages older than N days (0 keeps them)."""
        days = int(self.env['ir.config_parameter'].sudo().get_param(PARAM_RETENTION_DAYS) or 0)
        if days > 0:
            self.search([('date', '<', fields.Datetime.now() - timedelta(days=days))]).unlink()
