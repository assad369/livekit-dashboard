"""Tests for usage ingestion and rollups against a fake MongoDB."""

from datetime import datetime, timedelta, timezone

import pytest

from app.db import mongo
from app.services import usage
from tests.fake_mongo import FakeDatabase


PROJECT = "proj-1"


def dt(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def ns(moment: datetime) -> int:
    return int(moment.timestamp() * 1e9)


@pytest.fixture
async def db():
    database = FakeDatabase()
    # The idempotency index is what prevents double-billing, so the fake must
    # enforce it exactly as production does.
    await database["usage_events"].create_index(
        [("project_id", 1), ("event_id", 1)], unique=True
    )
    return database


def event(name, event_id, **payload):
    return dict({"event": name, "id": event_id}, **payload)


async def _rollup(db, day="2026-08-11"):
    return await db["usage_rollups"].find_one({"_id": f"{PROJECT}:{day}"})


# ---------------------------------------------------------------------------
# Idempotency — the single most important billing property
# ---------------------------------------------------------------------------

async def test_duplicate_delivery_is_stored_once(db):
    payload = event("room_started", "EV_1",
                    room={"sid": "RM_1", "name": "demo",
                          "creation_time": ns(dt(2026, 8, 11, 9))})

    assert await usage.ingest_event(db, PROJECT, payload) is True
    assert await usage.ingest_event(db, PROJECT, payload) is False

    assert await db["usage_events"].count_documents({}) == 1


async def test_duplicate_delivery_does_not_move_the_totals(db):
    """LiveKit retries with the same event id; the second must bill nothing."""
    joined = event("participant_joined", "EV_1",
                   participant={"sid": "PA_1", "identity": "alice",
                                "joined_at": ns(dt(2026, 8, 11, 9))})
    left = event("participant_left", "EV_2",
                 participant={"sid": "PA_1", "identity": "alice"},
                 created_at=ns(dt(2026, 8, 11, 9, 30)))

    await usage.ingest_event(db, PROJECT, joined)
    await usage.ingest_event(db, PROJECT, left)
    before = (await _rollup(db))["participant_minutes"]

    # Both events redelivered.
    await usage.ingest_event(db, PROJECT, joined)
    await usage.ingest_event(db, PROJECT, left)

    assert (await _rollup(db))["participant_minutes"] == before == pytest.approx(30.0)


async def test_the_same_event_id_in_two_projects_is_not_a_duplicate(db):
    payload = event("room_started", "EV_1", room={"sid": "RM_1", "name": "demo"})

    assert await usage.ingest_event(db, "proj-a", payload) is True
    assert await usage.ingest_event(db, "proj-b", payload) is True


# ---------------------------------------------------------------------------
# Session accounting
# ---------------------------------------------------------------------------

async def test_participant_minutes_are_measured_from_the_real_times(db):
    await usage.ingest_event(db, PROJECT, event(
        "participant_joined", "EV_1",
        participant={"sid": "PA_1", "identity": "alice",
                     "joined_at": ns(dt(2026, 8, 11, 9))}))
    await usage.ingest_event(db, PROJECT, event(
        "participant_left", "EV_2",
        participant={"sid": "PA_1", "identity": "alice"},
        created_at=ns(dt(2026, 8, 11, 9, 45))))

    rollup = await _rollup(db)
    assert rollup["participant_minutes"] == pytest.approx(45.0)
    assert rollup["participant_sessions"] == 1


async def test_room_minutes(db):
    await usage.ingest_event(db, PROJECT, event(
        "room_started", "EV_1",
        room={"sid": "RM_1", "name": "demo", "creation_time": ns(dt(2026, 8, 11, 9))}))
    await usage.ingest_event(db, PROJECT, event(
        "room_finished", "EV_2",
        room={"sid": "RM_1", "name": "demo"},
        created_at=ns(dt(2026, 8, 11, 11))))

    assert (await _rollup(db))["room_minutes"] == pytest.approx(120.0)


async def test_a_session_spanning_midnight_is_split_across_both_days(db):
    await usage.ingest_event(db, PROJECT, event(
        "participant_joined", "EV_1",
        participant={"sid": "PA_1", "joined_at": ns(dt(2026, 8, 11, 23, 30))}))
    await usage.ingest_event(db, PROJECT, event(
        "participant_left", "EV_2",
        participant={"sid": "PA_1"},
        created_at=ns(dt(2026, 8, 12, 0, 30))))

    assert (await _rollup(db, "2026-08-11"))["participant_minutes"] == pytest.approx(30.0)
    assert (await _rollup(db, "2026-08-12"))["participant_minutes"] == pytest.approx(30.0)


async def test_session_count_is_not_duplicated_across_days(db):
    await usage.ingest_event(db, PROJECT, event(
        "participant_joined", "EV_1",
        participant={"sid": "PA_1", "joined_at": ns(dt(2026, 8, 11, 23, 30))}))
    await usage.ingest_event(db, PROJECT, event(
        "participant_left", "EV_2",
        participant={"sid": "PA_1"},
        created_at=ns(dt(2026, 8, 12, 0, 30))))

    first = (await _rollup(db, "2026-08-11"))["participant_sessions"]
    second = (await _rollup(db, "2026-08-12")).get("participant_sessions", 0)
    assert (first, second) == (1, 0)


async def test_egress_uses_its_own_timestamps_and_bytes(db):
    await usage.ingest_event(db, PROJECT, event(
        "egress_started", "EV_1",
        egress_info={"egress_id": "EG_1", "room_composite": {"room_name": "demo"},
                     "started_at": ns(dt(2026, 8, 11, 9))}))
    await usage.ingest_event(db, PROJECT, event(
        "egress_ended", "EV_2",
        egress_info={"egress_id": "EG_1", "room_composite": {"room_name": "demo"},
                     "started_at": ns(dt(2026, 8, 11, 9)),
                     "ended_at": ns(dt(2026, 8, 11, 9, 20)),
                     "file_results": [{"size": 5_000_000}]}))

    rollup = await _rollup(db)
    assert rollup["egress_minutes"]["room_composite"] == pytest.approx(20.0)
    assert rollup["egress_bytes"] == 5_000_000


async def test_track_published_counts_by_kind(db):
    await usage.ingest_event(db, PROJECT, event("track_published", "EV_1",
                                                track={"type": 1},
                                                created_at=ns(dt(2026, 8, 11, 9))))
    await usage.ingest_event(db, PROJECT, event("track_published", "EV_2",
                                                track={"type": 0},
                                                created_at=ns(dt(2026, 8, 11, 9))))

    rollup = await _rollup(db)
    assert rollup["tracks_published"] == 2
    assert rollup["tracks_by_kind"] == {"audio": 1, "video": 1}


# ---------------------------------------------------------------------------
# Honesty about missing data
# ---------------------------------------------------------------------------

async def test_orphan_close_does_not_fabricate_a_duration(db):
    """Deploying mid-call leaves no start to measure from — bill nothing."""
    await usage.ingest_event(db, PROJECT, event(
        "participant_left", "EV_1",
        participant={"sid": "PA_unknown"},
        created_at=ns(dt(2026, 8, 11, 9, 30))))

    session = await db["usage_sessions"].find_one({"key": "PA_unknown"})
    assert session["orphan"] is True
    assert session["started_at"] is None

    rollup = await _rollup(db)
    assert rollup is None or rollup.get("participant_minutes", 0) == 0


async def test_dropped_events_raise_a_data_gap_flag(db):
    await usage.ingest_event(db, PROJECT, event(
        "room_started", "EV_1",
        room={"sid": "RM_1", "name": "demo"},
        num_dropped_events=7))

    day = usage.day_key(datetime.now(timezone.utc))
    rollup = await db["usage_rollups"].find_one({"_id": f"{PROJECT}:{day}"})
    assert rollup["dropped_events"] == 7
    assert rollup["data_gap"] is True


async def test_unknown_event_types_are_stored_but_not_billed(db):
    assert await usage.ingest_event(db, PROJECT, event("some_new_event", "EV_1")) is True
    assert await db["usage_events"].count_documents({}) == 1
    assert await db["usage_sessions"].count_documents({}) == 0


async def test_bandwidth_is_never_inferred_from_webhooks(db):
    """Cloud bills on bandwidth, but no webhook carries it — never guess."""
    await usage.ingest_event(db, PROJECT, event(
        "participant_joined", "EV_1",
        participant={"sid": "PA_1", "joined_at": ns(dt(2026, 8, 11, 9))}))
    await usage.ingest_event(db, PROJECT, event(
        "participant_left", "EV_2",
        participant={"sid": "PA_1"}, created_at=ns(dt(2026, 8, 11, 10))))

    rollup = await _rollup(db)
    assert rollup["bandwidth_bytes_down"] == 0
    assert rollup["bandwidth_source"] == "none"


# ---------------------------------------------------------------------------
# Peak concurrency
# ---------------------------------------------------------------------------

async def test_peak_concurrency_tracks_the_high_water_mark(db):
    for i in range(3):
        await usage.ingest_event(db, PROJECT, event(
            f"participant_joined", f"EV_join_{i}",
            participant={"sid": f"PA_{i}", "joined_at": ns(dt(2026, 8, 11, 9))}))

    await usage.ingest_event(db, PROJECT, event(
        "participant_left", "EV_leave_0",
        participant={"sid": "PA_0"}, created_at=ns(dt(2026, 8, 11, 9, 10))))

    assert (await _rollup(db))["peak_concurrent_participants"] == 3


# ---------------------------------------------------------------------------
# Stale-session sweeping
# ---------------------------------------------------------------------------

async def test_sweep_closes_sessions_whose_end_never_arrived(db):
    """A crashed LiveKit node never sends room_finished."""
    long_ago = datetime.now(timezone.utc) - timedelta(hours=48)
    await db["usage_sessions"].insert_one({
        "project_id": PROJECT, "kind": "room", "key": "RM_stuck",
        "subtype": "", "started_at": long_ago, "ended_at": None, "extra": {},
    })

    assert await usage.sweep_stale_sessions(db, max_age_hours=24) == 1

    session = await db["usage_sessions"].find_one({"key": "RM_stuck"})
    assert session["closed_reason"] == "timeout"
    assert session["estimated"] is True


async def test_swept_duration_is_capped_and_flagged_as_estimated(db):
    long_ago = datetime.now(timezone.utc) - timedelta(hours=100)
    await db["usage_sessions"].insert_one({
        "project_id": PROJECT, "kind": "participant", "key": "PA_stuck",
        "subtype": "", "started_at": long_ago, "ended_at": None, "extra": {},
    })

    await usage.sweep_stale_sessions(db, max_age_hours=24)

    docs = await db["usage_rollups"].find({"project_id": PROJECT}).to_list()
    billed = sum(d.get("participant_minutes", 0) for d in docs)
    estimated = sum(d.get("estimated_seconds", 0) for d in docs)

    # Capped at the sweep window, not the full 100 hours.
    assert billed == pytest.approx(24 * 60)
    assert estimated == pytest.approx(24 * 3600)


async def test_sweep_leaves_recent_sessions_alone(db):
    await db["usage_sessions"].insert_one({
        "project_id": PROJECT, "kind": "room", "key": "RM_live",
        "started_at": datetime.now(timezone.utc) - timedelta(minutes=5),
        "ended_at": None, "extra": {},
    })
    assert await usage.sweep_stale_sessions(db, max_age_hours=24) == 0


async def test_sweep_is_idempotent(db):
    long_ago = datetime.now(timezone.utc) - timedelta(hours=48)
    await db["usage_sessions"].insert_one({
        "project_id": PROJECT, "kind": "room", "key": "RM_stuck",
        "started_at": long_ago, "ended_at": None, "extra": {},
    })

    await usage.sweep_stale_sessions(db, max_age_hours=24)
    first = await db["usage_rollups"].find({}).to_list()

    await usage.sweep_stale_sessions(db, max_age_hours=24)
    second = await db["usage_rollups"].find({}).to_list()

    assert [d.get("room_minutes") for d in first] == [d.get("room_minutes") for d in second]


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

async def test_connection_minutes_returns_none_with_no_data(db):
    """None means "no webhook data", which is not the same as zero minutes."""
    result = await usage.get_connection_minutes(
        db, PROJECT, dt(2026, 8, 11), dt(2026, 8, 11, 23, 59)
    )
    assert result is None


async def test_connection_minutes_sums_the_window(db):
    for day, minutes in [("2026-08-10", 100), ("2026-08-11", 50), ("2026-08-12", 25)]:
        await db["usage_rollups"].insert_one({
            "_id": f"{PROJECT}:{day}", "project_id": PROJECT, "day": day,
            "month": "2026-08", "participant_minutes": minutes,
        })

    total = await usage.get_connection_minutes(
        db, PROJECT, dt(2026, 8, 10), dt(2026, 8, 11, 23, 59)
    )
    assert total == pytest.approx(150.0)


async def test_connection_minutes_excludes_other_projects(db):
    await db["usage_rollups"].insert_one({
        "_id": "other:2026-08-11", "project_id": "other", "day": "2026-08-11",
        "month": "2026-08", "participant_minutes": 999,
    })
    assert await usage.get_connection_minutes(
        db, PROJECT, dt(2026, 8, 11), dt(2026, 8, 11, 23, 59)
    ) is None


async def test_connection_minutes_without_a_database():
    assert await usage.get_connection_minutes(
        None, PROJECT, dt(2026, 8, 11), dt(2026, 8, 11)
    ) is None


async def test_webhook_analytics_empty_state(db):
    result = await usage.get_webhook_analytics(db, PROJECT)
    assert result["has_webhook_data"] is False
    assert result["peak_concurrent"] == 0


async def test_webhook_analytics_with_data(db):
    day = usage.day_key(datetime.now(timezone.utc))
    await db["usage_rollups"].insert_one({
        "_id": f"{PROJECT}:{day}", "project_id": PROJECT, "day": day,
        "month": day[:7], "participant_sessions": 12,
        "peak_concurrent_participants": 5, "tracks_published": 30,
    })

    result = await usage.get_webhook_analytics(db, PROJECT)
    assert result["has_webhook_data"] is True
    assert result["participant_sessions_today"] == 12
    assert result["peak_concurrent"] == 5


async def test_month_rollup_totals(db):
    for day, minutes in [("2026-08-10", 100), ("2026-08-11", 50)]:
        await db["usage_rollups"].insert_one({
            "_id": f"{PROJECT}:{day}", "project_id": PROJECT, "day": day,
            "month": "2026-08", "participant_minutes": minutes,
            "egress_bytes": 1000, "peak_concurrent_participants": 3,
        })
    await db["usage_rollups"].insert_one({
        "_id": f"{PROJECT}:2026-07-31", "project_id": PROJECT, "day": "2026-07-31",
        "month": "2026-07", "participant_minutes": 999,
    })

    total = await usage.month_rollup(db, PROJECT, "2026-08")
    assert total["participant_minutes"] == pytest.approx(150.0)
    assert total["egress_bytes"] == 2000
    assert total["peak_concurrent_participants"] == 3


async def test_month_rollup_all_groups_by_project(db):
    for project in ("proj-a", "proj-b"):
        await db["usage_rollups"].insert_one({
            "_id": f"{project}:2026-08-11", "project_id": project,
            "day": "2026-08-11", "month": "2026-08", "participant_minutes": 10,
        })

    grouped = await usage.month_rollup_all(db, "2026-08")
    assert set(grouped) == {"proj-a", "proj-b"}


async def test_daily_series_is_ordered(db):
    for day in ("2026-08-12", "2026-08-10", "2026-08-11"):
        await db["usage_rollups"].insert_one({
            "_id": f"{PROJECT}:{day}", "project_id": PROJECT, "day": day,
            "month": "2026-08", "participant_minutes": 1,
        })

    series = await usage.daily_series(db, PROJECT, "2026-08")
    assert [d["day"] for d in series] == ["2026-08-10", "2026-08-11", "2026-08-12"]


async def test_available_months_newest_first(db):
    for month in ("2026-06", "2026-08", "2026-07"):
        await db["usage_rollups"].insert_one({
            "_id": f"{PROJECT}:{month}-01", "project_id": PROJECT,
            "day": f"{month}-01", "month": month,
        })

    assert await usage.available_months(db) == ["2026-08", "2026-07", "2026-06"]


async def test_has_any_data(db):
    assert await usage.has_any_data(db, PROJECT) is False
    await db["usage_rollups"].insert_one({
        "_id": f"{PROJECT}:2026-08-11", "project_id": PROJECT,
        "day": "2026-08-11", "month": "2026-08",
    })
    assert await usage.has_any_data(db, PROJECT) is True
