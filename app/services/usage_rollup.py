"""Pure functions turning LiveKit webhook events into rollup increments.

No I/O lives here on purpose: this is where billing correctness is decided, so
it has to be exhaustively testable without a database. `app.services.usage`
supplies the Mongo layer around it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

# LiveKit timestamps are nanosecond epochs. Anything smaller than this is a
# second- or millisecond-epoch instead, which the SDK has used in places.
_NS_THRESHOLD = 1e17
_MS_THRESHOLD = 1e11

EGRESS_TYPES = ("room_composite", "participant", "track", "track_composite", "web", "other")
INGRESS_TYPES = ("rtmp", "whip", "url", "other")


@dataclass
class SessionOp:
    """What an event means for usage accounting."""

    action: str          # "open" | "close" | "count"
    kind: str            # "room" | "participant" | "egress" | "ingress" | "track"
    key: str             # the stable id used to pair an open with its close
    at: Optional[datetime] = None
    subtype: str = ""
    extra: dict = field(default_factory=dict)


def ns_to_dt(value: Any) -> Optional[datetime]:
    """Convert a LiveKit timestamp to an aware UTC datetime.

    Tolerates nanoseconds, milliseconds and seconds because the protobufs are
    not consistent, and returns None for absent or unusable values rather than
    inventing a time.
    """
    if value is None or value == "" or value == 0:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None

    if number >= _NS_THRESHOLD:
        seconds = number / 1e9
    elif number >= _MS_THRESHOLD:
        seconds = number / 1e3
    else:
        seconds = number

    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def day_key(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d")


def month_key(day: str) -> str:
    return day[:7]


def split_by_utc_day(start: datetime, end: datetime) -> list[tuple[str, float]]:
    """Split a session into per-UTC-day (day, seconds) slices.

    A call spanning midnight belongs partly to each day, so both the daily
    chart and the monthly total come out right. Returns [] when the interval
    is empty or inverted rather than guessing.
    """
    if start is None or end is None or end <= start:
        return []

    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)

    slices: list[tuple[str, float]] = []
    cursor = start
    while cursor < end:
        midnight = (cursor + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        boundary = min(midnight, end)
        slices.append((day_key(cursor), (boundary - cursor).total_seconds()))
        cursor = boundary

    return slices


def egress_type_of(egress_info: dict) -> str:
    """Classify an egress job by which request oneof is populated."""
    if not isinstance(egress_info, dict):
        return "other"
    for field_name, label in (
        ("room_composite", "room_composite"),
        ("participant", "participant"),
        ("track_composite", "track_composite"),
        ("track", "track"),
        ("web", "web"),
    ):
        if egress_info.get(field_name):
            return label
    return "other"


def ingress_type_of(ingress_info: dict) -> str:
    """Classify an ingress by its input type."""
    if not isinstance(ingress_info, dict):
        return "other"

    input_type = ingress_info.get("input_type")
    mapping = {
        0: "rtmp", "RTMP_INPUT": "rtmp",
        1: "whip", "WHIP_INPUT": "whip",
        2: "url", "URL_INPUT": "url",
    }
    if input_type in mapping:
        return mapping[input_type]
    if ingress_info.get("url"):
        return "url"
    return "other"


def track_kind_of(track: dict) -> str:
    kind = track.get("type") if isinstance(track, dict) else None
    if kind in (1, "VIDEO", "video"):
        return "video"
    return "audio"


def egress_bytes(egress_info: dict) -> int:
    """Total output bytes for a finished egress job.

    The only genuinely measured byte count LiveKit webhooks carry. Participant
    bandwidth is NOT derivable from webhooks and must not be inferred here.
    """
    if not isinstance(egress_info, dict):
        return 0
    total = 0
    for result in egress_info.get("file_results") or []:
        try:
            total += int(result.get("size") or 0)
        except (TypeError, ValueError):
            continue
    return total


def session_plan(event: dict) -> Optional[SessionOp]:
    """Translate a webhook event into an accounting intent.

    Unknown event types return None rather than raising — LiveKit adds new
    ones, and an unrecognised event must not fail ingestion.
    """
    if not isinstance(event, dict):
        return None

    name = event.get("event") or ""
    room = event.get("room") or {}
    participant = event.get("participant") or {}
    egress = event.get("egress_info") or {}
    ingress = event.get("ingress_info") or {}
    received = ns_to_dt(event.get("created_at"))

    if name == "room_started":
        key = room.get("sid") or room.get("name")
        if not key:
            return None
        return SessionOp("open", "room", key, ns_to_dt(room.get("creation_time")) or received,
                         extra={"room_name": room.get("name", "")})

    if name == "room_finished":
        key = room.get("sid") or room.get("name")
        if not key:
            return None
        return SessionOp("close", "room", key, received,
                         extra={"room_name": room.get("name", "")})

    if name == "participant_joined":
        key = participant.get("sid") or participant.get("identity")
        if not key:
            return None
        return SessionOp("open", "participant", key,
                         ns_to_dt(participant.get("joined_at")) or received,
                         extra={"identity": participant.get("identity", ""),
                                "room_name": room.get("name", "")})

    if name == "participant_left":
        key = participant.get("sid") or participant.get("identity")
        if not key:
            return None
        return SessionOp("close", "participant", key, received,
                         extra={"identity": participant.get("identity", "")})

    if name == "track_published":
        return SessionOp("count", "track", "", received,
                         subtype=track_kind_of(event.get("track") or {}))

    if name == "egress_started":
        key = egress.get("egress_id")
        if not key:
            return None
        return SessionOp("open", "egress", key,
                         ns_to_dt(egress.get("started_at")) or received,
                         subtype=egress_type_of(egress))

    if name == "egress_ended":
        key = egress.get("egress_id")
        if not key:
            return None
        return SessionOp("close", "egress", key,
                         ns_to_dt(egress.get("ended_at")) or received,
                         subtype=egress_type_of(egress),
                         extra={"bytes": egress_bytes(egress),
                                "started_at": ns_to_dt(egress.get("started_at"))})

    if name == "ingress_started":
        key = ingress.get("ingress_id")
        if not key:
            return None
        return SessionOp("open", "ingress", key, received, subtype=ingress_type_of(ingress))

    if name == "ingress_ended":
        key = ingress.get("ingress_id")
        if not key:
            return None
        return SessionOp("close", "ingress", key, received, subtype=ingress_type_of(ingress))

    return None


def rollup_increments(
    kind: str,
    subtype: str,
    day_seconds: float,
    extra: Optional[dict] = None,
) -> dict:
    """Build the Mongo update for one day's slice of one closed session."""
    extra = extra or {}
    minutes = day_seconds / 60.0
    inc: dict[str, float] = {}

    if kind == "room":
        inc["room_minutes"] = minutes
    elif kind == "participant":
        inc["participant_minutes"] = minutes
    elif kind == "egress":
        inc[f"egress_minutes.{subtype or 'other'}"] = minutes
        if extra.get("bytes"):
            inc["egress_bytes"] = extra["bytes"]
    elif kind == "ingress":
        inc[f"ingress_minutes.{subtype or 'other'}"] = minutes

    if extra.get("estimated"):
        inc["estimated_seconds"] = day_seconds

    return {"$inc": inc} if inc else {}


