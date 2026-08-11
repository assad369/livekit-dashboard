"""Persist LiveKit usage events and maintain daily rollups.

Three collections:

* ``usage_events``   raw webhook payloads, deduplicated by (project_id, event_id)
* ``usage_sessions`` open/closed intervals used to pair a start with its end
* ``usage_rollups``  one document per project per UTC day — the billing record

Everything here is derived from what LiveKit actually reports. Where a number
is not measurable (participant bandwidth is not in any webhook) it is left
absent so the UI can say "not measured" rather than showing a confident zero.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pymongo.errors import DuplicateKeyError

from app.services.usage_rollup import (
    count_increments,
    day_key,
    empty_rollup,
    month_key,
    rollup_increments,
    session_plan,
    split_by_utc_day,
)

logger = logging.getLogger(__name__)

EVENTS = "usage_events"
SESSIONS = "usage_sessions"
ROLLUPS = "usage_rollups"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _rollup_id(project_id: str, day: str) -> str:
    return f"{project_id}:{day}"


async def _bump_rollup(db, project_id: str, day: str, update: dict) -> None:
    """Apply an update to one day's rollup, creating it if absent."""
    if not update:
        return
    merged = {op: dict(fields) for op, fields in update.items()}
    merged.setdefault("$setOnInsert", {})
    merged["$setOnInsert"].update(empty_rollup(project_id, day))

    # MongoDB rejects an update where one operator touches a path that another
    # also touches, including a parent/child pair: `$inc {"egress_minutes.web"}`
    # conflicts with `$setOnInsert {"egress_minutes"}`. Drop the defaults for
    # any path already being written — a dotted $inc creates its parent anyway.
    touched = set()
    for operator in ("$inc", "$max", "$set"):
        touched.update(merged.get(operator, {}).keys())

    for path in list(merged["$setOnInsert"]):
        conflicts = any(
            other == path
            or other.startswith(f"{path}.")
            or path.startswith(f"{other}.")
            for other in touched
        )
        if conflicts:
            merged["$setOnInsert"].pop(path)

    merged["$set"] = dict(merged.get("$set", {}), updated_at=_now())

    await db[ROLLUPS].update_one(
        {"_id": _rollup_id(project_id, day)}, merged, upsert=True
    )


async def _touch_peak_concurrency(db, project_id: str, moment: datetime) -> None:
    """Record the running participant count as the day's peak if it is higher.

    Uses ``$max`` because webhook delivery is unordered: applying the same
    samples in any sequence has to reach the same answer.
    """
    open_now = await db[SESSIONS].count_documents(
        {"project_id": project_id, "kind": "participant", "ended_at": None}
    )
    await _bump_rollup(
        db, project_id, day_key(moment),
        {"$max": {"peak_concurrent_participants": open_now}},
    )


async def _close_and_bill(
    db, project_id: str, kind: str, subtype: str,
    started_at: datetime, ended_at: datetime, extra: dict,
) -> None:
    """Distribute a closed session's duration across the days it spans."""
    slices = split_by_utc_day(started_at, ended_at)
    for index, (day, seconds) in enumerate(slices):
        # Byte totals and the session count belong to the first day only,
        # otherwise a call spanning midnight would be counted twice.
        day_extra = dict(extra) if index == 0 else {
            k: v for k, v in extra.items() if k != "bytes"
        }
        update = rollup_increments(kind, subtype, seconds, day_extra)
        if index == 0:
            counts = count_increments(kind, subtype)
            if counts:
                update.setdefault("$inc", {})
                for path, value in counts["$inc"].items():
                    update["$inc"][path] = update["$inc"].get(path, 0) + value
        await _bump_rollup(db, project_id, day, update)


