"""Lease ids (LS-0001) from a SQLite sequence, and normalising ids people type."""

from __future__ import annotations

import re
import sqlite3

_ID = re.compile(r"^ls-?(\d{1,6})[.,:;!?)]*$", re.IGNORECASE)


def ensure_sequence_table(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS sequences (name TEXT PRIMARY KEY, value INTEGER)")


def next_lease_id(conn: sqlite3.Connection) -> str:
    """Next id; runs inside the caller's transaction when there is one."""
    ensure_sequence_table(conn)
    row = conn.execute(
        "INSERT INTO sequences(name, value) VALUES ('lease', 1) "
        "ON CONFLICT(name) DO UPDATE SET value = value + 1 RETURNING value"
    ).fetchone()
    return f"LS-{int(row[0]):04d}"


def normalize_lease_id(text: str) -> str | None:
    m = _ID.match(text.strip())
    return f"LS-{int(m.group(1)):04d}" if m else None
