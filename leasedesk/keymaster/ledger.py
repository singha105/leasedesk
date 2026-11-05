"""The lease-ledger room document: a markdown table, newest first, rewritten on every change.

Switch's update_room_document replaces the whole content, so the table is rebuilt from SQLite
each time. The newest 100 rows are shown; older ones are summarised. Every row written is also
appended to logs/ledger.jsonl.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path

from pydantic import BaseModel

from leasedesk.common import format as fmt
from leasedesk.common.logging import get_logger
from leasedesk.keymaster.store import Lease, LeaseStore
from leasedesk.switch.errors import SwitchError

log = get_logger(__name__)
HEADER = "| Lease | Requester | Access | Risk | Approved by | Start | Expiry | State |"
RULE = "| - | - | - | - | - | - | - | - |"
NAME = "lease-ledger"
DESCRIPTION = "Every LeaseDesk lease in this room, newest first. Written by keymaster."
INSTRUCTIONS = "Read-only record. keymaster rewrites it on every change; times are ET (UTC)."


class LedgerRow(BaseModel):
    lease: str
    requester: str
    access: str
    risk: str
    approved_by: str
    start: str
    expiry: str
    state: str


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def row_for(lease: Lease, tz: str) -> LedgerRow:
    return LedgerRow(
        lease=lease.id,
        requester=lease.requester_name or lease.requester_hint or "-",
        access=fmt.describe_access(lease.proposal.statements),
        risk=lease.risk or "-",
        approved_by=lease.approved_by or "-",
        start=fmt.fmt_time(lease.starts_at, tz) if lease.starts_at else "-",
        expiry=fmt.fmt_time(lease.expires_at, tz) if lease.expires_at else "-",
        state=lease.state.value,
    )


def render_table(rows: list[LedgerRow], keep: int = 100) -> str:
    lines = [HEADER, RULE]
    for r in rows[:keep]:
        cells = [r.lease, r.requester, r.access, r.risk, r.approved_by, r.start, r.expiry, r.state]
        lines.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    older = rows[keep:]
    if older:
        counts = Counter(r.state for r in older)
        summary = ", ".join(f"{n} {state}" for state, n in sorted(counts.items()))
        lines.append("")
        lines.append(f"{len(older)} older leases not shown ({summary}); see logs/ledger.jsonl.")
    return "\n".join(lines)


def append_jsonl(path: Path, row: LedgerRow) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row.model_dump()) + "\n")


class Ledger:
    def __init__(
        self,
        store: LeaseStore,
        writer: Callable[[str, str], Awaitable[None]],
        jsonl: Path,
        tz: str,
    ) -> None:
        self.store = store
        self.writer = writer
        self.jsonl = jsonl
        self.tz = tz

    async def record(self, lease: Lease) -> None:
        append_jsonl(self.jsonl, row_for(lease, self.tz))
        rows = [
            row_for(x, self.tz) for x in self.store.recent(limit=5000) if x.room_id == lease.room_id
        ]
        try:
            await self.writer(lease.room_id, render_table(rows))
        except SwitchError as exc:
            log.error(
                "ledger_write_failed", room=lease.room_id, status=exc.status, detail=exc.detail
            )
