"""duration: over 120 minutes adds MEDIUM, over the policy maximum is BLOCK, and write access
for more than 60 minutes adds MEDIUM."""

from __future__ import annotations

from leasedesk.common.schemas import Finding
from leasedesk.sentinel.actions import expand, is_write
from leasedesk.sentinel.context import ReviewContext
from leasedesk.sentinel.findings import LEAST_PRIVILEGE, make, register
from leasedesk.sentinel.model import ProposalView

NAME = "duration"
LONG = 120
WRITE_LONG = 60
register(
    "duration-over-max",
    "BLOCK",
    "Longer than access-policy max_duration_minutes",
    LEAST_PRIVILEGE,
    NAME,
)
register("duration-long", "MEDIUM", "Longer than 120 minutes", LEAST_PRIVILEGE, NAME)
register("write-long", "MEDIUM", "Write access for longer than 60 minutes", LEAST_PRIVILEGE, NAME)


def analyze(p: ProposalView, ctx: ReviewContext) -> list[Finding]:
    d = p.duration_minutes
    out: list[Finding] = []
    if ctx.policy is not None and d > ctx.policy.max_duration_minutes:
        top = ctx.policy.max_duration_minutes
        out.append(
            make(
                "duration-over-max",
                None,
                f"{d} minutes is over the {top}-minute maximum.",
                f"Ask for {top} minutes or less.",
            )
        )
    elif d > LONG:
        out.append(
            make(
                "duration-long",
                None,
                f"{d} minutes is over {LONG}.",
                "Ask for 60 minutes and extend if you need more.",
            )
        )
    writes = [s.sid for s in p.statements if any(is_write(a) for x in s.actions for a in expand(x))]
    if writes and d > WRITE_LONG:
        out.append(
            make(
                "write-long",
                writes[0],
                f"Write access ({', '.join(writes)}) for {d} minutes.",
                "Shorten the lease to 60 minutes.",
            )
        )
    return out
