"""Rendering a lease into the inline IAM policy keymaster writes.

Every statement is an Allow with two aws:CurrentTime conditions: DateGreaterThan (start minus
60 s, for clock skew) and DateLessThan (expiry). AWS stops honouring the policy at expiry even
if keymaster is down. The proposal's own conditions are kept; the schema already refuses any
proposal that mentions aws:CurrentTime, so nothing of the requester's is overwritten.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol


class StatementLike(Protocol):
    @property
    def sid(self) -> str: ...
    @property
    def actions(self) -> list[str]: ...
    @property
    def resources(self) -> list[str]: ...
    @property
    def conditions(self) -> Mapping[str, Any] | None: ...


class ProposalLike(Protocol):
    """A LeaseProposal, or sentinel's lenient ProposalView (for a preview of the policy)."""

    @property
    def statements(self) -> Sequence[StatementLike]: ...


# AWS IAM quotas (checked 2026-11-04): "User policy size can't exceed 2,048 characters", the
# aggregate of all inline policies on one user; "IAM doesn't count white space".
USER_INLINE_LIMIT = 2048
SKEW = timedelta(seconds=60)
_WS = re.compile(r"\s")


class PolicyTooLarge(ValueError):
    def __init__(self, total: int, limit: int) -> None:
        self.total = total
        self.limit = limit
        super().__init__(f"inline policies would total {total} characters (limit {limit})")


def policy_name(lease_id: str) -> str:
    return f"leasedesk-{lease_id}"


def _ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def render_policy(
    p: ProposalLike, lease_id: str, starts_at: datetime, expires_at: datetime
) -> dict[str, Any]:
    prefix = lease_id.replace("-", "")
    statements: list[dict[str, Any]] = []
    for s in p.statements:
        cond: dict[str, dict[str, Any]] = copy.deepcopy(dict(s.conditions or {}))
        cond.setdefault("DateGreaterThan", {})["aws:CurrentTime"] = _ts(starts_at - SKEW)
        cond.setdefault("DateLessThan", {})["aws:CurrentTime"] = _ts(expires_at)
        statements.append(
            {
                "Sid": f"{prefix}{s.sid}"[:128],
                "Effect": "Allow",
                "Action": list(s.actions),
                "Resource": list(s.resources),
                "Condition": cond,
            }
        )
    return {"Version": "2012-10-17", "Statement": statements}


def serialize(doc: dict[str, Any]) -> str:
    return json.dumps(doc, separators=(",", ":"))


def iam_size(doc: str) -> int:
    return len(_WS.sub("", doc))


def check_user_total(
    new_doc: str, other_docs: Iterable[str], limit: int = USER_INLINE_LIMIT
) -> None:
    total = iam_size(new_doc) + sum(iam_size(d) for d in other_docs)
    if total > limit:
        raise PolicyTooLarge(total, limit)
