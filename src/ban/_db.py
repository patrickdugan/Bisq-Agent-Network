"""SQLite helpers: one short-lived connection per operation, always closed."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator


@contextmanager
def immediate(database: str | Path) -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE`` transaction that commits on success and always closes."""
    db = sqlite3.connect(database, timeout=5.0, isolation_level=None)
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.execute("COMMIT")
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


@contextmanager
def reader(database: str | Path) -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(database, timeout=5.0, isolation_level=None)
    try:
        yield db
    finally:
        db.close()
