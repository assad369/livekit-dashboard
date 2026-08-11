"""Alert rules with threshold evaluation against dashboard stats.

Backed by MongoDB when configured, otherwise a JSON file (see
`app.services.store`). Rules are scoped per project. The evaluation logic
below is pure and unchanged — only the persistence moved.
"""

import os
import uuid
from dataclasses import dataclass, asdict
from typing import Optional

from app.services import store


_STORE_PATH = os.environ.get("ALERT_RULES_FILE", "/tmp/alert_rules.json")

COLLECTION = "alert_rules"

METRICS: dict[str, str] = {
    "rooms_total": "Total Rooms",
    "rooms_active": "Active Rooms",
    "participants_total": "Total Participants",
    "egress_active": "Active Egress Jobs",
    "ingress_active": "Active Ingress Streams",
    "api_latency_ms": "API Latency (ms)",
}

OPERATORS = [">", ">=", "<", "<="]
SEVERITIES = ["warning", "critical"]

_OPS = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
}


@dataclass
class AlertRule:
    id: str
    name: str
    metric: str
    operator: str
    threshold: float
    severity: str = "warning"
    enabled: bool = True

    def as_dict(self) -> dict:
        return asdict(self)

    def evaluate(self, stats) -> bool:
        """Return True if this rule is currently triggered by *stats*."""
        if not self.enabled:
            return False
        value = getattr(stats, self.metric, None)
        if value is None:
            return False
        op = _OPS.get(self.operator)
        if op is None:
            return False
        try:
            return op(float(value), float(self.threshold))
        except (TypeError, ValueError):
            return False


def _load() -> list:
    return store.read_json(_STORE_PATH, [])


def _save(rules: list) -> None:
    store.write_json(_STORE_PATH, rules)


def _from_doc(doc: dict) -> "AlertRule":
    return AlertRule(
        id=str(doc["_id"]),
        name=doc.get("name", ""),
        metric=doc.get("metric", ""),
        operator=doc.get("operator", ">"),
        threshold=float(doc.get("threshold", 0)),
        severity=doc.get("severity", "warning"),
        enabled=bool(doc.get("enabled", True)),
    )


async def list_rules(project_id: Optional[str] = None) -> list[AlertRule]:
    collection = store.collection(COLLECTION)
    if collection is None:
        return [AlertRule(**r) for r in _load()]

    cursor = collection.find({"project_id": store.scope(project_id)})
    return [_from_doc(doc) async for doc in cursor]


async def get_rule(rule_id: str, project_id: Optional[str] = None) -> Optional[AlertRule]:
    collection = store.collection(COLLECTION)
    if collection is None:
        for r in _load():
            if r["id"] == rule_id:
                return AlertRule(**r)
        return None

    doc = await collection.find_one(
        {"_id": rule_id, "project_id": store.scope(project_id)}
    )
    return _from_doc(doc) if doc else None


async def create_rule(
    name: str,
    metric: str,
    operator: str,
    threshold: float,
    severity: str = "warning",
    project_id: Optional[str] = None,
) -> AlertRule:
    if metric not in METRICS:
        raise ValueError(f"Unknown metric: {metric!r}")
    if operator not in OPERATORS:
        raise ValueError(f"Unknown operator: {operator!r}")
    if severity not in SEVERITIES:
        raise ValueError(f"Unknown severity: {severity!r}")

    rule = AlertRule(
        id=str(uuid.uuid4())[:8],
        name=name.strip(),
        metric=metric,
        operator=operator,
        threshold=float(threshold),
        severity=severity,
        enabled=True,
    )

    collection = store.collection(COLLECTION)
    if collection is None:
        rules = _load()
        rules.append(rule.as_dict())
        _save(rules)
        return rule

    doc = rule.as_dict()
    doc["_id"] = doc.pop("id")
    doc["project_id"] = store.scope(project_id)
    await collection.insert_one(doc)
    return rule


async def delete_rule(rule_id: str, project_id: Optional[str] = None) -> bool:
    collection = store.collection(COLLECTION)
    if collection is None:
        rules = _load()
        remaining = [r for r in rules if r["id"] != rule_id]
        if len(remaining) == len(rules):
            return False
        _save(remaining)
        return True

    result = await collection.delete_one(
        {"_id": rule_id, "project_id": store.scope(project_id)}
    )
    return result.deleted_count == 1


async def toggle_rule(rule_id: str, project_id: Optional[str] = None) -> Optional[bool]:
    """Flip enabled/disabled. Returns new enabled state, or None if not found."""
    collection = store.collection(COLLECTION)
    if collection is None:
        rules = _load()
        for r in rules:
            if r["id"] == rule_id:
                r["enabled"] = not r.get("enabled", True)
                _save(rules)
                return r["enabled"]
        return None

    doc = await collection.find_one(
        {"_id": rule_id, "project_id": store.scope(project_id)}
    )
    if doc is None:
        return None
    enabled = not doc.get("enabled", True)
    await collection.update_one({"_id": rule_id}, {"$set": {"enabled": enabled}})
    return enabled


async def evaluate_all(
    stats, project_id: Optional[str] = None
) -> list[tuple[AlertRule, bool]]:
    """Return (rule, triggered) pairs for every rule against current *stats*."""
    rules = await list_rules(project_id)
    return [(rule, rule.evaluate(stats)) for rule in rules]
