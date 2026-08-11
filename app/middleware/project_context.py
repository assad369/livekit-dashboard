"""Resolve the active project once per request.

Putting this in middleware is what lets the project switcher work without
editing a single route handler or template context dict: `base.html.j2` reads
`request.state.project` and `request.state.projects` directly, and
`get_livekit_client` picks up the same resolved project.
"""

from __future__ import annotations

import logging
import time

from starlette.types import ASGIApp, Receive, Scope, Send

from app.db import mongo
from app.security.session_auth import PUBLIC_PREFIXES
from app.services import projects as project_service

logger = logging.getLogger(__name__)

# The project list is re-read on nearly every page render; a short TTL keeps
# that off the database without making the switcher feel stale.
_LIST_TTL_SECONDS = 10.0
_list_cache: dict = {"projects": [], "ts": 0.0}


def invalidate_list_cache() -> None:
    """Force the next request to re-read the project list."""
    _list_cache["ts"] = 0.0


async def _cached_projects(db) -> list:
    now = time.monotonic()
    if now - _list_cache["ts"] < _LIST_TTL_SECONDS:
        return _list_cache["projects"]

    projects = await project_service.list_projects(db)
    _list_cache.update({"projects": projects, "ts": now})
    return projects


class ProjectContextMiddleware:
    """Attach the active project and the project list to each request."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        state = scope.setdefault("state", {})

        # Static assets and self-authenticating webhooks resolve their own
        # project (or need none), so skip the lookup entirely.
        if path.startswith(PUBLIC_PREFIXES) or state.get("user") is None:
            state.setdefault("project", None)
            state.setdefault("projects", [])
            await self.app(scope, receive, send)
            return

        db = mongo.get_database()
        try:
            state["projects"] = await _cached_projects(db)
            state["project"] = await project_service.resolve_active(
                scope.get("session") or {}, db
            )
        except project_service.NoProjectConfigured:
            # Render an onboarding banner rather than a 500.
            state["project"] = None
            state["projects"] = []
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("failed to resolve the active project: %s", exc)
            state["project"] = None
            state["projects"] = []

        await self.app(scope, receive, send)
