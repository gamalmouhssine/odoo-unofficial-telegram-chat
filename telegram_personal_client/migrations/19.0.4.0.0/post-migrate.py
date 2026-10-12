"""19.0.4.0.0: session keys are now bound to the database uuid.

Re-encrypt existing sessions with the new key so connected users stay
connected. Queued commands (short-lived, encrypted with the old key) are
dropped; users simply repeat a pending login step.
"""
import logging

from odoo import SUPERUSER_ID, api

from odoo.addons.telegram_personal_client.engine.cipher import SessionCipher

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    legacy, current = SessionCipher(env, legacy=True), SessionCipher(env)
    migrated = lost = 0
    for account in env['telegram.account'].search([('session_data', '!=', False)]):
        data = legacy.decrypt(account.session_data)
        if data is None:
            data = current.decrypt(account.session_data)
            if data is None:
                lost += 1
                continue
        account.session_data = current.encrypt(data)
        migrated += 1
    env['telegram.command'].search([]).unlink()
    _logger.info("Telegram sessions re-encrypted: %s (unreadable: %s)", migrated, lost)
