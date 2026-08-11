"""Tests for the pure usage-accounting functions.

No database here — this module decides how many billable minutes an event
sequence produces, so it is tested exhaustively in isolation.
"""

from datetime import datetime, timezone

import pytest

from app.services import usage_rollup as r


def dt(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def ns(moment: datetime) -> int:
    return int(moment.timestamp() * 1e9)


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

def test_ns_to_dt_nanoseconds():
    moment = dt(2026, 8, 11, 12, 30)
    assert r.ns_to_dt(ns(moment)) == moment


def test_ns_to_dt_seconds():
    moment = dt(2026, 8, 11, 12, 30)
    assert r.ns_to_dt(int(moment.timestamp())) == moment


def test_ns_to_dt_milliseconds():
    moment = dt(2026, 8, 11, 12, 30)
    assert r.ns_to_dt(int(moment.timestamp() * 1000)) == moment


def test_ns_to_dt_accepts_strings():
    moment = dt(2026, 8, 11)
    assert r.ns_to_dt(str(ns(moment))) == moment


@pytest.mark.parametrize("value", [None, 0, "", "not-a-number", -5, [], {}])
def test_ns_to_dt_returns_none_rather_than_guessing(value):
    assert r.ns_to_dt(value) is None


# ---------------------------------------------------------------------------
# Day splitting — the part that makes daily charts and month totals agree
# ---------------------------------------------------------------------------

def test_split_within_one_day():
    assert r.split_by_utc_day(dt(2026, 8, 11, 10), dt(2026, 8, 11, 11)) == [
        ("2026-08-11", 3600.0)
    ]


def test_split_across_midnight():
    slices = r.split_by_utc_day(dt(2026, 8, 11, 23, 30), dt(2026, 8, 12, 0, 30))
    assert slices == [("2026-08-11", 1800.0), ("2026-08-12", 1800.0)]


def test_split_across_several_days():
    slices = r.split_by_utc_day(dt(2026, 8, 11, 22), dt(2026, 8, 14, 2))
    assert [day for day, _ in slices] == [
        "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"
    ]
    assert sum(seconds for _, seconds in slices) == pytest.approx(52 * 3600)


def test_split_exactly_at_midnight():
    """A session ending on the boundary belongs entirely to the earlier day."""
    slices = r.split_by_utc_day(dt(2026, 8, 11, 23), dt(2026, 8, 12, 0))
    assert slices == [("2026-08-11", 3600.0)]


def test_split_starting_exactly_at_midnight():
    slices = r.split_by_utc_day(dt(2026, 8, 12, 0), dt(2026, 8, 12, 1))
    assert slices == [("2026-08-12", 3600.0)]


@pytest.mark.parametrize("start,end", [
    (dt(2026, 8, 11, 10), dt(2026, 8, 11, 10)),      # zero length
    (dt(2026, 8, 11, 11), dt(2026, 8, 11, 10)),      # inverted
    (None, dt(2026, 8, 11, 10)),
    (dt(2026, 8, 11, 10), None),
])
def test_split_refuses_to_invent_duration(start, end):
    assert r.split_by_utc_day(start, end) == []


def test_split_total_matches_the_real_elapsed_time():
    start, end = dt(2026, 8, 11, 8, 17, 33), dt(2026, 8, 13, 19, 42, 11)
    slices = r.split_by_utc_day(start, end)
    assert sum(s for _, s in slices) == pytest.approx((end - start).total_seconds())


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("info,expected", [
    ({"room_composite": {"room_name": "x"}}, "room_composite"),
    ({"participant": {"identity": "u"}}, "participant"),
    ({"track_composite": {"room_name": "x"}}, "track_composite"),
    ({"track": {"track_id": "t"}}, "track"),
    ({"web": {"url": "https://x"}}, "web"),
    ({}, "other"),
    ({"unknown_variant": {}}, "other"),
    (None, "other"),
    ("not-a-dict", "other"),
])
def test_egress_type_of(info, expected):
    assert r.egress_type_of(info) == expected


@pytest.mark.parametrize("info,expected", [
    ({"input_type": 0}, "rtmp"),
    ({"input_type": "RTMP_INPUT"}, "rtmp"),
    ({"input_type": 1}, "whip"),
    ({"input_type": "WHIP_INPUT"}, "whip"),
    ({"input_type": 2}, "url"),
    ({"url": "https://x/stream"}, "url"),
    ({}, "other"),
    (None, "other"),
])
def test_ingress_type_of(info, expected):
    assert r.ingress_type_of(info) == expected


@pytest.mark.parametrize("track,expected", [
    ({"type": 1}, "video"),
    ({"type": "VIDEO"}, "video"),
    ({"type": 0}, "audio"),
    ({}, "audio"),
])
def test_track_kind_of(track, expected):
    assert r.track_kind_of(track) == expected


def test_egress_bytes_sums_file_results():
    info = {"file_results": [{"size": 100}, {"size": "250"}, {"size": None}]}
    assert r.egress_bytes(info) == 350


def test_egress_bytes_is_zero_without_results():
    assert r.egress_bytes({}) == 0
    assert r.egress_bytes(None) == 0
    assert r.egress_bytes({"file_results": [{"no_size": 1}]}) == 0


# ---------------------------------------------------------------------------
# Event -> accounting intent
# ---------------------------------------------------------------------------

def test_room_started_opens_a_session():
    op = r.session_plan({
        "event": "room_started",
        "room": {"sid": "RM_1", "name": "demo", "creation_time": ns(dt(2026, 8, 11, 9))},
    })
    assert (op.action, op.kind, op.key) == ("open", "room", "RM_1")
    assert op.at == dt(2026, 8, 11, 9)


def test_room_finished_closes_a_session():
    op = r.session_plan({
        "event": "room_finished",
        "room": {"sid": "RM_1", "name": "demo"},
        "created_at": ns(dt(2026, 8, 11, 10)),
    })
    assert (op.action, op.kind, op.key) == ("close", "room", "RM_1")


def test_participant_joined_uses_joined_at():
    op = r.session_plan({
        "event": "participant_joined",
        "participant": {"sid": "PA_1", "identity": "alice",
                        "joined_at": ns(dt(2026, 8, 11, 9, 5))},
        "room": {"name": "demo"},
    })
    assert (op.action, op.kind, op.key) == ("open", "participant", "PA_1")
    assert op.at == dt(2026, 8, 11, 9, 5)
    assert op.extra["identity"] == "alice"


def test_participant_left_closes():
    op = r.session_plan({
        "event": "participant_left",
        "participant": {"sid": "PA_1", "identity": "alice"},
        "created_at": ns(dt(2026, 8, 11, 9, 35)),
    })
    assert (op.action, op.kind, op.key) == ("close", "participant", "PA_1")


def test_track_published_is_a_count_not_a_session():
    op = r.session_plan({"event": "track_published", "track": {"type": 1}})
    assert (op.action, op.kind, op.subtype) == ("count", "track", "video")


def test_egress_ended_carries_authoritative_times_and_bytes():
    op = r.session_plan({
        "event": "egress_ended",
        "egress_info": {
            "egress_id": "EG_1",
            "room_composite": {"room_name": "demo"},
            "started_at": ns(dt(2026, 8, 11, 9)),
            "ended_at": ns(dt(2026, 8, 11, 9, 30)),
            "file_results": [{"size": 1024}],
        },
    })
    assert (op.action, op.kind, op.subtype) == ("close", "egress", "room_composite")
    assert op.at == dt(2026, 8, 11, 9, 30)
    assert op.extra["started_at"] == dt(2026, 8, 11, 9)
    assert op.extra["bytes"] == 1024


def test_ingress_events():
    started = r.session_plan({
        "event": "ingress_started",
        "ingress_info": {"ingress_id": "IN_1", "input_type": 0},
    })
    ended = r.session_plan({
        "event": "ingress_ended",
        "ingress_info": {"ingress_id": "IN_1", "input_type": 0},
    })
    assert (started.action, started.subtype) == ("open", "rtmp")
    assert (ended.action, ended.subtype) == ("close", "rtmp")


def test_participant_falls_back_to_identity_when_sid_is_absent():
    op = r.session_plan({
        "event": "participant_joined",
        "participant": {"identity": "alice"},
    })
    assert op.key == "alice"


@pytest.mark.parametrize("event", [
    {"event": "some_future_event_type"},
    {"event": "room_started", "room": {}},          # no usable key
    {"event": "egress_started", "egress_info": {}},  # no egress id
    {},
    None,
    "not-a-dict",
])
def test_unusable_events_return_none_rather_than_raising(event):
    assert r.session_plan(event) is None


# ---------------------------------------------------------------------------
# Increments
# ---------------------------------------------------------------------------

def test_participant_minutes_increment():
    update = r.rollup_increments("participant", "", 90.0, {})
    assert update["$inc"]["participant_minutes"] == pytest.approx(1.5)


def test_egress_increment_is_per_type_and_includes_bytes():
    update = r.rollup_increments("egress", "room_composite", 600.0, {"bytes": 2048})
    assert update["$inc"]["egress_minutes.room_composite"] == pytest.approx(10.0)
    assert update["$inc"]["egress_bytes"] == 2048


def test_estimated_duration_is_tracked_separately():
    """The billing page discloses what share of minutes were estimated."""
    update = r.rollup_increments("participant", "", 3600.0, {"estimated": True})
    assert update["$inc"]["estimated_seconds"] == 3600.0


def test_unknown_kind_produces_no_increment():
    assert r.rollup_increments("mystery", "", 60.0, {}) == {}


def test_count_increments_for_tracks():
    update = r.count_increments("track", "video")
    assert update["$inc"] == {"tracks_published": 1, "tracks_by_kind.video": 1}


def test_count_increments_for_sessions():
    assert r.count_increments("participant", "")["$inc"] == {"participant_sessions": 1}


def test_empty_rollup_shape():
    doc = r.empty_rollup("p1", "2026-08-11")
    assert doc["month"] == "2026-08"
    assert doc["participant_minutes"] == 0.0
    assert set(doc["egress_minutes"]) == set(r.EGRESS_TYPES)
    # Bandwidth is not derivable from webhooks — it must start "not measured".
    assert doc["bandwidth_source"] == "none"


def test_month_key():
    assert r.month_key("2026-08-11") == "2026-08"
