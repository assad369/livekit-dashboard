"""Simple module-level TTL cache for agent dispatch results.

Keyed by ``LiveKitClient.cache_key`` — the project id plus a fingerprint of
the URL and API key. Keying by URL alone would let two projects pointed at
the same server with different credentials read each other's dispatches, and
would not notice a credential change. Lives at module scope so it survives
across requests.
"""

import time
from typing import Any, Dict, List

TTL: float = 30.0        # seconds before a full re-fetch
CONCURRENCY: int = 10    # max parallel ListDispatch calls

_store: Dict[str, Dict[str, Any]] = {}


def get(key: str) -> Dict[str, Any]:
    """Return the cache entry for *key*, creating it if absent."""
    return _store.setdefault(key, {"data": [], "latency": 0.0, "ts": 0.0})


def set(key: str, data: List, latency: float) -> None:  # noqa: A001
    """Overwrite the cache entry for *key* with fresh data."""
    _store[key] = {"data": data, "latency": latency, "ts": time.monotonic()}


def invalidate(key: str) -> None:
    """Force the next read to bypass the cache for *key*."""
    if key in _store:
        _store[key]["ts"] = 0.0


def is_fresh(key: str) -> bool:
    """Return True if the cached value for *key* is still within TTL."""
    return time.monotonic() - _store.get(key, {}).get("ts", 0.0) < TTL


def clear() -> None:
    """Drop every cached entry. Used by tests and on project deletion."""
    _store.clear()
