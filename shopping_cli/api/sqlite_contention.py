"""Narrow SQLite resource-contention classification; no business retry policy."""
from __future__ import annotations
import sqlite3

CONTENTION_MESSAGE = "temporary database contention; reconcile writes before retry"


def is_sqlite_contention(error: BaseException) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    return (isinstance(error, sqlite3.OperationalError) and isinstance(code, int)
            and code & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED))
