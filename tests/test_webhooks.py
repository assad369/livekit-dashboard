"""Tests for the inbound LiveKit webhook endpoint.

Deliveries are signed with real JWTs built the way the LiveKit server builds
them, so these exercise the actual SDK verification rather than a mock of it.
"""

import hashlib
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.db import mongo
from app.security import crypto
from app.services import projects as project_service
from app.services import webhooks
from tests.fake_mongo import FakeDatabase


API_KEY = "APItestkey"
API_SECRET = "test-secret-at-least-32-characters-long"


def sign(body: bytes, api_key: str = API_KEY, api_secret: str = API_SECRET) -> str:
    """Build the Authorization JWT exactly as the LiveKit server does.

    The signature covers a SHA-256 of the body, so tampering with either the
    payload or the token invalidates the delivery.
    """
    import jwt

    now = int(time.time())
    digest = hashlib.sha256(body).digest()
    import base64

    claims = {
        "iss": api_key,
        "exp": now + 300,
        "nbf": now - 10,
        "sha256": base64.b64encode(digest).decode(),
    }
    return jwt.encode(claims, api_secret, algorithm="HS256")


def payload(event: str = "room_started", event_id: str = "EV_1", **extra) -> bytes:
    body = {
        "event": event,
        "id": event_id,
        "createdAt": str(int(time.time())),
        "room": {"sid": "RM_1", "name": "demo", "creationTime": str(int(time.time()))},
    }
    body.update(extra)
    return json.dumps(body).encode()


@pytest.fixture
async def wired(monkeypatch):
    """A client whose app has one project using the test credentials."""
    monkeypatch.setenv("APP_ENCRYPTION_KEY", crypto.generate_key())
    db = FakeDatabase()
    await db["usage_events"].create_index(
        [("project_id", 1), ("event_id", 1)], unique=True
    )
    monkeypatch.setattr(mongo, "_db", db)
    webhooks.clear_receiver_cache()

    project = await project_service.create_project(
        db, name="Prod", livekit_url="wss://lk.example.com",
        api_key=API_KEY, api_secret=API_SECRET,
    )

    from app.main import app
    with TestClient(app) as client:
        yield client, db, project

    webhooks.clear_receiver_cache()


def post(client, body: bytes, token: str | None = None):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = token
    return client.post("/webhooks/livekit", content=body, headers=headers)


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------

def test_endpoint_is_reachable_without_a_session(wired):
    """It authenticates with LiveKit's JWT, not a dashboard login."""
    client, _, _ = wired
    body = payload()
    assert post(client, body, sign(body)).status_code == 200


def test_endpoint_works_in_readonly_mode(wired, monkeypatch):
    """Readonly stops operators mutating state; it must not stop billing data."""
    monkeypatch.setenv("DASHBOARD_ROLE", "readonly")
    client, _, _ = wired
    body = payload()
    assert post(client, body, sign(body)).status_code == 200


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def test_valid_delivery_is_stored(wired):
    client, db, project = wired
    body = payload()

    assert post(client, body, sign(body)).status_code == 200

    events = db["usage_events"].docs
    assert len(events) == 1
    assert events[0]["project_id"] == project.id
    assert events[0]["event_id"] == "EV_1"


def test_missing_authorization_is_rejected(wired):
    client, db, _ = wired
    assert post(client, payload()).status_code == 401
    assert db["usage_events"].docs == []


def test_wrong_secret_is_rejected(wired):
    client, db, _ = wired
    body = payload()
    assert post(client, body, sign(body, api_secret="a-completely-different-secret")).status_code == 401
    assert db["usage_events"].docs == []


def test_tampered_body_is_rejected(wired):
    """A valid token must not authenticate a payload it did not sign."""
    client, db, _ = wired
    token = sign(payload())

    assert post(client, payload(event_id="EV_TAMPERED"), token).status_code == 401
    assert db["usage_events"].docs == []


