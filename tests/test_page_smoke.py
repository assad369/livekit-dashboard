"""Render every page and assert none of them blow up.

Template errors — a renamed context key, a filter on the wrong type, a block
name that does not exist in the base template — only surface when the page is
actually rendered. Unit tests on the services underneath will not catch them.
"""

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Route

from app.db import mongo
from app.security import crypto
from app.security import session_auth
from app.services import projects as project_service
from tests.fake_mongo import FakeDatabase


# Pages needing a path parameter, or that are not HTML, are exercised elsewhere.
SKIP_EXACT = {
    "/health",
    "/health/deep",
    "/logout",
    "/login",
    "/events/stream",     # an infinite SSE stream
    "/export.json",       # JSON, covered in test_main
    "/rooms/export.csv",
    "/billing/export.csv",
}


def _html_get_paths() -> list[str]:
    from app.main import app

    paths = []
    for route in app.routes:
        if not isinstance(route, Route) or "GET" not in (route.methods or set()):
            continue
        if "{" in route.path or route.path in SKIP_EXACT:
            continue
        if session_auth.is_public(route.path):
            continue
        paths.append(route.path)
    return sorted(set(paths))


@pytest.fixture
async def rendered_client(monkeypatch):
    """Signed in, with a project and a database, so every page has real context."""
    monkeypatch.setenv("APP_ENCRYPTION_KEY", crypto.generate_key())
    monkeypatch.setenv("ENABLE_SIP", "true")

    db = FakeDatabase()
    monkeypatch.setattr(mongo, "_db", db)

    from app.middleware import project_context
    project_context.invalidate_list_cache()

    project = await project_service.create_project(
        db, name="Prod", livekit_url="wss://lk.example.com",
        api_key="APIk", api_secret="s", sip_enabled=True,
        prometheus_url="http://lk.example.com:6789/metrics",
    )

    from app.main import app
    from app.services.livekit import get_livekit_client
    from tests.conftest import log_in
    from unittest.mock import AsyncMock, MagicMock

    lk = MagicMock()
    lk.url = project.livekit_url
    lk.key = project.api_key
    lk.secret = project.api_secret
    lk.sip_enabled = True
    for name, value in [
        ("list_rooms", ([], 0.01)),
        ("list_participants", []),
        ("get_all_participants_across_rooms", []),
        ("get_server_info", {"status": "ok", "version": "1.0"}),
        ("get_room_analytics", {}),
        ("get_egress_analytics", {}),
        ("get_ingress_analytics", {}),
        ("get_enhanced_analytics", {}),
        ("get_sip_analytics", {}),
        ("list_egress", []),
        ("list_ingress", []),
        ("list_sip_outbound_trunks", []),
        ("list_sip_inbound_trunks", []),
        ("list_sip_dispatch_rules", []),
        ("list_agent_dispatches_all_rooms", ([], 0.0)),
    ]:
        setattr(lk, name, AsyncMock(return_value=value))

    app.dependency_overrides[get_livekit_client] = lambda: lk

    with TestClient(app, raise_server_exceptions=False) as client:
        log_in(client)
        yield client

    app.dependency_overrides.pop(get_livekit_client, None)
    project_context.invalidate_list_cache()


def test_every_page_renders(rendered_client):
    failures = []
    for path in _html_get_paths():
        response = rendered_client.get(path, follow_redirects=False)
        if response.status_code >= 500:
            failures.append((path, response.status_code))

    assert not failures, f"pages failed to render: {failures}"


@pytest.mark.parametrize("path", ["/", "/billing", "/billing/rates", "/projects", "/settings"])
def test_key_pages_return_200(rendered_client, path):
    assert rendered_client.get(path).status_code == 200


def test_project_edit_page_renders(rendered_client):
    from app.db import mongo as mongo_module

    project_id = mongo_module.get_database()["projects"].docs[0]["_id"]
    assert rendered_client.get(f"/projects/{project_id}/edit").status_code == 200


def test_overview_shows_an_em_dash_without_webhook_data(rendered_client):
    """A 0 would read as "nobody connected" rather than "no data arriving"."""
    body = rendered_client.get("/").text
    assert "No webhook data" in body


def test_navigation_links_to_the_new_pages(rendered_client):
    body = rendered_client.get("/").text
    assert 'href="/projects"' in body
    assert 'href="/billing"' in body


def test_logout_is_a_post_form_not_a_link(rendered_client):
    """A GET logout could be triggered by an <img> tag on another site."""
    body = rendered_client.get("/").text
    assert 'action="/logout"' in body
    assert '<a href="/logout"' not in body
