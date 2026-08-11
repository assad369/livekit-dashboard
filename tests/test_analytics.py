"""Tests that LiveKitClient analytics report real data, not fabricated estimates.

Regression coverage for the bug where get_enhanced_analytics/get_egress_analytics/
get_ingress_analytics invented numbers (platform breakdowns, connection minutes,
storage size, bitrate) that looked identical to real LiveKit metrics.
"""
from unittest.mock import AsyncMock, patch

import pytest
from livekit.protocol import egress as egress_pb
from livekit.protocol import ingress as ingress_pb

from app.services.livekit import LiveKitClient


@pytest.fixture
def lk():
    return LiveKitClient()


# ---------------------------------------------------------------------------
# get_egress_analytics
# ---------------------------------------------------------------------------

async def test_egress_storage_used_is_real_not_mocked(lk):
    """storage_used_gb must come from actual file_results sizes, not len(jobs) * 0.5."""
    job_with_file = egress_pb.EgressInfo(
        status=egress_pb.EgressStatus.EGRESS_COMPLETE,
        file_results=[egress_pb.FileInfo(size=2 * 1024 ** 3)],  # 2 GiB
    )
    job_without_file = egress_pb.EgressInfo(status=egress_pb.EgressStatus.EGRESS_COMPLETE)

    with patch.object(lk, "list_egress", new=AsyncMock(return_value=[job_with_file, job_without_file])):
        result = await lk.get_egress_analytics()

    # Old mock formula would have given len(all_egress) * 0.5 == 1.0 GB regardless of content.
    assert result["storage_used_gb"] == pytest.approx(2.0)


async def test_egress_storage_used_zero_when_no_file_results(lk):
    job = egress_pb.EgressInfo(status=egress_pb.EgressStatus.EGRESS_ACTIVE)

    with patch.object(lk, "list_egress", new=AsyncMock(return_value=[job])):
        result = await lk.get_egress_analytics()

    assert result["storage_used_gb"] == 0


# ---------------------------------------------------------------------------
# get_ingress_analytics
# ---------------------------------------------------------------------------

async def test_ingress_types_reflect_real_input_type(lk):
    """ingress_types must be classified from the real input_type, not always 'rtmp'."""
    items = [
        ingress_pb.IngressInfo(input_type=ingress_pb.IngressInput.RTMP_INPUT),
        ingress_pb.IngressInfo(input_type=ingress_pb.IngressInput.WHIP_INPUT),
        ingress_pb.IngressInfo(input_type=ingress_pb.IngressInput.URL_INPUT),
        ingress_pb.IngressInfo(input_type=ingress_pb.IngressInput.URL_INPUT),
    ]

    with patch.object(lk, "list_ingress", new=AsyncMock(return_value=items)):
        result = await lk.get_ingress_analytics()

    assert result["ingress_types"] == {"rtmp": 1, "whip": 1, "url": 2}


async def test_ingress_bitrate_averages_active_streams_only(lk):
    active = ingress_pb.IngressInfo(
        input_type=ingress_pb.IngressInput.RTMP_INPUT,
        state=ingress_pb.IngressState(
            status=ingress_pb.IngressState.Status.ENDPOINT_PUBLISHING,
            video=ingress_pb.InputVideoState(average_bitrate=4_000_000),
        ),
    )
    inactive = ingress_pb.IngressInfo(
        input_type=ingress_pb.IngressInput.RTMP_INPUT,
        state=ingress_pb.IngressState(status=ingress_pb.IngressState.Status.ENDPOINT_INACTIVE),
    )

    with patch.object(lk, "list_ingress", new=AsyncMock(return_value=[active, inactive])):
        result = await lk.get_ingress_analytics()

    # Old code hardcoded avg_bitrate_mbps = 2.5 regardless of input.
    assert result["avg_bitrate_mbps"] == pytest.approx(4.0)
    assert result["active_ingress"] == 1


async def test_ingress_analytics_no_data_when_empty(lk):
    with patch.object(lk, "list_ingress", new=AsyncMock(return_value=[])):
        result = await lk.get_ingress_analytics()

    assert result["avg_bitrate_mbps"] == 0
    assert result["ingress_types"] == {"rtmp": 0, "whip": 0, "url": 0}


# ---------------------------------------------------------------------------
# get_enhanced_analytics
# ---------------------------------------------------------------------------

async def test_enhanced_analytics_has_no_fabricated_breakdowns(lk):
    """platforms/connection_types/connection_minutes must not be invented."""
    room_analytics = {
        "total_rooms": 4,
        "active_rooms": 3,
        "total_participants": 17,
    }

    with patch.object(lk, "get_room_analytics", new=AsyncMock(return_value=room_analytics)):
        result = await lk.get_enhanced_analytics()

    # Real, derived value.
    assert result["connection_success"] == pytest.approx(75.0)
    # No data source exists for these — must be empty/zero, not estimated from participant count.
    assert result["platforms"] == {}
    assert result["connection_types"] == {}
    assert result["connection_minutes"] == 0


async def test_enhanced_analytics_error_fallback_is_honest(lk):
    with patch.object(lk, "get_room_analytics", new=AsyncMock(side_effect=RuntimeError("boom"))):
        result = await lk.get_enhanced_analytics()

    assert result["connection_success"] == 0
    assert result["platforms"] == {}
    assert result["connection_types"] == {}


# ---------------------------------------------------------------------------
# Where real usage data now comes from
# ---------------------------------------------------------------------------

async def test_enhanced_analytics_connection_minutes_still_zero_by_design():
    """LiveKit's real-time API has no history, so it cannot report minutes.

    This is not a gap waiting to be filled in here. Real connection minutes are
    derived from stored LiveKit webhook events by app/services/usage.py and
    injected into DashboardStats by gather_dashboard_stats(usage_provider=...).
    Estimating them from a live room snapshot is exactly the fabrication this
    module exists to prevent.
    """
    from app.services.livekit import LiveKitClient

    lk = LiveKitClient()
    with patch.object(lk, "list_rooms", new=AsyncMock(return_value=([], 0.01))), \
         patch.object(lk, "get_all_participants_across_rooms", new=AsyncMock(return_value=[])):
        analytics = await lk.get_enhanced_analytics()

    assert analytics["connection_minutes"] == 0
    assert analytics["platforms"] == {}
    assert analytics["connection_types"] == {}


def test_the_webhook_analytics_stub_is_gone():
    """It returned zeros with a TODO; usage.get_webhook_analytics is the real one."""
    from app.services.livekit import LiveKitClient

    assert not hasattr(LiveKitClient, "get_webhook_analytics")
