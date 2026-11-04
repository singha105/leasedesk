"""Spike: register an agent with POST /agents, save its key (mode 600), set addressing.

    uv run python scripts/spikes/register.py                       # leasedesk-probe
    uv run python scripts/spikes/register.py --name leasedesk-probe --overwrite
    uv run python scripts/spikes/register.py addressing --name leasedesk-probe --mode anyone

Prints the agent id, never the key. A taken name is a 409; --overwrite issues a NEW key and
replaces the integration profile, so the old key stops working.
"""

from __future__ import annotations

import time

import typer
from _common import ROOT, client, load_agent, record, registration_key, save_agent, show

app = typer.Typer(add_completion=False, no_args_is_help=False)


def set_addressing(name: str, mode: str) -> None:
    agent = load_agent(name)
    body = {"agent_id": agent.id, "addressing": mode}
    with client(agent.api_key) as c:
        r = c.post(f"/agents/{agent.id}/ops/update_agent_detail", json=body)
    record("update_agent_detail", r, body)
    typer.echo(f"update_agent_detail addressing={mode}: {show(r)}")
    if r.status_code != 200:
        raise typer.Exit(1)


@app.callback(invoke_without_command=True)
def register(
    ctx: typer.Context,
    name: str = typer.Option("leasedesk-probe", help="Agent name ^[a-z0-9][a-z0-9._-]*$"),
    description: str = typer.Option("LeaseDesk protocol probe (Part 1 spikes)"),
    connector_type: str = typer.Option("leasedesk-python", help="Free-form label"),
    connection_model: str = typer.Option(
        "always_on", help="always_on | session_addressable | auto_session | session_passive"
    ),
    overwrite: bool = typer.Option(False, help="Re-register a taken name (issues a new key)"),
    addressing: str = typer.Option("anyone", help="owner_only | owner_and_owner_agents | anyone"),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    if overwrite:
        typer.confirm(
            f"--overwrite issues a NEW api key for {name}; the saved key stops working. Continue?",
            abort=True,
        )
    body = {
        "name": name,
        "description": description,
        "connector_type": connector_type,
        "integration_profile": {
            "connection_model": connection_model,
            "message_exchange": True,
            "pre_invocation_mediation": [],
            "post_invocation_mediation": [],
            "event_reporting": [],
        },
    }
    if overwrite:
        body["overwrite"] = True
    with client(registration_key()) as c:
        r = c.post("/agents", json=body)
    record("register", r, body)

    if r.status_code == 409:
        typer.echo(f"409: name '{name}' is taken. {show(r)}")
        typer.echo("Not overwriting. Re-run with --overwrite to issue a new key (old one dies).")
        raise typer.Exit(1)
    if r.status_code not in (200, 201):
        typer.echo(f"registration failed: {show(r)}")
        raise typer.Exit(1)

    data = r.json()
    if "id" not in data or "api_key" not in data:
        typer.echo(f"unexpected response shape (keys: {sorted(data)}); not saving")
        raise typer.Exit(1)
    path = save_agent(
        name,
        {
            "name": name,
            "id": data["id"],
            "api_key": data["api_key"],
            "registered_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "response_keys": sorted(data),
        },
    )
    typer.echo(f"registered {name}: id={data['id']} (status {r.status_code})")
    typer.echo(f"response keys: {sorted(data)}")
    typer.echo(f"saved key to {path.relative_to(ROOT)} (mode 600)")
    set_addressing(name, addressing)


@app.command("addressing")
def addressing_cmd(
    name: str = typer.Option("leasedesk-probe"),
    mode: str = typer.Option("anyone"),
) -> None:
    """Only set addressing on an already registered agent."""
    set_addressing(name, mode)


if __name__ == "__main__":
    app()
