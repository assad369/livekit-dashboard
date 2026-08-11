"""Tests for the Prometheus bandwidth collector."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services import prom_collector as pc
from app.services.projects import Project
from tests.fake_mongo import FakeDatabase


METRICS_FIXTURE = """
# HELP livekit_bytes_out_counter Total bytes sent
# TYPE livekit_bytes_out_counter counter
livekit_bytes_out_counter{node_id="ND_1",direction="down"} 1000
livekit_bytes_out_counter{node_id="ND_2",direction="down"} 500
# HELP livekit_bytes_in_counter Total bytes received
# TYPE livekit_bytes_in_counter counter
livekit_bytes_in_counter{node_id="ND_1"} 300
# TYPE livekit_room_total gauge
livekit_room_total{node_id="ND_1"} 4
livekit_participant_total{node_id="ND_1"} 12
"""


def project(prometheus_url="http://lk.example.com:6789/metrics"):
    return Project(
        id="p1", name="Prod", slug="prod", livekit_url="wss://lk.example.com",
        api_key="APIk", api_secret="s", prometheus_url=prometheus_url,
    )


@pytest.fixture
def db():
    return FakeDatabase()


@pytest.fixture(autouse=True)
def _no_metric_overrides(monkeypatch):
    monkeypatch.delenv("LIVEKIT_BANDWIDTH_METRIC_DOWN", raising=False)
    monkeypatch.delenv("LIVEKIT_BANDWIDTH_METRIC_UP", raising=False)
    yield


def _response(text: str, status: int = 200) -> httpx.Response:
    """raise_for_status() needs a bound request, so build a complete response."""
    return httpx.Response(
        status, text=text, request=httpx.Request("GET", "http://lk.example.com:6789/metrics")
    )


def _mock_scrape(text):
    return patch.object(
        httpx.AsyncClient, "get", new=AsyncMock(return_value=_response(text)),
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parse_sums_across_label_sets():
    metrics = pc.parse_metrics(METRICS_FIXTURE)
    assert metrics["livekit_bytes_out_counter"] == 1500.0
    assert metrics["livekit_bytes_in_counter"] == 300.0


def test_parse_ignores_comments_and_blank_lines():
    metrics = pc.parse_metrics("\n# a comment\n\nfoo 1\n")
    assert metrics == {"foo": 1.0}


def test_parse_handles_metrics_without_labels():
    assert pc.parse_metrics("simple_metric 42")["simple_metric"] == 42.0


def test_parse_skips_unparseable_values():
    metrics = pc.parse_metrics("good 1\nbad NaN\nalso_bad +Inf\n")
    assert "good" in metrics
    assert "bad" not in metrics


def test_parse_of_empty_input():
    assert pc.parse_metrics("") == {}


# ---------------------------------------------------------------------------
# Counter selection — must never guess
# ---------------------------------------------------------------------------

def test_selects_the_known_counters():
    counters = pc.select_counters(pc.parse_metrics(METRICS_FIXTURE))
    assert counters["down"] == 1500.0
    assert counters["up"] == 300.0
    assert counters["down_name"] == "livekit_bytes_out_counter"


def test_reports_nothing_when_no_counter_is_recognised(caplog):
    """Better to say "not measured" than to bill against the wrong metric."""
    counters = pc.select_counters({"some_other_metric": 5.0})
    assert counters["down"] is None
    assert counters["up"] is None


def test_env_override_selects_an_explicit_metric(monkeypatch):
    monkeypatch.setenv("LIVEKIT_BANDWIDTH_METRIC_DOWN", "custom_out_bytes")
    counters = pc.select_counters({"custom_out_bytes": 999.0})
    assert counters["down"] == 999.0


def test_env_override_naming_a_missing_metric_reports_nothing(monkeypatch):
    monkeypatch.setenv("LIVEKIT_BANDWIDTH_METRIC_DOWN", "not_present")
    counters = pc.select_counters({"livekit_bytes_out_counter": 100.0})
    assert counters["down"] is None


def test_gauges_are_not_mistaken_for_counters():
    """LiveKit's *_total metrics are gauges; summing them would be nonsense."""
    counters = pc.select_counters({"livekit_room_total": 4.0,
                                   "livekit_participant_total": 12.0})
    assert counters["down"] is None


# ---------------------------------------------------------------------------
# Counter deltas
# ---------------------------------------------------------------------------

def test_delta_between_two_readings():
    assert pc.counter_delta(100.0, 250.0) == 150.0


def test_first_reading_bills_nothing():
    """The baseline reading has nothing to subtract from."""
    assert pc.counter_delta(None, 1000.0) is None


def test_counter_reset_skips_the_interval():
    """A restart resets the counter; treating it as a delta would spike the day."""
    assert pc.counter_delta(5_000_000.0, 12.0) is None


def test_delta_of_a_missing_current_reading():
    assert pc.counter_delta(100.0, None) is None


def test_delta_of_an_unchanged_counter_is_zero():
    assert pc.counter_delta(100.0, 100.0) == 0.0


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

