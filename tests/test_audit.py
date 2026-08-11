"""Tests for audit log service and routes."""

import pytest
from unittest.mock import patch


def _csrf(client):
    from tests.conftest import csrf_for
    return csrf_for(client)


# ---------------------------------------------------------------------------
# Service tests
# ---------------------------------------------------------------------------

async def test_list_entries_empty(tmp_path):
    import app.services.audit_log as al
    with patch.object(al, "_STORE_PATH", str(tmp_path / "audit.json")):
        assert await al.list_entries() == []


async def test_log_action_persists(tmp_path):
    import app.services.audit_log as al
    with patch.object(al, "_STORE_PATH", str(tmp_path / "audit.json")):
        await al.log_action("room.create", "my-room", user="admin", details={"max_participants": 10})
        entries = await al.list_entries()
    assert len(entries) == 1
    assert entries[0]["action"] == "room.create"
    assert entries[0]["target"] == "my-room"
    assert entries[0]["user"] == "admin"
    assert entries[0]["details"]["max_participants"] == 10


async def test_list_entries_newest_first(tmp_path):
    import app.services.audit_log as al
    with patch.object(al, "_STORE_PATH", str(tmp_path / "audit.json")):
        await al.log_action("room.create", "first")
        await al.log_action("room.delete", "second")
        entries = await al.list_entries()
    assert entries[0]["action"] == "room.delete"
    assert entries[1]["action"] == "room.create"


async def test_clear_removes_all(tmp_path):
    import app.services.audit_log as al
    with patch.object(al, "_STORE_PATH", str(tmp_path / "audit.json")):
        await al.log_action("room.create", "r")
        await al.clear()
        assert await al.list_entries() == []


async def test_log_action_never_raises(tmp_path):
    import app.services.audit_log as al
    with patch.object(al, "_STORE_PATH", "/nonexistent/path/audit.json"):
        await al.log_action("room.create", "r")  # must not raise


async def test_list_entries_limit(tmp_path):
    import app.services.audit_log as al
    with patch.object(al, "_STORE_PATH", str(tmp_path / "audit.json")):
        for i in range(10):
            await al.log_action("room.create", f"room-{i}")
        entries = await al.list_entries(limit=3)
    assert len(entries) == 3


# ---------------------------------------------------------------------------
# Route tests
# ---------------------------------------------------------------------------

def test_audit_page_requires_auth(unauth_client):
    resp = unauth_client.get("/audit", follow_redirects=False)
    assert resp.status_code == 401


def test_audit_page_returns_200(client, auth_headers, tmp_path):
    import app.services.audit_log as al
    with patch.object(al, "_STORE_PATH", str(tmp_path / "audit.json")):
        resp = client.get("/audit", headers=auth_headers)
    assert resp.status_code == 200
    assert b"Audit Log" in resp.content


async def test_audit_page_shows_entries(client, auth_headers, tmp_path):
    import app.services.audit_log as al
    with patch.object(al, "_STORE_PATH", str(tmp_path / "audit.json")):
        await al.log_action("room.create", "test-room", user="admin")
        resp = client.get("/audit", headers=auth_headers)
    assert b"room.create" in resp.content
    assert b"test-room" in resp.content


async def test_clear_audit_log_via_route(client, auth_headers, tmp_path):
    import app.services.audit_log as al
    with patch.object(al, "_STORE_PATH", str(tmp_path / "audit.json")):
        await al.log_action("room.create", "r")
        resp = client.post(
            "/audit/clear",
            data={"csrf_token": _csrf(client)},
            headers=auth_headers,
            follow_redirects=False,
        )
    assert resp.status_code == 303
