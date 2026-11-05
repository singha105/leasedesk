from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from leasedesk.common.ids import next_lease_id, normalize_lease_id


def test_ids_sequential_across_reopen(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    with sqlite3.connect(db) as conn:
        assert next_lease_id(conn) == "LS-0001"
        assert next_lease_id(conn) == "LS-0002"
    with sqlite3.connect(db) as conn:
        assert next_lease_id(conn) == "LS-0003"


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("LS-0007", "LS-0007"),
        ("ls-7", "LS-0007"),
        ("LS-0007,", "LS-0007"),
        ("ls7", "LS-0007"),
        ("LS-12345", "LS-12345"),
        ("7", None),
        ("LS-", None),
        ("LS-00x7", None),
    ],
)
def test_normalize_lease_id(text: str, want: str | None) -> None:
    assert normalize_lease_id(text) == want