async def test_scrape_parses_a_successful_response():
    with _mock_scrape(METRICS_FIXTURE):
        metrics = await pc.scrape("http://lk.example.com:6789/metrics")
    assert metrics["livekit_bytes_out_counter"] == 1500.0


async def test_scrape_returns_none_when_unreachable():
    with patch.object(httpx.AsyncClient, "get",
                      new=AsyncMock(side_effect=httpx.ConnectError("refused"))):
        assert await pc.scrape("http://lk.example.com:6789/metrics") is None


async def test_scrape_returns_none_on_an_error_status():
    with patch.object(httpx.AsyncClient, "get",
                      new=AsyncMock(return_value=_response("nope", status=500))):
        assert await pc.scrape("http://x") is None


async def test_scrape_of_a_blank_url():
    assert await pc.scrape("") is None


# ---------------------------------------------------------------------------
# Sampling into rollups
# ---------------------------------------------------------------------------

async def test_first_sample_establishes_a_baseline_only(db):
    with _mock_scrape(METRICS_FIXTURE):
        result = await pc.sample_once(db, project())

    assert result["baseline"] is True
    assert len(db["bandwidth_samples"].docs) == 1

    rollup = db["usage_rollups"].docs[0]
    assert rollup["bandwidth_bytes_down"] == 0
    assert rollup["bandwidth_source"] == "prometheus"


async def test_second_sample_records_the_delta(db):
    with _mock_scrape(METRICS_FIXTURE):
        await pc.sample_once(db, project())

    grown = METRICS_FIXTURE.replace(
        'livekit_bytes_out_counter{node_id="ND_1",direction="down"} 1000',
        'livekit_bytes_out_counter{node_id="ND_1",direction="down"} 3000',
    )
    with _mock_scrape(grown):
        result = await pc.sample_once(db, project())

    assert result["down_delta"] == 2000.0
    assert db["usage_rollups"].docs[0]["bandwidth_bytes_down"] == 2000


async def test_deltas_accumulate_across_samples(db):
    for value in (1000, 2000, 3000):
        text = METRICS_FIXTURE.replace(
            'livekit_bytes_out_counter{node_id="ND_1",direction="down"} 1000',
            f'livekit_bytes_out_counter{{node_id="ND_1",direction="down"}} {value}',
        )
        with _mock_scrape(text):
            await pc.sample_once(db, project())

    # 1000->2000 and 2000->3000; the first reading is only a baseline.
    assert db["usage_rollups"].docs[0]["bandwidth_bytes_down"] == 2000


async def test_counter_reset_is_skipped_and_flagged(db):
    """Restarting LiveKit must produce a gap, not a spike."""
    with _mock_scrape(METRICS_FIXTURE):
        await pc.sample_once(db, project())

    grown = METRICS_FIXTURE.replace(
        'livekit_bytes_out_counter{node_id="ND_1",direction="down"} 1000',
        'livekit_bytes_out_counter{node_id="ND_1",direction="down"} 9000',
    )
    with _mock_scrape(grown):
        await pc.sample_once(db, project())

    restarted = METRICS_FIXTURE.replace(
        'livekit_bytes_out_counter{node_id="ND_1",direction="down"} 1000',
        'livekit_bytes_out_counter{node_id="ND_1",direction="down"} 5',
    )
    with _mock_scrape(restarted):
        result = await pc.sample_once(db, project())

    assert result["restarted"] is True
    assert result["down_delta"] is None

    rollup = db["usage_rollups"].docs[0]
    assert rollup["bandwidth_gap"] is True
    assert rollup["bandwidth_bytes_down"] == 8000  # the reset interval added nothing


async def test_unreachable_endpoint_writes_nothing(db):
    with patch.object(httpx.AsyncClient, "get",
                      new=AsyncMock(side_effect=httpx.ConnectError("refused"))):
        result = await pc.sample_once(db, project())

    assert result["ok"] is False
    assert db["bandwidth_samples"].docs == []
    assert db["usage_rollups"].docs == []


async def test_unrecognised_metrics_leave_bandwidth_unmeasured(db):
    with _mock_scrape("some_unrelated_metric 5"):
        result = await pc.sample_once(db, project())

    assert result["ok"] is False
    assert result["reason"] == "no recognised counters"
    assert db["usage_rollups"].docs == []


async def test_project_without_a_metrics_url_is_skipped(db):
    result = await pc.sample_once(db, project(prometheus_url=""))
    assert result["ok"] is False
    assert db["bandwidth_samples"].docs == []


async def test_sample_without_a_database():
    result = await pc.sample_once(None, project())
    assert result["ok"] is False


async def test_samples_are_isolated_per_project(db):
    other = Project(id="p2", name="Other", slug="other",
                    livekit_url="wss://other.example.com", api_key="k",
                    api_secret="s", prometheus_url="http://other:6789/metrics")

    with _mock_scrape(METRICS_FIXTURE):
        await pc.sample_once(db, project())
        await pc.sample_once(db, other)

    project_ids = {d["project_id"] for d in db["bandwidth_samples"].docs}
    assert project_ids == {"p1", "p2"}
