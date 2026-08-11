"""Session authentication, enforced as middleware.

Enforcement used to be per-route (`dependencies=[Depends(requires_admin)]` on
each decorator), which meant a newly added route was unauthenticated until
someone remembered — and one already was: `/events/stream` streamed live room
and participant identities to anyone who asked.

This middleware inverts that: everything is protected unless explicitly
allowlisted. The per-route dependencies stay in place as a cheap second check;
`requires_admin` now reads what this middleware put on the request.

Implemented as raw ASGI rather than BaseHTTPMiddleware so it does not buffer
or wrap the SSE stream.
"""

from __future__ import annotations

import os
import time
from typing import Iterable
from urllib.parse import quote

from starlette.datastructures import Headers
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send


DEFAULT_SESSION_MAX_AGE = 86400  # 24h

# Reachable without a session.
PUBLIC_EXACT = frozenset({
    "/health",
    "/health/deep",
    "/login",
    "/logout",
    "/favicon.ico",
})

PUBLIC_PREFIXES = (
    "/static/",
    # Inbound LiveKit webhooks authenticate with their own signed JWT.
    "/webhooks/",
)

SESSION_USER_ID = "uid"
SESSION_USERNAME = "username"
SESSION_AUTH_AT = "auth_at"


def session_max_age() -> int:
    try:
        return int(os.environ.get("SESSION_MAX_AGE", DEFAULT_SESSION_MAX_AGE))
    except ValueError:
        return DEFAULT_SESSION_MAX_AGE


def is_public(path: str) -> bool:
    return path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIXES)


def safe_next(target: str) -> str:
    """Sanitise a post-login redirect target.

    Only same-origin absolute paths are allowed. `//evil.com` is a
    protocol-relative URL that browsers treat as another origin, so it is
    rejected alongside anything not starting with a slash.
    """
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


def login_url(path: str, query: bytes = b"") -> str:
    target = path
    if query:
        target = f"{path}?{query.decode('latin-1')}"
    if target in ("/", ""):
        return "/login"
    return f"/login?next={quote(target, safe='')}"


def current_user(scope: Scope) -> dict | None:
    return scope.get("state", {}).get("user")


class AuthMiddleware:
    """Reject unauthenticated requests to non-public paths."""

    def __init__(self, app: ASGIApp, public_exact: Iterable[str] | None = None):
        self.app = app
        self.public_exact = frozenset(public_exact) if public_exact else PUBLIC_EXACT

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        session = scope.get("session") or {}
        user = self._authenticated_user(session)

        # Public paths still identify the caller when they happen to have a
        # session — /health/deep uses it to decide how much detail to disclose.
        # They simply do not *require* one.
        path = scope.get("path", "")
        if path in self.public_exact or path.startswith(PUBLIC_PREFIXES):
            if user is not None:
                scope.setdefault("state", {})["user"] = user
            await self.app(scope, receive, send)
            return

        if user is None:
            response = self._challenge(scope)
            await response(scope, receive, send)
            return

        scope.setdefault("state", {})["user"] = user
        await self.app(scope, receive, send)

    @staticmethod
    def _authenticated_user(session: dict) -> dict | None:
        uid = session.get(SESSION_USER_ID)
        auth_at = session.get(SESSION_AUTH_AT)
        if not uid or not auth_at:
            return None

        # Absolute session lifetime. The signed cookie also carries its own
        # max_age, but that is client-controlled in the sense that a stolen
        # cookie stays valid until it expires; this bounds it server-side too.
        try:
            if time.time() - float(auth_at) > session_max_age():
                return None
        except (TypeError, ValueError):
            return None

        return {
            "id": uid,
            "username": session.get(SESSION_USERNAME, ""),
            "auth_at": auth_at,
        }

    @staticmethod
    def _challenge(scope: Scope) -> Response:
        headers = Headers(scope=scope)
        path = scope.get("path", "/")
        query = scope.get("query_string", b"")
        target = login_url(path, query)

        # HTMX swaps fragments, so a 303 would inject the login page into a
        # table cell. HX-Redirect tells it to navigate the whole window.
        if headers.get("HX-Request", "").lower() == "true":
            return Response(
                status_code=401,
                headers={"HX-Redirect": target},
            )

        accept = headers.get("accept", "")
        if scope.get("method") == "GET" and "text/html" in accept:
            return RedirectResponse(target, status_code=303)

        return JSONResponse({"detail": "Authentication required"}, status_code=401)
