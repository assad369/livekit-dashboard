"""Tests for the MongoDB connection layer.

These run without a live Mongo. What matters here is the degradation
contract: an unconfigured or unreachable database must never stop the app
from booting, and must never silently look like an empty-but-working one.
"""

import pytest
from fastapi import HTTPException

from app.db import mongo


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("MONGODB_REQUIRED", raising=False)
    monkeypatch.delenv("MONGODB_TIMEOUT_MS", raising=False)
    monkeypatch.setattr(mongo, "_db", None)
    monkeypatch.setattr(mongo, "_client", None)
    monkeypatch.setattr(
        mongo, "_state",
        {"enabled": False, "connected": False, "error": None, "index_errors": []},
    )
    yield


def test_disabled_when_uri_unset():
    assert mongo.is_enabled() is False


def test_disabled_when_uri_is_blank(monkeypatch):
    monkeypatch.setenv("MONGODB_URI", "   ")
    assert mongo.is_enabled() is False


def test_enabled_when_uri_set(monkeypatch):
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
    assert mongo.is_enabled() is True


def test_get_database_returns_none_when_disabled():
    assert mongo.get_database() is None


async def test_get_db_raises_503_when_unavailable():
    with pytest.raises(HTTPException) as exc:
        await mongo.get_db()
    assert exc.value.status_code == 503
    assert "not configured" in exc.value.detail


async def test_get_db_error_message_includes_connection_error(monkeypatch):
    monkeypatch.setattr(
        mongo, "_state",
        {"enabled": True, "connected": False, "error": "server selection timeout",
         "index_errors": []},
    )
    with pytest.raises(HTTPException) as exc:
        await mongo.get_db()
    assert "server selection timeout" in exc.value.detail


async def test_get_db_optional_returns_none_rather_than_raising():
    assert await mongo.get_db_optional() is None


async def test_connect_is_a_noop_when_disabled():
    await mongo.connect()
    assert mongo.get_database() is None
    assert mongo.health()["enabled"] is False


async def test_connect_degrades_when_unreachable(monkeypatch):
    """An unreachable database must not prevent startup."""
    monkeypatch.setenv("MONGODB_URI", "mongodb://127.0.0.1:1")
    monkeypatch.setenv("MONGODB_TIMEOUT_MS", "50")

    await mongo.connect()

    health = mongo.health()
    assert health["enabled"] is True
    assert health["connected"] is False
    assert health["error"]
    assert mongo.get_database() is None


async def test_connect_raises_when_mongo_is_required(monkeypatch):
    """MONGODB_REQUIRED=true trades degradation for a loud failure."""
    monkeypatch.setenv("MONGODB_URI", "mongodb://127.0.0.1:1")
    monkeypatch.setenv("MONGODB_TIMEOUT_MS", "50")
    monkeypatch.setenv("MONGODB_REQUIRED", "true")

    with pytest.raises(RuntimeError, match="MONGODB_REQUIRED"):
        await mongo.connect()


def test_health_shape():
    health = mongo.health()
    assert set(health) == {"enabled", "connected", "error", "index_errors", "db"}


def test_db_name_defaults(monkeypatch):
    monkeypatch.delenv("MONGODB_DB", raising=False)
    assert mongo.db_name() == "livekit_dashboard"
    monkeypatch.setenv("MONGODB_DB", "custom")
    assert mongo.db_name() == "custom"


async def test_ensure_indexes_records_failures_without_raising():
    """A failing index must be reported, not fatal — except in the logs."""
    class _FailingCollection:
        async def create_index(self, keys, **options):
            raise RuntimeError("boom")

    class _FailingDB:
        def __getitem__(self, name):
            return _FailingCollection()

    await mongo.ensure_indexes(_FailingDB())

    errors = mongo.health()["index_errors"]
    assert errors, "index failures should be recorded"
    assert any("usage_events" in e for e in errors)


async def test_ensure_indexes_creates_the_idempotency_index():
    """The (project_id, event_id) unique index is what prevents double-billing."""
    created = []

    class _Collection:
        def __init__(self, name):
            self.name = name

        async def create_index(self, keys, **options):
            created.append((self.name, [k for k, _ in keys], options))

    class _DB:
        def __getitem__(self, name):
            return _Collection(name)

    await mongo.ensure_indexes(_DB())

    idempotency = [
        c for c in created
        if c[0] == "usage_events" and c[1] == ["project_id", "event_id"]
    ]
    assert len(idempotency) == 1
    assert idempotency[0][2]["unique"] is True
    assert mongo.health()["index_errors"] == []


async def test_ensure_indexes_project_api_key_is_not_unique():
    """Two projects may reuse an API key against different servers."""
    created = []

    class _Collection:
        def __init__(self, name):
            self.name = name

        async def create_index(self, keys, **options):
            created.append((self.name, [k for k, _ in keys], options))

    class _DB:
        def __getitem__(self, name):
            return _Collection(name)

    await mongo.ensure_indexes(_DB())

    api_key_index = [
        c for c in created if c[0] == "projects" and c[1] == ["api_key"]
    ]
    assert len(api_key_index) == 1
    assert api_key_index[0][2].get("unique") is not True
