"""Tests for the rate card, cost estimation, and the billing page."""

import pytest
from fastapi.testclient import TestClient

from app.db import mongo
from app.security import crypto
from app.services import projects as project_service
from app.services import rates
from app.services.usage_rollup import empty_rollup
from tests.fake_mongo import FakeDatabase


GB = 1024 ** 3


def rollup(**overrides):
    doc = empty_rollup("p1", "2026-08-11")
    doc.update(overrides)
    return doc


@pytest.fixture
def db():
    return FakeDatabase()


# ---------------------------------------------------------------------------
# Rate card
# ---------------------------------------------------------------------------

def test_defaults_are_flagged_as_unverified():
    """A confident number from unconfirmed rates is worse than an obvious flag."""
    card = rates.default_rate_card()
    assert card["needs_verification"] is True
    assert card["verified_on"] is None
    assert card["source_url"].startswith("https://livekit.io")


def test_defaults_use_the_current_bandwidth_model():
    assert rates.default_rate_card()["pricing_model"] == "bandwidth"


async def test_get_active_falls_back_to_defaults(db):
    card = await rates.get_active(db)
    assert card["needs_verification"] is True


async def test_get_active_without_a_database():
    assert (await rates.get_active(None))["needs_verification"] is True


async def test_saving_marks_the_card_verified(db):
    saved = await rates.save(db, {"bandwidth_gb_usd": 0.09}, user="admin")

    assert saved["needs_verification"] is False
    assert saved["verified_on"] is not None
    assert saved["updated_by"] == "admin"
    assert (await rates.get_active(db))["bandwidth_gb_usd"] == 0.09


async def test_saving_retires_the_previous_card(db):
    await rates.save(db, {"bandwidth_gb_usd": 0.12})
    await rates.save(db, {"bandwidth_gb_usd": 0.08})

    active = [d for d in db["rates"].docs if d.get("is_active")]
    assert len(active) == 1
    assert active[0]["bandwidth_gb_usd"] == 0.08


async def test_a_partial_card_keeps_the_other_defaults(db):
    saved = await rates.save(db, {"bandwidth_gb_usd": 0.05})
    assert saved["currency"] == "USD"
    assert "egress_minute_usd" in saved


async def test_save_without_a_database_raises():
    with pytest.raises(RuntimeError, match="database is required"):
        await rates.save(None, {})


# ---------------------------------------------------------------------------
# Estimation
# ---------------------------------------------------------------------------

def test_zero_usage_costs_nothing():
    result = rates.estimate(rollup(), rates.default_rate_card())
    assert result["total"] == 0.0


def test_measured_bandwidth_is_priced():
    card = dict(rates.default_rate_card(), bandwidth_gb_usd=0.10)
    result = rates.estimate(
        rollup(bandwidth_bytes_down=10 * GB, bandwidth_source="prometheus"), card
    )

    line = next(l for l in result["lines"] if "bandwidth" in l["label"].lower())
    assert line["qty"] == pytest.approx(10.0)
    assert result["total"] == pytest.approx(1.0)


def test_unmeasured_bandwidth_is_not_a_zero_line():
    """A $0 row reads as "this was free"; it must say "not measured" instead."""
    result = rates.estimate(rollup(bandwidth_source="none"), rates.default_rate_card())

    assert not any("bandwidth" in l["label"].lower() for l in result["lines"])
    assert result["unmeasured"]
    assert "bandwidth" in result["unmeasured"][0]["label"].lower()
    assert result["bandwidth_measured"] is False


def test_included_allowance_is_subtracted():
    card = dict(rates.default_rate_card(), bandwidth_gb_usd=1.0)
    card["included"] = {"bandwidth_gb": 4.0, "participant_minutes": 0}

    result = rates.estimate(
        rollup(bandwidth_bytes_down=10 * GB, bandwidth_source="prometheus"), card
    )
    assert result["total"] == pytest.approx(6.0)


def test_allowance_larger_than_usage_does_not_go_negative():
    card = dict(rates.default_rate_card(), bandwidth_gb_usd=1.0)
    card["included"] = {"bandwidth_gb": 100.0, "participant_minutes": 0}

    result = rates.estimate(
        rollup(bandwidth_bytes_down=1 * GB, bandwidth_source="prometheus"), card
    )
    assert result["total"] == 0.0


def test_participant_minute_model_prices_minutes_not_bandwidth():
    card = dict(rates.default_rate_card(),
                pricing_model="participant_minute", participant_minute_usd=0.001)

    result = rates.estimate(rollup(participant_minutes=1000.0), card)

    assert result["total"] == pytest.approx(1.0)
    assert any("connection minutes" in l["label"].lower() for l in result["lines"])


def test_egress_is_priced_per_type():
    card = rates.default_rate_card()
    card["egress_minute_usd"]["room_composite"] = 0.01
    card["egress_minute_usd"]["track"] = 0.002

    result = rates.estimate(
        rollup(egress_minutes={"room_composite": 100.0, "track": 50.0}), card
    )
    assert result["total"] == pytest.approx(100 * 0.01 + 50 * 0.002)


