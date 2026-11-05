"""Run keymaster: ``python -m leasedesk.keymaster`` (or ``make run-keymaster``).

Exit codes: 0 stopped (SIGINT/SIGTERM), 1 unexpected, 2 configuration refused,
3 taken over by another keymaster process, 4 Switch rejected the API key.

Stopping never revokes anything. Active leases keep their policies, AWS still ends them at
expiry on its own, and the next start catches up from SQLite.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from leasedesk.common.logging import configure_logging, get_logger
from leasedesk.config import Settings
from leasedesk.keymaster.executor import Executor, ExecutorError, make_executor
from leasedesk.keymaster.policy import PolicyLoader, file_fetcher
from leasedesk.keymaster.service import Keymaster
from leasedesk.keymaster.store import LeaseStore
from leasedesk.switch.connection import ExitReason, RoomConnection
from leasedesk.switch.credentials import AgentCredentials, load_credentials
from leasedesk.switch.errors import SwitchError
from leasedesk.switch.ops import OpsClient

log = get_logger(__name__)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_TAKEN_OVER = 3
EXIT_AUTH = 4
EXIT_CODES = {
    ExitReason.STOPPED: EXIT_OK,
    ExitReason.TAKEN_OVER: EXIT_TAKEN_OVER,
    ExitReason.AUTH_FAILED: EXIT_AUTH,
}
POLICY_REFRESH_S = 300.0
POLICY_RETRY_S = 5.0


def config_problem(settings: Settings) -> str | None:
    if settings.dev_fake_verdict is not None and settings.aws_mode == "real":
        return "LEASEDESK_DEV_FAKE_VERDICT is dev-only and is refused in real AWS mode."
    if not settings.switch_base_url:
        return "SWITCH_BASE_URL isn't set (see .env.example)."
    return None


def document_fetcher(km: Callable[[], Keymaster], name: str) -> Callable[[], Awaitable[str]]:
    """Read the person-written library document ``name`` through any ready room connection.

    Only documents with ``created_by_agent_id`` null count, so no agent (including a
    prompt-injected one) can write the policy keymaster enforces.
    """

    async def fetch() -> str:
        agent = km()
        ready = [s for s in agent.sessions.values() if s.ready.is_set()]
        if not ready:
            raise LookupError("not connected to any room yet")
        found: dict[str, str] = {}  # document id -> connection id to load it through
        try:
            for session in ready:
                refs = await agent.ops.list_references(connection_id=session.connection_id)
                for d in refs.documents:
                    if d.name == name and d.created_by_agent_id is None:
                        found.setdefault(d.id, session.connection_id)
            if not found:
                raise LookupError(f"no person-written document named {name} is attached to me")
            if len(found) > 1:
                raise LookupError(f"more than one document is named {name}; keep one")
            ((doc_id, connection_id),) = found.items()
            loaded = await agent.ops.load_internal_documents([doc_id], connection_id=connection_id)
        except SwitchError as exc:
            raise LookupError(f"couldn't read {name}: {exc.detail}") from exc
        if not loaded:
            raise LookupError(f"{name} came back empty")
        return loaded[0].content

    return fetch


async def keep_policy_fresh(km: Keymaster) -> None:
    """Retry until the first policy loads, then re-read it every 5 minutes."""
    while True:
        await km.reload_policy()
        await asyncio.sleep(POLICY_RETRY_S if km.policy is None else POLICY_REFRESH_S)


async def serve(
    settings: Settings,
    *,
    creds: AgentCredentials,
    ops: OpsClient,
    executor: Executor,
    store: LeaseStore,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    connection_factory: Callable[..., Any] = RoomConnection,
) -> int:
    holder: list[Keymaster] = []
    if settings.policy_source == "file":
        fetch = file_fetcher(settings.policy_file)
    else:
        fetch = document_fetcher(lambda: holder[0], settings.policy_document_name)
    km = Keymaster(
        settings,
        creds,
        ops,
        store=store,
        executor=executor,
        loader=PolicyLoader(fetch),
        clock=clock,
        own_id=settings.keymaster_id,
        jsonl=Path("logs/ledger.jsonl"),
        connection_factory=connection_factory,
    )
    holder.append(km)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, km.request_stop)

    async def startup_reconcile() -> None:
        try:
            report = await km.reconciler.reconcile()
            log.info("startup_reconcile", removed=len(report.removed), failed=len(report.failed))
        except Exception:
            log.exception("startup_reconcile_failed")
        await km.reconciler.run()

    background = [
        asyncio.ensure_future(keep_policy_fresh(km)),
        asyncio.ensure_future(km.scheduler.run()),
        asyncio.ensure_future(startup_reconcile()),
    ]
    log.info("keymaster_started", aws_mode=settings.aws_mode, policy=settings.policy_source)
    try:
        reason = await km.run_until_stopped()
    finally:
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
    log.info("keymaster_stopped", reason=str(reason), revoked_on_stop=0)
    return EXIT_CODES.get(reason, EXIT_ERROR)


def keymaster_credentials(settings: Settings) -> AgentCredentials:
    """``.secrets/agents/keymaster.json``, written by ``make register``."""
    return load_credentials("keymaster", settings.secrets_dir)


async def _run(settings: Settings) -> int:
    try:
        creds = keymaster_credentials(settings)
    except (OSError, ValueError) as exc:
        log.error("credentials_missing", hint="run `make register` first", error=str(exc))
        return EXIT_CONFIG
    try:
        executor = make_executor(settings)
    except ExecutorError as exc:
        log.error("executor_unavailable", error=str(exc))
        return EXIT_CONFIG
    store = LeaseStore(settings.db_path, clock=lambda: datetime.now(UTC))
    try:
        async with OpsClient(settings.switch_base_url or "", creds) as ops:
            return await serve(settings, creds=creds, ops=ops, executor=executor, store=store)
    finally:
        store.close()


def main(settings: Settings | None = None) -> int:
    settings = settings or Settings()
    configure_logging(settings.log_level)
    problem = config_problem(settings)
    if problem is not None:
        log.error("config_refused", problem=problem)
        return EXIT_CONFIG
    if settings.dev_fake_verdict is not None:
        for _ in range(3):
            log.warning(
                "DEV_FAKE_VERDICT_ON",
                verdict=settings.dev_fake_verdict,
                note="keymaster invents review verdicts. Mock demos only.",
            )
    with contextlib.suppress(KeyboardInterrupt):
        return asyncio.run(_run(settings))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
