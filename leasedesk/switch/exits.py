"""Process exit codes shared by every agent and by scripts/demo/supervise.py.

0 stopped (SIGINT/SIGTERM), 1 unexpected, 2 configuration refused, 3 taken over by another
process with the same agent key, 4 Switch rejected the API key. The supervisor restarts 1 and
signals, and never 2, 3 or 4: restarting can't fix those.
"""

from __future__ import annotations

from leasedesk.switch.connection import ExitReason

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFIG = 2
EXIT_TAKEN_OVER = 3
EXIT_AUTH = 4
EXIT_CODES = {
    ExitReason.STOPPED: EXIT_OK,
    ExitReason.TAKEN_OVER: EXIT_TAKEN_OVER,
    ExitReason.AUTH_FAILED: EXIT_AUTH,
}
FINAL = {
    EXIT_CONFIG: "configuration refused (see the log line above)",
    EXIT_TAKEN_OVER: "another process took over this agent's connection",
    EXIT_AUTH: "Switch rejected the API key (run `make register`)",
}