def test_zero_minute_types_are_omitted():
    result = rates.estimate(
        rollup(egress_minutes={"room_composite": 0.0, "web": 0.0}),
        rates.default_rate_card(),
    )
    assert not any("Egress" in l["label"] for l in result["lines"])


def test_platform_fee_is_added():
    card = dict(rates.default_rate_card(), monthly_platform_fee_usd=50.0)
    assert rates.estimate(rollup(), card)["total"] == 50.0


def test_savings_math():
    card = dict(rates.default_rate_card(),
                bandwidth_gb_usd=1.0, vps_monthly_cost_usd=40.0)
    result = rates.estimate(
        rollup(bandwidth_bytes_down=500 * GB, bandwidth_source="prometheus"), card
    )

    assert result["total"] == pytest.approx(500.0)
    assert result["savings"] == pytest.approx(460.0)
    assert result["savings_pct"] == pytest.approx(92.0)


def test_negative_savings_when_cloud_would_be_cheaper():
    card = dict(rates.default_rate_card(),
                bandwidth_gb_usd=0.01, vps_monthly_cost_usd=100.0)
    result = rates.estimate(
        rollup(bandwidth_bytes_down=10 * GB, bandwidth_source="prometheus"), card
    )
    assert result["savings"] < 0


def test_estimate_tolerates_missing_input():
    result = rates.estimate({}, {})
    assert result["total"] == 0.0


# ---------------------------------------------------------------------------
# Data quality
# ---------------------------------------------------------------------------

def test_data_quality_reports_the_estimated_share():
    quality = rates.data_quality(
        rollup(participant_minutes=100.0, room_minutes=0.0, estimated_seconds=3000.0)
    )
    assert quality["estimated_pct"] == pytest.approx(50.0)
    assert quality["estimated_minutes"] == pytest.approx(50.0)


def test_data_quality_flags_dropped_events():
    quality = rates.data_quality(rollup(dropped_events=5, data_gap=True))
    assert quality["dropped_events"] == 5
    assert quality["data_gap"] is True


def test_data_quality_on_an_empty_month():
    quality = rates.data_quality({})
    assert quality["estimated_pct"] == 0.0
    assert quality["bandwidth_measured"] is False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@pytest.fixture
async def billing_client(monkeypatch, db):
    monkeypatch.setenv("APP_ENCRYPTION_KEY", crypto.generate_key())
    monkeypatch.setattr(mongo, "_db", db)

    from app.middleware import project_context
    project_context.invalidate_list_cache()

    project = await project_service.create_project(
        db, name="Prod", livekit_url="wss://lk.example.com",
        api_key="APIk", api_secret="s",
    )

    from app.main import app
    from tests.conftest import log_in

    with TestClient(app) as client:
        log_in(client)
        yield client, db, project

    project_context.invalidate_list_cache()


def _csrf(client):
    from tests.conftest import csrf_for
    return csrf_for(client)


def test_billing_requires_auth(unauth_client):
    assert unauth_client.get("/billing").status_code == 401


def test_billing_empty_state_shows_the_webhook_config(billing_client):
    """Without this snippet an operator has no way to know why there is no data."""
    client, _, project = billing_client
    resp = client.get("/billing")

    assert resp.status_code == 200
    assert "No usage recorded yet" in resp.text
    assert "/webhooks/livekit" in resp.text
    assert "prometheus_port" in resp.text
    assert project.api_key in resp.text


def test_billing_renders_totals(billing_client):
    client, db, project = billing_client
    db["usage_rollups"].docs.append(rollup(
        _id=f"{project.id}:2026-08-11", project_id=project.id,
        day="2026-08-11", month="2026-08",
        participant_minutes=1234.5, participant_sessions=42,
        peak_concurrent_participants=9,
    ))

    resp = client.get(f"/billing?month=2026-08&project_id={project.id}")
    assert resp.status_code == 200
    assert "1,234.5" in resp.text or "1234.5" in resp.text


def test_month_filter_excludes_other_months(billing_client):
    client, db, project = billing_client
    for month, minutes in [("2026-08", 100.0), ("2026-07", 999.0)]:
        db["usage_rollups"].docs.append(rollup(
            _id=f"{project.id}:{month}-01", project_id=project.id,
            day=f"{month}-01", month=month, participant_minutes=minutes,
        ))

    resp = client.get(f"/billing?month=2026-08&project_id={project.id}")
    assert "999" not in resp.text


def test_invalid_month_falls_back_to_the_current_one(billing_client):
    client, _, _ = billing_client
    assert client.get("/billing?month=not-a-month").status_code == 200


