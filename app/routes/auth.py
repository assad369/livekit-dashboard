"""Authentication routes: login form, session establishment, logout."""

import logging
import os
import time
from collections import deque
from typing import Deque, Dict

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import mongo
from app.security.csrf import get_csrf_token, rotate_csrf_token, verify_csrf_token
from app.security.session_auth import (
    SESSION_AUTH_AT,
    SESSION_USER_ID,
    SESSION_USERNAME,
    safe_next,
)
from app.services import users as user_service

logger = logging.getLogger(__name__)

router = APIRouter()

# Login throttling. In-memory is correct for this deployment — the Dockerfile
# runs a single uvicorn process with no --workers. If that ever changes this
# needs a shared backing store; see docs/ARCHITECTURE.md.
MAX_ATTEMPTS = 10
ATTEMPT_WINDOW_SECONDS = 300
# Bounds on the throttle map itself. Without them the map is an unauthenticated
# memory-exhaustion primitive: the key embeds the submitted username, so an
# attacker could allocate an entry per request with an arbitrarily long one.
MAX_TRACKED_KEYS = 4096
MAX_USERNAME_KEY_LEN = 64

_attempts: Dict[str, Deque[float]] = {}


def _client_ip(request: Request) -> str:
    """The caller's address, honouring X-Forwarded-For only when told to.

    Behind a reverse proxy `request.client.host` is the proxy, which collapses
    every caller onto one throttle key. Trusting the header unconditionally
    would be worse — it is client-supplied — so this is opt-in via
    TRUST_PROXY_HEADERS, to be set only when a proxy actually rewrites it.
    """
    if os.environ.get("TRUST_PROXY_HEADERS", "false").lower() == "true":
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"


def _throttle_key(request: Request, username: str) -> str:
    return f"{_client_ip(request)}|{username[:MAX_USERNAME_KEY_LEN]}"


def _prune(now: float) -> None:
    """Drop keys whose attempts have all aged out."""
    for key in [k for k, v in _attempts.items()
                if not v or now - v[-1] > ATTEMPT_WINDOW_SECONDS]:
        _attempts.pop(key, None)


def _retry_after(key: str) -> int:
    """Seconds until the oldest attempt in the window expires, or 0."""
    now = time.monotonic()
    attempts = _attempts.get(key)
    if attempts is None:
        return 0
    while attempts and now - attempts[0] > ATTEMPT_WINDOW_SECONDS:
        attempts.popleft()
    if len(attempts) < MAX_ATTEMPTS:
        return 0
    return max(1, int(ATTEMPT_WINDOW_SECONDS - (now - attempts[0])))


def _record_attempt(key: str) -> None:
    now = time.monotonic()
    if key not in _attempts and len(_attempts) >= MAX_TRACKED_KEYS:
        _prune(now)
        if len(_attempts) >= MAX_TRACKED_KEYS:
            # Still full of live entries: this is a flood, so stop growing.
            # Existing throttles keep applying.
            return
    _attempts.setdefault(key, deque()).append(now)


def _clear_attempts(key: str) -> None:
    _attempts.pop(key, None)


def reset_throttle() -> None:
    """Clear all throttling state. Used by tests."""
    _attempts.clear()


def _render_login(request: Request, *, error: str = "", next_url: str = "/",
                  username: str = "", status_code: int = 200) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "login.html.j2",
        {
            "request": request,
            "error": error,
            "next": next_url,
            "username": username,
            "csrf_token": get_csrf_token(request),
        },
        status_code=status_code,
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    """Render the login form, or bounce to the dashboard if already signed in."""
    if request.session.get(SESSION_USER_ID):
        return RedirectResponse(safe_next(next), status_code=303)
    return _render_login(request, next_url=safe_next(next))


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
    csrf_token: str = Form(""),
):
    """Verify credentials and establish a session."""
    await verify_csrf_token(request)

    target = safe_next(next)
    key = _throttle_key(request, username)

    retry_after = _retry_after(key)
    if retry_after:
        response = _render_login(
            request,
            error="Too many failed attempts. Try again shortly.",
            next_url=target,
            username=username,
            status_code=429,
        )
        response.headers["Retry-After"] = str(retry_after)
        return response

    user = await user_service.authenticate(mongo.get_database(), username, password)

    if user is None:
        _record_attempt(key)
        logger.warning("failed login for %r from %s", username, key.split("|")[0])
        # Deliberately generic: distinguishing "no such user" from "wrong
        # password" tells an attacker which usernames are worth attacking.
        return _render_login(
            request,
            error="Invalid username or password.",
            next_url=target,
            username=username,
            status_code=401,
        )

    _clear_attempts(key)

    # Session fixation defence: discard anything the pre-login session held
    # rather than upgrading it in place.
    request.session.clear()
    request.session[SESSION_USER_ID] = user.id
    request.session[SESSION_USERNAME] = user.username
    request.session[SESSION_AUTH_AT] = time.time()
    rotate_csrf_token(request)

    logger.info("login succeeded for %r (source=%s)", user.username, user.source)
    return RedirectResponse(target, status_code=303)


@router.post("/logout")
async def logout(request: Request, csrf_token: str = Form("")):
    """Clear the session."""
    await verify_csrf_token(request)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/logout", response_class=HTMLResponse)
async def logout_page(request: Request):
    """Confirmation page with a POST form.

    Logout mutates state, so it must not be reachable by GET — a link or an
    <img> on another site could otherwise sign the operator out.
    """
    return request.app.state.templates.TemplateResponse(
        request,
        "logout.html.j2",
        {
            "request": request,
            "csrf_token": get_csrf_token(request),
            "signed_in": bool(request.session.get(SESSION_USER_ID)),
        },
    )
