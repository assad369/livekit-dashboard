"""Billing: usage totals, LiveKit Cloud cost estimate, and the savings view."""

import csv
import io
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from app.db import mongo
from app.security.basic_auth import get_current_user, requires_admin
from app.security.csrf import get_csrf_token, verify_csrf_token
from app.services import audit_log, projects as project_service, rates, store, usage
from app.utils.flash import flash, get_flash

logger = logging.getLogger(__name__)

router = APIRouter()

ALL_PROJECTS = "all"


def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _valid_month(value: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m")
        return value
    except (ValueError, TypeError):
        return _current_month()


def _webhook_url(request: Request) -> str:
    return str(request.base_url).rstrip("/") + "/webhooks/livekit"


async def _gather(request: Request, month: str, project_filter: str):
    db = mongo.get_database()
    active = request.scope.get("state", {}).get("project")
    all_projects = await project_service.list_projects(db)

    if project_filter == ALL_PROJECTS:
        rollup = {}
        per_project = await usage.month_rollup_all(db, month)
        for totals in per_project.values():
            rollup = _merge(rollup, totals)
        scope_id = None
    else:
        scope_id = project_filter or (active.id if active else None)
        rollup = await usage.month_rollup(db, scope_id, month) if scope_id else {}
        per_project = {scope_id: rollup} if scope_id else {}

    card = await rates.get_active(db)
    return {
        "db": db,
        "active": active,
        "all_projects": all_projects,
        "scope_id": scope_id,
        "rollup": rollup,
        "per_project": per_project,
        "card": card,
        "estimate": rates.estimate(rollup, card),
        "quality": rates.data_quality(rollup),
        "series": await usage.daily_series(db, scope_id, month),
        "months": await usage.available_months(db),
    }


def _merge(a: dict, b: dict) -> dict:
    """Combine two rollup totals."""
    if not a:
        return dict(b)
    merged = dict(a)
    for key, value in b.items():
        if isinstance(value, dict):
            target = dict(merged.get(key) or {})
            for sub, sub_value in value.items():
                target[sub] = target.get(sub, 0) + sub_value
            merged[key] = target
        elif isinstance(value, bool):
            merged[key] = merged.get(key, False) or value
        elif isinstance(value, (int, float)):
            if key == "peak_concurrent_participants":
                merged[key] = max(merged.get(key, 0), value)
            else:
                merged[key] = merged.get(key, 0) + value
    return merged


@router.get("/billing", response_class=HTMLResponse, dependencies=[Depends(requires_admin)])
async def billing_page(request: Request, month: str = "", project_id: str = ""):
    month = _valid_month(month or _current_month())
    data = await _gather(request, month, project_id)
    flash_message, flash_type = get_flash(request)

    has_data = await usage.has_any_data(data["db"], data["scope_id"])

    return request.app.state.templates.TemplateResponse(
        request,
        "billing/index.html.j2",
        {
            "request": request,
            "month": month,
            "months": data["months"] or [month],
            "project_filter": project_id or ALL_PROJECTS if project_id == ALL_PROJECTS
            else (data["scope_id"] or ""),
            "all_projects": data["all_projects"],
            "project_names": {p.id: p.name for p in data["all_projects"]},
            "per_project": data["per_project"],
            "rollup": data["rollup"],
            "estimate": data["estimate"],
            "quality": data["quality"],
            "card": data["card"],
            "series": data["series"],
            "has_data": has_data,
            "storage_available": data["db"] is not None,
            "db_health": mongo.health(),
            "webhook_url": _webhook_url(request),
            "current_user": get_current_user(request),
            "csrf_token": get_csrf_token(request),
            "flash_message": flash_message,
            "flash_type": flash_type,
        },
    )


@router.get("/billing/export.csv", dependencies=[Depends(requires_admin)])
async def export_csv(request: Request, month: str = "", project_id: str = ""):
    month = _valid_month(month or _current_month())
    data = await _gather(request, month, project_id)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "day", "participant_minutes", "participant_sessions", "room_minutes",
        "room_sessions", "peak_concurrent_participants", "egress_minutes_total",
        "egress_bytes", "ingress_minutes_total", "tracks_published",
        "bandwidth_bytes_down", "bandwidth_source", "estimated_seconds",
        "dropped_events",
    ])
    for row in data["series"]:
        writer.writerow([
            row.get("day", ""),
            round(float(row.get("participant_minutes") or 0), 2),
            int(row.get("participant_sessions") or 0),
            round(float(row.get("room_minutes") or 0), 2),
            int(row.get("room_sessions") or 0),
            int(row.get("peak_concurrent_participants") or 0),
            round(sum((row.get("egress_minutes") or {}).values()), 2),
            int(row.get("egress_bytes") or 0),
            round(sum((row.get("ingress_minutes") or {}).values()), 2),
            int(row.get("tracks_published") or 0),
            int(row.get("bandwidth_bytes_down") or 0),
            row.get("bandwidth_source", "none"),
            round(float(row.get("estimated_seconds") or 0), 1),
            int(row.get("dropped_events") or 0),
        ])

    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="usage-{month}.csv"'},
    )


