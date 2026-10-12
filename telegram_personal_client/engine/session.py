"""Telethon session stored in the Odoo database (encrypted) instead of a file."""
import base64
import datetime

from .vendor import ensure_libraries

ensure_libraries()
from telethon.crypto import AuthKey  # noqa: E402
from telethon.sessions import MemorySession  # noqa: E402
from telethon.tl import types  # noqa: E402

MAX_ENTITIES = 5000


class DbSession(MemorySession):
    """MemorySession that converts to and from a JSON-serialisable dict.

    Holds the authorization key, the data center, the update state (so
    missed messages are fetched on reconnect) and the cached access hashes.
    """

    @classmethod
    def from_dict(cls, data):
        session = cls()
        if not data:
            return session
        if data.get('dc_id'):
            session.set_dc(data['dc_id'], data['server_address'], data['port'])
        if data.get('auth_key'):
            session.auth_key = AuthKey(base64.b64decode(data['auth_key']))
        for entity_id, (pts, qts, date, seq) in (data.get('update_states') or {}).items():
            session.set_update_state(int(entity_id), types.updates.State(
                pts=pts, qts=qts, seq=seq, unread_count=0,
                date=datetime.datetime.fromtimestamp(date, tz=datetime.timezone.utc)))
        session._entities = {tuple(row) for row in data.get('entities') or []}
        return session

    def to_dict(self):
        auth_key = self.auth_key
        return {
            'dc_id': self.dc_id,
            'server_address': self.server_address,
            'port': self.port,
            'auth_key': base64.b64encode(auth_key.key).decode() if auth_key and auth_key.key else None,
            'update_states': {
                str(entity_id): [state.pts, state.qts, _timestamp(state.date), state.seq]
                for entity_id, state in self.get_update_states()
            },
            'entities': [list(row) for row in list(self._entities)[-MAX_ENTITIES:]],
        }


def _timestamp(value):
    if isinstance(value, datetime.datetime):
        return int(value.timestamp())
    return int(value or 0)
