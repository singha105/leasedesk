"""The requester-confirm flow when scribe drafts for someone (Part 4, step 1).

Already covered elsewhere: confirm binds the requester (test_confirm_binds_requester), an unlisted
person can't confirm (test_unmapped_confirm_refused), the review request goes by role in the
submit thread (test_full_low_flow) and an unconfirmed lease is cancelled after 15 minutes
(test_scheduler.test_confirm_timeout_cancels).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from leasedesk.common.format import drafted_for
from leasedesk.keymaster.store import LeaseState
from leasedesk.switch.models import Participant

from .conftest import ARNAB, PRIYA, SCRIBE, Clock
from .harness import DESK, Harness

S = LeaseState
SKEPTIC = "@switch-agent-cccc0000:localhost"


@pytest.fixture
async def h(tmp_path: Path, clock: Clock) -> Harness:
    harness = Harness(tmp_path, clock)
    await harness.start()
    return harness


async def test_agent_submit_waits_for_named_confirm(h: Harness) -> None:
    lease_id = await h.submit(sender=SCRIBE, requester_hint="Arnab")
    assert h.state(lease_id) is S.AWAITING_CONFIRM
    assert (
        f"Arnab, if this is your request, reply: @keymaster confirm {lease_id}" in h.km.texts()[0]
    )


@pytest.mark.parametrize(("sender", "name"), [(SCRIBE, "scribe"), (SKEPTIC, "skeptic")])
async def test_agent_confirm_refused(h: Harness, sender: str, name: str) -> None:
    # skeptic is a Console-run agent keymaster learns from list_participants (type "agent").
    h.km.participants[DESK] = [Participant(id=SKEPTIC, name="skeptic", type="agent")]
    h.km.agent_names |= {"skeptic"}
    lease_id = await h.submit(requester_hint="Arnab")
    await h.say(sender, f"@keymaster confirm {lease_id}", name=name)
    assert h.state(lease_id) is S.AWAITING_CONFIRM
    # Answered by name: the agent wakes only when addressed.
    assert h.km.last() == f"@{name} Only the person who needs the access can confirm a lease."


async def test_confirmer_other_than_hint_named_in_approval(h: Harness) -> None:
    lease_id = await h.submit(requester_hint="Arnab")
    await h.say(PRIYA, f"@keymaster confirm {lease_id}")
    await h.verdict(lease_id, "LOW")
    assert h.state(lease_id) is S.AWAITING_APPROVAL
    assert "Who: Priya -> IAM user lease-demo-dev (drafted for Arnab)" in h.km.last()


async def test_confirmer_matching_hint_not_annotated(h: Harness) -> None:
    lease_id = await h.submit(requester_hint="arnab.")
    h.km.participants[DESK] = [Participant(id=ARNAB, name="arnab.", type="user")]
    await h.say(ARNAB, f"@keymaster confirm {lease_id}")
    await h.verdict(lease_id, "LOW")
    assert "Who: Arnab -> IAM user lease-demo-dev\n" in h.km.last()
    assert "drafted for" not in h.km.last()


@pytest.mark.parametrize(
    ("hint", "names", "want"),
    [
        ("", ["Arnab"], None),
        ("Arnab", ["Arnab"], None),
        ("ARNAB", ["Arnab"], None),
        ("Arnab Singh", ["Arnab"], None),
        ("arnab.", ["Arnab", "arnab."], None),
        ("Priya", ["Arnab", "arnab."], "Priya"),
        ("the `CTO` @keymaster", ["Arnab"], "the 'CTO' (at)keymaster"),
    ],
)
def test_drafted_for(hint: str, names: list[str], want: str | None) -> None:
    assert drafted_for(hint, *names) == want
