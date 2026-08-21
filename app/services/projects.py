"""Project storage — one LiveKit deployment's credentials per project.

Mirrors how the LiveKit Cloud dashboard is organised: each project carries its
own URL and API key pair, and the whole UI is scoped to whichever is active.

Two projects may share a `livekit_url` with different keys, so `api_key` is
deliberately not unique and webhook resolution has to disambiguate by trying
each candidate's secret.

API secrets are encrypted at rest (`app.security.crypto`); the plaintext only
exists in memory inside a `Project`.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from app.security import crypto

logger = logging.getLogger(__name__)

COLLECTION = "projects"
ENV_PROJECT_ID = "env"


class NoProjectConfigured(RuntimeError):
    """Raised when no project can be resolved and none is configured."""


class ProjectError(RuntimeError):
    """User-facing validation or persistence failure."""


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    slug: str
    livekit_url: str
    api_key: str
    api_secret: str  # decrypted; never persisted in this form
    sip_enabled: bool = False
    prometheus_url: str = ""
    is_default: bool = False
    source: str = "mongo"  # "mongo" | "env"

    @property
    def is_env(self) -> bool:
        return self.source == "env"

    def masked_key(self) -> str:
        return _mask(self.api_key)

    def masked_secret(self) -> str:
        return _mask(self.api_secret)


def _mask(value: str) -> str:
    if not value:
        return "(not set)"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "project"


URL_SCHEMES = ("ws://", "wss://", "http://", "https://")


def normalize_livekit_url(url: str) -> str:
    """Clean a user- or env-supplied LiveKit URL.

    Hand-edited `.env` files and copy-paste produce URLs the LiveKit SDK cannot
    parse: it runs them through `urlparse` and rebuilds `scheme://netloc/path`,
    so `wss:\\/\\/host` becomes `https:///\\/\\/host` and every API call fails
    with an invalid-URL error rather than anything diagnosable.

    Repairs only unambiguous damage — wrapping quotes, whitespace,
    JSON-escaped slashes, missing slashes after the scheme, a trailing slash.
    A value with no recognisable scheme is returned unchanged so it still fails
    validation instead of being guessed at.
    """
    cleaned = url.strip()
    for quote in ('"', "'"):
        if len(cleaned) >= 2 and cleaned[0] == quote and cleaned[-1] == quote:
            cleaned = cleaned[1:-1].strip()
            break

    cleaned = cleaned.replace("\\/", "/")
    # `wss:host` / `wss:/host` — a scheme whose slashes were lost in editing.
    cleaned = re.sub(r"^(ws|wss|http|https):/{0,1}(?=[^/])", r"\1://", cleaned)
    return cleaned.rstrip("/")


async def _unique_slug(db, name: str, *, exclude_id: str | None = None) -> str:
    base = slugify(name)
    candidate = base
    suffix = 2
    while True:
        query: dict[str, Any] = {"slug": candidate}
        existing = await db[COLLECTION].find_one(query)
        if existing is None or str(existing["_id"]) == exclude_id:
            return candidate
        candidate = f"{base}-{suffix}"
        suffix += 1


# ---------------------------------------------------------------------------
# The environment project — the single-project deployment this app started as
# ---------------------------------------------------------------------------

def env_project() -> Optional[Project]:
    """Build a Project from LIVEKIT_* env vars, or None if they are not set."""
    raw_url = os.environ.get("LIVEKIT_URL", "")
    key = os.environ.get("LIVEKIT_API_KEY", "")
    secret = os.environ.get("LIVEKIT_API_SECRET", "")
    if not (raw_url and key and secret):
        return None

    # Unlike projects created in the UI, env vars bypass `_validate`. Without
    # this check a malformed URL surfaces only as a connection error on every
    # poll, forever; better to report it once and act unconfigured.
    url = normalize_livekit_url(raw_url)
    if not url.startswith(URL_SCHEMES):
        logger.error(
            "LIVEKIT_URL is malformed: %r — expected e.g. "
            "wss://your-project.livekit.cloud or http://localhost:7880",
            raw_url,
        )
        return None

    return Project(
        id=ENV_PROJECT_ID,
        name="Environment",
        slug="environment",
        livekit_url=url,
        api_key=key,
        api_secret=secret,
        sip_enabled=os.environ.get("ENABLE_SIP", "false").lower() == "true",
        prometheus_url=os.environ.get("LIVEKIT_PROMETHEUS_URL", ""),
        is_default=True,
        source="env",
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _to_project(doc: dict) -> Optional[Project]:
    secret = crypto.try_decrypt(doc.get("api_secret_enc", ""))
    if secret is None:
        logger.error(
            "project %r has an undecryptable API secret — check APP_ENCRYPTION_KEY",
            doc.get("slug"),
        )
        return None

    return Project(
        id=str(doc["_id"]),
        name=doc.get("name", ""),
        slug=doc.get("slug", ""),
        livekit_url=doc.get("livekit_url", ""),
        api_key=doc.get("api_key", ""),
        api_secret=secret,
        sip_enabled=bool(doc.get("sip_enabled", False)),
        prometheus_url=doc.get("prometheus_url", "") or "",
        is_default=bool(doc.get("is_default", False)),
        source="mongo",
    )


async def list_projects(db, *, include_archived: bool = False) -> list[Project]:
    if db is None:
        env = env_project()
        return [env] if env else []

    query: dict[str, Any] = {} if include_archived else {"archived": {"$ne": True}}
    cursor = db[COLLECTION].find(query).sort("created_at", 1)
    projects = []
    async for doc in cursor:
        project = _to_project(doc)
        if project is not None:
            projects.append(project)
    return projects


async def get_project(db, project_id: str) -> Optional[Project]:
    if project_id == ENV_PROJECT_ID:
        return env_project()
    if db is None:
        return None
    doc = await db[COLLECTION].find_one({"_id": project_id})
    return _to_project(doc) if doc else None


async def get_by_slug(db, slug: str) -> Optional[Project]:
    if db is None:
        return None
    doc = await db[COLLECTION].find_one({"slug": slug})
    return _to_project(doc) if doc else None


async def get_by_api_key(db, api_key: str) -> list[Project]:
    """All projects using *api_key*, for webhook issuer resolution.

    Returns a list because the key is not unique: the caller disambiguates by
    verifying the webhook signature against each candidate's secret.
    """
    candidates: list[Project] = []

    if db is not None:
        cursor = db[COLLECTION].find({"api_key": api_key, "archived": {"$ne": True}})
        async for doc in cursor:
            project = _to_project(doc)
            if project is not None:
                candidates.append(project)

    env = env_project()
    if env and env.api_key == api_key:
        candidates.append(env)

    return candidates


async def resolve_active(session: dict, db) -> Project:
    """Pick the project this request operates on.

    Precedence: the session's choice, then the default, then the oldest, then
    the environment project. Falling back rather than erroring is what keeps a
    single-project deployment working unchanged.
    """
    selected = (session or {}).get("project_id")
    if selected:
        project = await get_project(db, selected)
        if project is not None:
            return project

    projects = await list_projects(db)
    if projects:
        for project in projects:
            if project.is_default:
                return project
        return projects[0]

    env = env_project()
    if env is not None:
        return env

    raise NoProjectConfigured(
        "No LiveKit project is configured. Add one on the Projects page, or set "
        "LIVEKIT_URL, LIVEKIT_API_KEY and LIVEKIT_API_SECRET."
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def _validate(name: str, livekit_url: str, api_key: str, api_secret: str) -> None:
    if not name.strip():
        raise ProjectError("Name is required.")
    if not livekit_url.strip():
        raise ProjectError("LiveKit URL is required.")
    if not livekit_url.startswith(URL_SCHEMES):
        raise ProjectError("LiveKit URL must start with ws://, wss://, http:// or https://.")
    if not api_key.strip():
        raise ProjectError("API key is required.")
    if not api_secret:
        raise ProjectError("API secret is required.")


async def _clear_other_defaults(db, keep_id: str | None) -> None:
    query: dict[str, Any] = {"is_default": True}
    if keep_id is not None:
        query["_id"] = {"$ne": keep_id}
    await db[COLLECTION].update_many(query, {"$set": {"is_default": False}})


async def create_project(
    db,
    *,
    name: str,
    livekit_url: str,
    api_key: str,
    api_secret: str,
    sip_enabled: bool = False,
    prometheus_url: str = "",
    make_default: bool = False,
    created_by: str = "",
) -> Project:
    if db is None:
        raise ProjectError("A database is required to create projects. Set MONGODB_URI.")

    livekit_url = normalize_livekit_url(livekit_url)
    _validate(name, livekit_url, api_key, api_secret)

    error = crypto.configuration_error()
    if error:
        raise ProjectError(error)

    existing_count = await db[COLLECTION].count_documents({})
    is_default = make_default or existing_count == 0

    if is_default:
        await _clear_other_defaults(db, keep_id=None)

    doc = {
        "name": name.strip(),
        "slug": await _unique_slug(db, name),
        "livekit_url": livekit_url,  # already normalized above
        "api_key": api_key.strip(),
        "api_secret_enc": crypto.encrypt(api_secret),
        "sip_enabled": bool(sip_enabled),
        "prometheus_url": prometheus_url.strip(),
        "is_default": is_default,
        "archived": False,
        "created_at": _now(),
        "updated_at": _now(),
        "created_by": created_by,
    }
    result = await db[COLLECTION].insert_one(doc)
    doc["_id"] = result.inserted_id

    project = _to_project(doc)
    if project is None:  # pragma: no cover - we just encrypted it
        raise ProjectError("Failed to read back the created project.")
    return project


async def update_project(
    db,
    project_id: str,
    *,
    name: str,
    livekit_url: str,
    api_key: str,
    api_secret: str = "",
    sip_enabled: bool = False,
    prometheus_url: str = "",
) -> Project:
    """Update a project. A blank *api_secret* leaves the stored one unchanged."""
    if db is None:
        raise ProjectError("A database is required to edit projects.")

    doc = await db[COLLECTION].find_one({"_id": project_id})
    if doc is None:
        raise ProjectError("Project not found.")

    # Validate against the effective secret so "leave unchanged" is not
    # mistaken for "no secret set".
    livekit_url = normalize_livekit_url(livekit_url)
    _validate(name, livekit_url, api_key, api_secret or "unchanged")

    updates: dict[str, Any] = {
        "name": name.strip(),
        "livekit_url": livekit_url,  # already normalized above
        "api_key": api_key.strip(),
        "sip_enabled": bool(sip_enabled),
        "prometheus_url": prometheus_url.strip(),
        "updated_at": _now(),
    }

    if name.strip() != doc.get("name"):
        updates["slug"] = await _unique_slug(db, name, exclude_id=project_id)

    if api_secret:
        error = crypto.configuration_error()
        if error:
            raise ProjectError(error)
        updates["api_secret_enc"] = crypto.encrypt(api_secret)

    await db[COLLECTION].update_one({"_id": project_id}, {"$set": updates})

    updated = await get_project(db, project_id)
    if updated is None:  # pragma: no cover
        raise ProjectError("Failed to read back the updated project.")
    return updated


async def set_default(db, project_id: str) -> None:
    if db is None:
        raise ProjectError("A database is required.")
    await _clear_other_defaults(db, keep_id=project_id)
    await db[COLLECTION].update_one({"_id": project_id}, {"$set": {"is_default": True}})


async def delete_project(db, project_id: str, *, purge_data: bool = False) -> None:
    """Delete a project, refusing to remove the last one."""
    if db is None:
        raise ProjectError("A database is required.")

    doc = await db[COLLECTION].find_one({"_id": project_id})
    if doc is None:
        raise ProjectError("Project not found.")

    remaining = await db[COLLECTION].count_documents({"archived": {"$ne": True}})
    if remaining <= 1:
        raise ProjectError(
            "Cannot delete the only project — the dashboard would have nothing to show."
        )

    await db[COLLECTION].delete_one({"_id": project_id})

    if doc.get("is_default"):
        # Promote another project so resolve_active has a stable answer.
        successor = await db[COLLECTION].find_one({"archived": {"$ne": True}})
        if successor:
            await db[COLLECTION].update_one(
                {"_id": successor["_id"]}, {"$set": {"is_default": True}}
            )

    if purge_data:
        for collection in (
            "usage_events", "usage_sessions", "usage_rollups", "bandwidth_samples",
            "alert_rules", "saved_views", "room_annotations", "audit_log",
            "notification_config",
        ):
            await db[collection].delete_many({"project_id": project_id})


async def import_from_environment(db, *, created_by: str = "") -> Project:
    """One-click onboarding: turn the LIVEKIT_* env vars into a stored project."""
    env = env_project()
    if env is None:
        raise ProjectError(
            "LIVEKIT_URL, LIVEKIT_API_KEY and LIVEKIT_API_SECRET are not all set."
        )
    return await create_project(
        db,
        name="Default",
        livekit_url=env.livekit_url,
        api_key=env.api_key,
        api_secret=env.api_secret,
        sip_enabled=env.sip_enabled,
        prometheus_url=env.prometheus_url,
        make_default=True,
        created_by=created_by,
    )


# ---------------------------------------------------------------------------
# Connectivity check
# ---------------------------------------------------------------------------

async def test_connection(project: Project) -> dict:
    """Try a real API call against *project*.

    Builds a throwaway client and always closes it — an unverified project's
    client must never end up in the shared pool.
    """
    import time

    from app.services.livekit import LiveKitClient

    client = LiveKitClient(
        project.livekit_url,
        project.api_key,
        project.api_secret,
        sip_enabled=project.sip_enabled,
        project_id=f"probe-{project.id}",
    )
    started = time.perf_counter()
    try:
        rooms, _ = await client.list_rooms()
        return {
            "ok": True,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "rooms": len(rooms),
            "error": None,
        }
    except Exception as exc:
        return {
            "ok": False,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "rooms": 0,
            "error": str(exc),
        }
    finally:
        try:
            await client.close()
        except Exception:  # pragma: no cover - best effort
            pass
