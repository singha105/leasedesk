"""Run sentinel: ``python -m leasedesk.sentinel`` (or ``make run-sentinel``).

Exit codes are the shared ones (leasedesk/switch/exits.py): 0 stopped, 1 unexpected,
2 configuration refused, 3 taken over, 4 Switch rejected the API key.

sentinel reads access-policy and resource-catalog the same way keymaster reads the policy:
person-written library documents (or files with ``LEASEDESK_POLICY_SOURCE=file``), re-read
every 5 minutes. Mock mode makes no AWS call; real mode uses only profile
``leasedesk-sentinel``.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from leasedesk.common.logging import configure_logging, get_logger
from leasedesk.common.policy import PolicyLoader, file_fetcher
from leasedesk.config import Settings
from leasedesk.sentinel.agent import Sentinel
from leasedesk.sentinel.analyzers.aws import AwsChecks, real_client_factory
from leasedesk.sentinel.catalog import CatalogLoader, catalog_file_fetcher
from leasedesk.sentinel.store import ReviewStore
from leasedesk.switch.connection import RoomConnection
from leasedesk.switch.credentials import AgentCredentials, load_credentials
from leasedesk.switch.documents import person_document_fetcher
from leasedesk.switch.exits import EXIT_CODES, EXIT_CONFIG, EXIT_ERROR, EXIT_OK
from leasedesk.switch.ops import OpsClient

log = get_logger(__name__)
REFRESH_S = 300.0
RETRY_S = 5.0
Fetch = Callable[[], Awaitable[str]]


def config_problem(settings: Settings) -> str | None:
    if not settings.switch_base_url:
        return "SWITCH_BASE_URL isn't set (see .env.example)."
    if settings.aws_mode == "real" and not settings.aws_account_id:
        return "LEASEDESK_AWS_ACCOUNT_ID must be set in real mode (the account leases are for)."
    return None


def fetchers(settings: Settings, agent: Callable[[], Sentinel]) -> tuple[Fetch, Fetch]:
    if settings.policy_source == "file":
        return file_fetcher(settings.policy_file), catalog_file_fetcher(settings.catalog_file)
    return (
        person_document_fetcher(agent, settings.policy_document_name),
        person_document_fetcher(agent, settings.catalog_document_name),
    )


async def keep_documents_fresh(sentinel: Sentinel) -> None:
    """Retry every 5 s until both documents load, then re-read them every 5 minutes."""
    while True:
        await sentinel.reload()
        missing = sentinel.policy_loader.current is None or sentinel.catalog_loader.current is None
        await asyncio.sleep(RETRY_S if missing else REFRESH_S)


async def serve(
    settings: Settings,
    *,
    creds: AgentCredentials,
    ops: OpsClient,
    store: ReviewStore,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    connection_factory: Callable[..., Any] = RoomConnection,
    aws: AwsChecks | None = None,
) -> int:
    holder: list[Sentinel] = []
    policy_fetch, catalog_fetch = fetchers(settings, lambda: holder[0])
    sentinel = Sentinel(
        settings,
        creds,
        ops,
        policy_loader=PolicyLoader(policy_fetch),
        catalog_loader=CatalogLoader(catalog_fetch),
        store=store,
        aws=aws,
        clock=clock,
        own_id=settings.sentinel_id,
        connection_factory=connection_factory,
    )
    holder.append(sentinel)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, sentinel.request_stop)
    if not settings.keymaster_id:
        log.warning(
            "keymaster_not_pinned",
            note="LEASEDESK_KEYMASTER_ID isn't set, so every answer is advisory",
        )
    background = [asyncio.ensure_future(keep_documents_fresh(sentinel))]
    log.info(
        "sentinel_started",
        aws_mode=settings.aws_mode,
        paid_checks=sentinel.context(gating=True).paid_checks,  # keymaster's requests only
        documents=settings.policy_source,
    )
    try:
        reason = await sentinel.run_until_stopped()
    finally:
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
    log.info("sentinel_stopped", reason=str(reason))
    return EXIT_CODES.get(reason, EXIT_ERROR)


async def _run(settings: Settings) -> int:
    try:
        creds = load_credentials("sentinel", settings.secrets_dir)
    except (OSError, ValueError) as exc:
        log.error("credentials_missing", hint="run `make register` first", error=str(exc))
        return EXIT_CONFIG
    aws = AwsChecks(real_client_factory(settings)) if settings.aws_mode == "real" else None
    store = ReviewStore(settings.sentinel_db_path, clock=lambda: datetime.now(UTC))
    try:
        async with OpsClient(settings.switch_base_url or "", creds) as ops:
            return await serve(settings, creds=creds, ops=ops, store=store, aws=aws)
    finally:
        store.close()


def main(settings: Settings | None = None) -> int:
    settings = settings or Settings()
    configure_logging(settings.log_level)
    problem = config_problem(settings)
    if problem is not None:
        log.error("config_refused", problem=problem)
        return EXIT_CONFIG
    with contextlib.suppress(KeyboardInterrupt):
        return asyncio.run(_run(settings))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
