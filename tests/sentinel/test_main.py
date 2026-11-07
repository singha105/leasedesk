"""python -m leasedesk.sentinel: config refusals, the reviewer role, SIGINT exits 0."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any

import yaml

from leasedesk.config import Settings
from leasedesk.sentinel.__main__ import main, serve
from leasedesk.sentinel.store import ReviewStore
from leasedesk.switch.exits import EXIT_CONFIG, EXIT_OK
from leasedesk.switch.models import RoomSummary

from ..keymaster.conftest import Clock, policy_dict
from ..keymaster.test_main import IdleConnection, StubOps
from .conftest import CATALOG
from .harness import CREDS


def settings_for(tmp: Path, **env: Any) -> Settings:
    pfile, cfile = tmp / "access-policy.yaml", tmp / "resource-catalog.md"
    pfile.write_text(yaml.safe_dump(policy_dict()))
    cfile.write_text("```yaml\n" + yaml.safe_dump(CATALOG) + "```\n")
    base: dict[str, Any] = {
        "SWITCH_BASE_URL": "http://switch.test",
        "LEASEDESK_POLICY_SOURCE": "file",
        "LEASEDESK_POLICY_FILE": pfile,
        "LEASEDESK_CATALOG_FILE": cfile,
        "LEASEDESK_STATE_DIR": tmp / ".state",
        "LEASEDESK_SECRETS_DIR": tmp / ".secrets",
        "LEASEDESK_SENTINEL_DB": tmp / "sentinel.db",
    }
    return Settings(_env_file=None, **{**base, **env})  # type: ignore[call-arg]


def test_missing_base_url_exits_2(tmp_path: Path) -> None:
    assert main(settings_for(tmp_path, SWITCH_BASE_URL=None)) == EXIT_CONFIG


def test_real_mode_needs_the_account_id(tmp_path: Path) -> None:
    assert main(settings_for(tmp_path, LEASEDESK_AWS_MODE="real")) == EXIT_CONFIG


def test_missing_credentials_exit_2(tmp_path: Path) -> None:
    assert main(settings_for(tmp_path)) == EXIT_CONFIG


async def test_reviewer_role_claimed_and_sigint_exits_0(tmp_path: Path, clock: Clock) -> None:
    settings = settings_for(tmp_path)
    ops = StubOps([RoomSummary(room_id="r1", name="Discord: access-desk")])
    task = asyncio.ensure_future(
        serve(
            settings,
            creds=CREDS,
            ops=ops,  # type: ignore[arg-type]
            store=ReviewStore(settings.sentinel_db_path, clock=clock),
            clock=clock,
            connection_factory=IdleConnection,
        )
    )
    for _ in range(100):
        if ops.roles:
            break
        await asyncio.sleep(0.02)
    os.kill(os.getpid(), signal.SIGINT)
    assert await asyncio.wait_for(task, 5) == EXIT_OK
    assert ops.roles == ["reviewer"]
