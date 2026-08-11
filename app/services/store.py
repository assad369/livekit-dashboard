"""Shared backing-store plumbing for the operator-state services.

Alerts, saved views, the audit log, room annotations and notification config
all began life as JSON files under /tmp — which meant they were lost on every
container restart, shared across every project, and written with an unlocked
read-modify-write.

They now prefer MongoDB and fall back to the JSON files when it is not
configured, so the app still runs standalone and the test suite needs no
database. Each record carries a `project_id` so a pin or alert on one project
does not show up on another.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from app.db import mongo

logger = logging.getLogger(__name__)

# Records written before multi-project existed, or by a deployment with no
# projects configured, live under this id.
GLOBAL_PROJECT_ID = "global"


def use_mongo() -> bool:
    return mongo.get_database() is not None


def scope(project_id: Optional[str]) -> str:
    return project_id or GLOBAL_PROJECT_ID


def collection(name: str):
    """Return the collection handle, or None when Mongo is unavailable."""
    db = mongo.get_database()
    return db[name] if db is not None else None


# ---------------------------------------------------------------------------
# JSON fallback
# ---------------------------------------------------------------------------

def read_json(path: str, default: Any) -> Any:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default
    except OSError as exc:
        logger.warning("could not read %s: %s", path, exc)
        return default


def write_json(path: str, data: Any) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def request_project_id(request) -> Optional[str]:
    """Extract the active project id from a request, tolerating its absence."""
    project = getattr(request, "scope", {}).get("state", {}).get("project")
    return project.id if project is not None else None
