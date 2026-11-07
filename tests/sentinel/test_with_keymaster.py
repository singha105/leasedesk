"""keymaster and sentinel in one process: the review request and the verdict, end to end."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from leasedesk.keymaster.store import LeaseState

from ..keymaster.conftest import ARNAB, KEYMASTER, SENTINEL, Clock
from ..keymaster.harness import Harness
from .conftest import BUCKET, stmt
from .harness import SentinelHarness

S = LeaseState


@pytest.mark.parametrize(
    ("statements", "state"),
    [
        (None, S.AWAITING_APPROVAL),
        ([stmt("All", ["s3:*"], [BUCKET])], S.ESCALATED),
        ([stmt("Pol", ["s3:PutBucketPolicy"], [BUCKET])], S.BLOCKED),
    ],
)
async def test_sentinel_verdict_moves_the_lease(
    tmp_path: Path, clock: Clock, statements: list[dict[str, Any]] | None, state: LeaseState
) -> None:
    km = Harness(tmp_path / "km", clock)
    await km.start()
    over: dict[str, Any] = {"duration_minutes": 60}
    if statements:
        over["statements"] = statements
    lease_id = await km.submit(**over)
    await km.say(ARNAB, f"@keymaster confirm {lease_id}")
    thread = km.lease(lease_id).thread_id
    request = next(t for _, _, t in km.km.sent if "```review" in t)

    se = SentinelHarness(tmp_path / "se", clock)
    await se.start()
    await se.say(KEYMASTER, request, name="keymaster", thread_id=thread)
    reply = se.sentinel.texts()[0]
    await km.say(SENTINEL, reply, name="sentinel", thread_id=thread)
    assert km.state(lease_id) is state
