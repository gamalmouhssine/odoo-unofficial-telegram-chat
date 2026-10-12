"""Makes the libraries bundled in lib/ importable (see lib/VENDORED.md)."""
import importlib
import logging
import os
import sys

LIB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'lib')
# Always taken from lib/: the module is tested with these exact versions.
PINNED = ('pyaes', 'rsa', 'telethon')


def ensure_libraries():
    if 'telethon' in sys.modules:
        return
    # Telethon is chatty at INFO level (import, every reconnect); keep its
    # warnings only, unless the administrator configured its logger.
    telethon_logger = logging.getLogger('telethon')
    if telethon_logger.level == logging.NOTSET:
        telethon_logger.setLevel(logging.WARNING)
    # pyasn1 is also used by other Odoo dependencies: keep the server's copy
    # when it has one, so loading Telethon never changes it under their feet.
    try:
        import pyasn1  # noqa: F401
    except ImportError:
        pass
    sys.path.insert(0, LIB_DIR)
    try:
        for name in PINNED:
            importlib.import_module(name)
    finally:
        sys.path.remove(LIB_DIR)
        # Fallback for pyasn1 on servers that do not have it.
        sys.path.append(LIB_DIR)
