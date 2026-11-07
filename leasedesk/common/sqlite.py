"""Open a SQLite database in WAL mode with every file it writes at mode 600."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def open_private(path: Path) -> sqlite3.Connection:
    """SQLite gives the -wal and -shm files the database file's mode, so set 600 first."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        if p.exists():
            p.chmod(0o600)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn
