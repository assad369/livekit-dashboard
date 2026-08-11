"""Inbound LiveKit webhook receiver.

This is the only unauthenticated write endpoint in the app: it authenticates
via the signed JWT LiveKit sends rather than a session, so it is allowlisted
in AuthMiddleware and exempt from readonly mode.
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.db import mongo
from app.services import usage, webhooks

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])

# LiveKit event payloads are small; anything larger is not from LiveKit.
MAX_BODY_BYTES = 256 * 1024


@router.post("/livekit")
async def livekit_webhook(request: Request):
    # Reject on the declared length before reading anything: this endpoint is
    # unauthenticated, so buffering the body first would let anyone push
    # gigabytes into memory before the size check ran.
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_BODY_BYTES:
                return JSONResponse({"detail": "payload too large"}, status_code=413)
        except ValueError:
            return JSONResponse({"detail": "bad content-length"}, status_code=400)

    # Chunked or mis-declared bodies have no usable Content-Length, so also
    # stream with a hard cap rather than trusting the header.
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_BODY_BYTES:
            return JSONResponse({"detail": "payload too large"}, status_code=413)
        chunks.append(chunk)
    body = b"".join(chunks)

    db = mongo.get_database()

    try:
        project, event = await webhooks.verify(
            db, body, request.headers.get("Authorization", "")
        )
    except webhooks.WebhookVerificationError as exc:
        logger.warning("rejected webhook delivery: %s", exc)
        return JSONResponse({"detail": "unauthorized"}, status_code=401)

    if db is None:
        # Deliberately not 200. LiveKit retries on failure, so reporting an
        # outage here means a database blip delays billing data instead of
        # losing it.
        logger.error("dropping verified webhook — no database configured")
        return JSONResponse({"detail": "storage unavailable"}, status_code=503)

    try:
        # Ingested inline rather than via BackgroundTasks: returning 200 before
        # the write lands would tell LiveKit not to retry an event we lost.
        # A duplicate returns False and is also a success — the retry did its
        # job, and the event is already recorded exactly once.
        await usage.ingest_event(db, project.id, event, raw=body)
    except Exception as exc:
        logger.exception("failed to ingest webhook event: %s", exc)
        return JSONResponse({"detail": "ingest failed"}, status_code=503)

    return Response(status_code=200)
