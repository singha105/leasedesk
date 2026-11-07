"""keymaster's command grammar: the only words it acts on. Case-insensitive; the command is the
first word after the mention. Anything else in a message, including instructions inside a
lease's summary or justification, is data and never executed.

    submit + lease block · confirm <id> · approve <id> · deny <id> <reason> · revoke <id>
    extend <id> <minutes> · status [id] · leases · whoami · freeze · unfreeze
    reload-policy · help · (a verdict block for a known lease)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from leasedesk.common import format as fmt
from leasedesk.common.fences import FenceError, extract_blocks, extract_one
from leasedesk.common.ids import normalize_lease_id
from leasedesk.common.schemas import (
    AccessPolicy,
    LeaseProposal,
    LeaseVerdict,
    ReviewRequest,
    proposal_hash,
)
from leasedesk.keymaster.rules import (
    approval_satisfied,
    can_approve,
    required_approvals,
    validate_proposal,
    who_may_approve,
)
from leasedesk.keymaster.store import (
    PENDING,
    Lease,
    LeaseState,
)
from leasedesk.switch.agent import CommandContext
from leasedesk.switch.errors import SwitchError

if TYPE_CHECKING:
    from leasedesk.keymaster.service import Keymaster

S = LeaseState
Handler = Callable[["Keymaster", CommandContext], Awaitable[None]]
SECURITY_GROUP = "security"
REVIEWER_ROLE = "reviewer"
REVIEWER_NAME = "sentinel"


def _errors(exc: ValidationError) -> list[str]:
    out = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "lease"
        out.append(f"{loc}: {err['msg']}")
    return out


def _sender_name(km: Keymaster, ctx: CommandContext) -> str:
    pol = km.policy
    named = pol.name_for(ctx.msg.sender) if pol else None
    return named or ctx.msg.sender_name or "someone"


async def _is_agent(km: Keymaster, ctx: CommandContext) -> bool:
    """Whether the sender is an agent. A sender keymaster can't place yet may be an agent
    invited after it connected (no frame announces that), so the room is re-read first."""
    if km.is_agent(ctx.msg.sender, ctx.msg.sender_name):
        return True
    await km.refresh_agents(ctx.room_id)
    return km.is_agent(ctx.msg.sender, ctx.msg.sender_name)


async def _lease_arg(km: Keymaster, ctx: CommandContext, usage: str) -> Lease | None:
    token = ctx.args.split()[0] if ctx.args.split() else ""
    lease_id = normalize_lease_id(token)
    if lease_id is None:
        await km.reply(ctx, f"Usage: @keymaster {usage}")
        return None
    lease = km.store.get(lease_id)
    if lease is None:
        await km.reply(ctx, f"I don't know a lease called {lease_id}.")
    return lease


def _in_security(policy: AccessPolicy, sender: str) -> bool:
    a = policy.approver(sender)
    return a is not None and SECURITY_GROUP in a.groups


# submit -------------------------------------------------------------------------------------


async def submit(km: Keymaster, ctx: CommandContext) -> None:
    policy = km.policy
    if km.store.get_flag("frozen"):
        await km.reply(
            ctx, "LeaseDesk is frozen: new leases are blocked until @keymaster unfreeze."
        )
        return
    if policy is None:
        await km.reply(ctx, "No access policy is loaded, so I can't accept leases yet.")
        return
    try:
        block = extract_one(ctx.msg.body or "", "lease")
        proposal = LeaseProposal.model_validate(block.data)
    except FenceError as exc:
        await km.reply(ctx, fmt.proposal_error([str(exc)]))
        return
    except ValidationError as exc:
        await km.reply(ctx, fmt.proposal_error(_errors(exc)))
        return
    problems = validate_proposal(proposal, policy, dev_mode=km.settings.dev_mode)
    if problems:
        await km.reply(ctx, fmt.proposal_error(problems))
        return
    lease = km.store.create_lease(
        proposal,
        submitted_by=ctx.msg.sender,
        submitted_by_name=ctx.msg.sender_name,
        room_id=ctx.room_id,
        thread_id=ctx.thread_root,
    )
    by = ctx.msg.sender_name or "someone"
    await km.reply(ctx, fmt.submitted(lease.id, by, proposal.requester_hint, proposal, km.tz))
    await km.ledger_record(lease)


# confirm and verdicts -----------------------------------------------------------------------


async def confirm(km: Keymaster, ctx: CommandContext) -> None:
    lease = await _lease_arg(km, ctx, "confirm <lease id>")
    policy = km.policy
    if lease is None or policy is None:
        return
    async with km.lock(lease.id):
        lease = km.store.require(lease.id)
        if lease.state is not S.AWAITING_CONFIRM:
            await km.reply(ctx, fmt.head(lease.id, lease.state, "nothing to confirm."))
            return
        if await _is_agent(km, ctx):
            await km.reply(ctx, "Only the person who needs the access can confirm a lease.")
            return
        iam_user = policy.principals.get(ctx.msg.sender)
        name = _sender_name(km, ctx)
        if iam_user is None:
            await km.reply(
                ctx,
                f"{name}, you're not in access-policy principals, so I can't attach a lease to "
                "you. Ask security to add your id (@keymaster whoami shows it).",
            )
            return
        size_problem = await km.size_problem(lease, iam_user)
        if size_problem is not None:
            done = km.store.transition(
                lease.id,
                [S.AWAITING_CONFIRM],
                S.CANCELLED,
                "inline policy size cap",
                actor=ctx.msg.sender,
            )
            await km.reply(
                ctx,
                fmt.policy_too_large(lease.id, S.CANCELLED, size_problem.total, size_problem.limit),
            )
            await km.ledger_record(done)
            return
        lease = km.store.transition(
            lease.id,
            [S.AWAITING_CONFIRM],
            S.UNDER_REVIEW,
            f"confirmed by {name}",
            actor=ctx.msg.sender,
            requester=ctx.msg.sender,
            requester_name=name,
            iam_user=iam_user,
            review_requested_at=km.clock(),
        )
        review = ReviewRequest(
            lease_id=lease.id,
            proposal=lease.proposal,
            policy_hash=policy.hash(),
            iam_user=iam_user,
        )
        text = fmt.review_request(lease.id, name, review.model_dump_json())
        try:
            await km.deliver(
                ctx.room_id,
                text,
                roles=[REVIEWER_ROLE],
                names=[REVIEWER_NAME],
                thread_id=lease.thread_id,
            )
        except SwitchError as exc:
            # The lease stays UNDER_REVIEW; the review timeout cancels it if nothing answers.
            km.log.error("review_request_undelivered", lease=lease.id, detail=exc.detail)
        await km.ledger_record(lease)
    fake = km.settings.dev_fake_verdict
    # Dev-only stand-in for sentinel; __main__ refuses it in real mode, and so does this.
    if fake is not None and km.settings.aws_mode == "mock":
        await km.post(ctx.room_id, fmt.dev_fake_verdict(lease.id, fake), thread_id=lease.thread_id)
        verdict = LeaseVerdict(
            kind="lease_verdict",
            version=1,
            lease_id=lease.id,
            risk=fake,
            score=0,
            findings=[],
            aws_validation="skipped",
            proposal_hash=proposal_hash(lease.proposal),
        )
        await apply_verdict(km, verdict, source="dev fake verdict")


async def verdict(km: Keymaster, ctx: CommandContext) -> None:
    """A verdict block. Who sent it is checked before the block is even parsed."""
    pinned = km.settings.sentinel_id
    if pinned and ctx.msg.sender != pinned:
        km.log.warning("verdict_from_wrong_sender", sender=ctx.msg.sender)
        await km.reply(ctx, "Verdict ignored: only sentinel's verdicts count.")
        return
    if not pinned:
        # A display name can be copied by anyone, so outside dev mode only the pinned id counts.
        if not km.settings.dev_mode:
            await km.reply(
                ctx, "Verdict ignored: LEASEDESK_SENTINEL_ID isn't set, so no verdict counts."
            )
            return
        if not await _is_agent(km, ctx):
            await km.reply(ctx, "Verdict ignored: verdicts come from the review agent.")
            return
    try:
        blocks = [b for b in extract_blocks(ctx.msg.body or "") if b.kind == "verdict"]
        if len(blocks) != 1:
            raise FenceError("send exactly one ```verdict block")
        v = LeaseVerdict.model_validate(blocks[0].data)
    except FenceError as exc:
        await km.reply(ctx, f"Verdict not accepted: {exc}")
        return
    except ValidationError as exc:
        await km.reply(ctx, "Verdict not accepted: " + "; ".join(_errors(exc)[:4]))
        return
    if not pinned:
        km.log.warning("verdict_sender_not_pinned", sender=ctx.msg.sender, lease=v.lease_id)
    lease = km.store.get(v.lease_id)
    if lease is not None and v.proposal_hash != proposal_hash(lease.proposal):
        km.log.warning("verdict_hash_mismatch", sender=ctx.msg.sender, lease=v.lease_id)
        await km.reply(ctx, f"Verdict ignored: it is for a different proposal than {v.lease_id}.")
        return
    await apply_verdict(km, v, source=ctx.msg.sender_name or "reviewer", ctx=ctx)


async def apply_verdict(
    km: Keymaster, v: LeaseVerdict, *, source: str, ctx: CommandContext | None = None
) -> None:
    policy = km.policy
    lease = km.store.get(v.lease_id)
    if lease is None or policy is None:
        if ctx is not None:
            await km.reply(ctx, f"Verdict ignored: I don't know {v.lease_id}.")
        return
    async with km.lock(lease.id):
        lease = km.store.require(lease.id)
        if lease.state is not S.UNDER_REVIEW:
            if ctx is not None:
                await km.reply(ctx, f"Verdict ignored: {lease.id} is {lease.state}.")
            return
        now = km.clock()
        common: dict[str, Any] = {
            "risk": v.risk,
            "score": v.score,
            "verdict_json": v.model_dump_json(),
        }
        if v.risk == "BLOCK":
            lease = km.store.transition(
                lease.id, [S.UNDER_REVIEW], S.BLOCKED, f"blocked by {source}", **common
            )
            await km.notify(lease, fmt.blocked(lease.id, list(v.findings)))
            await km.ledger_record(lease)
            return
        target = S.ESCALATED if v.risk == "HIGH" else S.AWAITING_APPROVAL
        lease = km.store.transition(
            lease.id,
            [S.UNDER_REVIEW],
            target,
            f"{v.risk} verdict from {source}",
            approval_requested_at=now,
            **common,
        )
        request = km.approval_text(lease, source=source)
        if target is S.ESCALATED:
            need = required_approvals(lease.risk, policy)
            assert need is not None and need.group and need.room  # noqa: S101 - HIGH always has them
            await km.notify(lease, fmt.escalated(lease.id, need.count, need.group, need.room))
            rooms = km.room_ids_named(need.room)
            if rooms:
                km.store.update(lease.id, approval_room_id=rooms[0])
                await km.post(rooms[0], request)
            else:
                await km.notify(
                    lease,
                    f"I'm not in {need.room}, so nobody can approve this yet. Invite me there.",
                )
        else:
            await km.notify(lease, request)
        await km.ledger_record(lease)


# approve, deny ------------------------------------------------------------------------------


async def approve(km: Keymaster, ctx: CommandContext) -> None:
    lease = await _lease_arg(km, ctx, "approve <lease id>")
    policy = km.policy
    if lease is None or policy is None:
        return
    async with km.lock(lease.id):
        lease = km.store.require(lease.id)
        extending = lease.state is S.ACTIVE and lease.pending_extension_minutes is not None
        if lease.state not in (S.AWAITING_APPROVAL, S.ESCALATED) and not extending:
            await km.reply(ctx, fmt.head(lease.id, lease.state, "nothing to approve."))
            return
        agents = set(km.known_agent_ids)
        if await _is_agent(km, ctx):
            agents.add(ctx.msg.sender)
        check = can_approve(
            lease,
            ctx.msg.sender,
            policy,
            agents,
            room_name=ctx.room_name,
            approver_name=ctx.msg.sender_name,
        )
        who_may = who_may_approve(lease, policy, km.known_agent_ids)
        if not check.ok:
            await km.reply(ctx, fmt.refusal(lease.id, lease.state, check.reason, who_may))
            return
        started = lease.approval_requested_at or lease.created_at
        if km.clock() >= started + timedelta(minutes=policy.approval_timeout_minutes):
            await km.reply(
                ctx,
                fmt.refusal(
                    lease.id,
                    lease.state,
                    f"The approval window ({policy.approval_timeout_minutes} min) has closed.",
                    "nobody now; submit it again",
                ),
            )
            return
        approver = policy.approver(ctx.msg.sender)
        name = approver.name if approver else _sender_name(km, ctx)
        added = km.store.add_approval(
            lease.id,
            round=lease.round,
            approver_id=ctx.msg.sender,
            approver_name=name,
            decision="approve",
            room_id=ctx.room_id,
            message_id=ctx.msg.message_id,
        )
        if not added:
            await km.reply(
                ctx, fmt.head(lease.id, lease.state, f"{name}'s approval is already counted.")
            )
            return
        approvals = km.store.approvals(lease.id, lease.round)
        if not approval_satisfied(lease, approvals, policy):
            need = required_approvals(lease.risk, policy)
            have = len({a.approver_id for a in approvals if a.decision == "approve"})
            await km.reply(
                ctx,
                fmt.approval_counted(lease.id, lease.state, name, have, need.count if need else 1),
            )
            return
        names = ", ".join(sorted({a.approver_name for a in approvals if a.decision == "approve"}))
        if extending:
            await km.activate_extension(lease, names, ctx)
        else:
            await km.activate(lease, names, ctx)


async def deny(km: Keymaster, ctx: CommandContext) -> None:
    lease = await _lease_arg(km, ctx, "deny <lease id> <reason>")
    policy = km.policy
    if lease is None or policy is None:
        return
    reason = ctx.args.split(None, 1)[1].strip() if len(ctx.args.split(None, 1)) > 1 else ""
    if not reason:
        await km.reply(ctx, f"Please give a reason: @keymaster deny {lease.id} <reason>")
        return
    approver = policy.approver(ctx.msg.sender)
    if approver is None or await _is_agent(km, ctx):
        await km.reply(
            ctx,
            fmt.head(lease.id, lease.state, "deny not counted: only listed approvers can deny."),
        )
        return
    async with km.lock(lease.id):
        lease = km.store.require(lease.id)
        if lease.state is S.ACTIVE and lease.pending_extension_minutes:
            km.store.update(lease.id, pending_extension_minutes=None)
            km.store.record_event(lease.id, "extension", f"denied by {approver.name}: {reason}")
            await km.reply(
                ctx,
                fmt.head(lease.id, lease.state, f"extension denied by {approver.name}: {reason}"),
            )
            return
        if lease.state not in (S.AWAITING_APPROVAL, S.ESCALATED, S.UNDER_REVIEW):
            await km.reply(ctx, fmt.head(lease.id, lease.state, "nothing to deny."))
            return
        km.store.add_approval(
            lease.id,
            round=lease.round,
            approver_id=ctx.msg.sender,
            approver_name=approver.name,
            decision="deny",
            room_id=ctx.room_id,
            message_id=ctx.msg.message_id,
        )
        done = km.store.transition(
            lease.id,
            [S.AWAITING_APPROVAL, S.ESCALATED, S.UNDER_REVIEW],
            S.DENIED,
            f"denied by {approver.name}: {reason}",
            actor=ctx.msg.sender,
        )
        await km.notify(done, fmt.denied(done.id, approver.name, reason))
        if ctx.room_id != done.room_id:
            await km.reply(ctx, fmt.denied(done.id, approver.name, reason))
        await km.ledger_record(done)


# revoke, extend -----------------------------------------------------------------------------


async def revoke(km: Keymaster, ctx: CommandContext) -> None:
    lease = await _lease_arg(km, ctx, "revoke <lease id>")
    policy = km.policy
    if lease is None or policy is None:
        return
    sender = ctx.msg.sender
    allowed = not await _is_agent(km, ctx) and (
        sender == lease.requester or policy.approver(sender) is not None
    )
    if not allowed:
        await km.reply(
            ctx,
            fmt.head(
                lease.id, lease.state, "only the requester, an approver or security can revoke it."
            ),
        )
        return
    name = _sender_name(km, ctx)
    async with km.lock(lease.id):
        lease = km.store.require(lease.id)
        if lease.state is S.ACTIVE:
            await km.revoke_active(lease, f"revoked by {name}", actor=sender, by=name)
        elif lease.state in PENDING:
            done = km.store.transition(
                lease.id, list(PENDING), S.CANCELLED, f"withdrawn by {name}", actor=sender
            )
            await km.notify(done, fmt.cancelled(done.id, f"withdrawn by {name}"))
            await km.ledger_record(done)
        else:
            await km.reply(ctx, fmt.head(lease.id, lease.state, "already over; nothing to revoke."))


async def extend(km: Keymaster, ctx: CommandContext) -> None:
    lease = await _lease_arg(km, ctx, "extend <lease id> <minutes>")
    policy = km.policy
    if lease is None or policy is None:
        return
    parts = ctx.args.split()
    minutes = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    if minutes <= 0:
        await km.reply(ctx, f"Usage: @keymaster extend {lease.id} <minutes>")
        return
    async with km.lock(lease.id):
        lease = km.store.require(lease.id)
        if ctx.msg.sender != lease.requester:
            await km.reply(
                ctx, fmt.head(lease.id, lease.state, "only the requester can ask to extend it.")
            )
            return
        if lease.state is not S.ACTIVE or lease.starts_at is None or lease.expires_at is None:
            await km.reply(
                ctx, fmt.head(lease.id, lease.state, "only an ACTIVE lease can be extended.")
            )
            return
        total = int((lease.expires_at - lease.starts_at).total_seconds() // 60) + minutes
        if total > policy.max_duration_minutes:
            await km.reply(
                ctx,
                fmt.head(
                    lease.id,
                    lease.state,
                    f"can't extend: {total} min in total would pass the "
                    f"{policy.max_duration_minutes}-minute maximum.",
                ),
            )
            return
        lease = km.store.update(
            lease.id,
            round=lease.round + 1,
            pending_extension_minutes=minutes,
            approval_requested_at=km.clock(),
        )
        km.store.record_event(
            lease.id, "extension", f"requested +{minutes} min", actor=ctx.msg.sender
        )
        await km.notify(
            lease, km.approval_text(lease, source="extension", extension_minutes=minutes)
        )


# status and admin ---------------------------------------------------------------------------


async def status(km: Keymaster, ctx: CommandContext) -> None:
    token = ctx.args.split()[0] if ctx.args.split() else ""
    if token:
        lease = await _lease_arg(km, ctx, "status [lease id]")
        if lease is not None:
            await km.reply(ctx, km.status_line(lease))
        return
    policy = km.policy
    active = len(km.store.in_states(S.ACTIVE))
    pending = len(km.store.in_states(*PENDING))
    frozen = "yes" if km.store.get_flag("frozen") else "no"
    phash = policy.hash() if policy else "none loaded"
    await km.reply(
        ctx,
        f"keymaster · policy {phash} · {active} active, {pending} pending · frozen: {frozen} · "
        f"AWS mode: {km.settings.aws_mode}",
    )


async def leases(km: Keymaster, ctx: CommandContext) -> None:
    await km.reply(ctx, fmt.leases_list([km.status_line(x) for x in km.store.recent(50)]))


async def whoami(km: Keymaster, ctx: CommandContext) -> None:
    await km.reply(ctx, fmt.whoami(ctx.msg.sender_name or "you", ctx.msg.sender))


async def _security_only(km: Keymaster, ctx: CommandContext, what: str) -> str | None:
    policy = km.policy
    if policy is None or await _is_agent(km, ctx) or not _in_security(policy, ctx.msg.sender):
        await km.reply(ctx, f"Only the {SECURITY_GROUP} group can {what}.")
        return None
    return _sender_name(km, ctx)


async def freeze(km: Keymaster, ctx: CommandContext) -> None:
    name = await _security_only(km, ctx, "freeze LeaseDesk")
    if name is None:
        return
    km.store.set_flag("frozen", True)
    count = 0
    for lease in km.store.in_states(S.ACTIVE):
        async with km.lock(lease.id):
            fresh = km.store.require(lease.id)
            if fresh.state is S.ACTIVE and await km.revoke_active(
                fresh, f"freeze by {name}", actor=ctx.msg.sender, by=name, announce=True
            ):
                count += 1
    await km.reply(ctx, fmt.frozen(name, count))


async def unfreeze(km: Keymaster, ctx: CommandContext) -> None:
    name = await _security_only(km, ctx, "unfreeze LeaseDesk")
    if name is None:
        return
    km.store.set_flag("frozen", False)
    await km.reply(ctx, fmt.unfrozen(name))


async def reload_policy(km: Keymaster, ctx: CommandContext) -> None:
    result = await km.reload_policy()
    if result.error is not None:
        await km.reply(
            ctx, fmt.policy_warning(result.error, km.policy.hash() if km.policy else None)
        )
        return
    assert result.policy is not None  # noqa: S101
    word = "changed" if result.changed else "unchanged"
    await km.reply(ctx, f"access-policy reloaded: {word} (hash {result.policy.hash()}).")


async def help_(km: Keymaster, ctx: CommandContext) -> None:
    await km.reply(ctx, fmt.help_text())


HANDLERS: dict[str, Handler] = {
    "submit": submit,
    "confirm": confirm,
    "verdict": verdict,
    "approve": approve,
    "deny": deny,
    "revoke": revoke,
    "extend": extend,
    "status": status,
    "leases": leases,
    "whoami": whoami,
    "freeze": freeze,
    "unfreeze": unfreeze,
    "reload-policy": reload_policy,
    "help": help_,
}
