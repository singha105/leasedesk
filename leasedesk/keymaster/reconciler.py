"""On startup and every 60 s: make AWS match the database.

Any leasedesk-* inline policy without an ACTIVE, unexpired lease behind it is deleted and
reported ("reconciled: removed orphaned policy"). An ACTIVE lease whose policy has vanished
becomes FAILED with an alert, because the person no longer has the access they were told about.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from leasedesk.common import format as fmt
from leasedesk.common.logging import get_logger
from leasedesk.keymaster.executor import Executor, ExecutorError
from leasedesk.keymaster.notify import Notifier
from leasedesk.keymaster.render import policy_name
from leasedesk.keymaster.store import IllegalTransition, LeaseState, LeaseStore, StaleState

log = get_logger(__name__)
S = LeaseState


@dataclass
class ReconcileReport:
    users: list[str] = field(default_factory=list)
    found: int = 0
    removed: list[tuple[str, str]] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


class Reconciler:
    def __init__(
        self,
        store: LeaseStore,
        executor: Executor,
        notifier: Notifier,
        iam_users: Callable[[], set[str]],
        clock: Callable[[], datetime],
        *,
        lock: Callable[[str], asyncio.Lock] | None = None,
    ) -> None:
        self.store = store
        self.executor = executor
        self.notifier = notifier
        self.iam_users = iam_users
        self.clock = clock
        self._lock = lock or (lambda _id: asyncio.Lock())

    async def reconcile(self) -> ReconcileReport:
        now = self.clock()
        active = {policy_name(lease.id): lease for lease in self.store.in_states(S.ACTIVE)}
        users = sorted(
            self.iam_users() | {lease.iam_user for lease in active.values() if lease.iam_user}
        )
        report = ReconcileReport(users=users)
        found = await self.executor.list_leasedesk_policies(users)
        for user, policies in found.items():
            for name in sorted(policies):
                report.found += 1
                lease_id = name.removeprefix("leasedesk-")
                async with self._lock(lease_id):
                    why = self._orphan_reason(lease_id, user, now)
                    if why is None:
                        continue
                    try:
                        await self.executor.revoke(user, name)
                    except ExecutorError as exc:
                        log.error("reconcile_revoke_failed", user=user, policy=name, error=str(exc))
                        continue
                report.removed.append((user, name))
                log.warning("reconciled_orphan", user=user, policy=name, why=why)
                await self.notifier.notify(None, fmt.reconciled(name, user, why))
        for name, lease in active.items():
            if lease.expires_at is not None and now >= lease.expires_at:
                continue
            if lease.iam_user and name not in found.get(lease.iam_user, {}):
                fresh = self.store.get(lease.id)
                if fresh is None or fresh.state is not S.ACTIVE:
                    continue
                try:
                    done = self.store.transition(
                        lease.id, [S.ACTIVE], S.FAILED, "policy vanished from AWS"
                    )
                except (StaleState, IllegalTransition):
                    continue
                report.failed.append(lease.id)
                log.error("lease_policy_vanished", lease=lease.id, user=lease.iam_user)
                await self.notifier.notify(
                    done,
                    fmt.alert(
                        done.id, f"policy {name} vanished from {lease.iam_user}; lease FAILED"
                    ),
                )
                await self.notifier.ledger(done)
        log.info(
            "reconcile_done",
            users=report.users,
            found=report.found,
            removed=len(report.removed),
            failed=len(report.failed),
        )
        return report

    def _orphan_reason(self, lease_id: str, user: str, now: datetime) -> str | None:
        """Why a leasedesk-* policy should go, or None if a live ACTIVE lease owns it."""
        lease = self.store.get(lease_id)
        if lease is None:
            return "no lease in the database"
        if lease.state is not S.ACTIVE or lease.iam_user != user:
            return f"lease is {lease.state}"
        if lease.expires_at is not None and now >= lease.expires_at:
            return f"lease {lease.id} is past expiry"
        return None

    async def run(self, interval: float = 60.0) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.reconcile()
            except Exception:
                log.exception("reconcile_failed")
