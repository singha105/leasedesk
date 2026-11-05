from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from leasedesk.keymaster.ledger import LedgerRow, render_table

from .conftest import T0


def row(i: int, state: str = "EXPIRED") -> LedgerRow:
    return LedgerRow(
        lease=f"LS-{i:04d}",
        requester="Arnab",
        access="s3:GetObject on billing",
        risk="LOW",
        approved_by="Priya",
        start=str(T0 + timedelta(minutes=i)),
        expiry="-",
        state=state,
    )


def test_table_header_and_order() -> None:
    table = render_table([row(2, "ACTIVE"), row(1)])
    lines = table.splitlines()
    assert (
        lines[0] == "| Lease | Requester | Access | Risk | Approved by | Start | Expiry | State |"
    )
    assert lines[2].startswith("| LS-0002 |") and lines[3].startswith("| LS-0001 |")


def test_ledger_keeps_100_rows_and_summarises() -> None:
    rows = [row(i, "EXPIRED" if i % 2 else "DENIED") for i in range(130, 0, -1)]
    table = render_table(rows, keep=100)
    body = [ln for ln in table.splitlines() if ln.startswith("| LS-")]
    assert len(body) == 100 and body[0].startswith("| LS-0130 |")
    assert "30 older leases" in table and "15 DENIED" in table and "15 EXPIRED" in table


def test_pipes_in_cells_escaped() -> None:
    r = row(1)
    r.requester = "a|b"
    assert "a\\|b" in render_table([r])


async def test_jsonl_mirror(tmp_path: Path) -> None:
    from leasedesk.keymaster.ledger import append_jsonl

    append_jsonl(tmp_path / "logs" / "ledger.jsonl", row(1))
    append_jsonl(tmp_path / "logs" / "ledger.jsonl", row(2))
    lines = (tmp_path / "logs" / "ledger.jsonl").read_text().splitlines()
    assert [json.loads(x)["lease"] for x in lines] == ["LS-0001", "LS-0002"]