def test_unknown_issuer_is_rejected(wired):
    client, db, _ = wired
    body = payload()
    assert post(client, body, sign(body, api_key="APInobodyknows")).status_code == 401
    assert db["usage_events"].docs == []


def test_garbage_token_is_rejected(wired):
    client, _, _ = wired
    assert post(client, payload(), "not-a-jwt").status_code == 401


def test_bearer_prefix_is_tolerated(wired):
    client, _, _ = wired
    body = payload()
    assert post(client, body, f"Bearer {sign(body)}").status_code == 200


def test_oversized_body_is_refused(wired):
    client, db, _ = wired
    huge = b"x" * (256 * 1024 + 1)
    assert post(client, huge, sign(huge)).status_code == 413
    assert db["usage_events"].docs == []


# ---------------------------------------------------------------------------
# Idempotency and durability
# ---------------------------------------------------------------------------

def test_retried_delivery_is_recorded_once(wired):
    """LiveKit retries on failure; a retry must not bill twice."""
    client, db, _ = wired
    body = payload()
    token = sign(body)

    assert post(client, body, token).status_code == 200
    assert post(client, body, token).status_code == 200

    assert len(db["usage_events"].docs) == 1


def test_storage_outage_returns_503_so_livekit_retries(wired, monkeypatch):
    """Returning 200 here would tell LiveKit to discard billing data.

    The delivery still has to verify, so this uses the environment project —
    with Mongo gone, a Mongo-only project could not be looked up at all.
    """
    client, _, _ = wired
    monkeypatch.setenv("LIVEKIT_API_KEY", API_KEY)
    monkeypatch.setenv("LIVEKIT_API_SECRET", API_SECRET)
    monkeypatch.setattr(mongo, "_db", None)
    webhooks.clear_receiver_cache()

    body = payload()
    assert post(client, body, sign(body)).status_code == 503


def test_unverifiable_delivery_during_an_outage_is_rejected_not_accepted(wired, monkeypatch):
    """With storage down a Mongo-only project cannot be authenticated.

    Rejecting is correct: accepting an unverified payload would let anyone
    inject usage data by waiting for a database blip.
    """
    client, _, _ = wired
    monkeypatch.delenv("LIVEKIT_API_KEY", raising=False)
    monkeypatch.setattr(mongo, "_db", None)
    webhooks.clear_receiver_cache()

    body = payload()
    assert post(client, body, sign(body)).status_code == 401


def test_ingest_failure_returns_503_without_a_partial_write(wired, monkeypatch):
    client, db, _ = wired

    async def _boom(*args, **kwargs):
        raise RuntimeError("write failed")

    from app.services import usage
    monkeypatch.setattr(usage, "ingest_event", _boom)

    body = payload()
    assert post(client, body, sign(body)).status_code == 503


# ---------------------------------------------------------------------------
# Multi-project attribution
# ---------------------------------------------------------------------------

async def test_two_projects_sharing_an_api_key_are_disambiguated_by_signature(
    wired, monkeypatch
):
    """The key only narrows candidates — the signature picks the real project."""
    client, db, first = wired

    other_secret = "another-secret-that-is-also-32-chars"
    second = await project_service.create_project(
        db, name="Staging", livekit_url="wss://staging.example.com",
        api_key=API_KEY, api_secret=other_secret,
    )
    webhooks.clear_receiver_cache()

    body = payload(event_id="EV_FOR_SECOND")
    assert post(client, body, sign(body, api_secret=other_secret)).status_code == 200

    stored = [e for e in db["usage_events"].docs if e["event_id"] == "EV_FOR_SECOND"]
    assert len(stored) == 1
    assert stored[0]["project_id"] == second.id
    assert stored[0]["project_id"] != first.id


async def test_events_land_under_the_signing_project(wired):
    client, db, project = wired
    body = payload()
    post(client, body, sign(body))
    assert db["usage_events"].docs[0]["project_id"] == project.id


# ---------------------------------------------------------------------------
# Verification unit tests
# ---------------------------------------------------------------------------

