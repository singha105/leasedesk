"""Write one secret into .env without echoing it, then clear the clipboard.

    uv run python scripts/set_env_secret.py SWITCH_REGISTRATION_KEY            # hidden prompt
    uv run python scripts/set_env_secret.py SWITCH_REGISTRATION_KEY --clipboard  # from pbpaste

Prints only the variable name and the value's length. .env is kept at mode 600.
"""

from __future__ import annotations

import getpass
import re
import subprocess
from pathlib import Path

import typer

ENV = Path(__file__).resolve().parent.parent / ".env"
app = typer.Typer(add_completion=False)


@app.command()
def main(
    name: str = typer.Argument(..., help="Variable name, e.g. SWITCH_REGISTRATION_KEY"),
    clipboard: bool = typer.Option(False, help="Read the value from the macOS clipboard"),
) -> None:
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
        raise typer.BadParameter("name must look like AN_ENV_VAR")
    if clipboard:
        value = subprocess.run(["pbpaste"], capture_output=True, text=True, check=True).stdout
    else:
        value = getpass.getpass(f"{name} (input hidden): ")
    value = value.strip()
    if not value or any(c in value for c in "\n\r\"'"):
        typer.echo("refusing: empty value, or it contains newlines or quotes")
        raise typer.Exit(1)

    lines = ENV.read_text().splitlines() if ENV.exists() else []
    out, replaced = [], False
    for line in lines:
        if line.startswith(f"{name}="):
            out.append(f"{name}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{name}={value}")
    ENV.touch(mode=0o600, exist_ok=True)
    ENV.chmod(0o600)
    ENV.write_text("\n".join(out) + "\n")
    ENV.chmod(0o600)
    if clipboard:
        subprocess.run(["pbcopy"], input="", text=True, check=True)
    typer.echo(
        f"{name} set in .env ({len(value)} chars, mode 600)"
        + (", clipboard cleared" if clipboard else "")
    )


if __name__ == "__main__":
    app()
