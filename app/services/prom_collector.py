"""Scrape LiveKit's Prometheus endpoint for real bandwidth counters.

Webhooks carry no bandwidth at all, and LiveKit Cloud prices on downstream GB,
so this is the only way to make the cost comparison a measurement rather than
a guess. Enable it by setting `prometheus_port` in the LiveKit server's
config.yaml and the resulting URL on the project.

METRIC NAMES ARE NOT GUESSED. LiveKit's `*_total` metrics are gauges of the
current value rather than counters, so summing them would be meaningless. The
collector looks for the candidate names below, and if none are present it
records `bandwidth_source: "none"` and logs the metric names it did find so an
operator can set LIVEKIT_BANDWIDTH_METRIC_DOWN / _UP explicitly. Reporting
"not measured" is correct; inventing a number is not.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.services.usage_rollup import day_key, empty_rollup

logger = logging.getLogger(__name__)

SAMPLES = "bandwidth_samples"
ROLLUPS = "usage_rollups"

DEFAULT_INTERVAL_SECONDS = 60

# Tried in order. Override with LIVEKIT_BANDWIDTH_METRIC_DOWN / _UP after
# checking `curl http://<host>:6789/metrics` on the server.
DOWNSTREAM_CANDIDATES = (
    "livekit_bytes_out_counter",
    "livekit_node_bytes_out_counter",
    "livekit_bytes_out",
    "livekit_node_bytes_out",
)
UPSTREAM_CANDIDATES = (
    "livekit_bytes_in_counter",
    "livekit_node_bytes_in_counter",
    "livekit_bytes_in",
    "livekit_node_bytes_in",
)

_SAMPLE_LINE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?P<labels>\{[^}]*\})?\s+(?P<value>[^\s]+)")


def parse_metrics(text: str) -> dict[str, float]:
    """Parse the Prometheus text exposition format.

    Values for a metric are summed across label sets, because LiveKit reports
    per-node and per-direction series that all count toward one total.
    """
    totals: dict[str, float] = {}

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        match = _SAMPLE_LINE.match(line)
        if not match:
            continue

        raw = match.group("value")
        try:
            value = float(raw)
        except ValueError:
            continue  # NaN / +Inf and friends are not usable counters
        if value != value:  # NaN
            continue

        name = match.group("name")
        totals[name] = totals.get(name, 0.0) + value

    return totals


def _configured(env_var: str, candidates: tuple[str, ...], metrics: dict) -> Optional[str]:
    override = os.environ.get(env_var, "").strip()
    if override:
        return override if override in metrics else None
    for name in candidates:
        if name in metrics:
            return name
    return None


def select_counters(metrics: dict[str, float]) -> dict:
    """Pick the byte counters, reporting honestly when none are present."""
    down_name = _configured("LIVEKIT_BANDWIDTH_METRIC_DOWN", DOWNSTREAM_CANDIDATES, metrics)
    up_name = _configured("LIVEKIT_BANDWIDTH_METRIC_UP", UPSTREAM_CANDIDATES, metrics)

    if down_name is None and up_name is None:
        byte_like = sorted(n for n in metrics if "byte" in n.lower())
        logger.warning(
            "no recognised LiveKit byte counters at this metrics endpoint; "
            "bandwidth will be reported as not measured. Byte-ish metrics seen: %s. "
            "Set LIVEKIT_BANDWIDTH_METRIC_DOWN / _UP to choose explicitly.",
            byte_like or "(none)",
        )
        return {"down": None, "up": None, "down_name": None, "up_name": None}

    return {
        "down": metrics.get(down_name) if down_name else None,
        "up": metrics.get(up_name) if up_name else None,
        "down_name": down_name,
        "up_name": up_name,
    }


async def scrape(prometheus_url: str, timeout: float = 10.0) -> Optional[dict[str, float]]:
    """Fetch and parse the metrics endpoint. Returns None on any failure."""
    if not prometheus_url:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(prometheus_url)
            response.raise_for_status()
    except Exception as exc:
        logger.warning("could not scrape %s: %s", prometheus_url, exc)
        return None

    return parse_metrics(response.text)


def counter_delta(previous: Optional[float], current: Optional[float]) -> Optional[float]:
    """Difference between two counter readings, or None if unusable.

    A counter that went backwards means the server restarted. Returning None
    skips the interval — treating the new value as the delta would add the
    server's entire lifetime of traffic in one 60-second bucket.
    """
    if current is None:
        return None
    if previous is None:
        return None  # first reading establishes a baseline, bills nothing
    if current < previous:
        return None
    return current - previous


async def sample_once(db, project) -> dict:
    """Take one reading and add the delta since the previous one to today's rollup."""
    if db is None or not project.prometheus_url:
        return {"ok": False, "reason": "not configured"}

    metrics = await scrape(project.prometheus_url)
    if metrics is None:
        return {"ok": False, "reason": "unreachable"}

    counters = select_counters(metrics)
    if counters["down"] is None and counters["up"] is None:
        return {"ok": False, "reason": "no recognised counters"}

    now = datetime.now(timezone.utc)
    previous = await db[SAMPLES].find_one(
        {"project_id": project.id}, sort=[("ts", -1)]
    )

    await db[SAMPLES].insert_one({
        "project_id": project.id,
        "ts": now,
        "down": counters["down"],
        "up": counters["up"],
    })

    prev_down = (previous or {}).get("down")
    prev_up = (previous or {}).get("up")
    down_delta = counter_delta(prev_down, counters["down"])
    up_delta = counter_delta(prev_up, counters["up"])

    restarted = (
        previous is not None
        and (
            (counters["down"] is not None and prev_down is not None
             and counters["down"] < prev_down)
            or (counters["up"] is not None and prev_up is not None
                and counters["up"] < prev_up)
        )
    )

    day = day_key(now)
    inc = {}
    if down_delta:
        inc["bandwidth_bytes_down"] = int(down_delta)
    if up_delta:
        inc["bandwidth_bytes_up"] = int(up_delta)

    update: dict = {"$set": {"bandwidth_source": "prometheus"}}
    if inc:
        update["$inc"] = inc
    if restarted:
        # Disclose the gap rather than quietly under-reporting the month.
        update["$set"]["bandwidth_gap"] = True

    defaults = empty_rollup(project.id, day)
    for path in list(defaults):
        if path in inc or path in update["$set"]:
            defaults.pop(path)
    update["$setOnInsert"] = defaults

    await db[ROLLUPS].update_one({"_id": f"{project.id}:{day}"}, update, upsert=True)

    return {
        "ok": True,
        "down_delta": down_delta,
        "up_delta": up_delta,
        "restarted": restarted,
        "baseline": previous is None,
    }


async def collect_all(db) -> int:
    """Sample every project that has a metrics URL. Returns how many succeeded."""
    from app.services import projects as project_service

    if db is None:
        return 0

    collected = 0
    for project in await project_service.list_projects(db):
        if not project.prometheus_url:
            continue
        try:
            result = await sample_once(db, project)
            collected += 1 if result.get("ok") else 0
        except Exception as exc:
            logger.warning("bandwidth sample failed for %s: %s", project.slug, exc)
    return collected


async def collector_loop(interval: Optional[int] = None) -> None:
    """Background task: sample bandwidth on a fixed interval."""
    import asyncio

    from app.db import mongo

    period = interval or int(
        os.environ.get("BANDWIDTH_SAMPLE_INTERVAL", DEFAULT_INTERVAL_SECONDS)
    )

    while True:
        try:
            await collect_all(mongo.get_database())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - the loop must survive
            logger.warning("bandwidth collector iteration failed: %s", exc)
        await asyncio.sleep(period)
