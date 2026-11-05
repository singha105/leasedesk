"""Would the stored lease policy allow a request right now? (mock-mode demo helper)

    uv run python -m leasedesk.keymaster.simulate LS-0007 [--action ..] [--resource ..]
        [--prefix exports/2026-10/] [--at 2026-11-04T21:00:00Z]

Evaluates the lease's stored policy, the demo permissions boundary and the time with the small
local evaluator in evaluator.py (not AWS's: it knows Allow/Deny, globs, the boundary and the
DateGreaterThan/DateLessThan/StringEquals/StringLike operators, and fails closed otherwise).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import typer

from leasedesk.common.schemas import LeaseProposal
from leasedesk.config import Settings
from leasedesk.keymaster.evaluator import evaluate
from leasedesk.keymaster.executor import BOUNDARY_FILE
from leasedesk.keymaster.store import LeaseStore


@dataclass
class SimResult:
    decision: Literal["ALLOWED", "DENIED"]
    action: str
    resource: str
    at: datetime
    note: str = ""


def default_request(p: LeaseProposal) -> tuple[str, str, str | None]:
    s = p.statements[0]
    resource = s.resources[0]
    if resource.endswith("/*"):
        resource = resource[:-1] + "invoices.csv"
    return s.actions[0].replace("*", "GetObject"), resource, None


def _boundary(path: Path = BOUNDARY_FILE) -> dict[str, Any] | None:
    if not path.exists():
        return None
    doc: dict[str, Any] = json.loads(path.read_text().replace("<ACCOUNT_ID>", "*"))
    return doc


def simulate_lease(
    store: LeaseStore,
    lease_id: str,
    *,
    at: datetime,
    action: str | None = None,
    resource: str | None = None,
    prefix: str | None = None,
) -> SimResult:
    lease = store.get(lease_id)
    if lease is None:
        return SimResult("DENIED", action or "?", resource or "?", at, f"{lease_id} not found")
    d_action, d_resource, _ = default_request(lease.proposal)
    action, resource = action or d_action, resource or d_resource
    if not lease.final_policy_json:
        return SimResult(
            "DENIED", action, resource, at, f"{lease_id} has no policy ({lease.state})"
        )
    context = {"aws:CurrentTime": at.astimezone(UTC).isoformat()}
    if prefix is not None:
        context["s3:prefix"] = prefix
    docs = [json.loads(lease.final_policy_json)]
    decision = evaluate(docs, _boundary(), action, resource, context)
    return SimResult(decision, action, resource, at, f"lease state {lease.state}")


def main(
    lease_id: str = typer.Argument(..., help="e.g. LS-0007"),
    action: str | None = typer.Option(None),
    resource: str | None = typer.Option(None),
    prefix: str | None = typer.Option(None, help="s3:prefix for ListBucket"),
    at: str | None = typer.Option(None, help="ISO time, default now"),
) -> None:
    settings = Settings()
    when = datetime.fromisoformat(at.replace("Z", "+00:00")) if at else datetime.now(UTC)
    store = LeaseStore(settings.db_path, clock=lambda: datetime.now(UTC))
    r = simulate_lease(store, lease_id, at=when, action=action, resource=resource, prefix=prefix)
    typer.echo(f"{r.decision}  {r.action} on {r.resource} at {r.at.isoformat()} ({r.note})")
    raise typer.Exit(0 if r.decision == "ALLOWED" else 1)


if __name__ == "__main__":
    typer.run(main)
