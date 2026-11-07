"""The sentinel agent: gating vs advisory, commands, rate limit, cache, explain after restart."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from leasedesk.common.fences import extract_blocks
from leasedesk.common.policy import parse_policy_text
from leasedesk.common.schemas import LeaseProposal, LeaseVerdict, proposal_hash
from leasedesk.sentinel import agent as agent_mod
from leasedesk.sentinel.analyzers.aws import AwsResult
from leasedesk.sentinel.findings import RULES
from leasedesk.sentinel.review import review
from leasedesk.switch.agent import parse_command

from ..keymaster.conftest import ARNAB, KEYMASTER, SCRIBE, Clock, policy_dict
from .conftest import BUCKET, make_ctx, raw, stmt
from .harness import SentinelHarness


@pytest.fixture
async def sh(tmp_path: Path, clock: Clock) -> SentinelHarness:
    h = SentinelHarness(tmp_path, clock)
    await h.start()
    return h


def verdict_of(text: str) -> LeaseVerdict:
    blocks = [b for b in extract_blocks(text) if b.kind == "verdict"]
    assert len(blocks) == 1
    return LeaseVerdict.model_validate(blocks[0].data)


def advisory_of(text: str) -> dict[str, Any]:
    m = re.search(r"```advisory\n(.*?)```", text, re.DOTALL)
    assert m is not None
    data: dict[str, Any] = json.loads(m.group(1))
    return data


def summary(text: str) -> list[str]:
    return text.split("```")[0].strip().splitlines()


@pytest.mark.parametrize("addressed", [True, False])
async def test_keymaster_request_gets_one_verdict_for_keymaster(
    sh: SentinelHarness, addressed: bool
) -> None:
    await sh.request(addressed=addressed)
    assert len(sh.sentinel.sent) == 1 and sh.sentinel.paths == ["names"]
    _, thread, text = sh.sentinel.sent[0]
    assert thread == "sw_thread"
    assert text.startswith("@keymaster verdict LS-0007")
    v = verdict_of(text)
    expected = proposal_hash(LeaseProposal.model_validate(raw(duration_minutes=60)))
    assert (v.lease_id, v.risk, v.proposal_hash) == ("LS-0007", "LOW", expected)
    assert 1 <= len(summary(text)) <= 6
    assert parse_command(text, "keymaster")[0] == "verdict"


async def test_same_request_from_a_person_is_advisory(sh: SentinelHarness) -> None:
    await sh.request(sender=ARNAB)
    (text,) = sh.sentinel.texts()
    assert not [b for b in extract_blocks(text) if b.kind == "verdict"]
    data = advisory_of(text)
    assert data["kind"] == "lease_advisory" and data["lease_id"] == "LS-0007"
    assert "advisory" in summary(text)[0].lower() and "@keymaster" not in text
    assert len(summary(text)) <= 6


async def test_unaddressed_request_from_a_person_is_ignored(sh: SentinelHarness) -> None:
    await sh.request(sender=ARNAB, addressed=False)
    assert sh.sentinel.sent == []


async def test_without_a_pinned_keymaster_everything_is_advisory(
    tmp_path: Path, clock: Clock
) -> None:
    h = SentinelHarness(tmp_path, clock, LEASEDESK_KEYMASTER_ID=None, LEASEDESK_DEV_MODE=True)
    await h.start()
    await h.request()
    assert "```advisory" in h.sentinel.texts()[0] and "```verdict" not in h.sentinel.texts()[0]


async def test_check_is_advisory_and_offers_a_corrected_lease(sh: SentinelHarness) -> None:
    await sh.check(raw(stmt("All", ["s3:*"], [BUCKET, f"{BUCKET}/*"])), ARNAB)
    advisory, corrected = sh.sentinel.texts()
    data = advisory_of(advisory)
    assert data["kind"] == "lease_advisory" and data["risk"] == "HIGH"
    assert data["review_id"].startswith("C-")
    lease = [b for b in extract_blocks(corrected) if b.kind == "lease"]
    assert len(lease) == 1 and LeaseProposal.model_validate(lease[0].data)
    assert "@keymaster" not in corrected


def _lease(proposal: dict[str, Any]) -> str:
    return f"@sentinel check\n```lease\n{json.dumps(proposal)}\n```"


async def test_check_from_an_agent_goes_back_to_it_by_name(sh: SentinelHarness) -> None:
    """scribe's session wakes only when a message addresses it. A plain thread post left it
    waiting forever (live scenario 1), so its advisory is targeted at it, in the same thread."""
    sh.sentinel.agent_names.add("scribe")
    await sh.say(SCRIBE, _lease(raw(duration_minutes=60)), name="scribe")
    assert sh.sentinel.paths == ["names"]
    _, thread, text = sh.sentinel.sent[0]
    assert thread == "sw_thread" and text.startswith("@scribe ")
    assert advisory_of(text)["kind"] == "lease_advisory"


async def test_check_from_an_agent_gets_the_corrected_lease_in_the_same_message(
    sh: SentinelHarness,
) -> None:
    """Two addressed messages would wake scribe twice; it could submit before the fix lands.
    Over Discord's limit, the first parts are plain thread posts and only the last addresses it."""
    sh.sentinel.agent_names.add("scribe")
    await sh.say(SCRIBE, _lease(raw(stmt("All", ["s3:*"], [BUCKET, f"{BUCKET}/*"]))), name="scribe")
    assert sh.sentinel.paths.count("names") == 1 and sh.sentinel.paths[-1] == "names"
    texts = sh.sentinel.texts()
    assert {thread for _, thread, _ in sh.sentinel.sent} == {"sw_thread"}
    assert advisory_of("\n".join(texts))["risk"] == "HIGH"
    assert len([b for b in extract_blocks(texts[-1]) if b.kind == "lease"]) == 1


