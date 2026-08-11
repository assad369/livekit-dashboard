"""Tests for session authentication."""

import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routes.auth import MAX_ATTEMPTS, reset_throttle
from app.security import session_auth
from tests.conftest import csrf_for, log_in


@pytest.fixture(autouse=True)
def _reset_throttle():
    reset_throttle()
    yield
    reset_throttle()


# ---------------------------------------------------------------------------
# Login page
# ---------------------------------------------------------------------------

def test_login_page_is_public(unauth_client):
    resp = unauth_client.get("/login")
    assert resp.status_code == 200
    assert 'name="password"' in resp.text


def test_login_page_redirects_when_already_signed_in(client):
    resp = client.get("/login", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_login_page_does_not_render_the_sidebar(unauth_client):
    """The login page must not leak the app's navigation before sign-in."""
    resp = unauth_client.get("/login")
    assert "sidebar" not in resp.text.lower()


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def _login(client, username, password, next_url="/"):
    return client.post(
        "/login",
        data={
            "username": username,
            "password": password,
            "csrf_token": csrf_for(client),
            "next": next_url,
        },
        follow_redirects=False,
    )


def test_valid_login_sets_a_session_and_grants_access(unauth_client):
    import os

    resp = _login(unauth_client, os.environ["ADMIN_USERNAME"], os.environ["ADMIN_PASSWORD"])
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert "lkdash_session" in unauth_client.cookies

    assert unauth_client.get("/settings").status_code == 200


def test_bad_password_is_rejected_without_a_session(unauth_client):
    import os

    resp = _login(unauth_client, os.environ["ADMIN_USERNAME"], "not-the-password")
    assert resp.status_code == 401
    assert "Invalid username or password" in resp.text
    assert unauth_client.get("/settings").status_code == 401


def test_error_message_does_not_reveal_whether_the_user_exists(unauth_client):
    import os

    wrong_user = _login(unauth_client, "nobody-here", "whatever")
    reset_throttle()
    wrong_pass = _login(unauth_client, os.environ["ADMIN_USERNAME"], "whatever")

    assert wrong_user.status_code == wrong_pass.status_code == 401
    assert "Invalid username or password" in wrong_user.text
    assert "Invalid username or password" in wrong_pass.text


def test_login_requires_a_csrf_token(unauth_client):
    import os

    resp = unauth_client.post(
        "/login",
        data={
            "username": os.environ["ADMIN_USERNAME"],
            "password": os.environ["ADMIN_PASSWORD"],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_login_rotates_the_session_to_defeat_fixation(unauth_client):
    """The pre-login session must be discarded, not upgraded in place."""
    import os

    csrf_for(unauth_client)
    before = unauth_client.cookies.get("lkdash_session")

    _login(unauth_client, os.environ["ADMIN_USERNAME"], os.environ["ADMIN_PASSWORD"])
    after = unauth_client.cookies.get("lkdash_session")

    assert before != after


# ---------------------------------------------------------------------------
# Redirect handling
# ---------------------------------------------------------------------------

def test_login_honours_a_safe_next_target(unauth_client):
    import os

    resp = _login(
        unauth_client, os.environ["ADMIN_USERNAME"], os.environ["ADMIN_PASSWORD"],
        next_url="/rooms",
    )
    assert resp.headers["location"] == "/rooms"


@pytest.mark.parametrize("hostile", [
    "https://evil.example.com/steal",
    "//evil.example.com",
    "javascript:alert(1)",
    "",
])
def test_login_refuses_offsite_redirects(unauth_client, hostile):
    import os

    resp = _login(
        unauth_client, os.environ["ADMIN_USERNAME"], os.environ["ADMIN_PASSWORD"],
        next_url=hostile,
    )
    assert resp.headers["location"] == "/"


def test_safe_next_unit():
    assert session_auth.safe_next("/rooms") == "/rooms"
    assert session_auth.safe_next("//evil.com") == "/"
    assert session_auth.safe_next("https://evil.com") == "/"
    assert session_auth.safe_next("") == "/"
    assert session_auth.safe_next(None) == "/"


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------

def test_logout_clears_the_session(client):
    resp = client.post(
        "/logout",
        data={"csrf_token": csrf_for(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    assert client.get("/settings").status_code == 401


def test_logout_requires_csrf(client):
    resp = client.post("/logout", data={}, follow_redirects=False)
    assert resp.status_code == 403
    # Still signed in.
    assert client.get("/settings").status_code == 200


def test_logout_get_only_shows_a_confirmation_form(client):
    """A GET must not sign the user out — another site could trigger it."""
    resp = client.get("/logout", follow_redirects=False)
    assert resp.status_code == 200
    assert 'method="post"' in resp.text
    assert client.get("/settings").status_code == 200


# ---------------------------------------------------------------------------
# Session expiry
# ---------------------------------------------------------------------------

def test_expired_session_is_rejected(client, monkeypatch):
    monkeypatch.setattr(session_auth, "session_max_age", lambda: 1)
    monkeypatch.setattr(time, "time", lambda: time.monotonic() + 10_000_000)

    assert client.get("/settings").status_code == 401


def test_session_max_age_reads_the_environment(monkeypatch):
    monkeypatch.setenv("SESSION_MAX_AGE", "60")
    assert session_auth.session_max_age() == 60
    monkeypatch.setenv("SESSION_MAX_AGE", "not-a-number")
    assert session_auth.session_max_age() == session_auth.DEFAULT_SESSION_MAX_AGE


# ---------------------------------------------------------------------------
# Challenge shape
# ---------------------------------------------------------------------------

def test_html_request_is_redirected_to_login(unauth_client):
    resp = unauth_client.get(
        "/rooms", headers={"Accept": "text/html"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=%2Frooms"


def test_html_redirect_preserves_the_query_string(unauth_client):
    resp = unauth_client.get(
        "/rooms?sort=asc", headers={"Accept": "text/html"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login?next=%2Frooms%3Fsort%3Dasc"


def test_htmx_request_gets_hx_redirect(unauth_client):
    """HTMX swaps fragments, so a 303 would inject the login page into the DOM."""
    resp = unauth_client.get(
        "/rooms", headers={"HX-Request": "true", "Accept": "text/html"},
        follow_redirects=False,
    )
    assert resp.status_code == 401
    assert resp.headers["HX-Redirect"] == "/login?next=%2Frooms"


def test_api_request_gets_json_401(unauth_client):
    resp = unauth_client.get("/export.json", headers={"Accept": "application/json"})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Authentication required"}


def test_post_is_not_redirected(unauth_client):
    """A 303 on POST would silently turn a mutation into a GET of the login page."""
    resp = unauth_client.post(
        "/views", headers={"Accept": "text/html"}, data={}, follow_redirects=False
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Allowlist and deny-by-default
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/health", "/health/deep", "/login", "/logout"])
def test_public_paths_are_reachable_without_a_session(unauth_client, path):
    assert unauth_client.get(path).status_code in (200, 503)


def test_static_files_are_public(unauth_client):
    assert unauth_client.get("/static/css/style.css").status_code == 200


def test_events_stream_is_no_longer_public(unauth_client):
    """This endpoint used to stream room and participant identities to anyone."""
    resp = unauth_client.get("/events/stream", follow_redirects=False)
    assert resp.status_code in (303, 401)


def test_every_route_is_protected_unless_allowlisted(unauth_client):
    """Deny-by-default: the property that makes the middleware worth having.

    A route added tomorrow without a `requires_admin` dependency is still
    protected. If this fails, a new endpoint is exposing data anonymously.
    """
    from starlette.routing import Route

    unprotected = []
    for route in app.routes:
        if not isinstance(route, Route) or "GET" not in (route.methods or set()):
            continue
        path = route.path
        if session_auth.is_public(path) or "{" in path:
            continue
        resp = unauth_client.get(path, follow_redirects=False)
        if resp.status_code not in (303, 401, 403):
            unprotected.append((path, resp.status_code))

    assert not unprotected, f"anonymously reachable routes: {unprotected}"


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

def test_repeated_failures_are_throttled(unauth_client):
    import os

    for _ in range(MAX_ATTEMPTS):
        resp = _login(unauth_client, os.environ["ADMIN_USERNAME"], "wrong")
        assert resp.status_code == 401

    resp = _login(unauth_client, os.environ["ADMIN_USERNAME"], "wrong")
    assert resp.status_code == 429
    assert int(resp.headers["Retry-After"]) > 0


def test_throttle_does_not_block_a_different_username(unauth_client):
    import os

    for _ in range(MAX_ATTEMPTS):
        _login(unauth_client, "victim", "wrong")

    # The real account is keyed separately, so it is not collateral damage.
    resp = _login(unauth_client, os.environ["ADMIN_USERNAME"], os.environ["ADMIN_PASSWORD"])
    assert resp.status_code == 303


def test_successful_login_clears_the_throttle(unauth_client):
    import os

    for _ in range(MAX_ATTEMPTS - 1):
        _login(unauth_client, os.environ["ADMIN_USERNAME"], "wrong")

    assert _login(
        unauth_client, os.environ["ADMIN_USERNAME"], os.environ["ADMIN_PASSWORD"]
    ).status_code == 303

    with TestClient(app) as fresh:
        for _ in range(MAX_ATTEMPTS - 1):
            assert _login(
                fresh, os.environ["ADMIN_USERNAME"], "wrong"
            ).status_code == 401


# ---------------------------------------------------------------------------
# Sessions are per-client
# ---------------------------------------------------------------------------

def test_sessions_are_not_shared_between_clients(client):
    with TestClient(app) as other:
        assert other.get("/settings").status_code == 401
        log_in(other)
        assert other.get("/settings").status_code == 200


# ---------------------------------------------------------------------------
# Throttle-map bounds
#
# The map is keyed by the submitted username, which is attacker-controlled, so
# an unbounded map is an unauthenticated memory-exhaustion primitive.
# ---------------------------------------------------------------------------

def test_long_usernames_are_truncated_in_the_throttle_key(unauth_client):
    from app.routes import auth as auth_routes

    _login(unauth_client, "A" * 100_000, "wrong")

    assert all(len(key) < 200 for key in auth_routes._attempts)


def test_the_throttle_map_is_bounded(unauth_client, monkeypatch):
    from app.routes import auth as auth_routes

    monkeypatch.setattr(auth_routes, "MAX_TRACKED_KEYS", 8)
    for i in range(50):
        auth_routes._record_attempt(f"1.2.3.4|user{i}")

    assert len(auth_routes._attempts) <= 8


def test_expired_entries_are_evicted(monkeypatch):
    import time as time_module
    from app.routes import auth as auth_routes

    auth_routes.reset_throttle()
    auth_routes._record_attempt("1.2.3.4|old")

    later = time_module.monotonic() + auth_routes.ATTEMPT_WINDOW_SECONDS + 1
    monkeypatch.setattr(auth_routes.time, "monotonic", lambda: later)
    auth_routes._prune(later)

    assert "1.2.3.4|old" not in auth_routes._attempts


def test_forwarded_headers_are_ignored_by_default(unauth_client, monkeypatch):
    """X-Forwarded-For is client-supplied; trusting it would let anyone
    sidestep the throttle by rotating the header."""
    from app.routes import auth as auth_routes

    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)
    for i in range(MAX_ATTEMPTS + 1):
        resp = unauth_client.post(
            "/login",
            data={"username": "admin", "password": "wrong",
                  "csrf_token": csrf_for(unauth_client)},
            headers={"X-Forwarded-For": f"10.0.0.{i}"},
            follow_redirects=False,
        )
    assert resp.status_code == 429


def test_forwarded_headers_are_used_when_trusted(unauth_client, monkeypatch):
    """Behind a real proxy the peer address is the proxy, collapsing all
    callers onto one key; the opt-in restores per-client throttling."""
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")

    for i in range(MAX_ATTEMPTS + 5):
        resp = unauth_client.post(
            "/login",
            data={"username": "admin", "password": "wrong",
                  "csrf_token": csrf_for(unauth_client)},
            headers={"X-Forwarded-For": f"10.0.0.{i}"},
            follow_redirects=False,
        )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Health disclosure
# ---------------------------------------------------------------------------

def test_health_deep_withholds_details_from_anonymous_callers(unauth_client, monkeypatch):
    """Mongo connection errors embed internal hostnames and replica-set names."""
    from app.db import mongo

    monkeypatch.setattr(
        mongo, "_state",
        {"enabled": True, "connected": False,
         "error": "internal-mongo.coolify.local:27017 replicaSet=rs0 timed out",
         "index_errors": ["usage_events[...]: boom"]},
    )

    body = unauth_client.get("/health/deep").json()
    assert "internal-mongo" not in str(body)
    assert body["mongodb"]["connected"] is False
    assert body["mongodb"]["index_errors"] == 1


def test_health_deep_shows_details_once_signed_in(client, monkeypatch):
    from app.db import mongo

    monkeypatch.setattr(
        mongo, "_state",
        {"enabled": True, "connected": False,
         "error": "internal-mongo.coolify.local:27017 timed out", "index_errors": []},
    )

    body = client.get("/health/deep").json()
    assert "internal-mongo" in body["mongodb"]["error"]
