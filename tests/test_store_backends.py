"""The five operator-state stores must behave identically on both backends.

Each runs against MongoDB when configured and a JSON file otherwise. These
tests run the same operation sequence through both and compare, so a change
to one path cannot silently diverge from the other.
"""

import pytest

from app.db import mongo
from app.services import alerts, audit_log, notifications, room_annotations, saved_views
from app.services import store
from tests.fake_mongo import FakeDatabase


@pytest.fixture
def json_backend(tmp_path, monkeypatch):
    """Force the JSON file backend with isolated paths."""
    monkeypatch.setattr(mongo, "_db", None)
    monkeypatch.setattr(saved_views, "_STORE_PATH", str(tmp_path / "views.json"))
    monkeypatch.setattr(alerts, "_STORE_PATH", str(tmp_path / "alerts.json"))
    monkeypatch.setattr(audit_log, "_STORE_PATH", str(tmp_path / "audit.json"))
    monkeypatch.setattr(room_annotations, "_STORE_PATH", str(tmp_path / "ann.json"))
    monkeypatch.setattr(notifications, "_STORE_PATH", str(tmp_path / "notif.json"))
    yield


@pytest.fixture
def mongo_backend(monkeypatch):
    """Force the MongoDB backend against the in-memory fake."""
    db = FakeDatabase()
    monkeypatch.setattr(mongo, "_db", db)
    yield db


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

def test_json_backend_selected_without_mongo(json_backend):
    assert store.use_mongo() is False
    assert store.collection("saved_views") is None


def test_mongo_backend_selected_when_available(mongo_backend):
    assert store.use_mongo() is True
    assert store.collection("saved_views") is not None


def test_missing_project_id_falls_back_to_a_global_scope():
    assert store.scope(None) == store.GLOBAL_PROJECT_ID
    assert store.scope("abc") == "abc"


# ---------------------------------------------------------------------------
# Equivalence
# ---------------------------------------------------------------------------

async def _views_sequence(project_id=None):
    created = await saved_views.create_view("My View", time_range="1h", project_id=project_id)
    listed = await saved_views.list_views(project_id)
    fetched = await saved_views.get_view(created.id, project_id)
    deleted = await saved_views.delete_view(created.id, project_id)
    after = await saved_views.list_views(project_id)
    return (
        [(v.name, v.time_range) for v in listed],
        (fetched.name, fetched.time_range),
        deleted,
        after,
    )


async def test_saved_views_behave_the_same(json_backend):
    assert await _views_sequence() == ([("My View", "1h")], ("My View", "1h"), True, [])


async def test_saved_views_behave_the_same_on_mongo(mongo_backend):
    assert await _views_sequence() == ([("My View", "1h")], ("My View", "1h"), True, [])


async def _alerts_sequence(project_id=None):
    rule = await alerts.create_rule("High rooms", "rooms_total", ">", 5, project_id=project_id)
    listed = await alerts.list_rules(project_id)
    toggled = await alerts.toggle_rule(rule.id, project_id)
    after_toggle = await alerts.get_rule(rule.id, project_id)
    deleted = await alerts.delete_rule(rule.id, project_id)
    return (
        [(r.name, r.metric, r.threshold) for r in listed],
        toggled,
        after_toggle.enabled,
        deleted,
        await alerts.list_rules(project_id),
    )


async def test_alerts_behave_the_same(json_backend):
    assert await _alerts_sequence() == (
        [("High rooms", "rooms_total", 5.0)], False, False, True, []
    )


async def test_alerts_behave_the_same_on_mongo(mongo_backend):
    assert await _alerts_sequence() == (
        [("High rooms", "rooms_total", 5.0)], False, False, True, []
    )


async def _annotations_sequence(project_id=None):
    await room_annotations.pin_room("room-a", project_id)
    await room_annotations.set_annotations("room-a", "a note", ["prod"], project_id)
    single = await room_annotations.get_annotations("room-a", project_id)
    blob = await room_annotations.get_all_annotations(project_id)
    await room_annotations.unpin_room("room-a", project_id)
    return single, blob, await room_annotations.get_pinned(project_id)


async def test_annotations_behave_the_same(json_backend):
    single, blob, pinned = await _annotations_sequence()
    assert single == {"note": "a note", "tags": ["prod"], "pinned": True}
    assert blob["pinned"] == ["room-a"]
    assert blob["notes"] == {"room-a": "a note"}
    assert pinned == []


