"""Storage providers: pluggable persistence behind a single Protocol.

Auto-selection in :func:`build_storage`:

* ``MOVATE_DB_URL`` set and starts with ``postgres://`` /
  ``postgresql://`` → :class:`PostgresProvider` (v0.5+).
* otherwise → :class:`SqliteProvider` at ``MOVATE_DB`` or
  ``~/.movate/local.db``.

Postgres dependency is in the ``[runtime]`` extra; importing the
provider only happens when the env points at it, so users on the
sqlite path never need ``asyncpg``.
"""

from __future__ import annotations

import os

from movate.storage.base import StorageProvider
from movate.storage.sqlite import SqliteProvider

__all__ = ["SqliteProvider", "StorageProvider", "build_storage"]


def build_storage() -> StorageProvider:
    """Auto-select a StorageProvider based on environment.

    * ``MOVATE_DB_URL`` (e.g. ``postgresql://user:pw@host/db``) →
      :class:`PostgresProvider`. Production / multi-worker.
    * ``MOVATE_DB`` or default ``~/.movate/local.db`` →
      :class:`SqliteProvider`. Local dev and CI.

    Both implement the same Protocol so application code never
    branches on backend.
    """
    db_url = os.environ.get("MOVATE_DB_URL")
    if db_url and db_url.startswith(("postgres://", "postgresql://")):
        # Lazy import — keeps asyncpg optional for sqlite-only users.
        from movate.storage.postgres import PostgresProvider  # noqa: PLC0415

        return PostgresProvider(dsn=db_url)
    db_path = os.environ.get("MOVATE_DB", "~/.movate/local.db")
    return SqliteProvider(db_path=db_path)
