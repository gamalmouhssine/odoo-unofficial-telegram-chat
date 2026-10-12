from odoo import http
from odoo.http import request


class TelegramController(http.Controller):
    """Routes for the logged-in user's own Telegram account.

    The account is always the caller's, resolved server-side; chat, message
    and contact ids are checked against the caller's record rules. Codes and
    passwords travel in the POST body (never in the URL, which Odoo logs) and
    are not stored in clear.
    """

    # Login ------------------------------------------------------------------

    @http.route('/telegram/auth/status', type='jsonrpc', auth='user')
    def auth_status(self):
        return request.env['telegram.account']._tg_auth_status()

    @http.route('/telegram/auth/start', type='jsonrpc', auth='user')
    def auth_start(self, method='qr'):
        return request.env['telegram.account']._tg_auth_start(method)

    @http.route('/telegram/auth/submit', type='jsonrpc', auth='user')
    def auth_submit(self, step, value):
        return request.env['telegram.account']._tg_auth_submit(step, value)

    @http.route('/telegram/auth/logout', type='jsonrpc', auth='user')
    def auth_logout(self):
        return request.env['telegram.account']._tg_auth_logout()

    @http.route('/telegram/heartbeat', type='jsonrpc', auth='user')
    def heartbeat(self):
        return request.env['telegram.account']._tg_heartbeat()

    # Chats and messages ------------------------------------------------------------

    @http.route('/telegram/chats', type='jsonrpc', auth='user')
    def chats(self):
        return request.env['telegram.chat']._tg_list()

    @http.route('/telegram/messages', type='jsonrpc', auth='user')
    def messages(self, chat_id, before=None, limit=50):
        return request.env['telegram.message']._tg_list(chat_id, before=before, limit=limit)

    @http.route('/telegram/send', type='jsonrpc', auth='user')
    def send(self, chat_id, text, local_uuid):
        return request.env['telegram.message']._tg_send(chat_id, text, local_uuid)

    @http.route('/telegram/retry', type='jsonrpc', auth='user')
    def retry(self, message_id):
        return request.env['telegram.message']._tg_retry(message_id)

    @http.route('/telegram/history', type='jsonrpc', auth='user')
    def history(self, chat_id):
        return request.env['telegram.chat']._tg_request_history(chat_id)

    @http.route('/telegram/read', type='jsonrpc', auth='user')
    def read(self, chat_id):
        return request.env['telegram.chat']._tg_mark_read(chat_id)

    @http.route('/telegram/contacts', type='jsonrpc', auth='user')
    def contacts(self, search=''):
        return request.env['telegram.contact']._tg_list(search if isinstance(search, str) else '')

    @http.route('/telegram/open_contact', type='jsonrpc', auth='user')
    def open_contact(self, contact_id):
        return request.env['telegram.contact']._tg_open_chat(contact_id)
