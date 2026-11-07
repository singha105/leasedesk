"""Every 5 s: expire ACTIVE leases, cancel stale pending ones, remind once about verdicts.

All state is in SQLite, so a restart loses nothing: the next tick after a restart catches up.
The clock is injected so tests never sleep.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta

from leasedesk.common import format as fmt
from leasedesk.common.logging import get_logger
from leasedesk.common.render import policy_name
from leasedesk.common.schemas import AccessPolicy
from leasedesk.keymaster.executor import Executor, ExecutorError
from leasedesk.keymaster.notify import Notifier
from leasedesk.keymaster.store import (
    IllegalTransition,
    Lease,
    LeaseState,
    LeaseStore,
    StaleState,
)

log = get_logger(__name__)
S = LeaseState
VERDICT_REMINDER = timedelta(seconds=60)


class Scheduler:
    def __init__(
        self,
        store: LeaseStore,
        executor: Executor,
        notifier: Notifier,
        policy: Callable[[], AccessPolicy | None],
        clock: Callable[[], datetime],
        tz: str,
        *,
        lock: Callable[[str], asyncio.Lock] | None = None,
    ) -> None:
        self.store = store
        self.executor = executor
        self.notifier = notifier
        self.policy = policy
        self.clock = clock
        self.tz = tz
        self._lock = lock or (lambda _id: asyncio.Lock())

    async def expire(self, lease: Lease) -> bool:
        if lease.iam_user:
            try:
                await self.executor.revoke(lease.iam_user, policy_name(lease.id))
            except ExecutorError as exc:
                log.error("expiry_revoke_failed", lease=lease.id, error=str(exc))
                await self.notifier.notify(
                    lease,
                    fmt.alert(
                        lease.id, lease.state, f"couldn't remove the policy at expiry ({exc})"
                    ),
                )
                return False
        try:
            done = self.store.transition(lease.id, [S.ACTIVE], S.EXPIRED, "expired")
        except (StaleState, IllegalTransition):
            return False
        log.info("lease_expired", lease=lease.id)
        await self.notifier.notify(
            done, fmt.expiry(done.id, done.expires_at or self.clock(), self.tz)
        )
        await self.notifier.ledger(done)
        return True

    async def _cancel(self, lease: Lease, frm: list[LeaseState], reason: str) -> bool:
        try:
            done = self.store.transition(lease.id, frm, S.CANCELLED, reason)
        except (StaleState, IllegalTransition):
            return False
        await self.notifier.notify(done, fmt.cancelled(done.id, reason))
        await self.notifier.ledger(done)
        return True

    async def tick(self) -> list[str]:
        now = self.clock()
        acted: list[str] = []
        for lease in self.store.in_states(S.ACTIVE):
            async with self._lock(lease.id):
                fresh = self.store.require(lease.id)
                if fresh.state is S.ACTIVE and fresh.expires_at and now >= fresh.expires_at:
                    if await self.expire(fresh):
                        acted.append(fresh.id)
                elif (
                    fresh.state is S.ACTIVE
                    and fresh.pending_extension_minutes
                    and (pol := self.policy()) is not None
                    and fresh.approval_requested_at
                    and now
                    >= fresh.approval_requested_at + timedelta(minutes=pol.approval_timeout_minutes)
                ):
                    self.store.update(fresh.id, pending_extension_minutes=None)
                    self.store.record_event(fresh.id, "extension", "extension request timed out")
                    await self.notifier.notify(
                        fresh,
                        fmt.head(
                            fresh.id, fresh.state, "extension request timed out; lease unchanged."
                        ),
                    )
        pol = self.policy()
        if pol is not None:
            confirm = timedelta(minutes=pol.confirm_timeout_minutes)
            approve = timedelta(minutes=pol.approval_timeout_minutes)
            for lease in self.store.in_states(S.AWAITING_CONFIRM):
                if now >= lease.created_at + confirm:
                    reason = f"nobody confirmed within {pol.confirm_timeout_minutes} min"
                    if await self._cancel(lease, [S.AWAITING_CONFIRM], reason):
                        acted.append(lease.id)
            for lease in self.store.in_states(S.AWAITING_APPROVAL, S.ESCALATED):
                started = lease.approval_requested_at or lease.created_at
                if now >= started + approve:
                    reason = f"no approval within {pol.approval_timeout_minutes} min"
                    async with self._lock(lease.id):
                        if await self._cancel(lease, [S.AWAITING_APPROVAL, S.ESCALATED], reason):
                            acted.append(lease.id)
        for lease in self.store.in_states(S.UNDER_REVIEW):
            started = lease.review_requested_at or lease.created_at
            if not lease.reminded and now >= started + VERDICT_REMINDER:
                self.store.update(lease.id, reminded=True)
                await self.notifier.notify(lease, fmt.reminder(lease.id))
        return acted

    async def run(self, interval: float = 5.0) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler_tick_failed")
            await asyncio.sleep(interval)
