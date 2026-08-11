"""Audit log of operator actions.

Backed by MongoDB when configured (with a TTL index handling retention),
otherwise a JSON file trimmed to MAX_ENTRIES. Entries are scoped per project.
All reads return newest-first.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from app.services import store

logger = logging.getLogger(__name__)

_STORE_PATH = os.environ.get("AUDIT_LOG_FILE", "/tmp/audit_log.json")
MAX_ENTRIES = 500

COLLECTION = "audit_log"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> list[dict]:
    return store.read_json(_STORE_PATH, [])


def _save(entries: list[dict]) -> None:
    store.write_json(_STORE_PATH, entries[-MAX_ENTRIES:])


async def log_action(
    action: str,
    target: str,
    user: str = "admin",
    details: dict[str, Any] | None = None,
    project_id: Optional[str] = None,
) -> None:
    """Append one audit entry.

    Swallows every error: a failed audit write must never break the operator's
    intended action, which has usually already happened by this point.
    """
    entry = {
        "ts": _now_iso(),
        "action": action,
        "target": target,
        "user": user or "admin",
        "details": details or {},
    }

    try:
        collection = store.collection(COLLECTION)
        if collection is None:
            entries = _load()
            entries.append(entry)
            _save(entries)
            return

        # No trimming in Mongo — the TTL index handles retention.
        doc = dict(entry, project_id=store.scope(project_id))
        await collection.insert_one(doc)
    except Exception as exc:
        logger.warning("failed to write audit entry %r: %s", action, exc)


async def list_entries(limit: int = 100, project_id: Optional[str] = None) -> list[dict]:
    """Return up to *limit* most-recent entries, newest first."""
    collection = store.collection(COLLECTION)
    if collection is None:
        return list(reversed(_load()))[:limit]

    cursor = (
        collection.find({"project_id": store.scope(project_id)})
        .sort("ts", -1)
        .limit(limit)
    )
    entries = []
    async for doc in cursor:
        doc.pop("_id", None)
        doc.pop("project_id", None)
        entries.append(doc)
    return entries


async def clear(project_id: Optional[str] = None) -> None:
    collection = store.collection(COLLECTION)
    if collection is None:
        _save([])
        return
    await collection.delete_many({"project_id": store.scope(project_id)})
