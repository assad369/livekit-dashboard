"""CSRF Protection.

Tokens are signed with APP_SECRET_KEY *and* bound to the caller's session.
The signature alone is not enough: without the session binding any token this
app ever issued — including one an attacker fetched anonymously — would
validate against an authenticated user's request, which defeats the point.
"""

import os
import secrets
from typing import Optional

from fastapi import Request, HTTPException, status
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired


SESSION_KEY = "_csrf"


def get_secret_key() -> str:
    """Get secret key from environment"""
    return os.environ.get("APP_SECRET_KEY", "dev-secret-key-change-in-production")


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_secret_key())


def generate_csrf_token(raw: Optional[str] = None) -> str:
    """Sign *raw* (or a fresh random value) into a transportable CSRF token."""
    return _serializer().dumps(raw or secrets.token_urlsafe(32), salt="csrf-token")


def _unsign(token: str, max_age: int) -> Optional[str]:
    """Return the raw value inside *token*, or None if it doesn't verify."""
    if not token:
        return None
    try:
        return _serializer().loads(token, salt="csrf-token", max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None


def validate_csrf_token(
    token: str,
    max_age: int = 3600,
    request: Optional[Request] = None,
) -> bool:
    """Validate a CSRF token.

    When *request* is supplied the token must also match the value stored in
    that request's session. Callers that omit it get signature-only checking,
    which is weaker — pass the request wherever one is available.
    """
    raw = _unsign(token, max_age)
    if raw is None:
        return False

    if request is None:
        return True

    expected = _session_raw(request)
    if not expected:
        return False
    return secrets.compare_digest(raw, expected)


def _session_raw(request: Request) -> str:
    """Read the session's raw CSRF value, tolerating a missing session."""
    try:
        return request.session.get(SESSION_KEY, "")
    except (AssertionError, AttributeError):
        # SessionMiddleware not installed (e.g. a bare ASGI test app).
        return ""


def get_csrf_token(request: Request) -> str:
    """Get or generate the CSRF token for a request.

    The raw value is persisted in the session so every token issued to one
    browser shares a secret that tokens issued to other browsers do not.
    """
    if hasattr(request.state, "csrf_token"):
        return request.state.csrf_token

    raw = _session_raw(request)
    if not raw:
        raw = secrets.token_urlsafe(32)
        try:
            request.session[SESSION_KEY] = raw
        except (AssertionError, AttributeError):
            pass  # session-less mode: fall back to signature-only tokens

    token = generate_csrf_token(raw)
    request.state.csrf_token = token
    return token


def rotate_csrf_token(request: Request) -> str:
    """Issue a brand-new session CSRF secret.

    Call after login: ``session.clear()`` drops the old secret, and any form
    rendered before login must not keep working afterwards.
    """
    raw = secrets.token_urlsafe(32)
    try:
        request.session[SESSION_KEY] = raw
    except (AssertionError, AttributeError):
        pass
    token = generate_csrf_token(raw)
    request.state.csrf_token = token
    return token


async def verify_csrf_token(request: Request) -> None:
    """Verify CSRF token from form data"""
    if request.method in ["POST", "PUT", "DELETE", "PATCH"]:
        form_data = await request.form()
        token = form_data.get("csrf_token", "")

        if not validate_csrf_token(token, request=request):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid or expired CSRF token",
            )
