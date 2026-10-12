"""Demo data for UI checks and store screenshots (run with `odoo shell`).

Made-up people and messages only. The engine is switched off in this
database and the API credentials are dummies, so nothing ever talks to
Telegram here.
"""
from datetime import timedelta

from odoo import Command, fields

USER_GROUP = 'telegram_personal_client.group_telegram_user'
now = fields.Datetime.now().replace(second=0, microsecond=0)

env.ref('telegram_personal_client.ir_cron_telegram_engine').active = False
params = env['ir.config_parameter']
params.set_param('telegram_personal_client.api_id', '1111111')
params.set_param('telegram_personal_client.api_hash', 'f' * 32)
env.ref('base.user_admin').password = 'admin-check-1'


def user(login, name):
    record = env['res.users'].search([('login', '=', login)]) or env['res.users'].create({
        'login': login, 'name': name, 'email': f'{login}@example.com',
        'group_ids': [Command.link(env.ref(USER_GROUP).id)],
    })
    record.password = f'{login}-check-1'
    return record


demo = user('demo', 'Mona Hassan')
newcomer = user('newcomer', 'Karim Adel')  # not connected: shows the login wizard

Account = env['telegram.account']
Account.search([('user_id', 'in', (demo | newcomer).ids)]).unlink()
account = Account.create({
    'user_id': demo.id, 'state': 'connected', 'tg_user_id': '700100', 'tg_username': 'mona',
    'tg_name': 'Mona Hassan', 'phone_masked': '+201*******45', 'connected_since': now - timedelta(days=3),
    'dialogs_synced_at': now, 'contacts_synced_at': now,
})

Contact = env['telegram.contact']
contacts = {}
for tg_id, first, last, username in [
    ('1001', 'Ahmed', 'Karim', 'ahmed_k'), ('1002', 'Sara', 'Youssef', 'sara.y'),
    ('1003', 'Omar', 'Nabil', ''), ('1004', 'Layla', 'Fouad', 'layla'), ('1005', 'Youssef', 'Samir', ''),
]:
    contacts[tg_id] = Contact.create({
        'account_id': account.id, 'tg_user_id': tg_id, 'first_name': first, 'last_name': last,
        'username': username, 'is_contact': True,
    })
env['res.partner'].create({'name': 'Karim Trading LLC', 'is_company': True})

Chat = env['telegram.chat']
Message = env['telegram.message']


def chat(tg_id, title, chat_type, messages, contact=None, unread=0, pinned=False, read_out=0):
    record = Chat.create({
        'account_id': account.id, 'tg_chat_id': tg_id, 'title': title, 'chat_type': chat_type,
        'contact_id': contact.id if contact else False, 'unread_count': unread, 'is_pinned': pinned,
        'last_read_outbox_key': read_out, 'history_loaded': True, 'history_complete': True,
    })
    for index, (minutes_ago, sender, body, extra) in enumerate(messages, start=1):
        outgoing = sender is None
        Message.create({
            'chat_id': record.id, 'tg_message_id': str(index * 10), 'sort_key': index * 10,
            'date': now - timedelta(minutes=minutes_ago), 'is_outgoing': outgoing,
            'sender_name': 'Mona Hassan' if outgoing else sender, 'body': body,
            'state': 'sent' if outgoing else 'received', **extra,
        })
    last = record.message_ids.sorted('date')[-1:]
    preview = last.body or '[%s]' % (last.content_meta or {}).get('label', 'Message')
    record.write({'last_message_date': last.date, 'last_message_preview': preview[:120]})
    return record


chat('1001', 'Ahmed Karim', 'private', [
    (180, 'Ahmed Karim', 'Good morning! Did the concrete delivery for Block B arrive?', {}),
    (176, None, 'Yes, 12 trucks so far. The last 3 are expected before noon.', {}),
    (175, None, 'I will send the delivery notes to accounting today.', {}),
    (60, 'Ahmed Karim', 'Great, thanks.\nCan you also confirm the crane schedule for Thursday?', {}),
    (4, 'Ahmed Karim', 'The client wants to visit the site at 2 pm', {}),
], contact=contacts['1001'], unread=2, pinned=True, read_out=30)
chat('-5001', 'Site Team – Phase 2', 'basic_group', [
    (300, 'Sara Youssef', 'Safety inspection passed on floors 3 and 4 ✅', {}),
    (290, 'Omar Nabil', '', {'content_type': 'photo', 'content_meta': {'label': 'Photo'}}),
    (288, 'Omar Nabil', 'Scaffolding on the east side is ready.', {}),
    (240, None, 'Thanks team. Daily report due at 5 pm.', {}),
    (35, 'Layla Fouad', 'Material request #482 approved.', {}),
], unread=1, read_out=40)
chat('1002', 'Sara Youssef', 'private', [
    (1500, None, 'Here is the updated BOQ for the facade works.', {'content_type': 'document', 'content_meta': {'label': 'BOQ_facade_v3.xlsx'}}),
    (1490, 'Sara Youssef', 'Received, I will review it tonight.', {}),
    (1400, 'Sara Youssef', '', {'content_type': 'voice', 'content_meta': {'label': 'Voice message'}}),
], contact=contacts['1002'], read_out=10)
chat('-1001234', 'Procurement', 'supergroup', [
    (2900, 'Youssef Samir', 'Steel prices updated for October.', {}),
    (2880, 'Layla Fouad', 'Supplier quotes are in the shared folder.', {}),
])
chat('1003', 'Omar Nabil', 'private', [
    (4400, 'Omar Nabil', 'Can we move the meeting to Sunday?', {}),
    (4390, None, 'Sunday 10 am works for me.', {}),
], contact=contacts['1003'], read_out=20)
chat('700100', 'Saved Messages', 'private', [
    (6000, None, 'Reminder: renew site insurance before the 30th', {}),
])
env.cr.commit()
print("SEEDED chats:", Chat.search_count([('account_id', '=', account.id)]),
      "messages:", Message.search_count([('account_id', '=', account.id)]),
      "contacts:", Contact.search_count([('account_id', '=', account.id)]))
