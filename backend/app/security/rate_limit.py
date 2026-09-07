"""In-process sliding-window rate limiter.

Deliberately simple and dependency-free. A single process is the deployment
unit in the reference compose stack; behind multiple replicas, swap
``MemoryRateLimiter`` for a Redis-backed implementation of the same interface.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RateDecision:
    allowed: bool
    remaining: int
    retry_after: int
    limit: int


class MemoryRateLimiter:
    def __init__(self, *, max_buckets: int = 50_000) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._max_buckets = max_buckets

    def check(self, key: str, *, limit: int, window: int) -> RateDecision:
        now = time.monotonic()
        cutoff = now - window
        with self._lock:
            if len(self._hits) > self._max_buckets:
                self._evict(cutoff)
            bucket = self._hits[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int(window - (now - bucket[0])) + 1)
                return RateDecision(False, 0, retry_after, limit)
            bucket.append(now)
            return RateDecision(True, limit - len(bucket), 0, limit)

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)

    def _evict(self, cutoff: float) -> None:
        stale = [key for key, bucket in self._hits.items() if not bucket or bucket[-1] <= cutoff]
        for key in stale:
            del self._hits[key]


limiter = MemoryRateLimiter()
