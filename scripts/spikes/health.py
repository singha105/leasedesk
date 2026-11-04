"""Spike: find the Switch agent bridge base URL by probing GET /health on candidate addresses.

Candidates come from --url, SWITCH_BASE_URL, ports Docker publishes for containers whose name
contains "switch", local listening ports, and a few defaults. Prints a verdict per candidate and
the bridge base URL it found. Sends no credentials.

    uv run python scripts/spikes/health.py [--url http://localhost:8000] [--all]
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import httpx
import typer

app = typer.Typer(add_completion=False)
DEFAULT_PORTS = [8000, 8001, 8080, 8443, 3000, 9000]
CONSOLE_PORTS = (
    Path.home() / "Library/Application Support/switchdash/local-switch-server/ports.json"
)


def docker_ports() -> list[tuple[str, int]]:
    if not shutil.which("docker"):
        return []
    try:
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    found = []
    for line in out.splitlines():
        name, _, ports = line.partition("\t")
        for m in re.finditer(r"(?:[\d.]+|\[::\]):(\d+)->", ports):
            found.append((name, int(m.group(1))))
    return found


def listening_ports() -> list[int]:
    if not shutil.which("lsof"):
        return []
    out = subprocess.run(
        ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    ).stdout
    return sorted({int(m.group(1)) for m in re.finditer(r":(\d+) \(LISTEN\)", out)})


def classify(r: httpx.Response) -> str:
    ctype = r.headers.get("content-type", "")
    if r.status_code == 200 and "json" in ctype:
        return "BRIDGE (healthy JSON)"
    if r.status_code == 200 and "html" in ctype:
        return "web page (Gateway or messaging app, not the bridge)"
    if r.status_code == 200:
        return f"200 {ctype or 'no content-type'} (maybe bridge)"
    if r.status_code == 401:
        return "401: right host, wrong path"
    return f"{r.status_code}"


@app.command()
def main(
    url: list[str] = typer.Option([], help="Extra base URL(s) to probe"),  # noqa: B008
    show_all: bool = typer.Option(False, "--all", help="Also print non-answering candidates"),
) -> None:
    candidates: dict[str, str] = {}
    for u in url:
        candidates[u.rstrip("/")] = "--url"
    if env := os.environ.get("SWITCH_BASE_URL"):
        candidates[env.rstrip("/")] = "SWITCH_BASE_URL"
    if CONSOLE_PORTS.exists():
        # A Console-run server picks free ports at install and records them here.
        for name, port in json.loads(CONSOLE_PORTS.read_text()).items():
            candidates.setdefault(f"http://localhost:{port}", f"console ports.json:{name}")
    for name, port in docker_ports():
        candidates.setdefault(f"http://localhost:{port}", f"docker:{name}")
    for port in DEFAULT_PORTS + listening_ports():
        candidates.setdefault(f"http://localhost:{port}", "local port")

    bridges = []
    with httpx.Client(timeout=2, follow_redirects=False, verify=False) as client:  # noqa: S501
        for base, source in candidates.items():
            try:
                r = client.get(f"{base}/health")
            except httpx.HTTPError as exc:
                if show_all:
                    typer.echo(f"  {base:<28} {source:<28} {type(exc).__name__}")
                continue
            verdict = classify(r)
            body = (
                r.text[:120].replace("\n", " ")
                if "json" in r.headers.get("content-type", "")
                else ""
            )
            typer.echo(f"  {base:<28} {source:<28} {verdict} {body}")
            if verdict.startswith("BRIDGE"):
                bridges.append(base)

    if bridges:
        typer.echo(f"\nBridge base URL: {bridges[0]}")
        if len(bridges) > 1:
            typer.echo(f"(also healthy: {', '.join(bridges[1:])})")
    else:
        typer.echo("\nNo bridge found. Is the server running in Switch Console (and Docker up)?")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
