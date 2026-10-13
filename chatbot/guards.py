"""Protection of the site owner's API bill: who may call, how often, and how much per day."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from urllib.parse import urlparse


class RateLimiter:
    """Sliding window per key (the visitor's IP address). Kept in memory, so it is per process."""

    def __init__(self, limit: int, window_s: float = 60.0) -> None:
        self.limit, self.window = limit, window_s
        self._calls: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> tuple[bool, int]:
        """Returns (allowed, seconds to wait before the next call is allowed)."""
        now = time.monotonic() if now is None else now
        calls = self._calls[key]
        while calls and now - calls[0] >= self.window:
            calls.popleft()
        if len(calls) >= self.limit:
            return False, max(1, int(self.window - (now - calls[0])) + 1)
        calls.append(now)
        if len(self._calls) > 10_000:      # forget idle visitors so memory stays bounded
            for stale in [k for k, v in self._calls.items() if not v or now - v[-1] >= self.window]:
                del self._calls[stale]
        return True, 0


def origin_allowed(origin: str | None, host: str, allowed: list[str]) -> bool:
    """May a page on `origin` call this service?

    No Origin header means a same-origin GET, a server or a command-line tool, not a foreign
    web page. A browser always sends the header on cross-site requests and scripts cannot fake
    it, so this stops other websites from embedding the widget on the owner's bill. It does
    not stop a script outside a browser: the rate limit and the daily budget do that.
    """
    if not origin:
        return True
    if "*" in allowed or origin.rstrip("/") in allowed:
        return True
    return urlparse(origin).netloc == host
