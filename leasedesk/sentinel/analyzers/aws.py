"""AWS IAM Access Analyzer, real mode only, through profile ``leasedesk-sentinel``.

``validate``: ValidatePolicy (free) on the rendered preview. ERROR -> BLOCK, SECURITY_WARNING ->
HIGH, WARNING -> MEDIUM, SUGGESTION -> LOW. A timeout or any error gives ``skipped`` and adds
nothing: it never lowers a risk and never counts as a pass.

``crown_jewels``: CheckAccessNotGranted ($0.002 per call) against the catalog's crown-jewel
ARNs and sensitive actions, only with ``LEASEDESK_ENABLE_PAID_CHECKS=true``. FAIL -> BLOCK.
Every paid call is logged as ``paid_call``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from leasedesk.common.logging import get_logger
from leasedesk.common.schemas import Finding, Severity
from leasedesk.config import Settings
from leasedesk.sentinel.context import ReviewContext
from leasedesk.sentinel.findings import RULES, make, register

log = get_logger(__name__)
NAME = "aws_validate"
ANALYZER_DOCS = (
    "https://docs.aws.amazon.com/IAM/latest/UserGuide/access-analyzer-reference-policy-checks.html"
)
PRICING = "https://aws.amazon.com/iam/access-analyzer/pricing/"
PAID_PRICE_USD = 0.002
SEVERITY: dict[str, Severity] = {
    "ERROR": "BLOCK",
    "SECURITY_WARNING": "HIGH",
    "WARNING": "MEDIUM",
    "SUGGESTION": "LOW",
}
DEFAULT_S3_ACTIONS = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
register(
    "crown-jewel-access-granted",
    "BLOCK",
    "Access Analyzer: the lease grants access to a crown jewel",
    PRICING,
    "crown_jewels",
)
register(
    "crown-jewel-check-skipped",
    "INFO",
    "The paid crown-jewel check could not run",
    PRICING,
    "crown_jewels",
)

Status = Literal["pass", "findings", "skipped"]


@dataclass
class AwsResult:
    status: Status
    findings: list[Finding] = field(default_factory=list)
    note: str = ""


def _sid(f: dict[str, Any], sids: list[str]) -> str | None:
    for loc in f.get("locations", []):
        path = loc.get("path", [])
        for i, step in enumerate(path[:-1]):
            if step.get("value") == "Statement" and "index" in path[i + 1]:
                idx = path[i + 1]["index"]
                return sids[idx] if 0 <= idx < len(sids) else None
    return None


def _finding(f: dict[str, Any], sids: list[str]) -> Finding:
    kind = str(f.get("findingType", "WARNING"))
    code = str(f.get("issueCode", "UNKNOWN"))
    rule = "aws-" + code.lower().replace("_", "-")
    severity = SEVERITY.get(kind, "MEDIUM")
    link = str(f.get("learnMoreLink") or ANALYZER_DOCS)
    if rule not in RULES:
        register(rule, severity, f"Access Analyzer {code}", link, NAME)
    details = str(f.get("findingDetails", "")) or code
    return make(
        rule,
        _sid(f, sids),
        f"Access Analyzer {kind} {code}.",
        details,
        severity=severity,
        reference=link,
    )


def crown_jewel_access(ctx: ReviewContext) -> dict[str, list[str]] | None:
    jewels = ctx.catalog.crown_jewels() if ctx.catalog else []
    if not jewels:
        return None
    resources: list[str] = []
    actions: set[str] = set()
    for j in jewels:
        resources += j.covers()
        actions |= set(j.sensitive_actions or (DEFAULT_S3_ACTIONS if "s3" in j.arn else []))
    return {"actions": sorted(actions)[:100], "resources": resources[:100]}


class AwsChecks:
    def __init__(self, client_factory: Callable[[], Any], *, timeout_s: float = 5.0) -> None:
        self._factory = client_factory
        self.timeout_s = timeout_s

    async def _run(self, fn: Callable[[], Any]) -> Any:
        return await asyncio.wait_for(asyncio.to_thread(fn), self.timeout_s)

    async def validate(self, doc: dict[str, Any], sids: list[str]) -> AwsResult:
        def call() -> list[dict[str, Any]]:
            client = self._factory()
            found: list[dict[str, Any]] = []
            token: str | None = None
            while True:
                kw: dict[str, Any] = {
                    "policyDocument": json.dumps(doc),
                    "policyType": "IDENTITY_POLICY",
                }
                if token:
                    kw["nextToken"] = token
                r = client.validate_policy(**kw)
                found += r.get("findings", [])
                token = r.get("nextToken")
                if not token:
                    return found

        try:
            raw = await self._run(call)
        except Exception as exc:
            log.warning("aws_validate_skipped", error=type(exc).__name__)
            return AwsResult("skipped", note=type(exc).__name__)
        findings = [_finding(f, sids) for f in raw]
        return AwsResult("findings" if findings else "pass", findings)

    async def crown_jewels(self, doc: dict[str, Any], ctx: ReviewContext) -> list[Finding]:
        if not (ctx.paid_checks and ctx.aws_mode == "real"):
            return []
        access = crown_jewel_access(ctx)
        if access is None:
            return []

        def call() -> Any:
            return self._factory().check_access_not_granted(
                policyDocument=json.dumps(doc), access=[access], policyType="IDENTITY_POLICY"
            )

        log.info(
            "paid_call",
            api="CheckAccessNotGranted",
            price_usd=PAID_PRICE_USD,
            resources=len(access["resources"]),
        )
        try:
            r = await self._run(call)
        except Exception as exc:
            log.warning("crown_jewel_check_skipped", error=type(exc).__name__)
            return [
                make(
                    "crown-jewel-check-skipped",
                    None,
                    f"CheckAccessNotGranted failed ({type(exc).__name__}).",
                )
            ]
        if r.get("result") == "FAIL":
            message = str(r.get("message", "")) or "access to a crown-jewel resource is granted"
            return [
                make(
                    "crown-jewel-access-granted",
                    None,
                    f"Access Analyzer: {message}.",
                    "Remove the crown-jewel resources or actions from the lease.",
                )
            ]
        return []


def real_client_factory(settings: Settings) -> Callable[[], Any]:
    """An Access Analyzer client on the named sentinel profile only (never ``default``)."""

    def make_client() -> Any:
        import boto3
        from botocore.config import Config

        session = boto3.Session(
            profile_name=settings.sentinel_profile, region_name=settings.aws_region
        )
        cfg = Config(connect_timeout=2, read_timeout=4, retries={"max_attempts": 2})
        return session.client("accessanalyzer", config=cfg)

    return make_client
