"""Ten requests a minute per sender (keymaster is exempt; the agent checks that).

The 11th request in a window gets one "slow down" reply; anything after it in the same
window is dropped without a reply, so a flood can't make sentinel flood the room back.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Literal

LIMIT = 10
WINDOW_S = 60.0
Decision = Literal["ok", "warn", "drop"]


class RateLimiter:
    def __init__(
        self,
        limit: int = LIMIT,
        window_s: float = WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._hits: defaultdict[str, deque[float]] = defaultdict(deque)
        self._warned: set[str] = set()

    def check(self, sender: str) -> Decision:
        now = self._clock()
        hits = self._hits[sender]
        while hits and now - hits[0] >= self.window_s:
            hits.popleft()
        if not hits:
            self._warned.discard(sender)
        if len(hits) < self.limit:
            hits.append(now)
            return "ok"
        if sender in self._warned:
            return "drop"
        self._warned.add(sender)
        return "warn"
