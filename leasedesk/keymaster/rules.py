"""keymaster's decision rules. Pure functions: no I/O, no clock, fully unit-tested."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

from leasedesk.common.schemas import AccessPolicy, LeaseProposal
from leasedesk.keymaster.store import Approval, Lease

MIN_DURATION = 5
MIN_DURATION_DEV = 3


def glob_intersects(a: str, b: str) -> bool:
    """True when some action name matches both IAM patterns (``*`` and ``?``; any case).

    Checked both ways at once, so ``iam:*`` vs ``iam:PassRole`` and ``s3:*`` vs
    ``s3:DeleteBucket`` both intersect, and so do ``s3:Get*`` vs ``s3:*Object``.
    """
    x, y = a.lower(), b.lower()

    @cache
    def go(i: int, j: int) -> bool:
        if i == len(x):
            return all(c == "*" for c in y[j:])
        if j == len(y):
            return all(c == "*" for c in x[i:])
        if x[i] == "*":
            return go(i + 1, j) or go(i, j + 1)
        if y[j] == "*":
            return go(i, j + 1) or go(i + 1, j)
        return (x[i] == y[j] or "?" in (x[i], y[j])) and go(i + 1, j + 1)

    return go(0, 0)


def validate_proposal(p: LeaseProposal, policy: AccessPolicy, *, dev_mode: bool) -> list[str]:
    """Problems with a schema-valid proposal under the access policy. Empty means valid."""
    problems: list[str] = []
    for s in p.statements:
        for r in s.resources:
            if not any(r.startswith(prefix) for prefix in policy.allowed_resource_prefixes):
                problems.append(f"{s.sid}: resource {r} is outside the allowed prefixes")
        for action in s.actions:
            for forbidden in policy.forbidden_actions:
                if glob_intersects(action, forbidden):
                    problems.append(f"{s.sid}: {action} overlaps forbidden action {forbidden}")
    low = MIN_DURATION_DEV if dev_mode else MIN_DURATION
    if not low <= p.duration_minutes <= policy.max_duration_minutes:
        problems.append(
            f"duration_minutes must be {low} to {policy.max_duration_minutes} "
            f"(got {p.duration_minutes})"
        )
    return problems


@dataclass(frozen=True)
class Required:
    count: int
    group: str | None
    room: str | None


def required_approvals(risk: str | None, policy: AccessPolicy) -> Required | None:
    if risk == "BLOCK":
        return None
    if risk == "HIGH":
        hr = policy.high_risk
        return Required(hr.approvals, hr.require_group, hr.approval_room)
    return Required(1, None, None)


@dataclass(frozen=True)
class Check:
    ok: bool
    reason: str = ""


def room_matches(room_name: str, wanted: str) -> bool:
    name, want = room_name.lower(), wanted.lower().lstrip("#")
    return name == want or name.endswith(f": {want}") or name.endswith(f": #{want}")


def can_approve(
    lease: Lease,
    approver_id: str,
    policy: AccessPolicy,
    known_agent_ids: set[str],
    *,
    room_name: str,
    approver_name: str | None = None,
) -> Check:
    if approver_id in known_agent_ids:
        return Check(False, "Approvals from agents are never counted.")
    approver = policy.approver(approver_id)
    if approver is None:
        who = approver_name or "This person"
        return Check(False, f"{who} isn't in the approvers list in access-policy.")
    if approver_id == lease.requester and not policy.solo_mode:
        name = lease.requester_name or approver.name
        return Check(False, f"{name} requested this lease, so {name} can't approve it.")
    need = required_approvals(lease.risk, policy)
    if need is None:
        return Check(False, "A blocked lease can't be approved.")
    if need.group and need.group not in approver.groups:
        return Check(
            False,
            f"HIGH-risk leases need approval from group {need.group}; {approver.name} isn't in it.",
        )
    if need.room and not room_matches(room_name, need.room):
        return Check(False, f"HIGH-risk approvals count only when given in {need.room}.")
    return Check(True)


def _counts(lease: Lease, a: Approval, policy: AccessPolicy) -> bool:
    approver = policy.approver(a.approver_id)
    if a.decision != "approve" or approver is None:
        return False
    if a.approver_id == lease.requester and not policy.solo_mode:
        return False
    need = required_approvals(lease.risk, policy)
    return need is not None and (need.group is None or need.group in approver.groups)


def approval_satisfied(lease: Lease, approvals: list[Approval], policy: AccessPolicy) -> bool:
    """Enough distinct approvals in the current round, re-checked under the current policy."""
    need = required_approvals(lease.risk, policy)
    if need is None:
        return False
    approvers = {
        a.approver_id for a in approvals if a.round == lease.round and _counts(lease, a, policy)
    }
    return len(approvers) >= need.count


def who_may_approve(lease: Lease, policy: AccessPolicy, known_agent_ids: set[str]) -> str:
    need = required_approvals(lease.risk, policy)
    names = [
        a.name
        for a in policy.approvers
        if a.id not in known_agent_ids
        and (policy.solo_mode or a.id != lease.requester)
        and (need is None or need.group is None or need.group in a.groups)
    ]
    listed = ", ".join(names) if names else "nobody in access-policy"
    if need is not None and need.group:
        return f"{need.count} of {listed} (group {need.group}, in {need.room})"
    return listed
