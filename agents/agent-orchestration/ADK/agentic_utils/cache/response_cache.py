"""
Thread-safe, TTL-based in-memory cache for LLM responses.
No external dependencies — stdlib only (hashlib, re, threading, time).
"""

import hashlib
import re
import threading
import time
from typing import Any

_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")

# Queries containing these terms embed real-time / session-specific data
# and must never be cached.
# Note: anomal\w* consumes the full word (anomaly/anomalies) so no trailing \b needed.
_REALTIME_TERMS = re.compile(
    r"\banomal\w*"
    r"|\blive\s*(stat|test|metric|dashboard)\b"
    r"|\bcurrent\s+(cpu|memory|disk|usage)\b"
    r"|\bright\s+now\b"
    r"|\blatest\s+alert\b"
    r"|\bsre\s+alert\b"
    r"|\bcost\s+suggestion\b"
    r"|\bdoppelio\s+stat\b"
    r"|\btest.?run\b"
    r"|\bload.?test\s+(running|progress|status)\b"
    r"|\bshow\s+live\b",
    re.IGNORECASE,
)


class ResponseCache:
    """
    TTL-keyed in-memory cache.

    Entries expire after `default_ttl` seconds. Eviction is lazy (checked on
    get). Thread-safe via a single stdlib Lock — works from both sync and
    async contexts (unlike asyncio.Lock).
    """

    def __init__(self, default_ttl: int = 300):
        self._store: dict[str, tuple[Any, float]] = {}
        self._lock = threading.Lock()
        self.default_ttl = default_ttl
        self.hits = 0
        self.misses = 0

    @staticmethod
    def normalize_query(query: str) -> str:
        """Lowercase, strip punctuation, collapse whitespace."""
        q = _PUNCT_RE.sub(" ", query.lower())
        return _WS_RE.sub(" ", q).strip()

    @staticmethod
    def is_realtime_query(query: str) -> bool:
        """Return True if the query contains real-time/live data indicators."""
        return bool(_REALTIME_TERMS.search(query))

    @staticmethod
    def make_key(text: str) -> str:
        """SHA-256 hex digest — use for long/variable strings like resource blobs."""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def get(self, key: str) -> Any | None:
        """Return cached value or None if missing/expired."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.misses += 1
                return None
            value, expires_at = entry
            if time.monotonic() > expires_at:
                del self._store[key]
                self.misses += 1
                return None
            self.hits += 1
            return value

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        """Store a value with optional per-entry TTL override."""
        expires_at = time.monotonic() + (ttl if ttl is not None else self.default_ttl)
        with self._lock:
            self._store[key] = (value, expires_at)

    def evict_expired(self) -> int:
        """Remove all expired entries proactively. Returns count removed."""
        now = time.monotonic()
        with self._lock:
            dead = [k for k, (_, exp) in self._store.items() if now > exp]
            for k in dead:
                del self._store[k]
        return len(dead)

    def stats(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {
                "size": len(self._store),
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / total, 3) if total else 0.0,
            }