async def ingest_event(db, project_id: str, event: dict, raw: bytes = b"") -> bool:
    """Record one webhook event. Returns False if it was a duplicate.

    Idempotency is the whole ballgame: LiveKit retries failed deliveries with
    the same event id, and without the unique index below every retry would
    bill the customer again.
    """
    if db is None:
        raise RuntimeError("ingest_event requires a database")

    event_id = event.get("id") or ""
    received = _now()

    try:
        await db[EVENTS].insert_one({
            "project_id": project_id,
            "event_id": event_id,
            "event": event.get("event", ""),
            "created_at": event.get("created_at"),
            "received_at": received,
            "payload": event,
        })
    except DuplicateKeyError:
        logger.debug("ignoring duplicate webhook event %s", event_id)
        return False

    # LiveKit reports how many events it gave up on. Surface that instead of
    # letting the totals quietly read low.
    dropped = int(event.get("num_dropped_events") or 0)
    if dropped:
        await _bump_rollup(
            db, project_id, day_key(received),
            {"$inc": {"dropped_events": dropped}, "$set": {"data_gap": True}},
        )

    op = session_plan(event)
    if op is None:
        return True

    moment = op.at or received

    if op.action == "count":
        await _bump_rollup(db, project_id, day_key(moment),
                           count_increments(op.kind, op.subtype))
        return True

    if op.action == "open":
        await db[SESSIONS].update_one(
            {"project_id": project_id, "kind": op.kind, "key": op.key, "ended_at": None},
            {"$setOnInsert": {
                "project_id": project_id,
                "kind": op.kind,
                "key": op.key,
                "subtype": op.subtype,
                "started_at": moment,
                "ended_at": None,
                "extra": op.extra,
            }},
            upsert=True,
        )
        if op.kind == "participant":
            await _touch_peak_concurrency(db, project_id, moment)
        return True

    # op.action == "close"
    session = await db[SESSIONS].find_one(
        {"project_id": project_id, "kind": op.kind, "key": op.key, "ended_at": None}
    )

    if session is None:
        # The dashboard was deployed (or restarted) mid-session, so there is no
        # start to measure from. Record the close and move on — inventing a
        # duration here would fabricate billable minutes.
        await db[SESSIONS].insert_one({
            "project_id": project_id,
            "kind": op.kind,
            "key": op.key,
            "subtype": op.subtype,
            "started_at": None,
            "ended_at": moment,
            "orphan": True,
            "extra": op.extra,
        })
        return True

    started_at = op.extra.get("started_at") or session.get("started_at")
    duration = (moment - started_at).total_seconds() if started_at else 0.0

    await db[SESSIONS].update_one(
        {"_id": session["_id"]},
        {"$set": {"ended_at": moment, "duration_s": duration, "closed_reason": "event"}},
    )

    if started_at:
        await _close_and_bill(
            db, project_id, op.kind, op.subtype, started_at, moment, op.extra
        )

    if op.kind == "participant":
        await _touch_peak_concurrency(db, project_id, moment)

    return True


async def sweep_stale_sessions(db, max_age_hours: int = 24) -> int:
    """Close sessions whose end event never arrived.

    A crashed LiveKit node never sends `room_finished`, so those sessions would
    stay open forever and their minutes would never be counted. Closing them
    caps the billed duration and marks it estimated, so the billing page can
    disclose what fraction of the total is a guess rather than a measurement.
    """
    if db is None:
        return 0

    cutoff = _now() - timedelta(hours=max_age_hours)
    stale = await db[SESSIONS].find(
        {"ended_at": None, "started_at": {"$lt": cutoff}}
    ).to_list(None)

    for session in stale:
        started_at = session["started_at"]
        capped_end = started_at + timedelta(hours=max_age_hours)
        await db[SESSIONS].update_one(
            {"_id": session["_id"]},
            {"$set": {
                "ended_at": capped_end,
                "duration_s": max_age_hours * 3600,
                "closed_reason": "timeout",
                "estimated": True,
            }},
        )
        await _close_and_bill(
            db, session["project_id"], session["kind"], session.get("subtype", ""),
            started_at, capped_end,
            dict(session.get("extra") or {}, estimated=True),
        )

    if stale:
        logger.info("swept %d stale usage session(s)", len(stale))
    return len(stale)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

async def has_any_data(db, project_id: Optional[str] = None) -> bool:
    if db is None:
        return False
    query = {"project_id": project_id} if project_id else {}
    return await db[ROLLUPS].count_documents(query, limit=1) > 0


async def get_connection_minutes(
    db, project_id: str, since: datetime, until: datetime
) -> Optional[float]:
    """Participant-minutes in a window, or None when there is no data.

    Returns None rather than 0.0 so callers can distinguish "nobody connected"
    from "we are not receiving webhooks", which look identical otherwise.
    """
    if db is None:
        return None

    days = _days_between(since, until)
    docs = await db[ROLLUPS].find(
        {"project_id": project_id, "day": {"$in": days}}
    ).to_list(None)

    if not docs:
        return None
    return sum(float(d.get("participant_minutes") or 0.0) for d in docs)


