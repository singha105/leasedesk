"""BotoIamExecutor against moto: idempotent apply/revoke, listing, the server-mode auth flip."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from leasedesk.common.schemas import LeaseProposal
from leasedesk.keymaster.executor import BotoIamExecutor
from leasedesk.keymaster.mockaws import MockAwsServer, MockSandbox, seed
from leasedesk.keymaster.render import render_policy, serialize

from .conftest import T0

USER = "lease-demo-dev"
DOC = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "s3:ListBucket",
                "Resource": "arn:aws:s3:::leasedesk-demo-x",
            }
        ],
    }
)


def fake_session() -> boto3.Session:
    return boto3.Session(
        aws_access_key_id="testing", aws_secret_access_key="testing", region_name="us-east-1"
    )


@pytest.fixture
def sandbox() -> Iterator[tuple[BotoIamExecutor, MockSandbox]]:
    with mock_aws():
        session = fake_session()
        box = seed(session)
        yield BotoIamExecutor(session, boundary_doc=box.boundary_doc), box


async def test_apply_overwrites_idempotently(sandbox: tuple[BotoIamExecutor, MockSandbox]) -> None:
    ex, _ = sandbox
    await ex.apply(USER, "leasedesk-LS-0001", DOC)
    await ex.apply(USER, "leasedesk-LS-0001", DOC)
    assert list((await ex.list_leasedesk_policies([USER]))[USER]) == ["leasedesk-LS-0001"]


async def test_revoke_missing_is_success(sandbox: tuple[BotoIamExecutor, MockSandbox]) -> None:
    ex, _ = sandbox
    assert await ex.revoke(USER, "leasedesk-LS-0099") is False
    await ex.apply(USER, "leasedesk-LS-0001", DOC)
    assert await ex.revoke(USER, "leasedesk-LS-0001") is True
    assert await ex.revoke(USER, "leasedesk-LS-0001") is False


async def test_list_only_leasedesk_policies(sandbox: tuple[BotoIamExecutor, MockSandbox]) -> None:
    ex, _ = sandbox
    fake_session().client("iam").put_user_policy(
        UserName=USER, PolicyName="hand-made", PolicyDocument=DOC
    )
    await ex.apply(USER, "leasedesk-LS-0002", DOC)
    listed = await ex.list_leasedesk_policies([USER])
    assert list(listed[USER]) == ["leasedesk-LS-0002"]
    assert json.loads(listed[USER]["leasedesk-LS-0002"]) == json.loads(DOC)
    every = await ex.list_inline_policies(USER)
    assert set(every) == {"leasedesk-LS-0002", "hand-made"}


async def test_simulate_uses_time_and_boundary(
    sandbox: tuple[BotoIamExecutor, MockSandbox], proposal: LeaseProposal
) -> None:
    ex, box = sandbox
    end = T0 + timedelta(minutes=10)
    raw = proposal.model_dump()
    raw["statements"][0]["resources"] = [f"arn:aws:s3:::{box.bucket}/exports/2026-10/*"]
    raw["statements"][1]["resources"] = [f"arn:aws:s3:::{box.bucket}"]
    doc = serialize(render_policy(LeaseProposal.model_validate(raw), "LS-0001", T0, end))
    await ex.apply(USER, "leasedesk-LS-0001", doc)
    obj = f"arn:aws:s3:::{box.bucket}/exports/2026-10/invoices.csv"
    assert await ex.simulate(USER, "s3:GetObject", obj, T0) == "ALLOWED"
    assert await ex.simulate(USER, "s3:GetObject", obj, end) == "DENIED"


def test_moto_server_auth_flip() -> None:
    with MockAwsServer(port=0) as server:
        box = server.sandbox
        ex = BotoIamExecutor(server.session("keymaster"), endpoint_url=server.url)
        dev = server.session("demo").client("s3", endpoint_url=server.url)

        def ls() -> str:
            try:
                dev.list_objects_v2(Bucket=box.bucket, Prefix="exports/2026-10/")
                return "ALLOWED"
            except ClientError as exc:
                return exc.response["Error"]["Code"]

        import asyncio

        assert ls() == "AccessDenied"
        asyncio.run(
            ex.apply(
                USER,
                "leasedesk-LS-0001",
                json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": ["s3:ListBucket"],
                                "Resource": [f"arn:aws:s3:::{box.bucket}"],
                            }
                        ],
                    }
                ),
            )
        )
        assert ls() == "ALLOWED"
        asyncio.run(ex.revoke(USER, "leasedesk-LS-0001"))
        assert ls() == "AccessDenied"
    import moto.settings

    assert float("inf") == moto.settings.INITIAL_NO_AUTH_ACTION_COUNT
