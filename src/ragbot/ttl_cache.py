"""A small thread-safe TTL + LRU cache, shared by the answer cache and the SQL result cache.

Values are returned as stored, never copied here; callers that hand a cached object to code that may
mutate it copy it themselves. Expiry uses a monotonic clock (injectable for tests)."""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Hashable, Optional


class TTLCache:
    def __init__(self, maxsize: int = 256, clock: Callable[[], float] = time.monotonic):
        self.maxsize = maxsize
        self.clock = clock
        self._d: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Hashable) -> Optional[Any]:
        with self._lock:
            hit = self._d.get(key)
            if hit is None:
                return None
            expires, value = hit
            if self.clock() >= expires:
                del self._d[key]
                return None
            self._d.move_to_end(key)
            return value

    def put(self, key: Hashable, value: Any, ttl: float) -> None:
        if ttl <= 0:
            return
        with self._lock:
            self._d[key] = (self.clock() + ttl, value)
            self._d.move_to_end(key)
            while len(self._d) > self.maxsize:
                self._d.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._d)
