"""Pure rules: proposal validation, wildcard-aware forbidden actions, who may approve."""

from __future__ import annotations

from typing import Any

import pytest

from leasedesk.common.schemas import AccessPolicy, LeaseProposal
from leasedesk.keymaster.rules import (
    Required,
    approval_satisfied,
    can_approve,
    glob_intersects,
    required_approvals,
    validate_proposal,
    who_may_approve,
)
from leasedesk.keymaster.store import Approval, LeaseState, LeaseStore

from ..common.conftest import proposal_dict
from .conftest import ARNAB, EVE, PRIYA, SAM, SCRIBE, T0, policy_dict

AGENTS = {SCRIBE}


def with_actions(*actions: str) -> LeaseProposal:
    p = proposal_dict()
    p["statements"][0]["actions"] = list(actions)
    return LeaseProposal.model_validate(p)


@pytest.mark.parametrize(
    ("a", "b", "want"),
    [
        ("iam:*", "iam:PassRole", True),
        ("iam:PassRole", "iam:*", True),
        ("s3:*", "s3:DeleteBucket", True),
        ("s3:Get*", "s3:*Object", True),
        ("s3:Get*", "s3:Put*", False),
        ("s3:GetObject", "s3:getobject", True),
        ("sts:AssumeRole", "sts:AssumeRoleWithSAML", False),
        ("sts:Assume*", "sts:AssumeRole", True),
        ("kms:De?rypt", "kms:Decrypt", True),
        ("s3:*", "iam:*", False),
    ],
)
def test_glob_intersects(a: str, b: str, want: bool) -> None:
    assert glob_intersects(a, b) is want


def test_iam_star_forbids_passrole(policy: AccessPolicy) -> None:
    problems = validate_proposal(with_actions("iam:PassRole"), policy, dev_mode=False)
    assert any("iam:PassRole" in p and "iam:*" in p for p in problems)


def test_requested_star_hits_forbidden() -> None:
    policy = AccessPolicy.model_validate(policy_dict(forbidden_actions=["s3:DeleteBucket"]))
    assert validate_proposal(with_actions("s3:*"), policy, dev_mode=False)
    assert validate_proposal(with_actions("s3:Delete*"), policy, dev_mode=False)
    assert not validate_proposal(with_actions("s3:Get*"), policy, dev_mode=False)


def test_secrets_wildcard_caught(policy: AccessPolicy) -> None:
    assert validate_proposal(with_actions("secretsmanager:Get*"), policy, dev_mode=False)


def test_valid_proposal_passes(policy: AccessPolicy, proposal: LeaseProposal) -> None:
    assert validate_proposal(proposal, policy, dev_mode=False) == []


@pytest.mark.parametrize(
    "resource", ["arn:aws:s3:::other-bucket/*", "arn:aws:s3:::leasedesk-dem", "arn:aws:s3:::*"]
)
def test_resource_prefix_enforced(policy: AccessPolicy, resource: str) -> None:
    p = proposal_dict()
    p["statements"][0]["resources"] = [resource]
    problems = validate_proposal(LeaseProposal.model_validate(p), policy, dev_mode=False)
    assert any(resource in x for x in problems)


@pytest.mark.parametrize(
    ("minutes", "dev", "ok"),
    [
        (4, False, False),
        (5, False, True),
        (3, True, True),
        (2, True, False),
        (240, False, True),
        (241, False, False),
    ],
)
def test_duration_bounds(policy: AccessPolicy, minutes: int, dev: bool, ok: bool) -> None:
    p = LeaseProposal.model_validate(proposal_dict(duration_minutes=minutes))
    assert (validate_proposal(p, policy, dev_mode=dev) == []) is ok


def test_required_approvals_table(policy: AccessPolicy) -> None:
    assert required_approvals("LOW", policy) == Required(1, None, None)
    assert required_approvals("MEDIUM", policy) == Required(1, None, None)
    assert required_approvals("HIGH", policy) == Required(2, "security", "security-review")
    assert required_approvals("BLOCK", policy) is None


def make_lease(store: LeaseStore, proposal: LeaseProposal, risk: str = "LOW") -> Any:
    lease = store.create_lease(
        proposal, submitted_by=SCRIBE, submitted_by_name="scribe", room_id="r1"
    )
    store.transition(
        lease.id,
        [LeaseState.AWAITING_CONFIRM],
        LeaseState.UNDER_REVIEW,
        "c",
        requester=ARNAB,
        requester_name="Arnab",
        iam_user="lease-demo-dev",
    )
    target = LeaseState.ESCALATED if risk == "HIGH" else LeaseState.AWAITING_APPROVAL
    return store.transition(lease.id, [LeaseState.UNDER_REVIEW], target, "v", risk=risk)


