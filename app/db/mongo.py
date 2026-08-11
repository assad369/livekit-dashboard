"""MongoDB connection lifecycle and index management.

Uses PyMongo's native async client (``AsyncMongoClient``), not Motor — Motor
is EOL and the API surface used here is identical. Async matters: the overview
page fans out concurrent LiveKit calls and the SSE route holds a long-lived
loop, so a blocking driver would stall every other in-flight request.

Degradation contract, relied on by every caller:

* ``MONGODB_URI`` unset      -> ``is_enabled()`` is False; the app runs exactly
                                as it did before Mongo existed (env-var
                                project, env-var auth, JSON-file stores).
* set but unreachable        -> the app still boots; ``get_database()`` returns
                                None and ``get_db()`` raises 503, so pages that
                                need storage show a banner instead of a stack
                                trace.
* ``MONGODB_REQUIRED=true``  -> startup fails loudly instead. Use in production.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)


DEFAULT_DB_NAME = "livekit_dashboard"

# Retention for high-volume collections. Rollups are never expired — they are
# the billing record and are tiny; raw events and metric samples are not.
USAGE_EVENTS_TTL_DAYS = int(os.environ.get("USAGE_EVENTS_TTL_DAYS", "90"))
BANDWIDTH_SAMPLES_TTL_DAYS = int(os.environ.get("BANDWIDTH_SAMPLES_TTL_DAYS", "90"))
AUDIT_TTL_DAYS = int(os.environ.get("AUDIT_TTL_DAYS", "365"))

_client: Any = None
_db: Any = None
_state: dict = {"enabled": False, "connected": False, "error": None, "index_errors": []}


def is_enabled() -> bool:
    """True when a connection string is configured."""
    return bool(os.environ.get("MONGODB_URI", "").strip())


def is_required() -> bool:
    return os.environ.get("MONGODB_REQUIRED", "false").lower() == "true"


def db_name() -> str:
    return os.environ.get("MONGODB_DB", DEFAULT_DB_NAME)


def get_database():
    """Return the database handle, or None when disabled or unreachable."""
    return _db


def health() -> dict:
    """Connection state, for /health/deep and the settings page."""
    return {
        "enabled": _state["enabled"],
        "connected": _state["connected"],
        "error": _state["error"],
        "index_errors": list(_state["index_errors"]),
        "db": db_name() if _state["enabled"] else None,
    }


async def connect() -> None:
    """Connect, ping, and ensure indexes. Never raises unless Mongo is required."""
    global _client, _db

    _state.update({"enabled": is_enabled(), "connected": False, "error": None})
    if not is_enabled():
        logger.info("MongoDB not configured (MONGODB_URI unset) — running without persistence")
        return

    from pymongo import AsyncMongoClient

    uri = os.environ["MONGODB_URI"]
    timeout_ms = int(os.environ.get("MONGODB_TIMEOUT_MS", "5000"))
    try:
        _client = AsyncMongoClient(
            uri,
            serverSelectionTimeoutMS=timeout_ms,
            connectTimeoutMS=timeout_ms,
            tz_aware=True,
            appname="livekit-dashboard",
        )
        await _client.admin.command("ping")
        _db = _client[db_name()]
        _state["connected"] = True
        logger.info("MongoDB connected (db=%s)", db_name())
    except Exception as exc:
        _client = None
        _db = None
        _state["error"] = str(exc)
        if is_required():
            raise RuntimeError(f"MONGODB_REQUIRED=true but connection failed: {exc}") from exc
        logger.error("MongoDB unavailable, continuing without it: %s", exc)
        return

    await ensure_indexes(_db)


async def disconnect() -> None:
    global _client, _db
    if _client is not None:
        try:
            await _client.close()
        except Exception as exc:  # pragma: no cover - shutdown best effort
            logger.warning("error closing MongoDB client: %s", exc)
    _client = None
    _db = None
    _state.update({"connected": False})


async def ensure_indexes(db) -> None:
    """Create every index the app relies on.

    Each is attempted independently and failures are recorded rather than
    raised — a missing index must not stop the app from booting. The one that
    matters most is ``usage_events (project_id, event_id)``: it is the sole
    guard against double-billing on webhook retries, so its failure is logged
    at ERROR and surfaced via health().
    """
    from pymongo import ASCENDING, DESCENDING

    specs: list[tuple[str, list, dict]] = [
        ("users", [("username", ASCENDING)], {"unique": True}),

        ("projects", [("slug", ASCENDING)], {"unique": True}),
        # NOT unique: two projects may legitimately reuse an API key against
        # different servers. This index only speeds up webhook issuer lookup.
        ("projects", [("api_key", ASCENDING)], {}),
        ("projects", [("is_default", ASCENDING)],
         {"unique": True, "partialFilterExpression": {"is_default": True}}),

        ("usage_events", [("project_id", ASCENDING), ("event_id", ASCENDING)],
         {"unique": True, "name": "uniq_project_event"}),
        ("usage_events", [("project_id", ASCENDING), ("created_at", DESCENDING)], {}),
        ("usage_events", [("received_at", ASCENDING)],
         {"expireAfterSeconds": USAGE_EVENTS_TTL_DAYS * 86400}),

        ("usage_sessions", [("project_id", ASCENDING), ("kind", ASCENDING), ("key", ASCENDING)],
         {"unique": True, "partialFilterExpression": {"ended_at": None}}),
        ("usage_sessions", [("project_id", ASCENDING), ("ended_at", ASCENDING)], {}),
        ("usage_sessions", [("project_id", ASCENDING), ("kind", ASCENDING),
                            ("started_at", ASCENDING)], {}),

        ("usage_rollups", [("project_id", ASCENDING), ("month", ASCENDING)], {}),
        ("usage_rollups", [("month", ASCENDING)], {}),

        ("bandwidth_samples", [("project_id", ASCENDING), ("ts", DESCENDING)], {}),
        ("bandwidth_samples", [("ts", ASCENDING)],
         {"expireAfterSeconds": BANDWIDTH_SAMPLES_TTL_DAYS * 86400}),

        ("rates", [("is_active", ASCENDING)],
         {"unique": True, "partialFilterExpression": {"is_active": True}}),

        ("alert_rules", [("project_id", ASCENDING)], {}),
        ("saved_views", [("project_id", ASCENDING)], {}),
        ("room_annotations", [("project_id", ASCENDING), ("room_name", ASCENDING)],
         {"unique": True}),

        ("audit_log", [("project_id", ASCENDING), ("ts", DESCENDING)], {}),
        ("audit_log", [("ts", ASCENDING)], {"expireAfterSeconds": AUDIT_TTL_DAYS * 86400}),
    ]

    _state["index_errors"] = []
    for collection, keys, options in specs:
        try:
            await db[collection].create_index(keys, **options)
        except Exception as exc:
            detail = f"{collection}{[k for k, _ in keys]}: {exc}"
            _state["index_errors"].append(detail)
            if collection == "usage_events" and options.get("unique"):
                logger.error(
                    "CRITICAL: could not create the usage_events idempotency index — "
                    "webhook retries will double-count usage. %s", detail
                )
            else:
                logger.warning("index creation failed for %s", detail)


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

async def get_db():
    """Dependency for routes that cannot function without storage."""
    if _db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Database unavailable: " + (_state["error"] or "MONGODB_URI is not configured")
            ),
        )
    return _db


async def get_db_optional() -> Optional[Any]:
    """Dependency for routes that degrade gracefully without storage."""
    return _db
