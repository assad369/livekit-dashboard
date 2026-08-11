#!/usr/bin/env python
"""Move the legacy /tmp JSON stores into MongoDB.

The five operator-state stores (alert rules, saved views, audit log, room
annotations, notification config) used to live in JSON files that defaulted
into /tmp — lost on every container restart. This copies whatever survives
into MongoDB, attributed to one project.

Idempotent: existing documents are upserted by their original id, so re-running
after a partial failure is safe.

Usage
-----
    poetry run python scripts/migrate_json_stores.py --project-slug production
    poetry run python scripts/migrate_json_stores.py --project-slug production --dry-run
    poetry run python scripts/migrate_json_stores.py --global      # unscoped
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from app.db import mongo  # noqa: E402
from app.services import projects as project_service  # noqa: E402
from app.services.store import GLOBAL_PROJECT_ID  # noqa: E402


def _read(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        print(f"  ! {path} is not valid JSON ({exc}); skipping")
        return None


async def _upsert(db, collection: str, docs: list[dict], dry_run: bool) -> int:
    if dry_run:
        return len(docs)
    count = 0
    for doc in docs:
        doc_id = doc.pop("_id")
        await db[collection].update_one({"_id": doc_id}, {"$set": doc}, upsert=True)
        count += 1
    return count


async def migrate(project_id: str, dry_run: bool) -> int:
    db = mongo.get_database()
    if db is None:
        print("MongoDB is not available. Set MONGODB_URI and try again.")
        return 1

    total = 0

    # --- alert rules ------------------------------------------------------
    path = os.environ.get("ALERT_RULES_FILE", "/tmp/alert_rules.json")
    rules = _read(path, [])
    if rules:
        docs = [dict(r, _id=r.pop("id"), project_id=project_id) for r in rules]
        n = await _upsert(db, "alert_rules", docs, dry_run)
        print(f"  alert_rules      {n:>4}  from {path}")
        total += n

    # --- saved views ------------------------------------------------------
    path = os.environ.get("SAVED_VIEWS_FILE", "/tmp/saved_views.json")
    views = _read(path, [])
    if views:
        docs = [dict(v, _id=v.pop("id"), project_id=project_id) for v in views]
        n = await _upsert(db, "saved_views", docs, dry_run)
        print(f"  saved_views      {n:>4}  from {path}")
        total += n

    # --- audit log --------------------------------------------------------
    path = os.environ.get("AUDIT_LOG_FILE", "/tmp/audit_log.json")
    entries = _read(path, [])
    if entries:
        # Entries have no stable id; derive one so re-runs do not duplicate.
        docs = [
            dict(e, _id=f"{project_id}:{e.get('ts')}:{i}", project_id=project_id)
            for i, e in enumerate(entries)
        ]
        n = await _upsert(db, "audit_log", docs, dry_run)
        print(f"  audit_log        {n:>4}  from {path}")
        total += n

    # --- room annotations -------------------------------------------------
    path = os.environ.get("ROOM_ANNOTATIONS_FILE", "/tmp/room_annotations.json")
    blob = _read(path, {})
    if blob:
        rooms = set(blob.get("pinned", [])) | set(blob.get("notes", {})) | set(blob.get("tags", {}))
        docs = [
            {
                "_id": f"{project_id}:{room}",
                "project_id": project_id,
                "room_name": room,
                "pinned": room in blob.get("pinned", []),
                "note": blob.get("notes", {}).get(room, ""),
                "tags": blob.get("tags", {}).get(room, []),
            }
            for room in sorted(rooms)
        ]
        n = await _upsert(db, "room_annotations", docs, dry_run)
        print(f"  room_annotations {n:>4}  from {path}")
        total += n

    # --- notification config ---------------------------------------------
    path = os.environ.get("NOTIFICATIONS_FILE", "/tmp/notifications_config.json")
    cfg = _read(path, {})
    if cfg:
        docs = [dict(cfg, _id=project_id)]
        n = await _upsert(db, "notification_config", docs, dry_run)
        print(f"  notifications    {n:>4}  from {path}")
        total += n

    if total == 0:
        print("  nothing to migrate — no legacy JSON stores found")

    print(f"\n{'Would migrate' if dry_run else 'Migrated'} {total} document(s).")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--project-slug", help="attribute the data to this project")
    group.add_argument("--global", dest="use_global", action="store_true",
                       help="import as unscoped data")
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args()

    await mongo.connect()
    try:
        if args.use_global:
            project_id = GLOBAL_PROJECT_ID
        else:
            project = await project_service.get_by_slug(mongo.get_database(), args.project_slug)
            if project is None:
                print(f"No project with slug {args.project_slug!r}. Create it first.")
                return 1
            project_id = project.id

        print(f"Migrating into project {project_id}"
              f"{' (dry run)' if args.dry_run else ''}\n")
        return await migrate(project_id, args.dry_run)
    finally:
        await mongo.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
