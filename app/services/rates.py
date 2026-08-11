"""Rate card and cost estimation.

The point of this feature is the comparison: what would this traffic have cost
on LiveKit Cloud, versus the flat monthly bill for the VPS it actually runs on.

The seeded numbers come from LiveKit's public pricing page but ship with
``needs_verification: True`` and a banner, because:

* LiveKit retired participant-minute pricing in favour of a bandwidth and
  transcoding model, so which model applies depends on when you signed up;
* both models are volume-tiered, and the seeded values are the entry tier;
* prices change.

A confidently wrong savings figure is worse than an obviously unconfirmed one,
so the card stays flagged until an operator opens /billing/rates and saves it.

Estimates are versioned (``effective_from`` / ``is_active``) so a past month's
figure stays reproducible after a rate change.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

COLLECTION = "rates"

PRICING_MODELS = ("bandwidth", "participant_minute")

# Metrics no data source can supply. Listed so the UI can say "not measured"
# instead of rendering a $0 line that reads like "this was free".
UNMEASURABLE_WITHOUT_PROMETHEUS = "bandwidth"


def default_rate_card() -> dict:
    """LiveKit Cloud's published entry-tier pricing, pending verification."""
    return {
        "name": "LiveKit Cloud (public pricing)",
        "currency": "USD",
        "source_url": "https://livekit.io/pricing",
        "verified_on": None,
        "needs_verification": True,
        # LiveKit's current model. Upstream is free; only downstream is metered.
        "pricing_model": "bandwidth",
        "bandwidth_gb_usd": 0.12,
        # The retired model, kept for accounts still on legacy pricing.
        "participant_minute_usd": 0.0005,
        # Egress and ingress are transcoding work and are billed per minute.
        # Left at zero rather than guessed — fill these from the pricing page.
        "egress_minute_usd": {
            "room_composite": 0.0,
            "participant": 0.0,
            "track": 0.0,
            "track_composite": 0.0,
            "web": 0.0,
            "other": 0.0,
        },
        "ingress_minute_usd": {"rtmp": 0.0, "whip": 0.0, "url": 0.0, "other": 0.0},
        "included": {"bandwidth_gb": 0.0, "participant_minutes": 0.0},
        "monthly_platform_fee_usd": 0.0,
        # What you actually pay for the VPS — the other side of the comparison.
        "vps_monthly_cost_usd": 0.0,
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_active(db) -> dict:
    """The active rate card, falling back to the unverified defaults."""
    if db is None:
        return default_rate_card()

    doc = await db[COLLECTION].find_one({"is_active": True})
    if doc is None:
        return default_rate_card()

    # Merge over the defaults so a card saved before a new field existed
    # still renders.
    card = default_rate_card()
    for key, value in doc.items():
        if key in ("_id", "is_active"):
            continue
        if isinstance(value, dict) and isinstance(card.get(key), dict):
            card[key].update(value)
        else:
            card[key] = value
    return card


async def save(db, card: dict, user: str = "") -> dict:
    """Store a new active card, retiring the previous one."""
    if db is None:
        raise RuntimeError("A database is required to save rates. Set MONGODB_URI.")

    merged = default_rate_card()
    for key, value in card.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value

    # Saving is the operator confirming the numbers.
    merged["needs_verification"] = False
    merged["verified_on"] = _now()
    merged["updated_by"] = user
    merged["effective_from"] = _now()

    await db[COLLECTION].update_many({"is_active": True}, {"$set": {"is_active": False}})
    await db[COLLECTION].insert_one(dict(merged, is_active=True))
    return merged


BYTES_PER_GB = 1024 ** 3


def _line(label, qty, unit, unit_price, measured=True, note=""):
    return {
        "label": label,
        "qty": qty,
        "unit": unit,
        "unit_price": unit_price,
        "amount": round(qty * unit_price, 4),
        "measured": measured,
        "note": note,
    }


def estimate(rollup: dict, card: dict) -> dict:
    """Price a month's usage against the rate card. Pure — no I/O.

    Anything not actually measured goes into ``unmeasured`` rather than
    becoming a zero-cost line item, so the total is never quietly understated.
    """
    rollup = rollup or {}
    card = card or default_rate_card()
    model = card.get("pricing_model", "bandwidth")

    lines: list[dict] = []
    unmeasured: list[dict] = []

    bandwidth_bytes = float(rollup.get("bandwidth_bytes_down") or 0)
    bandwidth_measured = rollup.get("bandwidth_source") == "prometheus"
    bandwidth_gb = bandwidth_bytes / BYTES_PER_GB

    if model == "bandwidth":
        if bandwidth_measured:
            included_gb = float(card.get("included", {}).get("bandwidth_gb") or 0)
            billable_gb = max(0.0, bandwidth_gb - included_gb)
            lines.append(_line(
                "Downstream bandwidth", round(billable_gb, 3), "GB",
                float(card.get("bandwidth_gb_usd") or 0),
                note=f"{included_gb:g} GB included" if included_gb else "",
            ))
        else:
            unmeasured.append({
                "label": "Downstream bandwidth",
                "reason": (
                    "No Prometheus metrics endpoint is configured for this project, "
                    "and LiveKit webhooks do not report bandwidth."
                ),
            })
    else:
        participant_minutes = float(rollup.get("participant_minutes") or 0)
        included_minutes = float(card.get("included", {}).get("participant_minutes") or 0)
        billable = max(0.0, participant_minutes - included_minutes)
        lines.append(_line(
            "Participant connection minutes", round(billable, 2), "min",
            float(card.get("participant_minute_usd") or 0),
            note=f"{included_minutes:g} min included" if included_minutes else "",
        ))

    for kind, rates_key, unit_label in (
        ("egress_minutes", "egress_minute_usd", "Egress"),
        ("ingress_minutes", "ingress_minute_usd", "Ingress"),
    ):
        rates = card.get(rates_key, {}) or {}
        for subtype, minutes in sorted((rollup.get(kind) or {}).items()):
            minutes = float(minutes or 0)
            if minutes <= 0:
                continue
            lines.append(_line(
                f"{unit_label} — {subtype.replace('_', ' ')}",
                round(minutes, 2), "min", float(rates.get(subtype) or 0),
            ))

    subtotal = round(sum(line["amount"] for line in lines), 2)
    platform_fee = float(card.get("monthly_platform_fee_usd") or 0)
    total = round(subtotal + platform_fee, 2)

    vps_cost = float(card.get("vps_monthly_cost_usd") or 0)
    savings = round(total - vps_cost, 2)
    savings_pct = round((savings / total) * 100, 1) if total > 0 else 0.0

    return {
        "lines": lines,
        "unmeasured": unmeasured,
        "subtotal": subtotal,
        "platform_fee": platform_fee,
        "total": total,
        "currency": card.get("currency", "USD"),
        "pricing_model": model,
        "vps_monthly_cost": vps_cost,
        "savings": savings,
        "savings_pct": savings_pct,
        "needs_verification": bool(card.get("needs_verification")),
        "bandwidth_gb": round(bandwidth_gb, 3),
        "bandwidth_measured": bandwidth_measured,
    }


def data_quality(rollup: dict) -> dict:
    """What the operator should distrust about this month's numbers."""
    rollup = rollup or {}
    estimated_seconds = float(rollup.get("estimated_seconds") or 0)
    participant_minutes = float(rollup.get("participant_minutes") or 0)
    room_minutes = float(rollup.get("room_minutes") or 0)
    total_minutes = participant_minutes + room_minutes

    estimated_pct = (
        round((estimated_seconds / 60.0) / total_minutes * 100, 1)
        if total_minutes > 0 else 0.0
    )

    return {
        "estimated_pct": estimated_pct,
        "estimated_minutes": round(estimated_seconds / 60.0, 1),
        "dropped_events": int(rollup.get("dropped_events") or 0),
        "data_gap": bool(rollup.get("data_gap")),
        "bandwidth_gap": bool(rollup.get("bandwidth_gap")),
        "bandwidth_measured": rollup.get("bandwidth_source") == "prometheus",
    }
