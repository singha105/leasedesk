"""Register LeaseDesk agents with Switch.

    uv run python -m leasedesk.switch.register keymaster sentinel [--overwrite] [--prompt]

Idempotent: an agent whose saved key still works is left alone. A taken name with no saved
key is an error unless --overwrite (which issues a new key and kills the old one). Then the
owner sets addressing to "anyone" through the Gateway (an agent can't change its own), and the
agent's sender id is written to .env as LEASEDESK_<NAME>_ID once its room_join is visible.
Keys are never printed or logged.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
import typer
from pydantic import SecretStr

from leasedesk.common.envfile import set_env_value
from leasedesk.common.logging import configure_logging, get_logger
from leasedesk.config import Settings
from leasedesk.switch.credentials import AgentCredentials, load_credentials, save_credentials
from leasedesk.switch.errors import SwitchError
from leasedesk.switch.gateway_admin import GatewayAdmin, admin_login
from leasedesk.switch.ops import OpsClient

log = get_logger(__name__)

DESCRIPTIONS = {
    "keymaster": "LeaseDesk keymaster: the only agent that writes time-boxed IAM policies",
    "sentinel": "LeaseDesk sentinel: deterministic and AWS checks on every lease proposal",
}


class RegisterError(Exception):
    pass


@dataclass
class RegisterResult:
    name: str
    id: str
    status: Literal["created", "existing", "overwritten"]
    participant_id: str | None
    addressing: str | None


def describe(r: RegisterResult) -> str:
    pid = r.participant_id or "not visible yet (invite it to a room, then re-run)"
    return (
        f"{r.name}: {r.status}, id={r.id}, addressing={r.addressing or 'unchanged'}, "
        f"sender id={pid}"
    )


async def _saved_key_works(base: str, creds: AgentCredentials) -> bool:
    async with OpsClient(base, creds) as ops:
        try:
            await ops.list_ops()
        except SwitchError as exc:
            if exc.status in (401, 403):
                return False
            raise
    return True


async def _find_sender_id(base: str, creds: AgentCredentials) -> str | None:
    async with OpsClient(base, creds) as ops:
        try:
            rooms = await ops.list_rooms()
            for room in rooms:
                for thread in await ops.read_context(room.room_id, limit=100):
                    for e in [thread.root, *thread.replies]:
                        if e.kind == "room_join" and e.sender_name == creds.name and e.sender:
                            return e.sender
        except SwitchError as exc:
            log.warning("sender_id_lookup_failed", agent=creds.name, detail=exc.detail)
    return None


async def register_agent(
    name: str,
    *,
    settings: Settings,
    overwrite: bool,
    admin: GatewayAdmin | None,
    env_path: Path,
) -> RegisterResult:
    base = (settings.switch_base_url or "").rstrip("/")
    if not base:
        raise RegisterError("SWITCH_BASE_URL is not set in .env")
    status: Literal["created", "existing", "overwritten"]
    creds: AgentCredentials | None = None
    if not overwrite:
        try:
            creds = load_credentials(name, settings.secrets_dir)
        except FileNotFoundError:
            creds = None
        if creds is not None and not await _saved_key_works(base, creds):
            raise RegisterError(
                f"the saved key for {name} is rejected; re-run with --overwrite to issue a new one"
            )
    if creds is not None:
        status = "existing"
    else:
        reg_key = settings.switch_registration_key
        if reg_key is None or not reg_key.get_secret_value():
            raise RegisterError("SWITCH_REGISTRATION_KEY is empty in .env")
        body: dict[str, object] = {
            "name": name,
            "description": DESCRIPTIONS.get(name, f"LeaseDesk {name}"),
            "connector_type": "leasedesk-python",
            "integration_profile": {
                "connection_model": "always_on",
                "message_exchange": True,
                "pre_invocation_mediation": [],
                "post_invocation_mediation": [],
                "event_reporting": [],
            },
        }
        if overwrite:
            body["overwrite"] = True
        async with httpx.AsyncClient(base_url=base, timeout=20) as http:
            r = await http.post(
                "/agents",
                json=body,
                headers={"Authorization": f"Bearer {reg_key.get_secret_value()}"},
            )
        if r.status_code == 409:
            raise RegisterError(
                f"{name} is already registered but no saved key was found; re-run with "
                "--overwrite to issue a new key (the old key stops working)"
            )
        if r.status_code not in (200, 201):
            raise RegisterError(f"registering {name} failed: HTTP {r.status_code}")
        data = r.json()
        creds = AgentCredentials(name=name, id=data["id"], api_key=SecretStr(data["api_key"]))
        save_credentials(creds, settings.secrets_dir)
        status = "overwritten" if overwrite else "created"
    log.info("agent_registered", agent=name, id=creds.id, status=status)

    addressing: str | None = None
    if admin is not None:
        await admin.set_addressing(creds.id, "anyone")
        addressing = "anyone"

    sender = await _find_sender_id(base, creds)
    if sender:
        set_env_value(env_path, f"LEASEDESK_{name.upper().replace('-', '_')}_ID", sender)
    return RegisterResult(name, creds.id, status, sender, addressing)


async def _main(names: list[str], overwrite: bool, prompt: bool, addressing: bool) -> int:
    settings = Settings()
    results: list[RegisterResult] = []
    admin: GatewayAdmin | None = None
    try:
        if addressing:
            email, password = admin_login(prompt)
            admin = await GatewayAdmin(
                settings.switch_base_url or "", email=email, password=password
            ).__aenter__()
        for name in names:
            results.append(
                await register_agent(
                    name,
                    settings=settings,
                    overwrite=overwrite,
                    admin=admin,
                    env_path=Path(".env"),
                )
            )
    except RegisterError as exc:
        typer.echo(f"error: {exc}", err=True)
        return 1
    finally:
        if admin is not None:
            await admin.__aexit__(None, None, None)
    for r in results:
        typer.echo(describe(r))
    return 0


app = typer.Typer(add_completion=False)


@app.command()
def main(
    names: list[str] = typer.Argument(..., help="Agent names, e.g. keymaster sentinel"),  # noqa: B008
    overwrite: bool = typer.Option(False, help="Re-register: issues NEW keys (old ones die)"),
    prompt: bool = typer.Option(False, help="Type the Gateway admin login instead"),
    addressing: bool = typer.Option(True, help="Set addressing to anyone via the Gateway"),
) -> None:
    configure_logging("WARNING")
    if overwrite:
        typer.confirm(f"--overwrite issues NEW keys for {', '.join(names)}. Continue?", abort=True)
    raise typer.Exit(asyncio.run(_main(names, overwrite, prompt, addressing)))


if __name__ == "__main__":
    app()
