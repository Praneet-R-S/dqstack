"""A small in-process rate limiter for sign-in, sign-up and password reset.

Account lockout protects one account from guessing; this protects the server from
one address trying many accounts. It is per process, which is enough for a single
server; a multi-server deployment should put the same limits in the reverse proxy
(documented in deploy/README.md).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException


class Limiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window_s: int) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > window_s:
                q.popleft()
            if len(q) >= limit:
                wait = int(window_s - (now - q[0])) + 1
                raise HTTPException(status_code=429,
                                    detail=f"Too many attempts. Wait {wait} seconds and try again.")
            q.append(now)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = Limiter()
