"""Encryption of Telegram session data and queued login values at rest."""
import base64
import json
import os

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# Recommended: set on the Odoo server so a database dump alone cannot
# decrypt sessions. Without it the database secret is used.
ENV_SESSION_KEY = 'TELEGRAM_SESSION_KEY'
_INFO = b'telegram_personal_client/session-v1'


def env_session_key():
    return os.environ.get(ENV_SESSION_KEY, '').strip()


def _fernet(secret, info):
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(secret.encode())
    return Fernet(base64.urlsafe_b64encode(key))


class SessionCipher:
    """Encrypts with TELEGRAM_SESSION_KEY when set, else the database secret.

    Keys are bound to the database's uuid, which Odoo regenerates when a
    database is duplicated or restored as a copy: copies cannot open the
    original's sessions. Decryption accepts either secret, so adding the
    environment key later keeps sessions working (re-encrypted on next save).

    legacy=True: keys of version 19.0.3 and earlier (not bound to the uuid),
    only used by the upgrade script.
    """

    def __init__(self, env, legacy=False):
        params = env['ir.config_parameter'].sudo()
        info = _INFO if legacy else _INFO + b'/' + (params.get_param('database.uuid') or '').encode()
        secrets = [secret for secret in (env_session_key(), params.get_param('database.secret')) if secret]
        if not secrets:
            raise RuntimeError("No secret available to encrypt Telegram sessions")
        self._fernets = [_fernet(secret, info) for secret in secrets]

    def encrypt(self, data):
        return self._fernets[0].encrypt(json.dumps(data).encode()).decode()

    def decrypt(self, token):
        """Return the decrypted dict, or None when no key matches."""
        for fernet in self._fernets:
            try:
                return json.loads(fernet.decrypt(token.encode()))
            except InvalidToken:
                continue
        return None
