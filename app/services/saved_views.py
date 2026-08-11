"""Store for saved dashboard filter views.

Backed by MongoDB when configured, otherwise a JSON file (see
`app.services.store` for why both exist). Views are scoped per project.
"""

import os
import uuid
from dataclasses import dataclass, asdict
from typing import Optional
from urllib.parse import quote

from app.services import store

_STORE_PATH = os.environ.get("SAVED_VIEWS_FILE", "/tmp/saved_views.json")

COLLECTION = "saved_views"


@dataclass
class SavedView:
    id: str
    name: str
    time_range: str = ""
    q: str = ""
    sort: str = "desc"
    sort_by: str = "created_at"

    def as_dict(self) -> dict:
        return asdict(self)

    def as_query_string(self) -> str:
        """Serialize non-default fields as a URL query string."""
        parts = []
        if self.time_range:
            parts.append(f"time_range={self.time_range}")
        if self.q:
            parts.append(f"q={quote(self.q)}")
        if self.sort != "desc":
            parts.append(f"sort={self.sort}")
        if self.sort_by != "created_at":
            parts.append(f"sort_by={self.sort_by}")
        return "&".join(parts)


def _from_doc(doc: dict) -> SavedView:
    return SavedView(
        id=str(doc["_id"]),
        name=doc.get("name", ""),
        time_range=doc.get("time_range", ""),
        q=doc.get("q", ""),
        sort=doc.get("sort", "desc"),
        sort_by=doc.get("sort_by", "created_at"),
    )


def _load() -> list:
    return store.read_json(_STORE_PATH, [])


def _save(views: list) -> None:
    store.write_json(_STORE_PATH, views)


async def list_views(project_id: Optional[str] = None) -> list[SavedView]:
    collection = store.collection(COLLECTION)
    if collection is None:
        return [SavedView(**v) for v in _load()]

    cursor = collection.find({"project_id": store.scope(project_id)})
    return [_from_doc(doc) async for doc in cursor]


async def get_view(view_id: str, project_id: Optional[str] = None) -> Optional[SavedView]:
    collection = store.collection(COLLECTION)
    if collection is None:
        for v in _load():
            if v["id"] == view_id:
                return SavedView(**v)
        return None

    doc = await collection.find_one(
        {"_id": view_id, "project_id": store.scope(project_id)}
    )
    return _from_doc(doc) if doc else None


async def create_view(
    name: str,
    time_range: str = "",
    q: str = "",
    sort: str = "desc",
    sort_by: str = "created_at",
    project_id: Optional[str] = None,
) -> SavedView:
    view = SavedView(
        id=str(uuid.uuid4())[:8],
        name=name.strip(),
        time_range=time_range,
        q=q.strip(),
        sort=sort,
        sort_by=sort_by,
    )

    collection = store.collection(COLLECTION)
    if collection is None:
        views = _load()
        views.append(view.as_dict())
        _save(views)
        return view

    doc = view.as_dict()
    doc["_id"] = doc.pop("id")
    doc["project_id"] = store.scope(project_id)
    await collection.insert_one(doc)
    return view


async def delete_view(view_id: str, project_id: Optional[str] = None) -> bool:
    collection = store.collection(COLLECTION)
    if collection is None:
        views = _load()
        remaining = [v for v in views if v["id"] != view_id]
        if len(remaining) == len(views):
            return False
        _save(remaining)
        return True

    result = await collection.delete_one(
        {"_id": view_id, "project_id": store.scope(project_id)}
    )
    return result.deleted_count == 1
