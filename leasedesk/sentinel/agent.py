"""The sentinel agent: answers keymaster's review requests with a verdict, and anyone else with
advice. No model anywhere; ``review`` is deterministic.

Only a review request whose sender is the pinned ``LEASEDESK_KEYMASTER_ID`` gets a ``verdict``
block (sent to keymaster by name, falling back to a plain ``@keymaster`` post). The same request
from anyone else, and every ``@sentinel check``, gets an ``advisory`` block that decides
nothing. keymaster, in turn, only counts verdicts from the pinned sentinel whose
``proposal_hash`` matches the lease it holds.
"""

from __future__ import annotations

import dataclasses
import functools
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from leasedesk.common.fences import FenceError, extract_one
from leasedesk.common.policy import PolicyLoader
from leasedesk.common.schemas import Finding, ReviewRequest, canonical_hash
from leasedesk.config import Settings
from leasedesk.sentinel.analyzers.aws import AwsChecks
from leasedesk.sentinel.catalog import CatalogLoader
from leasedesk.sentinel.context import MOTO_ACCOUNT, ReviewContext
from leasedesk.sentinel.findings import by_severity, make
from leasedesk.sentinel.ratelimit import RateLimiter
from leasedesk.sentinel.review import Review, review
from leasedesk.sentinel.store import ReviewStore
from leasedesk.sentinel.text import (
    advisory_message,
    clean,
    corrected_message,
    explain_text,
    help_text,
    rules_text,
    slow_down,
    verdict_message,
)
from leasedesk.sentinel.trust import request_mode
from leasedesk.switch.agent import CommandContext, SwitchAgent
from leasedesk.switch.connection import RoomConnection
from leasedesk.switch.credentials import AgentCredentials
from leasedesk.switch.errors import SwitchError
from leasedesk.switch.models import DomainEvent, MessagePayload
from leasedesk.switch.ops import OpsClient

REVIEWER_ROLE = "reviewer"
KEYMASTER_NAME = "keymaster"
CACHE_SIZE = 256
NOT_KEYMASTER = "only keymaster's review requests get a verdict"
ON_REQUEST = "a check on request"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _has_review(body: str) -> bool:
    return "```review" in body or '"lease_review_request"' in body


# keymaster's review request opens with "LS-n · confirmed by …" on its own first line (Switch may
# put "@sentinel " in front). keymaster echoes proposal text in other messages, so a review block
# anywhere else in a keymaster message is data, not a request.
_HEADER = re.compile(r"\s*(?:@\S+\s+)?(\S+) · confirmed by ")


def _headed(body: str, lease_id: str) -> bool:
    m = _HEADER.match(body)
    return m is not None and m.group(1) == lease_id


