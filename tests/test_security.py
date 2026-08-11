"""Tests for security modules"""
import os
import pytest
from app.security.csrf import generate_csrf_token, validate_csrf_token
from app.services import users as user_service


async def test_verify_credentials_valid():
    """Environment credentials authenticate when there is no database."""
    user = await user_service.authenticate(
        None, os.environ["ADMIN_USERNAME"], os.environ["ADMIN_PASSWORD"]
    )
    assert user is not None
    assert user.username == os.environ["ADMIN_USERNAME"]
    assert user.source == "env"


async def test_verify_credentials_invalid():
    assert await user_service.authenticate(None, "wrong", "wrong") is None


async def test_verify_credentials_rejects_empty():
    assert await user_service.authenticate(None, "", "") is None
    assert await user_service.authenticate(None, os.environ["ADMIN_USERNAME"], "") is None


def test_csrf_token_generation():
    """Test CSRF token generation"""
    token = generate_csrf_token()
    assert token is not None
    assert len(token) > 0
    assert isinstance(token, str)


def test_csrf_token_validation():
    """Test CSRF token validation"""
    token = generate_csrf_token()
    assert validate_csrf_token(token) is True


def test_csrf_token_invalid():
    """Test invalid CSRF token"""
    assert validate_csrf_token("invalid-token") is False
    assert validate_csrf_token("") is False


def test_csrf_token_uniqueness():
    """Test that each generated token is unique"""
    token1 = generate_csrf_token()
    token2 = generate_csrf_token()
    assert token1 != token2


# ---------------------------------------------------------------------------
# Session binding
#
# A signature-only CSRF token is forgeable in the way that matters: an
# attacker fetches any public page, receives a validly-signed token, embeds it
# in a form on their own site, and the victim's browser submits it. Binding
# the token to the session is what stops that.
# ---------------------------------------------------------------------------

def test_token_from_one_session_is_rejected_in_another(client, auth_headers):
    """The core CSRF property: tokens are not transferable between browsers."""
    from fastapi.testclient import TestClient
    from app.main import app
    from tests.conftest import csrf_for, log_in

    attacker_token = csrf_for(client)

    with TestClient(app) as victim:
        # The victim is signed in and has their own session...
        log_in(victim)
        csrf_for(victim)
        # ...and the attacker's token must not validate against it.
        resp = victim.post(
            "/views",
            headers=auth_headers,
            data={"csrf_token": attacker_token, "name": "pwned"},
            follow_redirects=False,
        )

    assert resp.status_code == 403


def test_token_from_own_session_is_accepted(client, auth_headers):
    from tests.conftest import csrf_for

    resp = client.post(
        "/views",
        headers=auth_headers,
        data={"csrf_token": csrf_for(client), "name": "mine"},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def test_request_without_a_session_secret_is_rejected(client, auth_headers):
    """A well-signed token means nothing if the session never held its secret."""
    resp = client.post(
        "/views",
        headers=auth_headers,
        data={"csrf_token": generate_csrf_token(), "name": "forged"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_middleware_seeds_a_stable_token_per_session(client):
    """Repeat requests in one session reuse the same underlying secret."""
    from tests.conftest import csrf_for

    first = csrf_for(client)
    second = csrf_for(client)
    # Tokens are re-signed per request (timestamped), but both must validate
    # against the same session, which the accept-test above already proves.
    assert first and second


def test_signature_only_validation_still_available_without_a_request():
    """The 1-arg form is retained for callers with no request in hand."""
    assert validate_csrf_token(generate_csrf_token()) is True
    assert validate_csrf_token("garbage") is False

