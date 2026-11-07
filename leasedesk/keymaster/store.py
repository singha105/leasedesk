"""keymaster's SQLite store (WAL): leases, approvals and an append-only event log.

Every state change goes through ``transition()``, which checks the SPEC state graph and writes
the event in the same transaction. The events table rejects UPDATE and DELETE with triggers.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Collection
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from leasedesk.common.ids import ensure_sequence_table, next_lease_id
from leasedesk.common.schemas import LeaseProposal
from leasedesk.common.sqlite import open_private


class LeaseState(StrEnum):
    DRAFTED = "DRAFTED"
    AWAITING_CONFIRM = "AWAITING_CONFIRM"
    UNDER_REVIEW = "UNDER_REVIEW"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    ESCALATED = "ESCALATED"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"
    DENIED = "DENIED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


S = LeaseState
TRANSITIONS: dict[LeaseState, frozenset[LeaseState]] = {
    S.DRAFTED: frozenset({S.AWAITING_CONFIRM, S.CANCELLED}),
    S.AWAITING_CONFIRM: frozenset({S.UNDER_REVIEW, S.CANCELLED}),
    S.UNDER_REVIEW: frozenset({S.AWAITING_APPROVAL, S.ESCALATED, S.BLOCKED, S.CANCELLED}),
    S.AWAITING_APPROVAL: frozenset({S.ACTIVE, S.DENIED, S.CANCELLED, S.FAILED}),
    S.ESCALATED: frozenset({S.ACTIVE, S.DENIED, S.CANCELLED, S.FAILED}),
    S.ACTIVE: frozenset({S.EXPIRED, S.REVOKED, S.FAILED}),
    S.EXPIRED: frozenset(),
    S.REVOKED: frozenset(),
    S.DENIED: frozenset(),
    S.BLOCKED: frozenset(),
    S.FAILED: frozenset(),
    S.CANCELLED: frozenset(),
}
PENDING = frozenset({S.AWAITING_CONFIRM, S.UNDER_REVIEW, S.AWAITING_APPROVAL, S.ESCALATED})


class IllegalTransition(Exception):
    pass


class StaleState(Exception):
    """The lease is no longer in a state the caller expected (someone else moved it)."""


class UnknownLease(KeyError):
    pass


class Lease(BaseModel):
    id: str
    state: LeaseState
    submitted_by: str
    submitted_by_name: str | None = None
    requester_hint: str = ""
    requester: str | None = None
    requester_name: str | None = None
    iam_user: str | None = None
    proposal: LeaseProposal
    final_policy_json: str | None = None
    risk: str | None = None
    score: int | None = None
    verdict_json: str | None = None
    room_id: str
    thread_id: str | None = None
    approval_room_id: str | None = None
    created_at: datetime
    review_requested_at: datetime | None = None
    approval_requested_at: datetime | None = None
    starts_at: datetime | None = None
    expires_at: datetime | None = None
    round: int = 1
    pending_extension_minutes: int | None = None
    reminded: bool = False
    approved_by: str | None = None


class Approval(BaseModel):
    lease_id: str
    round: int
    approver_id: str
    approver_name: str
    decision: str
    at: datetime
    room_id: str
    message_id: str


class LeaseEvent(BaseModel):
    id: int
    lease_id: str
    at: datetime
    kind: str
    from_state: LeaseState | None
    to_state: LeaseState | None
    reason: str
    actor: str | None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS leases (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    submitted_by_name TEXT,
    requester_hint TEXT NOT NULL DEFAULT '',
    requester TEXT,
    requester_name TEXT,
    iam_user TEXT,
    proposal_json TEXT NOT NULL,
    final_policy_json TEXT,
    risk TEXT,
    score INTEGER,
    verdict_json TEXT,
    room_id TEXT NOT NULL,
    thread_id TEXT,
    approval_room_id TEXT,
    created_at TEXT NOT NULL,
    review_requested_at TEXT,
    approval_requested_at TEXT,
    starts_at TEXT,
    expires_at TEXT,
    round INTEGER NOT NULL DEFAULT 1,
    pending_extension_minutes INTEGER,
    reminded INTEGER NOT NULL DEFAULT 0,
    approved_by TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
    lease_id TEXT NOT NULL REFERENCES leases(id),
    round INTEGER NOT NULL,
    approver_id TEXT NOT NULL,
    approver_name TEXT NOT NULL,
    decision TEXT NOT NULL,
    at TEXT NOT NULL,
    room_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    UNIQUE (lease_id, round, approver_id)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lease_id TEXT NOT NULL,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    reason TEXT NOT NULL,
    actor TEXT
);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TABLE IF NOT EXISTS flags (name TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
_DATETIME_COLS = {
    "created_at",
    "review_requested_at",
    "approval_requested_at",
    "starts_at",
    "expires_at",
}
_UPDATABLE = {
    "requester",
    "requester_name",
    "iam_user",
    "final_policy_json",
    "risk",
    "score",
    "verdict_json",
    "thread_id",
    "approval_room_id",
    "review_requested_at",
    "approval_requested_at",
    "starts_at",
    "expires_at",
    "round",
    "pending_extension_minutes",
    "reminded",
    "approved_by",
}


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class LeaseStore:
    def __init__(self, path: Path, clock: Callable[[], datetime]) -> None:
        self.path = path
        self.clock = clock
        self._conn = open_private(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        ensure_sequence_table(self._conn)

    def close(self) -> None:
        self._conn.close()

    # Reads ------------------------------------------------------------------------------

    def _lease(self, row: sqlite3.Row) -> Lease:
        data: dict[str, Any] = dict(row)
        data["proposal"] = LeaseProposal.model_validate_json(data.pop("proposal_json"))
        data["reminded"] = bool(data["reminded"])
        for col in _DATETIME_COLS:
            data[col] = _dt(data[col])
        return Lease.model_validate(data)

    def get(self, lease_id: str) -> Lease | None:
        row = self._conn.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone()
        return self._lease(row) if row else None

    def require(self, lease_id: str) -> Lease:
        lease = self.get(lease_id)
        if lease is None:
            raise UnknownLease(lease_id)
        return lease

    def in_states(self, *states: LeaseState) -> list[Lease]:
        marks = ",".join("?" for _ in states)
        rows = self._conn.execute(
            f"SELECT * FROM leases WHERE state IN ({marks}) ORDER BY id",  # noqa: S608
            [s.value for s in states],
        ).fetchall()
        return [self._lease(r) for r in rows]

    def recent(self, limit: int = 100) -> list[Lease]:
        rows = self._conn.execute(
            "SELECT * FROM leases ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._lease(r) for r in rows]

    def approvals(self, lease_id: str, round: int) -> list[Approval]:
        rows = self._conn.execute(
            "SELECT * FROM approvals WHERE lease_id = ? AND round = ? ORDER BY at",
            (lease_id, round),
        ).fetchall()
        return [Approval.model_validate({**dict(r), "at": _dt(r["at"])}) for r in rows]

    def events(self, lease_id: str) -> list[LeaseEvent]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE lease_id = ? ORDER BY id", (lease_id,)
        ).fetchall()
        return [LeaseEvent.model_validate({**dict(r), "at": _dt(r["at"])}) for r in rows]

    # Writes -----------------------------------------------------------------------------

    def _event(
        self,
        lease_id: str,
        kind: str,
        reason: str,
        *,
        from_state: LeaseState | None = None,
        to_state: LeaseState | None = None,
        actor: str | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO events (lease_id, at, kind, from_state, to_state, reason, actor) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                lease_id,
                _iso(self.clock()),
                kind,
                from_state.value if from_state else None,
                to_state.value if to_state else None,
                reason,
                actor,
            ),
        )

    def create_lease(
        self,
        proposal: LeaseProposal,
        *,
        submitted_by: str,
        submitted_by_name: str | None,
        room_id: str,
        thread_id: str | None = None,
    ) -> Lease:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            lease_id = next_lease_id(self._conn)
            self._conn.execute(
                "INSERT INTO leases (id, state, submitted_by, submitted_by_name, requester_hint, "
                "proposal_json, room_id, thread_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    lease_id,
                    S.DRAFTED.value,
                    submitted_by,
                    submitted_by_name,
                    proposal.requester_hint,
                    proposal.model_dump_json(),
                    room_id,
                    thread_id,
                    _iso(self.clock()),
                ),
            )
            self._event(lease_id, "state", "submitted", to_state=S.DRAFTED, actor=submitted_by)
            self._conn.execute(
                "UPDATE leases SET state = ? WHERE id = ?", (S.AWAITING_CONFIRM.value, lease_id)
            )
            self._event(
                lease_id,
                "state",
                "valid proposal; waiting for the requester to confirm",
                from_state=S.DRAFTED,
                to_state=S.AWAITING_CONFIRM,
                actor=submitted_by,
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return self.require(lease_id)

    def _set_clause(self, fields: dict[str, Any]) -> tuple[str, list[Any]]:
        bad = set(fields) - _UPDATABLE
        if bad:
            raise ValueError(f"not updatable outside transition(): {sorted(bad)}")
        cols, vals = [], []
        for k, v in fields.items():
            cols.append(f"{k} = ?")
            if isinstance(v, datetime):
                v = _iso(v)
            elif isinstance(v, bool):
                v = int(v)
            vals.append(v)
        return ", ".join(cols), vals

    def transition(
        self,
        lease_id: str,
        from_states: Collection[LeaseState],
        to_state: LeaseState,
        reason: str,
        *,
        actor: str | None = None,
        **fields: Any,
    ) -> Lease:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT state FROM leases WHERE id = ?", (lease_id,)
            ).fetchone()
            if row is None:
                raise UnknownLease(lease_id)
            current = LeaseState(row["state"])
            if to_state not in TRANSITIONS[current]:
                raise IllegalTransition(f"{lease_id}: {current} -> {to_state} is not allowed")
            if current not in from_states:
                raise StaleState(f"{lease_id} is {current}, expected one of {sorted(from_states)}")
            sets, vals = self._set_clause(fields) if fields else ("", [])
            sql = "UPDATE leases SET state = ?" + (f", {sets}" if sets else "") + " WHERE id = ?"  # noqa: S608 - column names come from _UPDATABLE
            self._conn.execute(sql, [to_state.value, *vals, lease_id])
            self._event(
                lease_id, "state", reason, from_state=current, to_state=to_state, actor=actor
            )
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        return self.require(lease_id)

    def update(self, lease_id: str, **fields: Any) -> Lease:
        if "state" in fields:
            raise ValueError("change state with transition(), never update()")
        sets, vals = self._set_clause(fields)
        self._conn.execute(f"UPDATE leases SET {sets} WHERE id = ?", [*vals, lease_id])  # noqa: S608
        return self.require(lease_id)

    def record_event(
        self, lease_id: str, kind: str, reason: str, *, actor: str | None = None
    ) -> None:
        self._event(lease_id, kind, reason, actor=actor)

    def add_approval(
        self,
        lease_id: str,
        *,
        round: int,
        approver_id: str,
        approver_name: str,
        decision: str,
        room_id: str,
        message_id: str,
    ) -> bool:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO approvals (lease_id, round, approver_id, approver_name, "
            "decision, at, room_id, message_id) VALUES (?,?,?,?,?,?,?,?)",
            (
                lease_id,
                round,
                approver_id,
                approver_name,
                decision,
                _iso(self.clock()),
                room_id,
                message_id,
            ),
        )
        if cur.rowcount == 1:
            self._event(
                lease_id,
                decision,
                f"{decision} by {approver_name} (round {round})",
                actor=approver_id,
            )
            return True
        return False

    def get_flag(self, name: str) -> bool:
        row = self._conn.execute("SELECT value FROM flags WHERE name = ?", (name,)).fetchone()
        return bool(row and json.loads(row["value"]))

    def set_flag(self, name: str, value: bool) -> None:
        self._conn.execute(
            "INSERT INTO flags(name, value) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            (name, json.dumps(value)),
        )