def _days_between(since: datetime, until: datetime) -> list[str]:
    days = []
    cursor = since.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end = until.astimezone(timezone.utc)
    while cursor <= end:
        days.append(day_key(cursor))
        cursor += timedelta(days=1)
    return days


async def get_webhook_analytics(db, project_id: str) -> dict:
    """Today's event-derived counters for the overview page.

    Replaces the old stub in livekit.py, which returned has_webhook_data:False
    with a TODO because there was nowhere to store events. The empty state is
    the same shape — it just now means "no events received" rather than
    "not implemented".
    """
    empty = {
        "has_webhook_data": False,
        "participant_sessions_today": 0,
        "room_sessions_today": 0,
        "track_publishes_today": 0,
        "peak_concurrent": 0,
        "participant_minutes_today": 0.0,
    }
    if db is None:
        return empty

    doc = await db[ROLLUPS].find_one({"_id": _rollup_id(project_id, day_key(_now()))})
    if doc is None:
        return empty

    return {
        "has_webhook_data": True,
        "participant_sessions_today": int(doc.get("participant_sessions") or 0),
        "room_sessions_today": int(doc.get("room_sessions") or 0),
        "track_publishes_today": int(doc.get("tracks_published") or 0),
        "peak_concurrent": int(doc.get("peak_concurrent_participants") or 0),
        "participant_minutes_today": float(doc.get("participant_minutes") or 0.0),
    }


def _sum_rollups(docs: list[dict]) -> dict:
    """Aggregate rollup documents into one total."""
    total = empty_rollup("", "")
    total.pop("project_id")
    total.pop("day")
    total.pop("month")

    for doc in docs:
        for field in ("participant_minutes", "room_minutes", "estimated_seconds"):
            total[field] += float(doc.get(field) or 0.0)
        for field in ("participant_sessions", "room_sessions", "egress_sessions",
                      "ingress_sessions", "egress_bytes", "tracks_published",
                      "dropped_events", "bandwidth_bytes_down", "bandwidth_bytes_up"):
            total[field] += int(doc.get(field) or 0)
        for group in ("egress_minutes", "ingress_minutes", "tracks_by_kind"):
            for key, value in (doc.get(group) or {}).items():
                total[group][key] = total[group].get(key, 0) + value
        total["peak_concurrent_participants"] = max(
            total["peak_concurrent_participants"],
            int(doc.get("peak_concurrent_participants") or 0),
        )
        total["data_gap"] = total["data_gap"] or bool(doc.get("data_gap"))
        total["bandwidth_gap"] = total["bandwidth_gap"] or bool(doc.get("bandwidth_gap"))
        if doc.get("bandwidth_source") == "prometheus":
            total["bandwidth_source"] = "prometheus"

    return total


async def month_rollup(db, project_id: str, month: str) -> dict:
    """Totals for one project over one YYYY-MM month."""
    if db is None:
        return _sum_rollups([])
    docs = await db[ROLLUPS].find(
        {"project_id": project_id, "month": month}
    ).to_list(None)
    return _sum_rollups(docs)


async def month_rollup_all(db, month: str) -> dict[str, dict]:
    """Per-project totals for a month, keyed by project id."""
    if db is None:
        return {}
    docs = await db[ROLLUPS].find({"month": month}).to_list(None)

    grouped: dict[str, list] = {}
    for doc in docs:
        grouped.setdefault(doc["project_id"], []).append(doc)
    return {pid: _sum_rollups(items) for pid, items in grouped.items()}


async def daily_series(db, project_id: Optional[str], month: str) -> list[dict]:
    """Per-day rows for the month, oldest first, for the charts and CSV."""
    if db is None:
        return []

    query: dict[str, Any] = {"month": month}
    if project_id:
        query["project_id"] = project_id

    docs = await db[ROLLUPS].find(query).sort("day", 1).to_list(None)

    if project_id:
        return docs

    # Aggregate across projects so an "all projects" view has one row per day.
    by_day: dict[str, list] = {}
    for doc in docs:
        by_day.setdefault(doc["day"], []).append(doc)
    return [
        dict(_sum_rollups(items), day=day, month=month)
        for day, items in sorted(by_day.items())
    ]


async def available_months(db, project_id: Optional[str] = None) -> list[str]:
    """Months that have data, newest first — for the billing month selector."""
    if db is None:
        return []
    query = {"project_id": project_id} if project_id else {}
    months = await db[ROLLUPS].distinct("month", query)
    return sorted((m for m in months if m), reverse=True)
