"""Tests for webhook notification service."""

import json
import httpx
import pytest
from dataclasses import dataclass
from unittest.mock import patch, AsyncMock


@dataclass
class _Rule:
    id: str = "r1"
    name: str = "Test rule"
    metric: str = "rooms_total"
    operator: str = ">"
    threshold: float = 0
    severity: str = "warning"


def _mock_post(status: int = 200):
    """Patch httpx.AsyncClient.post to return *status* without a network call."""
    return patch.object(
        httpx.AsyncClient,
        "post",
        new=AsyncMock(return_value=httpx.Response(status)),
    )


async def test_get_config_defaults(tmp_path):
    import app.services.notifications as n
    with patch.object(n, "_STORE_PATH", str(tmp_path / "notif.json")):
        cfg = await n.get_config()
    assert cfg["webhook_url"] == ""
    assert cfg["cooldown_minutes"] == 10
    assert cfg["last_fired"] == ""


async def test_save_and_get_config(tmp_path):
    import app.services.notifications as n
    with patch.object(n, "_STORE_PATH", str(tmp_path / "notif.json")):
        await n.save_config("https://example.com/hook", cooldown_minutes=5)
        cfg = await n.get_config()
    assert cfg["webhook_url"] == "https://example.com/hook"
    assert cfg["cooldown_minutes"] == 5


async def test_fire_webhook_no_triggered(tmp_path):
    import app.services.notifications as n
    with patch.object(n, "_STORE_PATH", str(tmp_path / "notif.json")):
        result = await n.fire_webhook([])
    assert result is None


async def test_fire_webhook_no_url(tmp_path):
    import app.services.notifications as n
    with patch.object(n, "_STORE_PATH", str(tmp_path / "notif.json")):
        result = await n.fire_webhook([_Rule()])
    assert result is None


async def test_fire_webhook_success(tmp_path):
    import app.services.notifications as n

    with patch.object(n, "_STORE_PATH", str(tmp_path / "notif.json")):
        await n.save_config("https://example.com/hook")
        with _mock_post(200):
            result = await n.fire_webhook([_Rule()])

    assert result is not None
    assert result["status"] == 200
    assert result["error"] is None


async def test_fire_webhook_records_last_fired(tmp_path):
    import app.services.notifications as n

    with patch.object(n, "_STORE_PATH", str(tmp_path / "notif.json")):
        await n.save_config("https://example.com/hook")
        with _mock_post(200):
            await n.fire_webhook([_Rule()])
        cfg = await n.get_config()

    assert cfg["last_fired"] != ""


async def test_fire_webhook_respects_cooldown(tmp_path):
    import app.services.notifications as n
    from datetime import datetime, timezone

    with patch.object(n, "_STORE_PATH", str(tmp_path / "notif.json")):
        await n.save_config("https://example.com/hook", cooldown_minutes=60)
        # Pre-populate a recent last_fired
        cfg_data = {"webhook_url": "https://example.com/hook", "cooldown_minutes": 60,
                    "last_fired": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        (tmp_path / "notif.json").write_text(json.dumps(cfg_data))

        with _mock_post(200) as mock_post:
            result = await n.fire_webhook([_Rule()])
            mock_post.assert_not_called()

    assert result is None


async def test_fire_webhook_force_bypasses_cooldown(tmp_path):
    import app.services.notifications as n
    from datetime import datetime, timezone

    with patch.object(n, "_STORE_PATH", str(tmp_path / "notif.json")):
        cfg_data = {"webhook_url": "https://example.com/hook", "cooldown_minutes": 60,
                    "last_fired": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        (tmp_path / "notif.json").write_text(json.dumps(cfg_data))

        with _mock_post(204):
            result = await n.fire_webhook([_Rule()], force=True)

    assert result is not None
    assert result["status"] == 204


async def test_fire_webhook_reports_transport_error(tmp_path):
    """A network failure is reported, not raised — the page must still render."""
    import app.services.notifications as n

    with patch.object(n, "_STORE_PATH", str(tmp_path / "notif.json")):
        await n.save_config("https://example.com/hook")
        with patch.object(
            httpx.AsyncClient,
            "post",
            new=AsyncMock(side_effect=httpx.ConnectError("refused")),
        ):
            result = await n.fire_webhook([_Rule()])

    assert result["status"] is None
    assert "refused" in result["error"]