async def test_annotations_behave_the_same_on_mongo(mongo_backend):
    single, blob, pinned = await _annotations_sequence()
    assert single == {"note": "a note", "tags": ["prod"], "pinned": True}
    assert blob["pinned"] == ["room-a"]
    assert blob["notes"] == {"room-a": "a note"}
    assert pinned == []


async def test_audit_log_behaves_the_same(json_backend):
    await audit_log.log_action("room.create", "r1", "admin")
    entries = await audit_log.list_entries()
    assert [e["action"] for e in entries] == ["room.create"]


async def test_audit_log_behaves_the_same_on_mongo(mongo_backend):
    await audit_log.log_action("room.create", "r1", "admin")
    entries = await audit_log.list_entries()
    assert [e["action"] for e in entries] == ["room.create"]


async def test_notification_config_behaves_the_same(json_backend):
    await notifications.save_config("https://hook.example.com", 5)
    cfg = await notifications.get_config()
    assert cfg["webhook_url"] == "https://hook.example.com"
    assert cfg["cooldown_minutes"] == 5


async def test_notification_config_behaves_the_same_on_mongo(mongo_backend):
    await notifications.save_config("https://hook.example.com", 5)
    cfg = await notifications.get_config()
    assert cfg["webhook_url"] == "https://hook.example.com"
    assert cfg["cooldown_minutes"] == 5


# ---------------------------------------------------------------------------
# Project scoping — only meaningful on the Mongo backend
# ---------------------------------------------------------------------------

async def test_views_are_isolated_per_project(mongo_backend):
    await saved_views.create_view("A only", project_id="proj-a")
    await saved_views.create_view("B only", project_id="proj-b")

    assert [v.name for v in await saved_views.list_views("proj-a")] == ["A only"]
    assert [v.name for v in await saved_views.list_views("proj-b")] == ["B only"]


async def test_pins_do_not_leak_between_projects(mongo_backend):
    """A pin on one project used to show up on every other one."""
    await room_annotations.pin_room("shared-name", "proj-a")

    assert await room_annotations.get_pinned("proj-a") == ["shared-name"]
    assert await room_annotations.get_pinned("proj-b") == []


async def test_alerts_are_isolated_per_project(mongo_backend):
    await alerts.create_rule("A rule", "rooms_total", ">", 1, project_id="proj-a")
    assert len(await alerts.list_rules("proj-a")) == 1
    assert await alerts.list_rules("proj-b") == []


async def test_audit_entries_are_isolated_per_project(mongo_backend):
    await audit_log.log_action("room.create", "r1", "admin", project_id="proj-a")
    await audit_log.log_action("room.delete", "r2", "admin", project_id="proj-b")

    assert [e["action"] for e in await audit_log.list_entries(project_id="proj-a")] == [
        "room.create"
    ]


async def test_notification_config_is_per_project(mongo_backend):
    await notifications.save_config("https://a.example.com", project_id="proj-a")
    await notifications.save_config("https://b.example.com", project_id="proj-b")

    assert (await notifications.get_config("proj-a"))["webhook_url"] == "https://a.example.com"
    assert (await notifications.get_config("proj-b"))["webhook_url"] == "https://b.example.com"


async def test_deleting_another_projects_view_is_refused(mongo_backend):
    view = await saved_views.create_view("A only", project_id="proj-a")
    assert await saved_views.delete_view(view.id, "proj-b") is False
    assert len(await saved_views.list_views("proj-a")) == 1


# ---------------------------------------------------------------------------
# Durability contract
# ---------------------------------------------------------------------------

async def test_audit_write_failure_does_not_raise(mongo_backend, monkeypatch):
    """A failed audit write must never break the action it was recording."""
    async def _boom(*args, **kwargs):
        raise RuntimeError("mongo is down")

    monkeypatch.setattr(mongo_backend["audit_log"], "insert_one", _boom)

    await audit_log.log_action("room.delete", "r1", "admin")  # must not raise


async def test_mongo_audit_log_is_not_trimmed_to_max_entries(mongo_backend):
    """The TTL index handles retention; the file-only 500-entry cap does not apply."""
    for i in range(audit_log.MAX_ENTRIES + 10):
        await audit_log.log_action("room.create", f"r{i}", "admin")

    assert await mongo_backend["audit_log"].count_documents({}) == audit_log.MAX_ENTRIES + 10
