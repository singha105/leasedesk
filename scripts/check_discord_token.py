"""Check a Discord bot token before pasting it into Switch Console (run this yourself).

    uv run python scripts/check_discord_token.py            # reads the token from the clipboard
    uv run python scripts/check_discord_token.py --prompt   # or type it (hidden)

Prints only facts about the token (its shape, which bot it belongs to, whether Discord accepts
it), never the token itself. The clipboard is left alone so you can paste it into Console next.
"""

from __future__ import annotations

import base64
import binascii
import getpass
import subprocess

import httpx
import typer

APP_ID = "1567400687627796602"  # LeaseDesk Switch
app = typer.Typer(add_completion=False)


def bot_id_from(token: str) -> str | None:
    head = token.split(".", 1)[0]
    try:
        return base64.b64decode(head + "=" * (-len(head) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError):
        return None


@app.command()
def main(prompt: bool = typer.Option(False, "--prompt", help="Type the token instead")) -> None:
    raw = (
        getpass.getpass("Bot token (hidden): ")
        if prompt
        else subprocess.run(["pbpaste"], capture_output=True, text=True, check=False).stdout
    )
    token = raw.strip()
    if raw != token:
        typer.echo("note: the copied text had spaces or newlines around it; Console may keep them")
    if not token:
        raise SystemExit("FAIL: clipboard is empty; copy the token from the Bot tab first")
    parts = token.split(".")
    if len(parts) != 3:
        hint = " (looks like the OAuth2 Client Secret)" if len(token) == 32 else ""
        raise SystemExit(f"FAIL: not a bot token: {len(parts)} part(s), {len(token)} chars{hint}")
    owner = bot_id_from(token)
    if owner != APP_ID:
        raise SystemExit(f"FAIL: token is for bot id {owner}, not LeaseDesk Switch ({APP_ID})")
    r = httpx.get(
        "https://discord.com/api/v10/users/@me",
        headers={"Authorization": f"Bot {token}"},
        timeout=15,
    )
    del token
    if r.status_code != 200:
        raise SystemExit(
            f"FAIL: Discord says HTTP {r.status_code}; the token was reset or is stale. "
            "Reset it once more, copy, and re-run."
        )
    me = r.json()
    typer.echo(f"OK: Discord accepts it, bot {me.get('username')} (id {me.get('id')})")
    typer.echo("Now paste it into the Bot Token field in Switch Console.")


if __name__ == "__main__":
    app()
