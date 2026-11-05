"""The only code that changes AWS: writing and deleting leasedesk-* inline policies.

``BotoIamExecutor`` serves both modes. Real mode uses the ``leasedesk-keymaster`` profile;
mock mode points the same code at the long-running moto server (``make moto``) with the mock
keymaster keys in .state/mock-aws.env. Revoking a missing policy is success.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from leasedesk.common.envfile import read_env_file
from leasedesk.common.logging import get_logger
from leasedesk.config import Settings
from leasedesk.keymaster.evaluator import evaluate

log = get_logger(__name__)
PREFIX = "leasedesk-"
BOUNDARY_FILE = Path("scripts/aws/policies/demo-boundary.json")
MOCK_ENV = "mock-aws.env"


class ExecutorError(RuntimeError):
    pass


class Executor(Protocol):
    async def apply(self, iam_user: str, name: str, document: str) -> None: ...

    async def revoke(self, iam_user: str, name: str) -> bool: ...

    async def list_leasedesk_policies(
        self, iam_users: Iterable[str]
    ) -> dict[str, dict[str, str]]: ...

    async def list_inline_policies(self, iam_user: str) -> dict[str, str]: ...

    async def simulate(
        self,
        iam_user: str,
        action: str,
        resource: str,
        at: datetime,
        context: dict[str, str] | None = None,
    ) -> Literal["ALLOWED", "DENIED"]: ...


def _compact(doc: Any) -> str:
    if isinstance(doc, str):
        doc = json.loads(doc)
    return json.dumps(doc, separators=(",", ":"))


class BotoIamExecutor:
    def __init__(
        self,
        session: boto3.Session,
        *,
        endpoint_url: str | None = None,
        boundary_doc: dict[str, Any] | None = None,
        propagation_timeout: float = 10.0,
        poll_interval: float = 0.5,
    ) -> None:
        self._iam = session.client("iam", endpoint_url=endpoint_url)
        self.boundary_doc = boundary_doc
        self._timeout = propagation_timeout
        self._poll = poll_interval

    async def _call(self, fn: Any, **kw: Any) -> Any:
        try:
            return await asyncio.to_thread(fn, **kw)
        except BotoCoreError as exc:
            raise ExecutorError(type(exc).__name__) from None

    async def apply(self, iam_user: str, name: str, document: str) -> None:
        want = json.loads(document)
        try:
            await self._call(
                self._iam.put_user_policy,
                UserName=iam_user,
                PolicyName=name,
                PolicyDocument=document,
            )
        except ClientError as exc:
            raise ExecutorError(exc.response["Error"].get("Code", "ClientError")) from None
        deadline = asyncio.get_running_loop().time() + self._timeout
        while True:
            try:
                got = await self._call(
                    self._iam.get_user_policy, UserName=iam_user, PolicyName=name
                )
                if got["PolicyDocument"] == want:
                    log.info("policy_applied", iam_user=iam_user, policy=name)
                    return
            except ClientError as exc:
                if exc.response["Error"].get("Code") != "NoSuchEntity":
                    raise ExecutorError(exc.response["Error"].get("Code", "ClientError")) from None
            if asyncio.get_running_loop().time() >= deadline:
                raise ExecutorError(f"{name} not visible on {iam_user} after {self._timeout}s")
            await asyncio.sleep(self._poll)

    async def revoke(self, iam_user: str, name: str) -> bool:
        try:
            await self._call(self._iam.delete_user_policy, UserName=iam_user, PolicyName=name)
        except ClientError as exc:
            if exc.response["Error"].get("Code") == "NoSuchEntity":
                log.info("policy_already_gone", iam_user=iam_user, policy=name)
                return False
            raise ExecutorError(exc.response["Error"].get("Code", "ClientError")) from None
        log.info("policy_revoked", iam_user=iam_user, policy=name)
        return True

    async def list_inline_policies(self, iam_user: str) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            names: list[str] = []
            marker: str | None = None
            while True:
                kw: dict[str, Any] = {"UserName": iam_user}
                if marker:
                    kw["Marker"] = marker
                page = await self._call(self._iam.list_user_policies, **kw)
                names.extend(page.get("PolicyNames", []))
                if not page.get("IsTruncated"):
                    break
                marker = page.get("Marker")
            for name in names:
                got = await self._call(
                    self._iam.get_user_policy, UserName=iam_user, PolicyName=name
                )
                out[name] = _compact(got["PolicyDocument"])
        except ClientError as exc:
            raise ExecutorError(exc.response["Error"].get("Code", "ClientError")) from None
        return out

    async def list_leasedesk_policies(self, iam_users: Iterable[str]) -> dict[str, dict[str, str]]:
        result: dict[str, dict[str, str]] = {}
        for user in iam_users:
            every = await self.list_inline_policies(user)
            result[user] = {k: v for k, v in every.items() if k.startswith(PREFIX)}
        return result

    async def simulate(
        self,
        iam_user: str,
        action: str,
        resource: str,
        at: datetime,
        context: dict[str, str] | None = None,
    ) -> Literal["ALLOWED", "DENIED"]:
        """Evaluate the user's inline policies plus the boundary locally (see evaluator.py)."""
        docs = [json.loads(d) for d in (await self.list_inline_policies(iam_user)).values()]
        ctx = {"aws:CurrentTime": at.astimezone(UTC).isoformat(), **(context or {})}
        return evaluate(docs, self.boundary_doc, action, resource, ctx)


def load_boundary(account_id: str | None) -> dict[str, Any]:
    text = BOUNDARY_FILE.read_text().replace("<ACCOUNT_ID>", account_id or "*")
    doc: dict[str, Any] = json.loads(text)
    return doc


def make_executor(settings: Settings) -> BotoIamExecutor:
    if settings.aws_mode == "real":
        session = boto3.Session(
            profile_name=settings.keymaster_profile, region_name=settings.aws_region
        )
        return BotoIamExecutor(session, boundary_doc=load_boundary(None))
    env = read_env_file(settings.state_dir / MOCK_ENV)
    if not env.get("LEASEDESK_MOCK_ENDPOINT"):
        raise ExecutorError("mock AWS isn't running: start it with `make moto` first")
    session = boto3.Session(
        aws_access_key_id=env["KEYMASTER_AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=env["KEYMASTER_AWS_SECRET_ACCESS_KEY"],
        region_name=settings.aws_region,
    )
    return BotoIamExecutor(
        session,
        endpoint_url=env["LEASEDESK_MOCK_ENDPOINT"],
        boundary_doc=load_boundary(env.get("LEASEDESK_MOCK_ACCOUNT", "123456789012")),
        poll_interval=0.1,
    )
