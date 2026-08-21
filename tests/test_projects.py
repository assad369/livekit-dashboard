"""Tests for multi-project support."""

import os

import pytest

from app.security import crypto
from app.services import cache as dispatch_cache
from app.services import lk_pool
from app.services import projects as project_service
from app.services.projects import NoProjectConfigured, Project, ProjectError
from tests.fake_mongo import FakeDatabase


@pytest.fixture
def db():
    return FakeDatabase()


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setenv("APP_ENCRYPTION_KEY", crypto.generate_key())
    monkeypatch.delenv("APP_ENCRYPTION_KEY_OLD", raising=False)
    yield


@pytest.fixture(autouse=True)
async def _clean_pool():
    dispatch_cache.clear()
    await lk_pool.close_all()
    yield
    await lk_pool.close_all()
    dispatch_cache.clear()


async def _make(db, name="Production", url="wss://lk.example.com",
                key="APIprod", secret="secret-prod", **kwargs):
    return await project_service.create_project(
        db, name=name, livekit_url=url, api_key=key, api_secret=secret, **kwargs
    )


# ---------------------------------------------------------------------------
# Environment project
# ---------------------------------------------------------------------------

def test_env_project_from_environment():
    project = project_service.env_project()
    assert project is not None
    assert project.livekit_url == os.environ["LIVEKIT_URL"]
    assert project.source == "env"
    assert project.is_env is True


def test_env_project_is_none_when_incomplete(monkeypatch):
    monkeypatch.delenv("LIVEKIT_API_SECRET", raising=False)
    assert project_service.env_project() is None


def test_env_project_repairs_a_mangled_url(monkeypatch):
    # A hand-edited .env can lose the slashes after the scheme; the SDK turns
    # that into https:///... and every API call fails with an invalid URL.
    monkeypatch.setenv("LIVEKIT_URL", r"wss:\/\/sr.livekit.cloud")
    project = project_service.env_project()
    assert project is not None
    assert project.livekit_url == "wss://sr.livekit.cloud"


def test_env_project_is_none_when_url_has_no_scheme(monkeypatch, caplog):
    monkeypatch.setenv("LIVEKIT_URL", "sr.livekit.cloud")
    with caplog.at_level("ERROR"):
        assert project_service.env_project() is None
    assert "LIVEKIT_URL is malformed" in caplog.text


# ---------------------------------------------------------------------------
# URL normalization
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    (r"wss:\/\/host", "wss://host"),
    ("wss:host", "wss://host"),
    ("wss:/host", "wss://host"),
    ('"wss://host"', "wss://host"),
    ("'wss://host'", "wss://host"),
    ("  wss://host  ", "wss://host"),
    ("wss://host/", "wss://host"),
    ("http://localhost:7880", "http://localhost:7880"),
    ("wss://host/sub/path", "wss://host/sub/path"),
    ("garbage", "garbage"),
    ("", ""),
])
def test_normalize_livekit_url(raw, expected):
    assert project_service.normalize_livekit_url(raw) == expected


# ---------------------------------------------------------------------------
# Secrets at rest
# ---------------------------------------------------------------------------

async def test_api_secret_is_encrypted_at_rest(db):
    await _make(db, secret="the-real-secret")

    raw = await db["projects"].find_one({})
    assert "the-real-secret" not in str(raw)
    assert "api_secret" not in raw
    assert raw["api_secret_enc"].startswith("gAAAAA")


async def test_api_key_is_stored_in_the_clear(db):
    """The key is an identifier and webhook issuer lookup needs to query it."""
    await _make(db, key="APIvisible")
    raw = await db["projects"].find_one({})
    assert raw["api_key"] == "APIvisible"


async def test_project_exposes_the_decrypted_secret_in_memory(db):
    project = await _make(db, secret="plaintext-in-memory")
    assert project.api_secret == "plaintext-in-memory"


async def test_masking_hides_the_middle(db):
    project = await _make(db, key="APIabcdefghij", secret="secret-value-here")
    assert project.masked_key().startswith("APIa")
    assert "bcdefgh" not in project.masked_key()
    assert "value" not in project.masked_secret()


