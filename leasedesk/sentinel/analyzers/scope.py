"""scope: which resources a lease reaches. ``*`` is HIGH; a resource outside the policy's
allowed_resource_prefixes, an ARN in another account, an action the policy forbids, or a write
to a crown-jewel resource is BLOCK; reading a crown jewel is HIGH; s3:ListBucket without an
s3:prefix condition is LOW with a fix that adds one."""

from __future__ import annotations

from leasedesk.common.globs import glob_intersects
from leasedesk.common.schemas import Finding
from leasedesk.sentinel.actions import expand, is_write
from leasedesk.sentinel.analyzers.structure import ARN
from leasedesk.sentinel.context import ReviewContext
from leasedesk.sentinel.findings import IAM_DOCS, LEAST_PRIVILEGE, make, register
from leasedesk.sentinel.model import ProposalView, StatementView
from leasedesk.sentinel.purpose import path_token

NAME = "scope"
S3_KEYS = "https://docs.aws.amazon.com/AmazonS3/latest/userguide/amazon-s3-policy-keys.html"
register(
    "resource-star",
    "HIGH",
    "Resource * (every resource the actions apply to)",
    LEAST_PRIVILEGE,
    NAME,
)
register(
    "resource-outside-prefixes",
    "BLOCK",
    "A resource outside access-policy allowed_resource_prefixes",
    LEAST_PRIVILEGE,
    NAME,
)
register(
    "resource-other-account",
    "BLOCK",
    "An ARN in another AWS account",
    f"{IAM_DOCS}/reference-arns.html",
    NAME,
)
register("forbidden-action", "BLOCK", "An action access-policy forbids", LEAST_PRIVILEGE, NAME)
register(
    "crown-jewel-read",
    "HIGH",
    "Reads a crown-jewel resource from resource-catalog",
    LEAST_PRIVILEGE,
    NAME,
)
register(
    "crown-jewel-write",
    "BLOCK",
    "Changes a crown-jewel resource from resource-catalog",
    LEAST_PRIVILEGE,
    NAME,
)
register(
    "list-bucket-no-prefix",
    "LOW",
    "s3:ListBucket without an s3:prefix condition lists the whole bucket",
    S3_KEYS,
    NAME,
)


def _bucket(arn: str) -> str | None:
    if not arn.startswith("arn:aws:s3:::") or arn == "arn:aws:s3:::":
        return None
    return arn.split("/", 1)[0]


def derived_prefix(p: ProposalView, bucket: str) -> str | None:
    """The folder a lease reads in ``bucket``, from its object ARNs or the summary."""
    for s in p.statements:
        for r in s.resources:
            if r.startswith(bucket + "/"):
                path = r[len(bucket) + 1 :]
                if path and path != "*":
                    return path
    token = path_token(p.summary)
    return token + "*" if token else None


def _list_fix(p: ProposalView, s: StatementView) -> str:
    buckets = [b for b in (_bucket(r) for r in s.resources) if b]
    prefix = derived_prefix(p, buckets[0]) if buckets else None
    folder = prefix or "<folder>/*"
    return f'Add "conditions": {{"StringLike": {{"s3:prefix": ["{folder}"]}}}} to {s.sid}.'


def _has_prefix_condition(s: StatementView) -> bool:
    return any(
        isinstance(keys, dict) and any(str(k).lower() == "s3:prefix" for k in keys)
        for keys in (s.conditions or {}).values()
    )


def _statement(p: ProposalView, s: StatementView, ctx: ReviewContext) -> list[Finding]:
    out: list[Finding] = []
    policy, catalog = ctx.policy, ctx.catalog
    writes = any(is_write(a) for action in s.actions for a in expand(action))
    named = [r for r in (r for st in p.statements for r in st.resources) if r != "*"]
    for r in s.resources:
        if r == "*":
            example = named[0] if named else "arn:aws:s3:::<bucket>/<folder>/*"
            out.append(
                make(
                    "resource-star",
                    s.sid,
                    f"Resource * gives {', '.join(s.actions[:3])} on every resource.",
                    f"Name the resource, e.g. {example}.",
                )
            )
        elif policy is not None and not any(
            r.startswith(x) for x in policy.allowed_resource_prefixes
        ):
            out.append(
                make(
                    "resource-outside-prefixes",
                    s.sid,
                    f"{r} is outside the allowed resource prefixes.",
                    f"Use a resource under {', '.join(policy.allowed_resource_prefixes)}.",
                )
            )
        m = ARN.match(r)
        if m and m.group(3) and m.group(3) not in (ctx.account_id, "aws"):
            out.append(
                make(
                    "resource-other-account",
                    s.sid,
                    f"{r} names account {m.group(3)}, not this one.",
                    "Leases only cover resources in this AWS account.",
                )
            )
        for jewel in catalog.crown_jewels() if catalog else []:
            if any(glob_intersects(r, pattern) for pattern in jewel.covers()):
                owner = f" (owner: {jewel.owner})" if jewel.owner else ""
                if writes:
                    out.append(
                        make(
                            "crown-jewel-write",
                            s.sid,
                            f"{r} is crown-jewel {jewel.arn}{owner} "
                            "and the statement can change it.",
                            "Crown-jewel resources can't be changed through a lease; "
                            "ask the owner.",
                        )
                    )
                else:
                    out.append(
                        make(
                            "crown-jewel-read",
                            s.sid,
                            f"{r} reaches crown-jewel {jewel.arn}{owner}.",
                            f"Read a copy outside {jewel.arn}, or get security's approval.",
                        )
                    )
                break
    for action in s.actions:
        hit = [
            f for f in (policy.forbidden_actions if policy else []) if glob_intersects(action, f)
        ]
        if hit:
            out.append(
                make(
                    "forbidden-action",
                    s.sid,
                    f"{action} overlaps forbidden action {hit[0]}.",
                    f"Drop {action}; access-policy forbids {hit[0]}.",
                )
            )
    if any(a.lower() == "s3:listbucket" for a in s.actions) and not _has_prefix_condition(s):
        out.append(
            make(
                "list-bucket-no-prefix",
                s.sid,
                f"{s.sid} lists every key in the bucket.",
                _list_fix(p, s),
            )
        )
    return out


def analyze(p: ProposalView, ctx: ReviewContext) -> list[Finding]:
    out: list[Finding] = []
    for s in p.statements:
        out += _statement(p, s, ctx)
    return out
