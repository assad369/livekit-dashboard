"""Pool of LiveKitClient instances, one per project credential set.

Previously `get_livekit_client` constructed a fresh LiveKitClient on every
request and nothing ever called `close()`, so each request leaked an aiohttp
session. Pooling fixes that and is also what makes multi-project work without
touching the ~46 routes that depend on the client.

Keyed by `LiveKitClient.cache_key`, which embeds a fingerprint of the URL and
API key — so editing a project's credentials naturally produces a new client
rather than silently reusing one built with the old ones.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict

from app.services.livekit import LiveKitClient
from app.services.projects import Project

logger = logging.getLogger(__name__)

_clients: Dict[str, LiveKitClient] = {}
_lock = asyncio.Lock()


def _build(project: Project) -> LiveKitClient:
    return LiveKitClient(
        project.livekit_url,
        project.api_key,
        project.api_secret,
        sip_enabled=project.sip_enabled,
        project_id=project.id,
        project_slug=project.slug,
    )


async def get_client(project: Project) -> LiveKitClient:
    """Return the pooled client for *project*, creating it if needed."""
    probe = _build(project)
    key = probe.cache_key

    existing = _clients.get(key)
    if existing is not None:
        return existing

    async with _lock:
        existing = _clients.get(key)
        if existing is not None:
            return existing
        _clients[key] = probe
        logger.debug("created LiveKit client for project %s", project.slug)
        return probe


async def invalidate(project_id: str) -> None:
    """Close and drop every client belonging to *project_id*.

    Called after a project's credentials change or it is deleted, so no
    request keeps talking to the old server.
    """
    async with _lock:
        stale = [k for k in _clients if k.startswith(f"{project_id}|")]
        for key in stale:
            client = _clients.pop(key)
            await _safe_close(client)

    from app.services import cache as dispatch_cache
    for key in stale:
        dispatch_cache.invalidate(key)


async def close_all() -> None:
    """Close every pooled client. Called from the app's shutdown hook."""
    async with _lock:
        clients = list(_clients.values())
        _clients.clear()
    for client in clients:
        await _safe_close(client)


async def _safe_close(client: LiveKitClient) -> None:
    try:
        await client.close()
    except Exception as exc:  # pragma: no cover - shutdown best effort
        logger.warning("error closing LiveKit client: %s", exc)


def stats() -> dict:
    return {
        "pooled_clients": len(_clients),
        "projects": sorted({k.split("|", 1)[0] for k in _clients}),
    }