def count_increments(kind: str, subtype: str) -> dict:
    """Build the Mongo update for a countable (non-duration) event."""
    if kind == "track":
        return {"$inc": {"tracks_published": 1, f"tracks_by_kind.{subtype or 'audio'}": 1}}
    if kind in ("room", "participant", "egress", "ingress"):
        return {"$inc": {f"{kind}_sessions": 1}}
    return {}


def empty_rollup(project_id: str, day: str) -> dict:
    """The zero-value shape a rollup document upserts into."""
    return {
        "project_id": project_id,
        "day": day,
        "month": month_key(day),
        "participant_minutes": 0.0,
        "participant_sessions": 0,
        "room_minutes": 0.0,
        "room_sessions": 0,
        "peak_concurrent_participants": 0,
        "egress_minutes": {t: 0.0 for t in EGRESS_TYPES},
        "egress_sessions": 0,
        "egress_bytes": 0,
        "ingress_minutes": {t: 0.0 for t in INGRESS_TYPES},
        "ingress_sessions": 0,
        "tracks_published": 0,
        "tracks_by_kind": {"audio": 0, "video": 0},
        "bandwidth_bytes_down": 0,
        "bandwidth_bytes_up": 0,
        "bandwidth_source": "none",
        "bandwidth_gap": False,
        "estimated_seconds": 0.0,
        "dropped_events": 0,
        "data_gap": False,
    }
