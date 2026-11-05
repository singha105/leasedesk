"""The keymaster agent: the only component that writes IAM, and only after a verdict and a
counted human approval. Commands live in ``commands.py``; this class owns the collaborators
(store, executor, policy, ledger, scheduler, reconciler) and the steps that touch AWS.

Every state change for a lease happens under that lease's asyncio lock, and every one is
re-read from SQLite inside the lock, so two approvals arriving together activate once.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from leasedesk.common import format as fmt
from leasedesk.common.schemas import AccessPolicy
from leasedesk.config import Settings
from leasedesk.keymaster import commands
from leasedesk.keymaster.executor import Executor, ExecutorError
from leasedesk.keymaster.ledger import DESCRIPTION, INSTRUCTIONS, NAME, Ledger
from leasedesk.keymaster.policy import LoadResult, PolicyLoader
from leasedesk.keymaster.reconciler import Reconciler
from leasedesk.keymaster.render import (
    PolicyTooLarge,
    check_user_total,
    policy_name,
    render_policy,
    serialize,
)
from leasedesk.keymaster.rules import who_may_approve
from leasedesk.keymaster.scheduler import Scheduler
from leasedesk.keymaster.store import Lease, LeaseState, LeaseStore
from leasedesk.switch.agent import CommandContext, SwitchAgent
from leasedesk.switch.connection import RoomConnection
from leasedesk.switch.credentials import AgentCredentials
from leasedesk.switch.errors import SwitchError
from leasedesk.switch.models import ControlFrame, SubscriptionChanged
from leasedesk.switch.ops import OpsClient

S = LeaseState
ACCESS_ROOM = "access-desk"
APPROVABLE = [S.AWAITING_APPROVAL, S.ESCALATED]
EXECUTOR_ROLE = "executor"
ROLE_RETRY_S = 60.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Keymaster(SwitchAgent):
    def __init__(
        self,
        settings: Settings,
        creds: AgentCredentials,
        ops: OpsClient,
        *,
        store: LeaseStore,
        executor: Executor,
        loader: PolicyLoader,
        clock: Callable[[], datetime] = _utcnow,
        own_id: str | None = None,
        ledger_writer: Callable[[str, str], Awaitable[None]] | None = None,
        jsonl: Path = Path("logs/ledger.jsonl"),
        connection_factory: Callable[..., Any] = RoomConnection,
        room_poll_s: float = 30.0,
    ) -> None:
        super().__init__(
            settings,
            creds,
            ops,
            connection_factory=connection_factory,
            own_id=own_id,
            room_poll_s=room_poll_s,
        )
        self.store = store
        self.executor = executor
        self.loader = loader
        self.clock = clock
        self.tz = settings.tz
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._ledger_doc_ids: dict[str, str] = {}
        self._roles: set[str] = set()  # connection ids holding the executor role
        self._claiming: set[str] = set()
        self._ledger = Ledger(store, ledger_writer or self._write_ledger_doc, jsonl, self.tz)
        self.scheduler = Scheduler(
            store, executor, self, lambda: self.policy, clock, self.tz, lock=self.lock
        )
        self.reconciler = Reconciler(store, executor, self, self.iam_users, clock, lock=self.lock)
        self.handlers = {
            word: functools.partial(fn, self) for word, fn in commands.HANDLERS.items()
        }

    # Policy and identity ------------------------------------------------------------------

    @property
    def policy(self) -> AccessPolicy | None:
        return self.loader.current

    def iam_users(self) -> set[str]:
        policy = self.policy
        return set(policy.principals.values()) if policy else set()

    async def reload_policy(self) -> LoadResult:
        result = await self.loader.load()
        if result.error is not None:
            self.log.warning("policy_load_failed", error=result.error[:300])
        elif result.changed and result.policy is not None:
            self.log.info("policy_loaded", hash=result.policy.hash())
        return result

    def lock(self, lease_id: str) -> asyncio.Lock:
        return self._locks[lease_id]

    # Notifier (scheduler and reconciler post through these) -------------------------------

    async def notify(self, lease: Lease | None, text: str) -> None:
        if lease is not None:
            room, thread = lease.room_id, lease.thread_id
        else:
            rooms = self.room_ids_named(ACCESS_ROOM) or sorted(self.sessions)
            if not rooms:
                self.log.warning("notify_without_room", text=text[:80])
                return
            room, thread = rooms[0], None
        try:
            await self.post(room, text, thread_id=thread)
        except SwitchError as exc:
            self.log.error("notify_failed", room=room, status=exc.status, detail=exc.detail)

    async def ledger(self, lease: Lease) -> None:
        await self.ledger_record(lease)

    async def ledger_record(self, lease: Lease) -> None:
        await self._ledger.record(lease)

    async def _write_ledger_doc(self, room_id: str, content: str) -> None:
        session = await self._ready_session(room_id)
        if session is None:
            raise SwitchError(0, f"no ready connection for room {room_id}", "ledger")
        cid = session.connection_id
        doc_id = self._ledger_doc_ids.get(room_id)
        if doc_id is None:
            refs = await self.ops.list_references(connection_id=cid)
            own = [
                d
                for d in refs.documents
                if d.name == NAME and d.created_by_agent_id == self.creds.id
            ]
            if own:
                doc_id = own[0].id
        if doc_id is None:
            doc_id = await self.ops.create_room_document(
                NAME, DESCRIPTION, INSTRUCTIONS, content, connection_id=cid
            )
        else:
            await self.ops.update_room_document(doc_id, connection_id=cid, content=content)
        self._ledger_doc_ids[room_id] = doc_id

    # Role: "executor" per room, best effort (rooms get roles in Part 4) ---------------------

    async def _try_role(self, room_id: str) -> bool:
        session = self.sessions.get(room_id)
        if session is None:
            return False
        if session.connection_id in self._roles:
            return True
        try:
            await self.ops.assume_role(EXECUTOR_ROLE, connection_id=session.connection_id)
        except SwitchError as exc:
            self.log.info("role_not_assumed", room=room_id, role=EXECUTOR_ROLE, detail=exc.detail)
            return False
        self._roles.add(session.connection_id)
        self.log.info("role_assumed", room=room_id, role=EXECUTOR_ROLE)
        return True

    async def _claim_role(self, room_id: str) -> None:
        if room_id in self._claiming:
            return
        self._claiming.add(room_id)
        try:
            for _ in range(10_000):  # about a week of minutes; a room without roles is fine
                if room_id not in self.sessions or await self._try_role(room_id):
                    return
                await asyncio.sleep(ROLE_RETRY_S)
        finally:
            self._claiming.discard(room_id)

    async def on_room_ready(self, room_id: str) -> None:
        self._spawn(self._claim_role(room_id))

    async def on_control(self, room_id: str, frame: ControlFrame) -> None:
        if isinstance(frame, SubscriptionChanged):
            await self._try_role(room_id)

    # Commands fall through here -----------------------------------------------------------

    async def on_unknown(self, ctx: CommandContext) -> None:
        if "```verdict" in (ctx.msg.body or ""):
            await commands.verdict(self, ctx)
            return
        await self.reply(ctx, fmt.short_help())

    # Steps that touch AWS -----------------------------------------------------------------

    def _render(self, lease: Lease, starts_at: datetime, expires_at: datetime) -> str:
        return serialize(render_policy(lease.proposal, lease.id, starts_at, expires_at))

    async def size_problem(self, lease: Lease, iam_user: str) -> PolicyTooLarge | None:
        """Would this lease push the user's inline policies past AWS's aggregate cap?"""
        now = self.clock()
        doc = self._render(lease, now, now + timedelta(minutes=lease.proposal.duration_minutes))
        try:
            others = await self.executor.list_inline_policies(iam_user)
        except ExecutorError as exc:
            # AWS enforces the cap itself; a failed listing only loses the friendly message.
            self.log.warning("inline_policy_list_failed", user=iam_user, error=str(exc))
            return None
        others.pop(policy_name(lease.id), None)
        try:
            check_user_total(doc, others.values())
        except PolicyTooLarge as exc:
            return exc
        return None

    async def _fail(self, lease: Lease, reason: str, text: str, actor: str | None) -> None:
        failed = self.store.transition(lease.id, APPROVABLE, S.FAILED, reason, actor=actor)
        await self.notify(failed, text)
        await self.ledger_record(failed)

    async def activate(self, lease: Lease, approved_by: str, ctx: CommandContext) -> None:
        """Write the time-boxed policy, then mark the lease ACTIVE. Caller holds the lock."""
        assert lease.iam_user is not None  # noqa: S101 - set at confirm, before any verdict
        actor = ctx.msg.sender
        too_large = await self.size_problem(lease, lease.iam_user)
        if too_large is not None:
            text = fmt.policy_too_large(lease.id, too_large.total, too_large.limit)
            await self._fail(lease, "inline policy size cap", text, actor)
            return
        now = self.clock()
        expires = now + timedelta(minutes=lease.proposal.duration_minutes)
        doc = self._render(lease, now, expires)
        name = policy_name(lease.id)
        try:
            await self.executor.apply(lease.iam_user, name, doc)
        except ExecutorError as exc:
            self.log.error("apply_failed", lease=lease.id, error=str(exc))
            with contextlib.suppress(ExecutorError):
                await self.executor.revoke(lease.iam_user, name)
            await self._fail(
                lease,
                f"apply failed: {exc}",
                fmt.failed(lease.id, f"couldn't write the IAM policy ({exc})"),
                actor,
            )
            return
        active = self.store.transition(
            lease.id,
            APPROVABLE,
            S.ACTIVE,
            f"approved by {approved_by}",
            actor=actor,
            final_policy_json=doc,
            starts_at=now,
            expires_at=expires,
            approved_by=approved_by,
        )
        self.log.info("lease_active", lease=lease.id, user=lease.iam_user, expires=expires)
        if ctx.room_id != active.room_id:
            await self.reply(ctx, f"{lease.id} · approval counted; the lease is now ACTIVE.")
        await self.notify(
            active,
            fmt.receipt(
                lease_id=lease.id,
                expires_at=expires,
                approved_by=approved_by,
                approved_at=now,
                policy_name=name,
                iam_user=lease.iam_user,
                tz=self.tz,
            ),
        )
        await self.ledger_record(active)

    async def activate_extension(self, lease: Lease, approved_by: str, ctx: CommandContext) -> None:
        """Re-write the policy with the later expiry. Caller holds the lock."""
        assert lease.iam_user and lease.starts_at and lease.expires_at  # noqa: S101 - ACTIVE
        minutes = lease.pending_extension_minutes or 0
        new_expiry = lease.expires_at + timedelta(minutes=minutes)
        doc = self._render(lease, lease.starts_at, new_expiry)
        try:
            await self.executor.apply(lease.iam_user, policy_name(lease.id), doc)
        except ExecutorError as exc:
            self.log.error("extension_apply_failed", lease=lease.id, error=str(exc))
            self.store.update(lease.id, pending_extension_minutes=None)
            self.store.record_event(lease.id, "extension", f"apply failed: {exc}")
            await self.notify(
                lease,
                fmt.alert(lease.id, f"extension failed ({exc}); the lease ends as before"),
            )
            return
        updated = self.store.update(
            lease.id,
            final_policy_json=doc,
            expires_at=new_expiry,
            pending_extension_minutes=None,
        )
        self.store.record_event(
            lease.id, "extended", f"+{minutes} min approved by {approved_by}", actor=ctx.msg.sender
        )
        await self.notify(
            updated,
            f"{lease.id} · extended by {minutes} min\n"
            + fmt.receipt(
                lease_id=lease.id,
                expires_at=new_expiry,
                approved_by=approved_by,
                approved_at=self.clock(),
                policy_name=policy_name(lease.id),
                iam_user=lease.iam_user,
                tz=self.tz,
            ),
        )
        await self.ledger_record(updated)

    async def revoke_active(
        self, lease: Lease, reason: str, *, actor: str, by: str, announce: bool = True
    ) -> bool:
        """Remove the policy, then mark the lease REVOKED. Caller holds the lock."""
        if lease.iam_user:
            try:
                await self.executor.revoke(lease.iam_user, policy_name(lease.id))
            except ExecutorError as exc:
                self.log.error("revoke_failed", lease=lease.id, error=str(exc))
                await self.notify(
                    lease, fmt.alert(lease.id, f"couldn't remove the policy ({exc}); still ACTIVE")
                )
                return False
        done = self.store.transition(lease.id, [S.ACTIVE], S.REVOKED, reason, actor=actor)
        if announce:
            await self.notify(done, fmt.revoked(done.id, by, self.clock(), self.tz))
        await self.ledger_record(done)
        return True

    # Text ---------------------------------------------------------------------------------

    def approval_text(
        self, lease: Lease, *, source: str, extension_minutes: int | None = None
    ) -> str:
        policy = self.policy
        assert policy is not None  # noqa: S101 - callers check
        now = self.clock()
        if extension_minutes:
            minutes = extension_minutes
            expires = (lease.expires_at or now) + timedelta(minutes=minutes)
            source = "original review"
        else:
            minutes = lease.proposal.duration_minutes
            expires = now + timedelta(minutes=minutes)
        return fmt.approval_request(
            lease_id=lease.id,
            requester=lease.requester_name or "unknown",
            iam_user=lease.iam_user or "unknown",
            statements=lease.proposal.statements,
            minutes=minutes,
            expires_at=expires,
            tz=self.tz,
            risk=lease.risk or "UNKNOWN",
            risk_source=source,
            score=lease.score or 0,
            skeptic_note=False,
            who_may=who_may_approve(lease, policy, self.known_agent_ids),
            extension=bool(extension_minutes),
        )

    def status_line(self, lease: Lease) -> str:
        parts = [f"{lease.id} · {lease.state}"]
        if lease.requester_name:
            parts.append(f"for {lease.requester_name}")
        if lease.state is S.ACTIVE and lease.expires_at:
            parts.append(f"until {fmt.fmt_time(lease.expires_at, self.tz)}")
        elif lease.risk:
            parts.append(f"risk {lease.risk}")
        parts.append(fmt.describe_access(lease.proposal.statements))
        return " · ".join(parts)