async def test_creation_is_refused_with_the_default_encryption_key(db, monkeypatch):
    monkeypatch.delenv("APP_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("APP_SECRET_KEY", crypto.DEV_SECRET_KEY)

    with pytest.raises(ProjectError, match="default development key"):
        await _make(db)


async def test_undecryptable_project_is_skipped_not_crashed(db, monkeypatch):
    """A wrong APP_ENCRYPTION_KEY must not take the whole page down."""
    await _make(db)
    monkeypatch.setenv("APP_ENCRYPTION_KEY", crypto.generate_key())

    assert await project_service.list_projects(db) == []


# ---------------------------------------------------------------------------
# Slugs and validation
# ---------------------------------------------------------------------------

def test_slugify():
    assert project_service.slugify("Production EU") == "production-eu"
    assert project_service.slugify("  Staging!! ") == "staging"
    assert project_service.slugify("!!!") == "project"


async def test_slug_collisions_get_a_suffix(db):
    first = await _make(db, name="Production")
    second = await _make(db, name="Production")
    third = await _make(db, name="Production")

    assert [first.slug, second.slug, third.slug] == [
        "production", "production-2", "production-3"
    ]


@pytest.mark.parametrize("kwargs,message", [
    ({"name": "  "}, "Name is required"),
    ({"url": ""}, "URL is required"),
    ({"url": "livekit.example.com"}, "must start with"),
    ({"key": ""}, "API key is required"),
    ({"secret": ""}, "API secret is required"),
])
async def test_validation(db, kwargs, message):
    with pytest.raises(ProjectError, match=message):
        await _make(db, **kwargs)


async def test_trailing_slash_is_stripped(db):
    project = await _make(db, url="wss://lk.example.com/")
    assert project.livekit_url == "wss://lk.example.com"


async def test_pasted_url_with_escaped_slashes_is_accepted(db):
    project = await _make(db, url=r"wss:\/\/lk.example.com")
    assert project.livekit_url == "wss://lk.example.com"


async def test_update_normalizes_the_url(db):
    project = await _make(db)
    updated = await project_service.update_project(
        db, project.id, name=project.name, livekit_url=r"wss:\/\/moved.example.com/",
        api_key=project.api_key,
    )
    assert updated.livekit_url == "wss://moved.example.com"


# ---------------------------------------------------------------------------
# Defaults, updates, deletion
# ---------------------------------------------------------------------------

async def test_first_project_becomes_the_default(db):
    assert (await _make(db, name="First")).is_default is True
    assert (await _make(db, name="Second")).is_default is False


async def test_only_one_default_at_a_time(db):
    first = await _make(db, name="First")
    second = await _make(db, name="Second")

    await project_service.set_default(db, second.id)

    projects = {p.name: p for p in await project_service.list_projects(db)}
    assert projects["Second"].is_default is True
    assert projects["First"].is_default is False


async def test_blank_secret_on_update_keeps_the_stored_one(db):
    project = await _make(db, secret="original-secret")

    updated = await project_service.update_project(
        db, project.id,
        name="Renamed", livekit_url=project.livekit_url, api_key=project.api_key,
        api_secret="",
    )

    assert updated.name == "Renamed"
    assert updated.api_secret == "original-secret"


async def test_supplying_a_secret_on_update_replaces_it(db):
    project = await _make(db, secret="original-secret")

    updated = await project_service.update_project(
        db, project.id,
        name=project.name, livekit_url=project.livekit_url, api_key=project.api_key,
        api_secret="rotated-secret",
    )

    assert updated.api_secret == "rotated-secret"


async def test_renaming_updates_the_slug(db):
    project = await _make(db, name="Old Name")
    updated = await project_service.update_project(
        db, project.id, name="New Name", livekit_url=project.livekit_url,
        api_key=project.api_key,
    )
    assert updated.slug == "new-name"


async def test_deleting_the_last_project_is_refused(db):
    project = await _make(db)
    with pytest.raises(ProjectError, match="only project"):
        await project_service.delete_project(db, project.id)


async def test_delete_promotes_a_new_default(db):
    first = await _make(db, name="First")
    await _make(db, name="Second")

    await project_service.delete_project(db, first.id)

    remaining = await project_service.list_projects(db)
    assert len(remaining) == 1
    assert remaining[0].is_default is True


async def test_purge_removes_the_projects_data(db):
    first = await _make(db, name="First")
    await _make(db, name="Second")
    await db["usage_rollups"].insert_one({"project_id": first.id, "participant_minutes": 10})
    await db["usage_rollups"].insert_one({"project_id": "other", "participant_minutes": 5})

    await project_service.delete_project(db, first.id, purge_data=True)

    remaining = await db["usage_rollups"].find({}).to_list()
    assert [r["project_id"] for r in remaining] == ["other"]


async def test_delete_without_purge_keeps_usage_history(db):
    first = await _make(db, name="First")
    await _make(db, name="Second")
    await db["usage_rollups"].insert_one({"project_id": first.id})

    await project_service.delete_project(db, first.id, purge_data=False)

    assert await db["usage_rollups"].count_documents({}) == 1


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

async def test_resolve_prefers_the_session_choice(db):
    await _make(db, name="First")
    second = await _make(db, name="Second")

    resolved = await project_service.resolve_active({"project_id": second.id}, db)
    assert resolved.id == second.id


async def test_resolve_falls_back_to_the_default(db):
    await _make(db, name="First")
    second = await _make(db, name="Second")
    await project_service.set_default(db, second.id)

    resolved = await project_service.resolve_active({}, db)
    assert resolved.id == second.id


async def test_resolve_ignores_a_stale_session_choice(db):
    project = await _make(db, name="Only")
    resolved = await project_service.resolve_active({"project_id": "deleted-id"}, db)
    assert resolved.id == project.id


async def test_resolve_falls_back_to_the_environment(db):
    """With no stored projects the single-project deployment still works."""
    resolved = await project_service.resolve_active({}, db)
    assert resolved.source == "env"


async def test_resolve_without_a_database_uses_the_environment():
    resolved = await project_service.resolve_active({}, None)
    assert resolved.source == "env"


async def test_resolve_raises_when_nothing_is_configured(db, monkeypatch):
    monkeypatch.delenv("LIVEKIT_URL", raising=False)
    with pytest.raises(NoProjectConfigured):
        await project_service.resolve_active({}, db)


# ---------------------------------------------------------------------------
# Webhook issuer lookup
# ---------------------------------------------------------------------------

async def test_get_by_api_key_returns_every_candidate(db):
    """Two projects may share an API key, so lookup returns a list."""
    await _make(db, name="A", url="wss://a.example.com", key="APIshared", secret="secret-a")
    await _make(db, name="B", url="wss://b.example.com", key="APIshared", secret="secret-b")

    candidates = await project_service.get_by_api_key(db, "APIshared")
    assert {c.name for c in candidates} == {"A", "B"}


async def test_get_by_api_key_includes_the_env_project(db):
    candidates = await project_service.get_by_api_key(db, os.environ["LIVEKIT_API_KEY"])
    assert any(c.source == "env" for c in candidates)


async def test_get_by_api_key_returns_nothing_for_an_unknown_issuer(db):
    assert await project_service.get_by_api_key(db, "APIunknown") == []


# ---------------------------------------------------------------------------
# Client pool
# ---------------------------------------------------------------------------

def _project(pid="p1", url="wss://lk.example.com", key="APIaaa", secret="s"):
    return Project(id=pid, name=pid, slug=pid, livekit_url=url,
                   api_key=key, api_secret=secret)


async def test_pool_reuses_one_client_per_project():
    first = await lk_pool.get_client(_project())
    second = await lk_pool.get_client(_project())
    assert first is second
    assert lk_pool.stats()["pooled_clients"] == 1


async def test_pool_separates_projects_sharing_a_url():
    a = await lk_pool.get_client(_project("p1", key="APIaaa"))
    b = await lk_pool.get_client(_project("p2", key="APIbbb"))

    assert a is not b
    assert a.url == b.url
    assert a.cache_key != b.cache_key
    assert lk_pool.stats()["pooled_clients"] == 2


async def test_changing_credentials_yields_a_new_client():
    before = await lk_pool.get_client(_project(key="APIold"))
    after = await lk_pool.get_client(_project(key="APInew"))
    assert before is not after


async def test_invalidate_drops_only_that_projects_clients():
    await lk_pool.get_client(_project("p1"))
    await lk_pool.get_client(_project("p2"))

    await lk_pool.invalidate("p1")

    assert lk_pool.stats()["projects"] == ["p2"]


async def test_close_all_empties_the_pool():
    await lk_pool.get_client(_project("p1"))
    await lk_pool.get_client(_project("p2"))

    await lk_pool.close_all()

    assert lk_pool.stats()["pooled_clients"] == 0


async def test_pooled_client_carries_the_project_credentials():
    client = await lk_pool.get_client(
        _project("p1", url="wss://custom.example.com", key="APIcustom", secret="scustom")
    )
    assert client.url == "wss://custom.example.com"
    assert client.key == "APIcustom"
    assert client.secret == "scustom"


async def test_concurrent_pool_access_creates_one_client():
    """The pool is shared across concurrent requests; construction must not race."""
    import asyncio

    clients = await asyncio.gather(
        *[lk_pool.get_client(_project()) for _ in range(50)]
    )
    assert len({id(c) for c in clients}) == 1


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@pytest.fixture
def mongo_client(monkeypatch, db):
    """A signed-in client whose app is backed by the fake database."""
    from fastapi.testclient import TestClient

    from app.db import mongo as mongo_module
    from app.main import app
    from app.middleware import project_context
    from tests.conftest import log_in

    monkeypatch.setattr(mongo_module, "_db", db)
    project_context.invalidate_list_cache()

    with TestClient(app) as c:
        log_in(c)
        yield c, db

    project_context.invalidate_list_cache()


def _csrf(client):
    from tests.conftest import csrf_for
    return csrf_for(client)


def test_projects_page_requires_auth(unauth_client):
    assert unauth_client.get("/projects").status_code == 401


def test_projects_page_renders(mongo_client):
    c, _ = mongo_client
    resp = c.get("/projects")
    assert resp.status_code == 200
    assert "Projects" in resp.text


def test_projects_page_shows_the_webhook_config_snippet(mongo_client):
    """The copy-paste server config is the main onboarding step."""
    c, _ = mongo_client
    resp = c.get("/projects")
    assert "/webhooks/livekit" in resp.text
    assert "prometheus_port" in resp.text


def test_create_project_via_form(mongo_client):
    c, db = mongo_client
    resp = c.post(
        "/projects",
        data={
            "csrf_token": _csrf(c),
            "name": "Production",
            "livekit_url": "wss://lk.example.com",
            "api_key": "APIprod",
            "api_secret": "s3cret",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert db["projects"].docs[0]["name"] == "Production"


def test_create_project_requires_csrf(mongo_client):
    c, db = mongo_client
    resp = c.post(
        "/projects",
        data={
            "name": "NoCsrf", "livekit_url": "wss://x.example.com",
            "api_key": "k", "api_secret": "s",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 403
    assert db["projects"].docs == []


def test_create_project_reports_validation_errors(mongo_client):
    c, db = mongo_client
    resp = c.post(
        "/projects",
        data={
            "csrf_token": _csrf(c), "name": "Bad",
            "livekit_url": "not-a-url", "api_key": "k", "api_secret": "s",
        },
        follow_redirects=True,
    )
    assert "must start with" in resp.text
    assert db["projects"].docs == []


def test_switch_project_changes_the_active_one(mongo_client):
    c, db = mongo_client

    for name, key in [("First", "APIone"), ("Second", "APItwo")]:
        c.post("/projects", data={
            "csrf_token": _csrf(c), "name": name,
            "livekit_url": "wss://lk.example.com", "api_key": key, "api_secret": "s",
        }, follow_redirects=False)

    second_id = db["projects"].docs[1]["_id"]

    resp = c.post(
        "/projects/switch",
        data={"csrf_token": _csrf(c), "project_id": second_id, "next": "/projects"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/projects"

    # The switcher is now rendered, with the chosen project active.
    assert "Second" in c.get("/projects").text


def test_switch_refuses_an_offsite_next(mongo_client):
    c, db = mongo_client
    c.post("/projects", data={
        "csrf_token": _csrf(c), "name": "Only", "livekit_url": "wss://lk.example.com",
        "api_key": "k", "api_secret": "s",
    }, follow_redirects=False)

    resp = c.post(
        "/projects/switch",
        data={
            "csrf_token": _csrf(c),
            "project_id": db["projects"].docs[0]["_id"],
            "next": "https://evil.example.com",
        },
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/"


def test_switching_scopes_the_livekit_client(mongo_client, monkeypatch):
    """The point of the whole feature: pages talk to the selected server."""
    c, db = mongo_client

    for name, url in [("First", "wss://one.example.com"), ("Second", "wss://two.example.com")]:
        c.post("/projects", data={
            "csrf_token": _csrf(c), "name": name, "livekit_url": url,
            "api_key": f"API{name}", "api_secret": "s",
        }, follow_redirects=False)

    from unittest.mock import AsyncMock, MagicMock

    seen = []

    async def _spy_get_client(project):
        seen.append(project.livekit_url)
        client = MagicMock()
        client.url = project.livekit_url
        client.key = project.api_key
        client.secret = project.api_secret
        client.sip_enabled = project.sip_enabled
        client.get_server_info = AsyncMock(return_value={"status": "ok"})
        return client

    monkeypatch.setattr(lk_pool, "get_client", _spy_get_client)

    c.post("/projects/switch", data={
        "csrf_token": _csrf(c),
        "project_id": db["projects"].docs[1]["_id"],
        "next": "/settings",
    }, follow_redirects=False)
    c.get("/settings")

    assert seen and seen[-1] == "wss://two.example.com"


def test_readonly_blocks_project_creation_but_allows_switching(mongo_client, monkeypatch):
    c, db = mongo_client
    c.post("/projects", data={
        "csrf_token": _csrf(c), "name": "Only", "livekit_url": "wss://lk.example.com",
        "api_key": "k", "api_secret": "s",
    }, follow_redirects=False)
    project_id = db["projects"].docs[0]["_id"]

    monkeypatch.setenv("DASHBOARD_ROLE", "readonly")

    blocked = c.post("/projects", data={
        "csrf_token": _csrf(c), "name": "Nope", "livekit_url": "wss://x.example.com",
        "api_key": "k", "api_secret": "s",
    }, follow_redirects=False)
    assert blocked.status_code == 403

    allowed = c.post(
        "/projects/switch",
        data={"csrf_token": _csrf(c), "project_id": project_id, "next": "/"},
        follow_redirects=False,
    )
    assert allowed.status_code == 303


def test_delete_requires_the_typed_name(mongo_client):
    c, db = mongo_client
    for name in ("First", "Second"):
        c.post("/projects", data={
            "csrf_token": _csrf(c), "name": name, "livekit_url": "wss://lk.example.com",
            "api_key": f"API{name}", "api_secret": "s",
        }, follow_redirects=False)

    project_id = db["projects"].docs[0]["_id"]

    c.post(f"/projects/{project_id}/delete",
           data={"csrf_token": _csrf(c), "confirm_name": "Wrong"},
           follow_redirects=False)
    assert await_count(db) == 2

    c.post(f"/projects/{project_id}/delete",
           data={"csrf_token": _csrf(c), "confirm_name": "First"},
           follow_redirects=False)
    assert await_count(db) == 1


def await_count(db) -> int:
    return len(db["projects"].docs)


def test_switcher_is_hidden_with_a_single_project(mongo_client):
    """A single-project deployment should look exactly as it did before."""
    c, _ = mongo_client
    c.post("/projects", data={
        "csrf_token": _csrf(c), "name": "Only", "livekit_url": "wss://lk.example.com",
        "api_key": "k", "api_secret": "s",
    }, follow_redirects=False)

    assert 'action="/projects/switch"' not in c.get("/projects").text


def test_connection_test_requires_csrf(mongo_client):
    """It makes the server open an outbound connection, so it is CSRF-checked
    like every other POST — SameSite is site-scoped, not origin-scoped."""
    c, db = mongo_client
    c.post("/projects", data={
        "csrf_token": _csrf(c), "name": "Only", "livekit_url": "wss://lk.example.com",
        "api_key": "k", "api_secret": "s",
    }, follow_redirects=False)
    project_id = db["projects"].docs[0]["_id"]

    assert c.post(f"/projects/{project_id}/test", data={}).status_code == 403


def test_every_mutating_route_verifies_csrf():
    """A POST without a CSRF check is a forgeable state change.

    The webhook receiver is the one exception: it authenticates with LiveKit's
    signed JWT and is called by a server, not a browser.
    """
    import inspect
    from starlette.routing import Route

    from app.main import app

    EXEMPT = {"/webhooks/livekit"}
    missing = []

    for route in app.routes:
        if not isinstance(route, Route):
            continue
        methods = route.methods or set()
        if not methods & {"POST", "PUT", "PATCH", "DELETE"}:
            continue
        if route.path in EXEMPT:
            continue
        try:
            source = inspect.getsource(route.endpoint)
        except (OSError, TypeError):
            continue
        if "verify_csrf_token" not in source:
            missing.append(route.path)

    assert not missing, f"mutating routes without a CSRF check: {missing}"
