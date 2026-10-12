"""Translation of Telethon errors into the module's error codes."""
import asyncio
import re

from .vendor import ensure_libraries

ensure_libraries()
from telethon import errors  # noqa: E402

# Telethon error class name -> our code
_CODES = {
    'PhoneNumberInvalidError': 'PHONE_NUMBER_INVALID',
    'PhoneNumberBannedError': 'PHONE_NUMBER_BANNED',
    'PhoneNumberFloodError': 'PHONE_NUMBER_FLOOD',
    'PhoneCodeInvalidError': 'PHONE_CODE_INVALID',
    'PhoneCodeExpiredError': 'PHONE_CODE_EXPIRED',
    'PhoneCodeEmptyError': 'PHONE_CODE_EMPTY',
    'PasswordHashInvalidError': 'PASSWORD_INVALID',
    'PhoneNumberUnoccupiedError': 'ACCOUNT_NOT_FOUND',
    'ApiIdInvalidError': 'API_ID_INVALID',
    'ApiIdPublishedFloodError': 'API_ID_PUBLISHED_FLOOD',
    'AuthKeyUnregisteredError': 'SESSION_REVOKED',
    'SessionRevokedError': 'SESSION_REVOKED',
    'SessionExpiredError': 'SESSION_REVOKED',
    'UserDeactivatedError': 'SESSION_REVOKED',
    'UserDeactivatedBanError': 'SESSION_REVOKED',
    'AuthKeyDuplicatedError': 'SESSION_REVOKED',
    'ChatWriteForbiddenError': 'CHAT_WRITE_FORBIDDEN',
    'ChatAdminRequiredError': 'CHAT_WRITE_FORBIDDEN',
    'ChatRestrictedError': 'CHAT_WRITE_FORBIDDEN',
    'UserBannedInChannelError': 'CHAT_WRITE_FORBIDDEN',
    'UserIsBlockedError': 'USER_BLOCKED',
    'YouBlockedUserError': 'USER_BLOCKED',
    'PeerFloodError': 'PEER_FLOOD',
    'ChannelPrivateError': 'CHAT_NOT_FOUND',
    'PeerIdInvalidError': 'CHAT_NOT_FOUND',
    'InputUserDeactivatedError': 'CHAT_NOT_FOUND',
}
# Errors after which the login or session cannot continue.
FATAL_CODES = {'SESSION_REVOKED', 'ACCOUNT_NOT_FOUND', 'PHONE_NUMBER_BANNED', 'EMAIL_SETUP_REQUIRED',
               'NOT_STARTED', 'QR_EXPIRED'}


class LoginRefused(Exception):
    def __init__(self, code, retry_after=None):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


def error_code(error):
    """Return (code, retry_after, detail) for an exception raised while talking to Telegram."""
    if isinstance(error, LoginRefused):
        return error.code, error.retry_after, ''
    if isinstance(error, (errors.FloodWaitError, errors.SlowModeWaitError)):
        return 'FLOOD_WAIT', error.seconds, ''
    if isinstance(error, ValueError) and 'input entity' in str(error):
        # Telethon has no access hash for this chat (never seen in this session).
        return 'CHAT_NOT_FOUND', None, ''
    code = _CODES.get(type(error).__name__)
    if code:
        return code, None, ''
    if isinstance(error, errors.RPCError):
        if (getattr(error, 'message', '') or '').startswith('FROZEN_'):
            return 'ACCOUNT_FROZEN', None, ''
        return 'TELEGRAM_ERROR', None, re.sub(r'\s+', ' ', str(error))[:200]
    if isinstance(error, (ConnectionError, OSError, asyncio.TimeoutError)):
        return 'NETWORK_ERROR', None, ''
    return 'UNEXPECTED_ERROR', None, type(error).__name__