def test_billing_shows_bandwidth_as_not_measured(billing_client):
    client, db, project = billing_client
    db["usage_rollups"].docs.append(rollup(
        _id=f"{project.id}:2026-08-11", project_id=project.id,
        day="2026-08-11", month="2026-08", participant_minutes=10.0,
    ))

    resp = client.get(f"/billing?month=2026-08&project_id={project.id}")
    assert "not measured" in resp.text


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def test_csv_export_headers_and_rows(billing_client):
    client, db, project = billing_client
    for day in ("2026-08-10", "2026-08-11"):
        db["usage_rollups"].docs.append(rollup(
            _id=f"{project.id}:{day}", project_id=project.id,
            day=day, month="2026-08", participant_minutes=60.0,
        ))

    resp = client.get(f"/billing/export.csv?month=2026-08&project_id={project.id}")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "usage-2026-08.csv" in resp.headers["content-disposition"]

    lines = resp.text.strip().splitlines()
    assert lines[0].startswith("day,participant_minutes")
    assert len(lines) == 3
    assert "2026-08-10" in lines[1]


def test_csv_export_is_empty_but_valid_with_no_data(billing_client):
    client, _, _ = billing_client
    resp = client.get("/billing/export.csv?month=2026-08")
    assert resp.status_code == 200
    assert resp.text.strip().splitlines()[0].startswith("day,")


# ---------------------------------------------------------------------------
# Rates page
# ---------------------------------------------------------------------------

def test_rates_page_renders(billing_client):
    client, _, _ = billing_client
    resp = client.get("/billing/rates")
    assert resp.status_code == 200
    assert "Rate card" in resp.text


def test_saving_rates_requires_csrf(billing_client):
    client, db, _ = billing_client
    resp = client.post("/billing/rates", data={"bandwidth_gb_usd": 0.5},
                       follow_redirects=False)
    assert resp.status_code == 403
    assert db["rates"].docs == []


def test_saving_rates_persists_and_clears_the_warning(billing_client):
    client, db, _ = billing_client

    resp = client.post("/billing/rates", data={
        "csrf_token": _csrf(client),
        "pricing_model": "bandwidth",
        "currency": "USD",
        "bandwidth_gb_usd": 0.09,
        "vps_monthly_cost_usd": 40,
    }, follow_redirects=False)

    assert resp.status_code == 303
    saved = [d for d in db["rates"].docs if d.get("is_active")][0]
    assert saved["bandwidth_gb_usd"] == 0.09
    assert saved["needs_verification"] is False

    assert "Rates not confirmed" not in client.get("/billing").text


def test_saving_rates_writes_an_audit_entry(billing_client):
    client, db, _ = billing_client
    client.post("/billing/rates", data={
        "csrf_token": _csrf(client), "pricing_model": "bandwidth",
        "bandwidth_gb_usd": 0.1,
    }, follow_redirects=False)

    actions = [e["action"] for e in db["audit_log"].docs]
    assert "billing.rates.save" in actions


def test_unknown_pricing_model_is_rejected(billing_client):
    client, db, _ = billing_client
    client.post("/billing/rates", data={
        "csrf_token": _csrf(client), "pricing_model": "make-it-up",
    }, follow_redirects=False)
    assert db["rates"].docs == []


def test_readonly_blocks_saving_rates(billing_client, monkeypatch):
    client, db, _ = billing_client
    monkeypatch.setenv("DASHBOARD_ROLE", "readonly")

    resp = client.post("/billing/rates", data={
        "csrf_token": _csrf(client), "bandwidth_gb_usd": 0.5,
    }, follow_redirects=False)

    assert resp.status_code == 403
    assert db["rates"].docs == []


def test_savings_panel_appears_once_a_server_cost_is_set(billing_client):
    client, db, project = billing_client
    db["usage_rollups"].docs.append(rollup(
        _id=f"{project.id}:2026-08-11", project_id=project.id,
        day="2026-08-11", month="2026-08",
        bandwidth_bytes_down=100 * GB, bandwidth_source="prometheus",
    ))
    client.post("/billing/rates", data={
        "csrf_token": _csrf(client), "pricing_model": "bandwidth",
        "bandwidth_gb_usd": 0.12, "vps_monthly_cost_usd": 20,
    }, follow_redirects=False)

    resp = client.get(f"/billing?month=2026-08&project_id={project.id}")
    assert "Self-hosting comparison" in resp.text
    assert "saved" in resp.text


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------

def test_reconcile_requires_csrf(billing_client):
    client, _, _ = billing_client
    assert client.post("/billing/reconcile", data={},
                       follow_redirects=False).status_code == 403


def test_reconcile_runs_the_sweep(billing_client):
    from datetime import datetime, timedelta, timezone

    client, db, project = billing_client
    db["usage_sessions"].docs.append({
        "_id": "s1", "project_id": project.id, "kind": "room", "key": "RM_1",
        "started_at": datetime.now(timezone.utc) - timedelta(hours=48),
        "ended_at": None, "extra": {},
    })

    resp = client.post("/billing/reconcile",
                       data={"csrf_token": _csrf(client)}, follow_redirects=False)

    assert resp.status_code == 303
    assert db["usage_sessions"].docs[0]["closed_reason"] == "timeout"
