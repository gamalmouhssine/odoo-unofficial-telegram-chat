"""Prefork engine check, part 1 (run with `odoo shell` before starting the server).

Configures dummy API credentials on Telegram's test servers and queues a
login code request. Once a prefork server (--workers) runs, its cron worker
must pick the engine job, reach Telegram and record API_ID_INVALID.
"""
params = env['ir.config_parameter']
params.set_param('telegram_personal_client.api_id', '12345')
params.set_param('telegram_personal_client.api_hash', '0' * 32)
params.set_param('telegram_personal_client.use_test_dc', 'True')
accounts = env['telegram.account'].with_user(env.ref('base.user_admin'))
print("START:", accounts._tg_auth_start('phone')['state'])
print("QUEUED:", accounts._tg_auth_submit('phone', '+99966 2 0001')['pending'])
env.cr.commit()
