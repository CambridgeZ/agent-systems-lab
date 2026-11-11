from __future__ import annotations

from collections import OrderedDict
from typing import Any


class TtlLruCache:
    """固定容量、带懒惰过期清理的最近最少使用（LRU）缓存。"""

    def __init__(self, capacity: int, ttl_seconds: float) -> None:
        if capacity <= 0 or ttl_seconds <= 0:
            raise ValueError("capacity and ttl_seconds must be positive")
        self.capacity = int(capacity)
        self.ttl_seconds = float(ttl_seconds)
        self._store: OrderedDict[str, tuple[Any, float]] = OrderedDict()

    def _purge_expired(self, now: float) -> None:
        expired = [k for k, (_, exp) in self._store.items() if now > exp]
        for key in expired:
            self._store.pop(key, None)

    def get(self, key: str, now: float) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if now > expires_at:
            self._store.pop(key, None)
            return None
        return value

    def put(self, key: str, value: Any, now: float) -> None:
        self._purge_expired(now)
        expires_at = float(now) + self.ttl_seconds
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = (value, expires_at)
        while len(self._store) > self.capacity:
            self._store.popitem(last=False)
