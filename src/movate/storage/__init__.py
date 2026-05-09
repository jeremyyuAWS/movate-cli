"""Storage providers: pluggable persistence behind a single Protocol."""

from __future__ import annotations

import os

from movate.storage.base import StorageProvider
from movate.storage.sqlite import SqliteProvider

__all__ = ["SqliteProvider", "StorageProvider", "build_storage"]


def build_storage() -> StorageProvider:
    """Auto-select a StorageProvider.

    v0.1: always SQLite. Path defaults to ``~/.movate/local.db``; override
    with ``MOVATE_DB`` (useful for hermetic tests, scratch dirs, or running
    multiple movate projects against separate DBs). Postgres lands in v0.5.
    """
    db_path = os.environ.get("MOVATE_DB", "~/.movate/local.db")
    return SqliteProvider(db_path=db_path)
