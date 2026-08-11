"""Tests for live SSE event stream route."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient


def _connected_request(disconnected: bool = False):
    """Minimal stand-in for the Request the SSE generator polls."""
    req = MagicMock()
    req.is_disconnected = AsyncMock(return_value=disconnected)
    return req


def test_events_page_renders(client, auth_headers):
    resp = client.get("/events", headers=auth_headers)
    assert resp.status_code == 200
    assert b"Event" in resp.content


def test_events_page_requires_auth(unauth_client):
    resp = unauth_client.get("/events")
    assert resp.status_code in (401, 403)


def test_events_stream_endpoint_registered():
    """SSE stream endpoint should be registered."""
    from app.routes.events import events_stream
    assert callable(events_stream)


async def test_sse_generator_emits_room_created():
    """Unit-test the SSE generator: room created event when a new room appears."""
    from app.routes.events import _sse_generator

    mock_room = MagicMock()
    mock_room.name = "test-room"

    mock_lk = AsyncMock()
    call_count = [0]

    async def list_rooms():
        call_count[0] += 1
        if call_count[0] == 1:
            return [mock_room], 5.0
        raise StopIteration

    mock_lk.list_rooms = list_rooms
    mock_lk.list_participants = AsyncMock(return_value=[])

    events = []
    gen = _sse_generator(mock_lk, _connected_request())
    with patch("asyncio.sleep", new_callable=AsyncMock):
        try:
            async for chunk in gen:
                events.append(chunk)
                if "room.created" in chunk:
                    break
                if len(events) > 10:
                    break
        except Exception:
            pass

    combined = "".join(events)
    assert "room.created" in combined
    assert "test-room" in combined


async def test_sse_generator_emits_participant_joined():
    from app.routes.events import _sse_generator

    mock_room = MagicMock()
    mock_room.name = "r1"
    mock_p = MagicMock()
    mock_p.identity = "alice"

    mock_lk = AsyncMock()
    call_count = [0]

    async def list_rooms():
        return [mock_room], 5.0

    async def list_participants(name):
        call_count[0] += 1
        return [] if call_count[0] == 1 else [mock_p]

    mock_lk.list_rooms = list_rooms
    mock_lk.list_participants = list_participants

    events = []
    gen = _sse_generator(mock_lk, _connected_request())
    with patch("asyncio.sleep", new_callable=AsyncMock):
        try:
            async for chunk in gen:
                events.append(chunk)
                if "participant.joined" in chunk:
                    break
                if len(events) > 20:
                    break
        except Exception:
            pass

    combined = "".join(events)
    assert "participant.joined" in combined
    assert "alice" in combined


async def test_sse_generator_emits_room_closed():
    from app.routes.events import _sse_generator

    mock_room = MagicMock()
    mock_room.name = "gone-room"

    mock_lk = AsyncMock()
    call_count = [0]

    async def list_rooms():
        call_count[0] += 1
        return ([mock_room], 5.0) if call_count[0] == 1 else ([], 5.0)

    mock_lk.list_rooms = list_rooms
    mock_lk.list_participants = AsyncMock(return_value=[])

    events = []
    gen = _sse_generator(mock_lk, _connected_request())
    with patch("asyncio.sleep", new_callable=AsyncMock):
        try:
            async for chunk in gen:
                events.append(chunk)
                if "room.closed" in chunk:
                    break
                if len(events) > 20:
                    break
        except Exception:
            pass

    combined = "".join(events)
    assert "room.closed" in combined
    assert "gone-room" in combined


async def test_sse_generator_emits_error_on_livekit_failure():
    from app.routes.events import _sse_generator

    mock_lk = AsyncMock()
    mock_lk.list_rooms = AsyncMock(side_effect=RuntimeError("connection refused"))

    events = []
    gen = _sse_generator(mock_lk, _connected_request())
    with patch("asyncio.sleep", new_callable=AsyncMock):
        try:
            async for chunk in gen:
                events.append(chunk)
                if "error" in chunk:
                    break
                if len(events) > 5:
                    break
        except Exception:
            pass

    combined = "".join(events)
    assert "error" in combined


async def test_sse_generator_stops_when_client_disconnects():
    """Closing a browser tab must end the poll loop, not leak a polling task."""
    from app.routes.events import _sse_generator

    mock_room = MagicMock()
    mock_room.name = "test-room"
    mock_lk = AsyncMock()
    mock_lk.list_rooms = AsyncMock(return_value=([mock_room], 5.0))
    mock_lk.list_participants = AsyncMock(return_value=[])

    chunks = []
    gen = _sse_generator(mock_lk, _connected_request(disconnected=True))
    with patch("asyncio.sleep", new_callable=AsyncMock):
        async for chunk in gen:
            chunks.append(chunk)

    # The generator finishes on its own after the initial keepalive, and never
    # polls LiveKit — without the disconnect check this would loop forever.
    assert chunks == [": keepalive\n\n"]
    mock_lk.list_rooms.assert_not_awaited()


async def test_sse_generator_polls_while_client_is_connected():
    """The disconnect check must not short-circuit a live connection."""
    from app.routes.events import _sse_generator

    mock_room = MagicMock()
    mock_room.name = "test-room"
    mock_lk = AsyncMock()
    mock_lk.list_rooms = AsyncMock(return_value=([mock_room], 5.0))
    mock_lk.list_participants = AsyncMock(return_value=[])

    gen = _sse_generator(mock_lk, _connected_request(disconnected=False))
    with patch("asyncio.sleep", new_callable=AsyncMock):
        async for chunk in gen:
            if "room.created" in chunk:
                break

    mock_lk.list_rooms.assert_awaited()