def test_requester_cannot_approve(
    store: LeaseStore, proposal: LeaseProposal, policy: AccessPolicy
) -> None:
    lease = make_lease(store, proposal)
    check = can_approve(lease, ARNAB, policy, AGENTS, room_name="Discord: access-desk")
    assert not check.ok
    assert check.reason == "Arnab requested this lease, so Arnab can't approve it."
    assert can_approve(lease, PRIYA, policy, AGENTS, room_name="Discord: access-desk").ok


def test_solo_mode_allows_requester(store: LeaseStore, proposal: LeaseProposal) -> None:
    policy = AccessPolicy.model_validate(policy_dict(solo_mode=True))
    lease = make_lease(store, proposal)
    assert can_approve(lease, ARNAB, policy, AGENTS, room_name="access-desk").ok


def test_agent_never_approves(
    store: LeaseStore, proposal: LeaseProposal, policy: AccessPolicy
) -> None:
    lease = make_lease(store, proposal)
    solo = AccessPolicy.model_validate(policy_dict(solo_mode=True))
    for p in (policy, solo):
        check = can_approve(lease, SCRIBE, p, AGENTS, room_name="access-desk")
        assert not check.ok and "agent" in check.reason


def test_unlisted_person_refused(
    store: LeaseStore, proposal: LeaseProposal, policy: AccessPolicy
) -> None:
    lease = make_lease(store, proposal)
    check = can_approve(lease, EVE, policy, AGENTS, room_name="access-desk", approver_name="eve.")
    assert not check.ok and "eve." in check.reason and "approvers" in check.reason


def test_high_needs_security_group_in_approval_room(
    store: LeaseStore, proposal: LeaseProposal, policy: AccessPolicy
) -> None:
    lease = make_lease(store, proposal, risk="HIGH")
    in_desk = can_approve(lease, PRIYA, policy, AGENTS, room_name="Discord: access-desk")
    assert not in_desk.ok and "security-review" in in_desk.reason
    assert can_approve(lease, PRIYA, policy, AGENTS, room_name="Discord: security-review").ok
    solo = AccessPolicy.model_validate(
        policy_dict(
            approvers=[
                {"id": SAM, "name": "Sam", "groups": ["eng-lead"]},
                {"id": PRIYA, "name": "Priya", "groups": ["security"]},
            ]
        )
    )
    lead = can_approve(lease, SAM, solo, AGENTS, room_name="security-review")
    assert not lead.ok and "security" in lead.reason


def approval(who: str, name: str, round: int = 1) -> Approval:
    return Approval(
        lease_id="LS-0001",
        round=round,
        approver_id=who,
        approver_name=name,
        decision="approve",
        at=T0,
        room_id="r",
        message_id=f"m-{who}",
    )


def test_approval_satisfied(
    store: LeaseStore, proposal: LeaseProposal, policy: AccessPolicy
) -> None:
    low = make_lease(store, proposal)
    assert not approval_satisfied(low, [], policy)
    assert approval_satisfied(low, [approval(PRIYA, "Priya")], policy)
    high = make_lease(store, proposal, risk="HIGH")
    assert not approval_satisfied(high, [approval(PRIYA, "Priya")], policy)
    assert approval_satisfied(high, [approval(PRIYA, "Priya"), approval(SAM, "Sam")], policy)


def test_removed_approver_no_longer_counts(store: LeaseStore, proposal: LeaseProposal) -> None:
    lease = make_lease(store, proposal)
    without_priya = AccessPolicy.model_validate(
        policy_dict(approvers=[{"id": SAM, "name": "Sam", "groups": ["security"]}])
    )
    assert not approval_satisfied(lease, [approval(PRIYA, "Priya")], without_priya)


def test_who_may_approve_names(
    store: LeaseStore, proposal: LeaseProposal, policy: AccessPolicy
) -> None:
    assert who_may_approve(make_lease(store, proposal), policy, AGENTS) == "Priya, Sam"
    high = make_lease(store, proposal, risk="HIGH")
    assert (
        who_may_approve(high, policy, AGENTS)
        == "2 of Priya, Sam (group security, in security-review)"
    )
