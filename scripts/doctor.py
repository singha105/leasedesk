"""make doctor: check env, Switch, rooms, roles, the governance package and AWS profiles.

Prints `PASS name` or `FAIL name: detail → fix` per check and exits 1 on any FAIL. Reads the
Gateway as the admin person (Console's local login, or --prompt) and Switch as keymaster and
sentinel with their own keys. Prints no secrets. The checks live in leasedesk/doctor.py.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from leasedesk import doctor
from leasedesk.common.logging import configure_logging
from leasedesk.config import Settings

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    configure_logging("WARNING")  # the Switch clients log every call at debug/info
    checks = asyncio.run(doctor.gather(Settings(), ROOT, prompt="--prompt" in sys.argv[1:]))
    text, code = doctor.report(checks)
    print(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
