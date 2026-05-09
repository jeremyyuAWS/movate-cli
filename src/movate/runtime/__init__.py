"""HTTP runtime — FastAPI app + auth middleware + wire schemas.

The runtime is intentionally a *thin* layer over the storage Protocol
and ``core/auth``. Nothing here re-implements business logic; the
handlers translate between HTTP wire types (``runtime/schemas.py``)
and the persisted models (``core/models.py``).

Public surface:

* :func:`build_app` — factory that returns a FastAPI app bound to a
  given storage backend. Tests pass an :class:`InMemoryStorage`;
  ``movate serve`` passes the configured :class:`SqliteProvider`.
* :class:`AuthContext` — what the auth dependency yields to handlers.

Wire schemas live separately from DB models on purpose — the API can
evolve (e.g. add ``priority`` to /run requests) without forcing a
schema migration, and vice versa.
"""

from __future__ import annotations

from movate.runtime.app import build_app
from movate.runtime.middleware import AuthContext

__all__ = ["AuthContext", "build_app"]
"""``AuthContext`` re-exported here so handlers can ``from movate.runtime
import AuthContext`` rather than reaching into ``runtime.middleware``."""
