"""A minimal in-memory stand-in for an async MongoDB database.

Enough of the driver surface to exercise the app's queries without a server:
equality/`$in`/`$lt`/`$gte`/`$ne`/`$exists` filters, `$set`/`$setOnInsert`/
`$inc`/`$max`/`$push`, upserts, unique-index enforcement, sorting, and a small
aggregation subset (`$match`/`$group`/`$sort`/`$limit`).

Unique indexes are modelled deliberately: `usage_events (project_id,
event_id)` is what stops webhook retries from double-billing, so tests must be
able to prove a duplicate insert actually raises.
"""

from __future__ import annotations

import copy
import itertools
from typing import Any

from pymongo.errors import DuplicateKeyError


_ids = itertools.count(1)


def _new_id() -> str:
    return f"oid{next(_ids):012d}"


def _get_path(doc: dict, path: str) -> Any:
    current: Any = doc
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _set_path(doc: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    current = doc
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def _matches(doc: dict, query: dict) -> bool:
    for key, condition in query.items():
        if key == "$or":
            if not any(_matches(doc, sub) for sub in condition):
                return False
            continue
        if key == "$and":
            if not all(_matches(doc, sub) for sub in condition):
                return False
            continue

        value = _get_path(doc, key)
        if isinstance(condition, dict) and any(k.startswith("$") for k in condition):
            for op, operand in condition.items():
                if op == "$in" and value not in operand:
                    return False
                if op == "$nin" and value in operand:
                    return False
                if op == "$ne" and value == operand:
                    return False
                if op == "$lt" and not (value is not None and value < operand):
                    return False
                if op == "$lte" and not (value is not None and value <= operand):
                    return False
                if op == "$gt" and not (value is not None and value > operand):
                    return False
                if op == "$gte" and not (value is not None and value >= operand):
                    return False
                if op == "$exists":
                    if operand != (value is not None):
                        return False
                if op == "$regex":
                    import re
                    if value is None or not re.search(operand, str(value)):
                        return False
        elif value != condition:
            return False
    return True


def _check_path_conflicts(update: dict) -> None:
    """Reject updates real MongoDB would reject.

    MongoDB errors when two operators touch overlapping paths, including a
    parent/child pair such as `$inc {"a.b": 1}` with `$setOnInsert {"a": {}}`.
    Silently allowing it here would let a broken update pass tests and fail in
    production, so the fake enforces the same rule.
    """
    seen: dict[str, str] = {}
    for op, fields in update.items():
        if not isinstance(fields, dict):
            continue
        for path in fields:
            for other, other_op in seen.items():
                if other_op == op:
                    continue
                if path == other or path.startswith(f"{other}.") or other.startswith(f"{path}."):
                    raise ValueError(
                        f"fake_mongo: conflicting update paths {path!r} ({op}) and "
                        f"{other!r} ({other_op}) — MongoDB would reject this"
                    )
            seen[path] = op


def _apply_update(doc: dict, update: dict, *, inserted: bool) -> None:
    _check_path_conflicts(update)
    for op, fields in update.items():
        if op == "$set":
            for path, value in fields.items():
                _set_path(doc, path, value)
        elif op == "$setOnInsert":
            if inserted:
                for path, value in fields.items():
                    _set_path(doc, path, value)
        elif op == "$inc":
            for path, value in fields.items():
                _set_path(doc, path, (_get_path(doc, path) or 0) + value)
        elif op == "$max":
            for path, value in fields.items():
                current = _get_path(doc, path)
                if current is None or value > current:
                    _set_path(doc, path, value)
        elif op == "$min":
            for path, value in fields.items():
                current = _get_path(doc, path)
                if current is None or value < current:
                    _set_path(doc, path, value)
        elif op == "$push":
            for path, value in fields.items():
                arr = _get_path(doc, path) or []
                arr.append(value)
                _set_path(doc, path, arr)
        elif op == "$unset":
            for path in fields:
                parts = path.split(".")
                current = doc
                for part in parts[:-1]:
                    current = current.get(part, {})
                current.pop(parts[-1], None)
        else:  # pragma: no cover - unsupported operator signals a test gap
            raise NotImplementedError(f"fake_mongo: unsupported update operator {op}")


class _Result:
    def __init__(self, matched=0, modified=0, upserted_id=None, inserted_id=None,
                 deleted=0):
        self.matched_count = matched
        self.modified_count = modified
        self.upserted_id = upserted_id
        self.inserted_id = inserted_id
        self.deleted_count = deleted


class _Cursor:
    def __init__(self, docs: list):
        self._docs = docs

    def sort(self, key_or_list, direction=None):
        pairs = [(key_or_list, direction or 1)] if isinstance(key_or_list, str) else list(key_or_list)
        for key, direction in reversed(pairs):
            self._docs.sort(
                key=lambda d: (_get_path(d, key) is None, _get_path(d, key)),
                reverse=direction < 0,
            )
        return self

    def limit(self, n: int):
        self._docs = self._docs[:n]
        return self

    def skip(self, n: int):
        self._docs = self._docs[n:]
        return self

    def __aiter__(self):
        async def gen():
            for doc in self._docs:
                yield copy.deepcopy(doc)
        return gen()

    async def to_list(self, length=None):
        docs = self._docs if length is None else self._docs[:length]
        return [copy.deepcopy(d) for d in docs]


class FakeCollection:
    def __init__(self, name: str):
        self.name = name
        self.docs: list[dict] = []
        self.unique_indexes: list[list[str]] = []

    # -- indexes ----------------------------------------------------------
    async def create_index(self, keys, **options):
        fields = [k for k, _ in keys] if isinstance(keys, (list, tuple)) else [keys]
        if options.get("unique") and "partialFilterExpression" not in options:
            self.unique_indexes.append(fields)
        return "_".join(fields)

    def _check_unique(self, candidate: dict, *, ignore: dict | None = None):
        for fields in self.unique_indexes:
            key = tuple(_get_path(candidate, f) for f in fields)
            if any(k is None for k in key):
                continue
            for existing in self.docs:
                if existing is ignore:
                    continue
                if tuple(_get_path(existing, f) for f in fields) == key:
                    raise DuplicateKeyError(
                        f"E11000 duplicate key error: {self.name} {fields} {key}"
                    )

    # -- reads ------------------------------------------------------------
    async def find_one(self, query=None, *args, sort=None, **kwargs):
        matched = [d for d in self.docs if _matches(d, query or {})]
        if sort:
            matched = _Cursor(matched).sort(sort)._docs
        return copy.deepcopy(matched[0]) if matched else None

    def find(self, query=None, *args, **kwargs):
        return _Cursor([d for d in self.docs if _matches(d, query or {})])

    async def count_documents(self, query=None, limit=None, **kwargs):
        matched = [d for d in self.docs if _matches(d, query or {})]
        return len(matched) if limit is None else min(len(matched), limit)

    async def distinct(self, field, query=None):
        seen = []
        for doc in self.docs:
            if _matches(doc, query or {}):
                value = _get_path(doc, field)
                if value not in seen:
                    seen.append(value)
        return seen

    # -- writes -----------------------------------------------------------
    async def insert_one(self, doc):
        doc = copy.deepcopy(doc)
        doc.setdefault("_id", _new_id())
        self._check_unique(doc)
        self.docs.append(doc)
        return _Result(inserted_id=doc["_id"])

    async def update_one(self, query, update, upsert=False, **kwargs):
        for doc in self.docs:
            if _matches(doc, query):
                candidate = copy.deepcopy(doc)
                _apply_update(candidate, update, inserted=False)
                self._check_unique(candidate, ignore=doc)
                doc.clear()
                doc.update(candidate)
                return _Result(matched=1, modified=1)

        if not upsert:
            return _Result()

        doc = {k: v for k, v in query.items() if not k.startswith("$")
               and not isinstance(v, dict)}
        doc.setdefault("_id", _new_id())
        _apply_update(doc, update, inserted=True)
        self._check_unique(doc)
        self.docs.append(doc)
        return _Result(matched=0, modified=0, upserted_id=doc["_id"])

    async def update_many(self, query, update, **kwargs):
        count = 0
        for doc in self.docs:
            if _matches(doc, query):
                _apply_update(doc, update, inserted=False)
                count += 1
        return _Result(matched=count, modified=count)

    async def find_one_and_update(self, query, update, return_document=None, **kwargs):
        for doc in self.docs:
            if _matches(doc, query):
                before = copy.deepcopy(doc)
                _apply_update(doc, update, inserted=False)
                # ReturnDocument.AFTER is truthy, BEFORE is falsy.
                return copy.deepcopy(doc) if return_document else before
        return None

    async def replace_one(self, query, replacement, upsert=False, **kwargs):
        for doc in self.docs:
            if _matches(doc, query):
                replacement = copy.deepcopy(replacement)
                replacement["_id"] = doc["_id"]
                self._check_unique(replacement, ignore=doc)
                doc.clear()
                doc.update(replacement)
                return _Result(matched=1, modified=1)
        if upsert:
            return await self.insert_one(replacement)
        return _Result()

    async def delete_one(self, query):
        for i, doc in enumerate(self.docs):
            if _matches(doc, query):
                self.docs.pop(i)
                return _Result(deleted=1)
        return _Result()

    async def delete_many(self, query):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not _matches(d, query or {})]
        return _Result(deleted=before - len(self.docs))

    # -- aggregation ------------------------------------------------------
    def aggregate(self, pipeline):
        docs = [copy.deepcopy(d) for d in self.docs]

        for stage in pipeline:
            if "$match" in stage:
                docs = [d for d in docs if _matches(d, stage["$match"])]
            elif "$sort" in stage:
                for key, direction in reversed(list(stage["$sort"].items())):
                    docs.sort(
                        key=lambda d: (_get_path(d, key) is None, _get_path(d, key)),
                        reverse=direction < 0,
                    )
            elif "$limit" in stage:
                docs = docs[: stage["$limit"]]
            elif "$group" in stage:
                docs = self._group(docs, stage["$group"])
            elif "$project" in stage:
                pass  # projections are not asserted on in these tests
            else:  # pragma: no cover
                raise NotImplementedError(f"fake_mongo: unsupported stage {stage}")

        return _Cursor(docs)

    @staticmethod
    def _group(docs: list, spec: dict) -> list:
        id_spec = spec["_id"]
        groups: dict = {}

        for doc in docs:
            if id_spec is None:
                key = None
            elif isinstance(id_spec, str):
                key = _get_path(doc, id_spec.lstrip("$"))
            else:
                key = tuple(
                    (k, _get_path(doc, v.lstrip("$"))) for k, v in sorted(id_spec.items())
                )
            bucket = groups.setdefault(key, {"_id": key if not isinstance(key, tuple)
                                             else dict(key)})
            for field, accumulator in spec.items():
                if field == "_id":
                    continue
                op, operand = next(iter(accumulator.items()))
                value = (_get_path(doc, operand.lstrip("$"))
                         if isinstance(operand, str) and operand.startswith("$")
                         else operand)
                if op == "$sum":
                    bucket[field] = (bucket.get(field) or 0) + (value or 0)
                elif op == "$max":
                    current = bucket.get(field)
                    if value is not None and (current is None or value > current):
                        bucket[field] = value
                elif op == "$min":
                    current = bucket.get(field)
                    if value is not None and (current is None or value < current):
                        bucket[field] = value
                elif op == "$first":
                    bucket.setdefault(field, value)
                elif op == "$push":
                    bucket.setdefault(field, []).append(value)
                else:  # pragma: no cover
                    raise NotImplementedError(f"fake_mongo: unsupported accumulator {op}")

        return list(groups.values())


class FakeDatabase:
    def __init__(self):
        self._collections: dict[str, FakeCollection] = {}

    def __getitem__(self, name: str) -> FakeCollection:
        return self._collections.setdefault(name, FakeCollection(name))

    def __getattr__(self, name: str) -> FakeCollection:
        if name.startswith("_"):
            raise AttributeError(name)
        return self[name]

    def collection_names(self) -> list[str]:
        return sorted(self._collections)
