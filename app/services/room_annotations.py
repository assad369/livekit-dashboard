"""Store for room notes, tags, and pinned state.

Backed by MongoDB when configured (one document per project + room name),
otherwise the original single-blob JSON file. `get_all_annotations()` returns
the legacy blob shape either way so the rooms routes and templates are
unaffected by which backend is in use.
"""

import os
from typing import List, Optional

from app.services import store

_STORE_PATH = os.environ.get("ROOM_ANNOTATIONS_FILE", "/tmp/room_annotations.json")

PRESET_TAGS = ["prod", "demo", "support", "VIP"]

COLLECTION = "room_annotations"

def _empty() -> dict:
    """A fresh blank blob.

    Built per call rather than copied from a module constant — a shallow copy
    would share the inner list and dicts, so appending a pin would mutate the
    default itself and leak into every later read.
    """
    return {"pinned": [], "notes": {}, "tags": {}}


def _load() -> dict:
    data = store.read_json(_STORE_PATH, _empty())
    for key, default in _empty().items():
        data.setdefault(key, default)
    return data


def _save(data: dict) -> None:
    store.write_json(_STORE_PATH, data)


def _doc_id(project_id: Optional[str], room_name: str) -> str:
    return f"{store.scope(project_id)}:{room_name}"


async def get_pinned(project_id: Optional[str] = None) -> List[str]:
    collection = store.collection(COLLECTION)
    if collection is None:
        return _load()["pinned"]

    cursor = collection.find(
        {"project_id": store.scope(project_id), "pinned": True}
    )
    return [doc["room_name"] async for doc in cursor]


async def pin_room(room_name: str, project_id: Optional[str] = None) -> None:
    collection = store.collection(COLLECTION)
    if collection is None:
        data = _load()
        if room_name not in data["pinned"]:
            data["pinned"].append(room_name)
        _save(data)
        return

    await collection.update_one(
        {"_id": _doc_id(project_id, room_name)},
        {
            "$set": {"pinned": True},
            "$setOnInsert": {
                "project_id": store.scope(project_id),
                "room_name": room_name,
                "note": "",
                "tags": [],
            },
        },
        upsert=True,
    )


async def unpin_room(room_name: str, project_id: Optional[str] = None) -> None:
    collection = store.collection(COLLECTION)
    if collection is None:
        data = _load()
        data["pinned"] = [r for r in data["pinned"] if r != room_name]
        _save(data)
        return

    await collection.update_one(
        {"_id": _doc_id(project_id, room_name)}, {"$set": {"pinned": False}}
    )


async def get_annotations(room_name: str, project_id: Optional[str] = None) -> dict:
    collection = store.collection(COLLECTION)
    if collection is None:
        data = _load()
        return {
            "note": data["notes"].get(room_name, ""),
            "tags": data["tags"].get(room_name, []),
            "pinned": room_name in data["pinned"],
        }

    doc = await collection.find_one({"_id": _doc_id(project_id, room_name)})
    if doc is None:
        return {"note": "", "tags": [], "pinned": False}
    return {
        "note": doc.get("note", ""),
        "tags": doc.get("tags", []),
        "pinned": bool(doc.get("pinned", False)),
    }


async def set_annotations(
    room_name: str,
    note: str,
    tags: List[str],
    project_id: Optional[str] = None,
) -> None:
    collection = store.collection(COLLECTION)
    if collection is None:
        data = _load()
        data["notes"][room_name] = note
        data["tags"][room_name] = tags
        _save(data)
        return

    await collection.update_one(
        {"_id": _doc_id(project_id, room_name)},
        {
            "$set": {"note": note, "tags": tags},
            "$setOnInsert": {
                "project_id": store.scope(project_id),
                "room_name": room_name,
                "pinned": False,
            },
        },
        upsert=True,
    )


async def get_all_annotations(project_id: Optional[str] = None) -> dict:
    """Return the legacy `{pinned, notes, tags}` blob shape.

    Reassembled from per-room documents under Mongo so the callers in
    app/routes/rooms.py and their templates need no changes.
    """
    collection = store.collection(COLLECTION)
    if collection is None:
        return _load()

    data = {"pinned": [], "notes": {}, "tags": {}}
    cursor = collection.find({"project_id": store.scope(project_id)})
    async for doc in cursor:
        room = doc["room_name"]
        if doc.get("pinned"):
            data["pinned"].append(room)
        if doc.get("note"):
            data["notes"][room] = doc["note"]
        if doc.get("tags"):
            data["tags"][room] = doc["tags"]
    return data


def build_timeline(room, participants: list) -> list:
    """Build a synthetic timeline from current room + participant state."""
    events = []

    if room and getattr(room, "creation_time", None):
        events.append({
            "ts": room.creation_time,
            "kind": "room_created",
            "label": "Room created",
            "icon": "bi-door-open",
            "color": "success",
        })

    for p in participants:
        joined_at = getattr(p, "joined_at", None)
        identity = getattr(p, "identity", "unknown")
        name = getattr(p, "name", "") or identity

        if joined_at:
            events.append({
                "ts": joined_at,
                "kind": "participant_joined",
                "label": f"{name} joined",
                "icon": "bi-person-plus",
                "color": "primary",
                "identity": identity,
            })

        for track in getattr(p, "tracks", []):
            sid = getattr(track, "sid", "")
            track_type = getattr(track, "type", 0)
            muted = getattr(track, "muted", False)
            kind = "video" if track_type == 1 else "audio"
            icon = "bi-camera-video" if kind == "video" else "bi-mic"
            if muted:
                events.append({
                    "ts": joined_at,
                    "kind": "track_muted",
                    "label": f"{name} {kind} muted",
                    "icon": "bi-mic-mute",
                    "color": "warning",
                    "identity": identity,
                })
            else:
                events.append({
                    "ts": joined_at,
                    "kind": "track_published",
                    "label": f"{name} published {kind}",
                    "icon": icon,
                    "color": "info",
                    "identity": identity,
                })

    events.sort(key=lambda e: e.get("ts") or 0)
    return events
