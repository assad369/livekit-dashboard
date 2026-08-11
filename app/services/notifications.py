"""Webhook notification delivery for triggered alert rules.

Sends a single POST to a configured URL with a JSON payload listing all
currently-triggered rules. Uses a cooldown so repeated page loads don't
spam the endpoint.

Config lives in MongoDB when configured, otherwise a JSON file, and is
scoped per project.
Schema: {"webhook_url": "...", "cooldown_minutes": 10, "last_fired": "ISO"}
"""

import json
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from app.services import store


_STORE_PATH = os.environ.get("NOTIFICATIONS_FILE", "/tmp/notifications_config.json")

COLLECTION = "notification_config"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load() -> dict:
    return store.read_json(_STORE_PATH, {})


def _save(cfg: dict) -> None:
    store.write_json(_STORE_PATH, cfg)


async def _read(project_id: Optional[str]) -> dict:
    collection = store.collection(COLLECTION)
    if collection is None:
        return _load()
    doc = await collection.find_one({"_id": store.scope(project_id)})
    return doc or {}


async def _write(project_id: Optional[str], updates: dict) -> None:
    collection = store.collection(COLLECTION)
    if collection is None:
        cfg = _load()
        cfg.update(updates)
        _save(cfg)
        return
    await collection.update_one(
        {"_id": store.scope(project_id)}, {"$set": updates}, upsert=True
    )


async def get_config(project_id: Optional[str] = None) -> dict:
    cfg = await _read(project_id)
    return {
        "webhook_url": cfg.get("webhook_url", ""),
        "cooldown_minutes": int(cfg.get("cooldown_minutes", 10)),
        "last_fired": cfg.get("last_fired", ""),
    }


async def save_config(
    webhook_url: str, cooldown_minutes: int = 10, project_id: Optional[str] = None
) -> None:
    await _write(project_id, {
        "webhook_url": webhook_url.strip(),
        "cooldown_minutes": max(1, int(cooldown_minutes)),
    })


def _cooldown_elapsed(cfg: dict) -> bool:
    """Return True if enough time has passed since the last webhook fire."""
    last_fired = cfg.get("last_fired", "")
    if not last_fired:
        return True
    try:
        last_dt = datetime.fromisoformat(last_fired)
        cooldown = timedelta(minutes=int(cfg.get("cooldown_minutes", 10)))
        return _now() - last_dt >= cooldown
    except (ValueError, TypeError):
        return True


async def fire_webhook(
    triggered_rules: list, force: bool = False, project_id: Optional[str] = None
) -> Optional[dict]:
    """POST a notification if rules are triggered and cooldown has elapsed.

    Returns a result dict {"status": int|None, "error": str|None} or None
    when skipped (no URL, no triggered rules, or cooldown active).

    Async because this runs inside request handlers; a blocking urlopen here
    stalled the whole event loop for up to 5s on every /alerts page load.
    """
    if not triggered_rules:
        return None

    cfg = await _read(project_id)
    url = cfg.get("webhook_url", "").strip()
    if not url:
        return None

    if not force and not _cooldown_elapsed(cfg):
        return None

    payload = {
        "source": "livekit-dashboard",
        "fired_at": _now().isoformat(timespec="seconds"),
        "triggered_rules": [
            {
                "id": r.id,
                "name": r.name,
                "metric": r.metric,
                "operator": r.operator,
                "threshold": r.threshold,
                "severity": r.severity,
            }
            for r in triggered_rules
        ],
    }

    result: dict = {}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                url,
                content=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "livekit-dashboard/1.0",
                },
            )
        result = {"status": resp.status_code, "error": None}
    except httpx.HTTPError as exc:
        result = {"status": None, "error": str(exc)}
    except Exception as exc:
        result = {"status": None, "error": str(exc)}

    await _write(project_id, {"last_fired": _now().isoformat(timespec="seconds")})
    return result
