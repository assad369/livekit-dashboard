"""Pytest configuration and fixtures"""
import os
import re
import pytest
from fastapi.testclient import TestClient


_CSRF_META = re.compile(r'name="csrf-token" content="([^"]+)"')


def csrf_for(client) -> str:
    """Return a CSRF token bound to *client*'s session.

    CSRF tokens are tied to the browser session, so a token minted in
    isolation no longer validates — that is the whole point of the binding.
    Tests must obtain one the way a browser does: make a request, let the
    CSRF middleware seed the session, and read the token off the page.

    GET /logout is used as the seed page: it is public (so this works before
    sign-in too), renders the token, needs no LiveKit mocks, and has no side
    effects — signing out is the POST.
    """
    resp = client.get("/logout", follow_redirects=False)
    match = _CSRF_META.search(resp.text)
    if not match:  # pragma: no cover - signals a broken template/middleware
        raise AssertionError(
            f"no csrf-token meta in response (status {resp.status_code})"
        )
    return match.group(1)


@pytest.fixture(scope="session", autouse=True)
def setup_test_env():
    """Setup test environment variables"""
    # Set test environment variables
    os.environ["LIVEKIT_URL"] = "http://localhost:7880"
    os.environ["LIVEKIT_API_KEY"] = "test-key"
    os.environ["LIVEKIT_API_SECRET"] = "test-secret"
    os.environ["ADMIN_USERNAME"] = "admin"
    os.environ["ADMIN_PASSWORD"] = "testpass"
    os.environ["APP_SECRET_KEY"] = "test-secret-key"
    os.environ["DEBUG"] = "true"
    os.environ["ENABLE_SIP"] = "false"


def log_in(test_client) -> None:
    """Establish an authenticated session on *test_client*."""
    from app.routes.auth import reset_throttle

    reset_throttle()
    page = test_client.get("/login")
    match = _CSRF_META.search(page.text)
    assert match, "login page did not render a CSRF token"

    resp = test_client.post(
        "/login",
        data={
            "username": os.environ["ADMIN_USERNAME"],
            "password": os.environ["ADMIN_PASSWORD"],
            "csrf_token": match.group(1),
            "next": "/",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"login failed: {resp.status_code}"


@pytest.fixture
def unauth_client():
    """A test client with no session — for testing the auth gate itself."""
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def client(unauth_client):
    """A test client with an authenticated session."""
    log_in(unauth_client)
    return unauth_client


@pytest.fixture
def auth_headers():
    """Retained so existing tests keep passing.

    Authentication moved from a per-request Basic header to a session cookie
    carried by the `client` fixture, so there is nothing left to put in a
    header. Kept as an empty dict rather than deleted so the ~19 test files
    that pass `headers=auth_headers` need no changes.
    """
    return {}

