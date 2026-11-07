"""sentinel's SQLite store (``.state/sentinel.db``): every review it answered, for ``explain``.

Append-only; ``get`` returns the newest gating review for an id, else the newest advisory one,
so a forged request that reuses a real lease id can't replace what ``explain`` shows for it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from leasedesk.common.schemas import Finding, Risk
from leasedesk.common.sqlite import open_private
from leasedesk.sentinel.review import Review

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id TEXT NOT NULL,
    lease_id TEXT,
    gating INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reviews_by_id ON reviews (review_id COLLATE NOCASE);
"""


class StoredReview(BaseModel):
    review_id: str
    lease_id: str | None
    gating: bool
    created_at: datetime | None
    risk: Risk
    score: int
    raw_score: int
    arithmetic: str
    findings: list[Finding]
    aws_validation: str
    aws_note: str
    proposal_hash: str | None
    corrected: dict[str, Any] | None
    corrected_risk: Risk | None
    elapsed_ms: float

    @classmethod
    def of(
        cls,
        review_id: str,
        r: Review,
        *,
        lease_id: str | None,
        gating: bool,
        at: datetime | None,
    ) -> StoredReview:
        return cls(
            review_id=review_id,
            lease_id=lease_id,
            gating=gating,
            created_at=at,
            risk=r.scored.risk,
            score=r.scored.score,
            raw_score=r.scored.raw,
            arithmetic=r.scored.arithmetic,
            findings=r.findings,
            aws_validation=r.aws_validation,
            aws_note=r.aws_note,
            proposal_hash=r.proposal_hash,
            corrected=r.corrected,
            corrected_risk=r.corrected_risk,
            elapsed_ms=r.elapsed_ms,
        )


class ReviewStore:
    def __init__(self, path: Path, clock: Callable[[], datetime]) -> None:
        self.path = path
        self.clock = clock
        self._conn = open_private(path)
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def save(
        self, review_id: str, r: Review, *, lease_id: str | None, gating: bool
    ) -> StoredReview:
        stored = StoredReview.of(review_id, r, lease_id=lease_id, gating=gating, at=self.clock())
        self._conn.execute(
            "INSERT INTO reviews (review_id, lease_id, gating, created_at, body) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                review_id,
                lease_id,
                int(gating),
                stored.created_at.isoformat() if stored.created_at else "",
                stored.model_dump_json(),
            ),
        )
        return stored

    def get(self, review_id: str) -> StoredReview | None:
        row = self._conn.execute(
            "SELECT body FROM reviews WHERE review_id = ? COLLATE NOCASE "
            "ORDER BY gating DESC, seq DESC LIMIT 1",
            (review_id,),
        ).fetchone()
        return StoredReview.model_validate_json(row[0]) if row else None
