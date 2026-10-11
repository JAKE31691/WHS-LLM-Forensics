"""Shared first-stage parsers.

Importing this package registers every common parser with the registry.
"""

from . import chromium_sqlite  # noqa: F401
from . import chromium_preferences  # noqa: F401
from . import chromium_local_storage  # noqa: F401
from . import chromium_session_storage  # noqa: F401
from . import chromium_cache  # noqa: F401
from . import chromium_sessions  # noqa: F401
from . import chromium_indexeddb  # noqa: F401
