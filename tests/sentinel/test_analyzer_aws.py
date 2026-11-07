"""aws_validate and crown_jewels against stubbed Access Analyzer (moto has none)."""

from __future__ import annotations

import time
from typing import Any

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from botocore.stub import Stubber
from structlog.testing import capture_logs

from leasedesk.sentinel.analyzers.aws import AwsChecks, crown_jewel_access
from leasedesk.sentinel.context import ReviewContext

from .conftest import DECOY, make_ctx

DOC: dict[str, Any] = {
    "Version": "2012-10-17",
    "Statement": [
        {"Sid": "LS0000A", "Effect": "Allow", "Action": ["s3:GetObject"], "Resource": ["*"]},
        {"Sid": "LS0000B", "Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": ["*"]},
    ],
}
SIDS = ["A", "B"]


def client() -> Any:
    return boto3.client(
        "accessanalyzer",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


def finding(kind: str, code: str, index: int) -> dict[str, Any]:
    return {
        "findingDetails": f"{code} details",
        "findingType": kind,
        "issueCode": code,
        "learnMoreLink": f"https://docs.aws.amazon.com/{code}",
        "locations": [
            {
                "path": [{"value": "Statement"}, {"index": index}],
                "span": {
                    "start": {"line": 0, "column": 0, "offset": 0},
                    "end": {"line": 0, "column": 1, "offset": 1},
                },
            }
        ],
    }


async def test_findings_map_to_severity_and_sid() -> None:
    c = client()
    with Stubber(c) as stub:
        stub.add_response(
            "validate_policy",
            {
                "findings": [
                    finding("ERROR", "INVALID_ACTION", 0),
                    finding("SECURITY_WARNING", "PASS_ROLE_WITH_STAR_IN_RESOURCE", 1),
                    finding("WARNING", "MISSING_VERSION", 0),
                    finding("SUGGESTION", "EMPTY_ARRAY_RESOURCE", 1),
                ]
            },
        )
        result = await AwsChecks(lambda: c).validate(DOC, SIDS)
    assert result.status == "findings"
    got = [(f.rule, f.severity, f.statement_sid) for f in result.findings]
    assert got == [
        ("aws-invalid-action", "BLOCK", "A"),
        ("aws-pass-role-with-star-in-resource", "HIGH", "B"),
        ("aws-missing-version", "MEDIUM", "A"),
        ("aws-empty-array-resource", "LOW", "B"),
    ]
    assert all(f.reference and f.reference.startswith("https://") for f in result.findings)
    assert all(f.fix for f in result.findings if f.severity in ("HIGH", "MEDIUM"))


async def test_no_findings_is_pass() -> None:
    c = client()
    with Stubber(c) as stub:
        stub.add_response("validate_policy", {"findings": []})
        result = await AwsChecks(lambda: c).validate(DOC, SIDS)
    assert result.status == "pass" and result.findings == []


async def test_aws_error_is_skipped_never_pass() -> None:
    c = client()
    with Stubber(c) as stub:
        stub.add_client_error("validate_policy", "AccessDeniedException", http_status_code=403)
        result = await AwsChecks(lambda: c).validate(DOC, SIDS)
    assert result.status == "skipped" and result.findings == []


async def test_connection_error_and_no_client_are_skipped() -> None:
    def broken() -> Any:
        raise EndpointConnectionError(endpoint_url="https://access-analyzer")

    assert (await AwsChecks(broken).validate(DOC, SIDS)).status == "skipped"


async def test_timeout_is_skipped() -> None:
    class Slow:
        def validate_policy(self, **_: Any) -> Any:
            time.sleep(0.3)
            return {"findings": []}

    result = await AwsChecks(lambda: Slow(), timeout_s=0.05).validate(DOC, SIDS)
    assert result.status == "skipped"


def jewel_ctx(**over: Any) -> ReviewContext:
    return make_ctx(aws_mode="real", paid_checks=True, **over)


async def test_paid_check_fail_blocks_and_is_logged() -> None:
    c = client()
    with Stubber(c) as stub:
        stub.add_response(
            "check_access_not_granted", {"result": "FAIL", "message": "grants s3:GetObject"}
        )
        with capture_logs() as logs:
            found = await AwsChecks(lambda: c).crown_jewels(DOC, jewel_ctx())
    assert [(f.rule, f.severity) for f in found] == [("crown-jewel-access-granted", "BLOCK")]
    paid = [e for e in logs if e["event"] == "paid_call"]
    assert len(paid) == 1 and paid[0]["api"] == "CheckAccessNotGranted"


async def test_paid_check_pass_adds_nothing() -> None:
    c = client()
    with Stubber(c) as stub:
        stub.add_response("check_access_not_granted", {"result": "PASS", "message": "ok"})
        assert await AwsChecks(lambda: c).crown_jewels(DOC, jewel_ctx()) == []


@pytest.mark.parametrize("over", [{"paid_checks": False}, {"aws_mode": "mock"}])
async def test_paid_check_needs_flag_and_real_mode(over: dict[str, Any]) -> None:
    def never() -> Any:
        raise AssertionError("no AWS call expected")

    ctx = make_ctx(**{"aws_mode": "real", "paid_checks": True, **over})
    with capture_logs() as logs:
        assert await AwsChecks(never).crown_jewels(DOC, ctx) == []
    assert not [e for e in logs if e["event"] == "paid_call"]


def test_crown_jewel_access_comes_from_the_catalog() -> None:
    access = crown_jewel_access(make_ctx())
    assert access is not None
    assert access["resources"] == [
        "arn:aws:s3:::leasedesk-demo-prod-secrets-*",
        "arn:aws:s3:::leasedesk-demo-prod-secrets-*/*",
    ]
    assert access["actions"] == ["s3:DeleteObject", "s3:GetObject", "s3:PutObject"]
    assert DECOY.startswith(access["resources"][0][:-1])
