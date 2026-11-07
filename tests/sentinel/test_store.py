"""The review store keeps every file it writes at mode 600, WAL and shared-memory files too."""

from __future__ import annotations

import stat
from datetime import UTC, datetime
from pathlib import Path

from leasedesk.sentinel.review import review
from leasedesk.sentinel.store import ReviewStore

from .conftest import make_ctx, raw


async def test_database_and_its_wal_files_are_private(tmp_path: Path) -> None:
    path = tmp_path / "state" / "sentinel.db"
    store = ReviewStore(path, clock=lambda: datetime(2026, 11, 6, tzinfo=UTC))
    store.save("LS-0001", await review(raw(), make_ctx()), lease_id="LS-0001", gating=True)
    files = [p for p in path.parent.iterdir() if p.name.startswith("sentinel.db")]
    assert {p.name for p in files} >= {"sentinel.db", "sentinel.db-wal", "sentinel.db-shm"}
    assert {p.name: oct(stat.S_IMODE(p.stat().st_mode)) for p in files} == {
        p.name: "0o600" for p in files
    }
    store.close()