async def test_verify_rejects_a_blank_header():
    with pytest.raises(webhooks.WebhookVerificationError, match="missing Authorization"):
        await webhooks.verify(None, b"{}", "")


async def test_verify_rejects_a_token_without_an_issuer():
    import jwt
    token = jwt.encode({"exp": int(time.time()) + 60}, "s", algorithm="HS256")
    with pytest.raises(webhooks.WebhookVerificationError, match="no issuer"):
        await webhooks.verify(None, b"{}", token)


async def test_verify_rejects_non_utf8_bodies(monkeypatch):
    monkeypatch.setenv("LIVEKIT_API_KEY", API_KEY)
    monkeypatch.setenv("LIVEKIT_API_SECRET", API_SECRET)
    body = b"\xff\xfe not utf-8"
    with pytest.raises(webhooks.WebhookVerificationError, match="not valid UTF-8"):
        await webhooks.verify(None, body, sign(body))


async def test_verify_falls_back_to_the_environment_project(monkeypatch):
    """A deployment with no stored projects must still receive webhooks."""
    monkeypatch.setenv("LIVEKIT_API_KEY", API_KEY)
    monkeypatch.setenv("LIVEKIT_API_SECRET", API_SECRET)
    webhooks.clear_receiver_cache()

    body = payload()
    project, event = await webhooks.verify(None, body, sign(body))

    assert project.source == "env"
    assert event["event"] == "room_started"


# ---------------------------------------------------------------------------
# Hardening of the unauthenticated surface
# ---------------------------------------------------------------------------

def test_oversized_body_is_refused_before_it_is_buffered(wired):
    """The endpoint is unauthenticated, so it must not buffer a huge body
    into memory and only then check the size."""
    client, db, _ = wired

    resp = client.post(
        "/webhooks/livekit",
        content=b"x" * 1024,
        headers={"Authorization": "x", "Content-Length": str(50 * 1024 * 1024)},
    )
    assert resp.status_code == 413
    assert db["usage_events"].docs == []


def test_bad_content_length_is_refused(wired):
    client, _, _ = wired
    resp = client.post(
        "/webhooks/livekit",
        content=b"{}",
        headers={"Authorization": "x", "Content-Length": "not-a-number"},
    )
    assert resp.status_code in (400, 413, 422)


def craft_token(claims: dict) -> str:
    """Hand-assemble a JWT.

    PyJWT refuses to *encode* a non-string `iss`, but an attacker is not using
    PyJWT — they are writing the base64 segments directly. Only hand-crafting
    reproduces the real attack.
    """
    import base64
    import hashlib
    import hmac

    def seg(obj) -> bytes:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    signing_input = seg({"alg": "HS256", "typ": "JWT"}) + b"." + seg(claims)
    signature = hmac.new(b"any-secret", signing_input, hashlib.sha256).digest()
    return (signing_input + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()


@pytest.mark.parametrize("hostile_iss", [
    {"$regex": "^(a+)+$"},
    {"$ne": None},
    ["APItestkey"],
    12345,
    None,
])
async def test_non_string_issuer_is_rejected(hostile_iss):
    """A JWT payload is attacker-controlled JSON. An object `iss` reaching the
    Mongo query would smuggle in operators — an unauthenticated database DoS."""
    token = craft_token({"iss": hostile_iss, "exp": int(time.time()) + 300})

    with pytest.raises(webhooks.WebhookVerificationError, match="no issuer"):
        await webhooks.verify(None, b"{}", token)


async def test_object_issuer_never_reaches_the_database(wired):
    """Guards the query itself, not just the error message."""
    client, db, _ = wired
    queried = []

    original_find = db["projects"].find

    def _spy(query=None, *args, **kwargs):
        queried.append(query)
        return original_find(query, *args, **kwargs)

    db["projects"].find = _spy

    token = craft_token({"iss": {"$ne": None}, "exp": int(time.time()) + 300})

    resp = post(client, payload(), token)

    assert resp.status_code == 401
    assert queried == [], "an attacker-controlled object was passed to Mongo"