class Sentinel(SwitchAgent):
    def __init__(
        self,
        settings: Settings,
        creds: AgentCredentials,
        ops: OpsClient,
        *,
        policy_loader: PolicyLoader,
        catalog_loader: CatalogLoader,
        store: ReviewStore,
        aws: AwsChecks | None = None,
        limiter: RateLimiter | None = None,
        clock: Callable[[], datetime] = _utcnow,
        own_id: str | None = None,
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
        self.role = REVIEWER_ROLE
        self.policy_loader = policy_loader
        self.catalog_loader = catalog_loader
        self.store = store
        self.aws = aws
        self.limiter = limiter or RateLimiter()
        self.clock = clock
        self._cache: OrderedDict[str, Review] = OrderedDict()
        commands: dict[str, Callable[[CommandContext], Awaitable[None]]] = {
            "check": self._check,
            "explain": self._explain,
            "rules": self._rules,
            "help": self._help,
        }
        self.handlers = {
            word: functools.partial(self._limited, fn) for word, fn in commands.items()
        }

    # Documents and context ----------------------------------------------------------------

    async def reload(self) -> None:
        result = await self.policy_loader.load()
        if result.error is not None:
            self.log.warning("policy_load_failed", error=result.error[:300])
        elif result.changed and result.policy is not None:
            self.log.info("policy_loaded", hash=result.policy.hash())
        before = self.catalog_loader.current
        catalog = await self.catalog_loader.load()
        if self.catalog_loader.last_error is not None:
            self.log.warning("catalog_load_failed", error=self.catalog_loader.last_error[:300])
        elif catalog is not None and (before is None or before.hash() != catalog.hash()):
            self.log.info(
                "catalog_loaded", hash=catalog.hash(), crown_jewels=len(catalog.crown_jewels())
            )

    def policy_hash(self) -> str:
        policy = self.policy_loader.current
        return policy.hash() if policy else ""

    def context(self, *, gating: bool = False) -> ReviewContext:
        """The paid AWS check runs only for keymaster's own review requests, never for a
        person's check or a forged request, so nobody else can spend money."""
        real = self.settings.aws_mode == "real"
        return ReviewContext(
            policy=self.policy_loader.current,
            catalog=self.catalog_loader.current,
            account_id=self.settings.aws_account_id or MOTO_ACCOUNT,
            aws_mode=self.settings.aws_mode,
            paid_checks=real and self.settings.enable_paid_checks and gating,
        )

    async def _review(self, raw: Any, *, gating: bool = False) -> Review:
        ctx = self.context(gating=gating)
        key = f"{canonical_hash(raw)}|{ctx.key()}"
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
            self.log.info("review_cache_hit", risk=hit.scored.risk)
            return hit
        result = await review(raw, ctx, aws=self.aws)
        # An AWS check that was skipped in real mode may work next time, so don't keep it.
        if not (ctx.aws_mode == "real" and result.aws_validation == "skipped"):
            self._cache[key] = result
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        return result

    async def _policy_notes(self, want: str) -> list[Finding]:
        """Reload once if keymaster asked against another access-policy; note it if still off."""
        if self.policy_hash() == want:
            return []
        await self.policy_loader.load()
        have = self.policy_hash()
        if not have or have == want:
            return []  # no policy at all is a policy-unavailable BLOCK from the review itself
        self.log.warning("policy_hash_mismatch", want=clean(want[:16]), have=have)
        return [
            make(
                "policy-hash-mismatch",
                None,
                f"keymaster asked with access-policy {clean(want[:16])}; sentinel judged against "
                f"{have}, the one it could load.",
            )
        ]

    # Rate limit -----------------------------------------------------------------------------

    async def _allow(self, ctx: CommandContext) -> bool:
        keymaster = self.settings.keymaster_id
        if keymaster and ctx.msg.sender == keymaster:
            return True
        decision = self.limiter.check(ctx.msg.sender)
        if decision == "warn":
            self.log.warning("rate_limited", sender=ctx.msg.sender)
            await self.reply(ctx, slow_down())
        return decision == "ok"

    async def _limited(
        self, fn: Callable[[CommandContext], Awaitable[None]], ctx: CommandContext
    ) -> None:
        if await self._allow(ctx):
            await fn(ctx)

    # Incoming -------------------------------------------------------------------------------

    async def on_unknown(self, ctx: CommandContext) -> None:
        if not await self._allow(ctx):
            return
        if _has_review(ctx.msg.body or ""):
            await self._review_request(ctx)
        else:
            await self.reply(ctx, help_text())

    async def on_unaddressed(self, room_id: str, ev: DomainEvent, msg: MessagePayload) -> None:
        """keymaster's request may arrive as a plain post. Other unaddressed text is chatter."""
        keymaster = self.settings.keymaster_id
        if not keymaster or msg.sender != keymaster or not _has_review(msg.body or ""):
            return
        ctx = CommandContext(
            room_id=room_id,
            event=ev,
            msg=msg,
            word="review",
            args="",
            thread_root=msg.thread_id or msg.message_id,
            room_name=self.room_names.get(room_id, ""),
        )
        await self._review_request(ctx)

    async def _review_request(self, ctx: CommandContext) -> None:
        gating = request_mode(ctx.msg.sender, self.settings) == "gating"
        try:
            req = ReviewRequest.model_validate(extract_one(ctx.msg.body or "", "review").data)
        except FenceError as exc:
            await self.reply(ctx, f"Review request not readable: {clean(str(exc))}")
            return
        except ValidationError as exc:
            problems = [
                f"{'.'.join(str(x) for x in e['loc']) or 'request'}: {e['msg']}"
                for e in exc.errors()[:3]
            ]
            await self.reply(ctx, "Review request not readable: " + clean("; ".join(problems)))
            return
        if gating and not _headed(ctx.msg.body or "", req.lease_id):
            self.log.warning("review_request_refused", lease=clean(req.lease_id[:24]))
            return
        notes = await self._policy_notes(req.policy_hash) if gating else []
        result = await self._review(req.proposal.model_dump(mode="json"), gating=gating)
        if notes:
            result = dataclasses.replace(result, findings=by_severity([*result.findings, *notes]))
        if gating and result.proposal_hash is not None:
            review_id = req.lease_id
            self.store.save(review_id, result, lease_id=req.lease_id, gating=True)
            try:
                path = await self.deliver(
                    ctx.room_id,
                    verdict_message(req.lease_id, result),
                    names=[KEYMASTER_NAME],
                    thread_id=ctx.thread_root,
                )
            except SwitchError as exc:
                self.log.error("verdict_undelivered", lease=req.lease_id, detail=exc.detail)
                return
            self.log.info("verdict_sent", lease=req.lease_id, risk=result.scored.risk, path=path)
        else:
            review_id = self._advisory_id(result)
            self.store.save(review_id, result, lease_id=req.lease_id, gating=False)
            await self.reply(
                ctx, advisory_message(review_id, result, lease_id=req.lease_id, why=NOT_KEYMASTER)
            )
            self.log.info("advisory_sent", lease=req.lease_id, sender=ctx.msg.sender)
        await self._offer_corrected(ctx, review_id, result)

    async def _offer_corrected(self, ctx: CommandContext, review_id: str, result: Review) -> None:
        text = corrected_message(review_id, result)
        if text is not None:
            await self.reply(ctx, text)

    @staticmethod
    def _advisory_id(result: Review) -> str:
        digest = result.proposal_hash or "sha256:unreadable"
        return "C-" + digest.removeprefix("sha256:")[:8]

    # Commands -------------------------------------------------------------------------------

    async def _check(self, ctx: CommandContext) -> None:
        try:
            block = extract_one(ctx.msg.body or "", "lease")
        except FenceError as exc:
            await self.reply(
                ctx, f"Send @sentinel check with one ```lease``` block ({clean(str(exc))})."
            )
            return
        result = await self._review(block.data)
        review_id = self._advisory_id(result)
        self.store.save(review_id, result, lease_id=None, gating=False)
        await self.reply(ctx, advisory_message(review_id, result, lease_id=None, why=ON_REQUEST))
        await self._offer_corrected(ctx, review_id, result)

    async def _explain(self, ctx: CommandContext) -> None:
        token = ctx.args.split()[0].strip(".,;:!?") if ctx.args.split() else ""
        if not token:
            await self.reply(ctx, "Say @sentinel explain <id>, for example explain LS-0007.")
            return
        stored = self.store.get(token)
        if stored is None:
            await self.reply(ctx, f"I have no review for {clean(token[:24])}.")
            return
        await self.reply(ctx, explain_text(stored.review_id, stored))

    async def _rules(self, ctx: CommandContext) -> None:
        await self.reply(ctx, rules_text())

    async def _help(self, ctx: CommandContext) -> None:
        await self.reply(ctx, help_text())
