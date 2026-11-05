"""In-memory stand-ins for keymaster's collaborators."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Literal

from leasedesk.keymaster.evaluator import evaluate
from leasedesk.keymaster.executor import ExecutorError
from leasedesk.keymaster.store import Lease


class FakeExecutor:
    def __init__(self) -> None:
        self.policies: dict[str, dict[str, str]] = {}
        self.applied: list[tuple[str, str]] = []
        self.revoked: list[tuple[str, str]] = []
        self.fail_apply = False

    async def apply(self, iam_user: str, name: str, document: str) -> None:
        if self.fail_apply:
            raise ExecutorError("AccessDenied")
        self.policies.setdefault(iam_user, {})[name] = document
        self.applied.append((iam_user, name))

    async def revoke(self, iam_user: str, name: str) -> bool:
        self.revoked.append((iam_user, name))
        return self.policies.get(iam_user, {}).pop(name, None) is not None

    async def list_inline_policies(self, iam_user: str) -> dict[str, str]:
        return dict(self.policies.get(iam_user, {}))

    async def list_leasedesk_policies(self, iam_users: Iterable[str]) -> dict[str, dict[str, str]]:
        return {
            u: {k: v for k, v in self.policies.get(u, {}).items() if k.startswith("leasedesk-")}
            for u in iam_users
        }

    async def simulate(
        self,
        iam_user: str,
        action: str,
        resource: str,
        at: datetime,
        context: dict[str, str] | None = None,
    ) -> Literal["ALLOWED", "DENIED"]:
        docs = [json.loads(d) for d in self.policies.get(iam_user, {}).values()]
        ctx = {"aws:CurrentTime": at.astimezone(UTC).isoformat(), **(context or {})}
        return evaluate(docs, None, action, resource, ctx)


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str | None, str]] = []
        self.ledger_rows: list[tuple[str, str]] = []

    async def notify(self, lease: Lease | None, text: str) -> None:
        self.messages.append((lease.id if lease else None, text))

    async def ledger(self, lease: Lease) -> None:
        self.ledger_rows.append((lease.id, lease.state.value))

    def texts(self) -> list[str]:
        return [t for _, t in self.messages]
