"""make doctor: check tools, env, Switch health and credential file modes. Prints no secrets."""

from __future__ import annotations

import shutil
import stat
import sys
from pathlib import Path

import httpx

from leasedesk.config import Settings

ROOT = Path(__file__).resolve().parent.parent
OK, WARN, FAIL = "ok  ", "warn", "FAIL"
problems = 0


def report(status: str, msg: str) -> None:
    global problems
    if status == FAIL:
        problems += 1
    print(f"[{status}] {msg}")


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def main() -> int:
    for tool in ("uv", "docker", "aws", "git"):
        report(OK if shutil.which(tool) else WARN, f"{tool} on PATH")

    env = ROOT / ".env"
    if env.exists():
        m = mode_of(env)
        report(OK if m == 0o600 else FAIL, f".env mode {oct(m)} (want 0o600)")
    else:
        report(WARN, ".env missing (cp .env.example .env && chmod 600 .env)")

    secrets = ROOT / ".secrets"
    if secrets.exists():
        for p in [secrets, *secrets.rglob("*")]:
            want = 0o700 if p.is_dir() else 0o600
            m = mode_of(p)
            if m != want:
                report(FAIL, f"{p.relative_to(ROOT)} mode {oct(m)} (want {oct(want)})")
        report(OK, ".secrets/ checked")

    settings = Settings()
    report(OK, f"AWS mode: {settings.aws_mode}")
    key = settings.switch_registration_key
    has_key = key is not None and bool(key.get_secret_value())
    report(OK if has_key else WARN, "SWITCH_REGISTRATION_KEY " + ("set" if has_key else "not set"))
    if settings.switch_base_url:
        url = settings.switch_base_url.rstrip("/") + "/health"
        try:
            r = httpx.get(url, timeout=3)
            report(OK if r.status_code == 200 else FAIL, f"GET {url} -> {r.status_code}")
        except httpx.HTTPError as exc:
            report(FAIL, f"GET {url} -> {type(exc).__name__}")
    else:
        report(WARN, "SWITCH_BASE_URL not set (run scripts/spikes/health.py)")

    print("doctor: " + ("all good" if problems == 0 else f"{problems} problem(s)"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
