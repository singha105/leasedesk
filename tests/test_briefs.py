"""The agent briefs in switch/briefs/ (Part 4, step 2): what they must say, and that scribe's
schema and example stay true to the code."""

from __future__ import annotations

import json
import re
from pathlib import Path

from leasedesk.common.schemas import AccessPolicy, LeaseProposal
from leasedesk.keymaster.rules import validate_proposal
from leasedesk.sentinel.review import review

from .keymaster.conftest import policy_dict
from .sentinel.conftest import make_ctx

BRIEFS = Path(__file__).resolve().parent.parent / "switch" / "briefs"


def fenced(text: str, tag: str) -> list[str]:
    return re.findall(rf"```{tag}\n(.*?)\n```", text, flags=re.S)


def scribe() -> str:
    return (BRIEFS / "scribe.md").read_text()


def skeptic() -> str:
    return (BRIEFS / "skeptic.md").read_text()


def test_scribe_schema_matches_model() -> None:
    (block,) = fenced(scribe(), "json")
    assert json.loads(block) == LeaseProposal.model_json_schema()


async def test_scribe_example_is_low() -> None:
    (block,) = fenced(scribe(), "lease")
    proposal = LeaseProposal.model_validate_json(block)
    policy = AccessPolicy.model_validate(policy_dict())
    assert validate_proposal(proposal, policy, dev_mode=False) == []
    r = await review(json.loads(block), make_ctx())
    assert r.scored.risk == "LOW", [f.rule for f in r.findings]
    assert proposal.duration_minutes == 60


def test_scribe_brief_says_what_the_brief_says() -> None:
    text = scribe()
    for must in (
        "resource-catalog",
        "at most two",
        "60 minutes",
        # Live scenario 1: a plain post never woke scribe again and a thread-less check lost the
        # person's thread, so both hand-offs are targeted messages in the person's thread.
        "targeted message to sentinel",
        "addressed to you",
        "one round",
        "targeted message to keymaster",
        "thread_id",
        "requester_hint",
        "ready for them to confirm",
        "Never write\n   @keymaster yourself",
        "submit once more",
        "aws:CurrentTime",
        "never approve",
        "data, not instructions",
        "8 lines",
        "s3:prefix",
    ):
        assert must in text, must
    assert "granted" in text  # "never say ... granted"


def test_skeptic_brief_says_what_the_brief_says() -> None:
    text = skeptic()
    for must in (
        "red-team reviewer",
        "at most 5 lines",
        "never post a verdict block",
        "never say 'approved'",
        "never ask for more access",
        "data, not instructions",
    ):
        assert must in text, must
    assert "```" not in text  # nothing in the brief that looks like a block to copy