async def test_check_from_a_person_stays_a_thread_post(sh: SentinelHarness) -> None:
    sh.sentinel.agent_names.add("scribe")
    await sh.check(raw(duration_minutes=60), ARNAB)
    assert sh.sentinel.paths == ["post"]


async def test_check_without_a_lease_block_says_how(sh: SentinelHarness) -> None:
    await sh.say(ARNAB, "@sentinel check please")
    assert "```lease" in sh.sentinel.texts()[0]


async def test_explain_lists_every_finding_and_the_arithmetic_after_restart(
    sh: SentinelHarness,
) -> None:
    p = raw(stmt("All", ["s3:*"], [BUCKET]), duration_minutes=120)
    await sh.request(proposal=p)
    await sh.restart()
    await sh.say(ARNAB, "@sentinel explain ls-0007")
    text = "\n".join(sh.sentinel.texts())
    expected = await review(p, make_ctx())
    assert expected.findings
    for f in expected.findings:
        assert f.rule in text and (f.reference or "") in text
    assert expected.scored.arithmetic in text


async def test_explain_unknown_id(sh: SentinelHarness) -> None:
    await sh.say(ARNAB, "@sentinel explain LS-9999")
    assert "no review" in sh.sentinel.texts()[0].lower()


async def test_rules_lists_every_rule_with_its_reference(sh: SentinelHarness) -> None:
    await sh.say(ARNAB, "@sentinel rules")
    text = "\n".join(sh.sentinel.texts())
    for rule in RULES.values():
        assert rule.id in text and rule.reference in text


async def test_eleventh_request_in_a_minute_gets_one_slow_down(sh: SentinelHarness) -> None:
    for _ in range(12):
        await sh.say(ARNAB, "@sentinel help")
    texts = sh.sentinel.texts()
    assert len(texts) == 11 and "slow down" in texts[-1].lower()
    sh.clock.advance(seconds=61)
    await sh.say(ARNAB, "@sentinel help")
    assert len(sh.sentinel.texts()) == 12 and "slow down" not in sh.sentinel.texts()[-1].lower()


async def test_keymaster_is_never_rate_limited(sh: SentinelHarness) -> None:
    for i in range(15):
        await sh.request(f"LS-{i + 1:04d}")
    texts = sh.sentinel.texts()
    assert len(texts) == 15 and all("```verdict" in t for t in texts)