@router.get("/billing/rates", response_class=HTMLResponse,
            dependencies=[Depends(requires_admin)])
async def rates_page(request: Request):
    card = await rates.get_active(mongo.get_database())
    flash_message, flash_type = get_flash(request)

    return request.app.state.templates.TemplateResponse(
        request,
        "billing/rates.html.j2",
        {
            "request": request,
            "card": card,
            "storage_available": mongo.get_database() is not None,
            "current_user": get_current_user(request),
            "csrf_token": get_csrf_token(request),
            "flash_message": flash_message,
            "flash_type": flash_type,
        },
    )


@router.post("/billing/rates", dependencies=[Depends(requires_admin)])
async def save_rates(
    request: Request,
    csrf_token: str = Form(""),
    pricing_model: str = Form("bandwidth"),
    currency: str = Form("USD"),
    bandwidth_gb_usd: float = Form(0.0),
    participant_minute_usd: float = Form(0.0),
    included_bandwidth_gb: float = Form(0.0),
    included_participant_minutes: float = Form(0.0),
    monthly_platform_fee_usd: float = Form(0.0),
    vps_monthly_cost_usd: float = Form(0.0),
    egress_room_composite: float = Form(0.0),
    egress_participant: float = Form(0.0),
    egress_track: float = Form(0.0),
    egress_track_composite: float = Form(0.0),
    egress_web: float = Form(0.0),
    ingress_rtmp: float = Form(0.0),
    ingress_whip: float = Form(0.0),
    ingress_url: float = Form(0.0),
):
    await verify_csrf_token(request)

    if pricing_model not in rates.PRICING_MODELS:
        flash(request, f"Unknown pricing model {pricing_model!r}.", "danger")
        return RedirectResponse("/billing/rates", status_code=303)

    card = {
        "pricing_model": pricing_model,
        "currency": currency.strip().upper()[:8] or "USD",
        "bandwidth_gb_usd": bandwidth_gb_usd,
        "participant_minute_usd": participant_minute_usd,
        "included": {
            "bandwidth_gb": included_bandwidth_gb,
            "participant_minutes": included_participant_minutes,
        },
        "monthly_platform_fee_usd": monthly_platform_fee_usd,
        "vps_monthly_cost_usd": vps_monthly_cost_usd,
        "egress_minute_usd": {
            "room_composite": egress_room_composite,
            "participant": egress_participant,
            "track": egress_track,
            "track_composite": egress_track_composite,
            "web": egress_web,
        },
        "ingress_minute_usd": {
            "rtmp": ingress_rtmp, "whip": ingress_whip, "url": ingress_url,
        },
    }

    try:
        await rates.save(mongo.get_database(), card, get_current_user(request) or "")
    except RuntimeError as exc:
        flash(request, str(exc), "danger")
        return RedirectResponse("/billing/rates", status_code=303)

    await audit_log.log_action(
        "billing.rates.save", "rate-card", get_current_user(request),
        {"pricing_model": pricing_model},
        project_id=store.request_project_id(request),
    )
    flash(request, "Rate card saved.", "success")
    return RedirectResponse("/billing", status_code=303)


@router.post("/billing/reconcile", dependencies=[Depends(requires_admin)])
async def reconcile(request: Request, csrf_token: str = Form("")):
    """Close sessions whose end event never arrived, so their minutes count."""
    await verify_csrf_token(request)

    swept = await usage.sweep_stale_sessions(mongo.get_database())
    flash(
        request,
        f"Reconciled {swept} stale session(s)." if swept
        else "No stale sessions to reconcile.",
        "success" if swept else "info",
    )
    return RedirectResponse("/billing", status_code=303)