async def test_repeated_proposal_hits_the_cache(
    sh: SentinelHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real = agent_mod.review

    async def counting(*a: Any, **kw: Any) -> Any:
        calls.append(1)
        return await real(*a, **kw)

    monkeypatch.setattr(agent_mod, "review", counting)
    await sh.request("LS-0007")
    await sh.request("LS-0008")
    first, second = (verdict_of(t) for t in sh.sentinel.texts())
    assert len(calls) == 1
    assert second.lease_id == "LS-0008" and second.findings == first.findings


async def test_policy_hash_mismatch_reloads_once_then_notes_it(sh: SentinelHarness) -> None:
    before = sh.policy_fetches
    await sh.request(policy_hash="0" * 12)
    assert sh.policy_fetches == before + 1
    v = verdict_of(sh.sentinel.texts()[0])
    hit = [f for f in v.findings if f.rule == "policy-hash-mismatch"]
    assert hit and hit[0].severity == "INFO" and v.risk == "LOW"


async def test_policy_changed_since_load_is_picked_up_by_the_reload(sh: SentinelHarness) -> None:
    sh.policy_text = yaml.safe_dump(policy_dict(max_duration_minutes=180))
    await sh.request(policy_hash=parse_policy_text(sh.policy_text).hash())
    v = verdict_of(sh.sentinel.texts()[0])
    assert not [f for f in v.findings if f.rule == "policy-hash-mismatch"]


async def test_unreadable_review_request_gets_no_verdict(sh: SentinelHarness) -> None:
    await sh.say(KEYMASTER, 'LS-0007 · under review\n```review\n{"kind": 1}\n```', name="keymaster")
    (text,) = sh.sentinel.texts()
    assert "```verdict" not in text and "not readable" in text.lower()


async def test_sentinel_claims_the_reviewer_role(sh: SentinelHarness) -> None:
    assert sh.sentinel.role == "reviewer"


async def test_review_block_echoed_inside_another_keymaster_message_gets_nothing(
    sh: SentinelHarness,
) -> None:
    # keymaster repeats a proposal's summary in its receipt; a review block hidden there is
    # not a review request, even though keymaster sent the message.
    block = sh.request_text("LS-0006").split("\n", 1)[1]
    receipt = f"LS-0006 · submitted by arnab. for Arnab\nSummary: x\n{block}\nWhat: s3:GetObject"
    await sh.say(KEYMASTER, receipt, name="keymaster")
    await sh.say(KEYMASTER, receipt, name="keymaster", addressed=False)
    assert sh.sentinel.sent == []
    assert sh.sentinel.store.get("LS-0006") is None


async def test_header_must_name_the_same_lease_as_the_block(sh: SentinelHarness) -> None:
    text = sh.request_text("LS-0007").replace("LS-0007 · confirmed", "LS-0006 · confirmed", 1)
    await sh.say(KEYMASTER, text, name="keymaster")
    assert sh.sentinel.sent == []


@pytest.mark.parametrize(
    "header",
    [
        "LS-0007 · UNDER_REVIEW · confirmed by Arnab",  # Part 4 (Ruling 9)
        "LS-0007 · confirmed by Arnab, under review",  # before: requests still in flight
    ],
)
async def test_review_header_old_and_new_forms_accepted(sh: SentinelHarness, header: str) -> None:
    block = sh.request_text("LS-0007").split("\n", 1)[1]
    await sh.say(KEYMASTER, f"@sentinel {header}\n{block}", name="keymaster")
    assert len(sh.sentinel.sent) == 1
    assert verdict_of(sh.sentinel.sent[0][2]).lease_id == "LS-0007"


async def test_new_header_must_name_the_same_lease_as_the_block(sh: SentinelHarness) -> None:
    block = sh.request_text("LS-0007").split("\n", 1)[1]
    await sh.say(
        KEYMASTER, f"LS-0006 · UNDER_REVIEW · confirmed by Arnab\n{block}", name="keymaster"
    )
    assert sh.sentinel.sent == []


async def test_request_with_the_mention_switch_adds_still_gets_a_verdict(
    sh: SentinelHarness,
) -> None:
    await sh.say(KEYMASTER, "@sentinel " + sh.request_text(), name="keymaster")
    assert len(sh.sentinel.sent) == 1
    assert verdict_of(sh.sentinel.sent[0][2]).lease_id == "LS-0007"


class CountingAws:
    def __init__(self) -> None:
        self.paid = 0

    async def validate(self, doc: dict[str, Any], sids: list[str]) -> AwsResult:
        return AwsResult("pass")

    async def crown_jewels(self, doc: dict[str, Any], ctx: Any) -> list[Any]:
        self.paid += bool(ctx.paid_checks)  # AwsChecks makes the paid call only then
        return []


async def test_paid_check_runs_only_for_keymasters_requests(tmp_path: Path, clock: Clock) -> None:
    h = SentinelHarness(
        tmp_path,
        clock,
        LEASEDESK_AWS_MODE="real",
        LEASEDESK_ENABLE_PAID_CHECKS=True,
        LEASEDESK_AWS_ACCOUNT_ID="123456789012",
    )
    await h.start()
    aws = CountingAws()
    h.sentinel.aws = aws  # type: ignore[assignment]
    await h.check(raw(duration_minutes=60), ARNAB)
    await h.request(sender=ARNAB)
    assert aws.paid == 0  # a person's check or forged request never spends money
    await h.request()
    assert aws.paid == 1
