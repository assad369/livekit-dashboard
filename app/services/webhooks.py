"""Verify inbound LiveKit webhooks and attribute them to a project.

LiveKit signs each delivery with a JWT in the Authorization header, whose
`iss` claim is the API key and whose body carries a SHA-256 of the payload.
Because two projects may share an API key against different servers, the key
only narrows the candidates — the signature decides which one it really is.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.services import projects as project_service
from app.services.projects import Project

logger = logging.getLogger(__name__)

_receivers: dict[str, object] = {}


class WebhookVerificationError(Exception):
    """The delivery could not be attributed to a project."""


def _unverified_issuer(token: str) -> Optional[str]:
    """Read the `iss` claim without verifying — just to pick candidates.

    Nothing is trusted from this: the signature check below is what actually
    authenticates the delivery.
    """
    import jwt

    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except Exception as exc:
        logger.debug("could not decode webhook JWT: %s", exc)
        return None

    issuer = claims.get("iss")
    # A JWT payload is arbitrary attacker-controlled JSON, so `iss` may be an
    # object. Passing one into a Mongo query would smuggle in operators —
    # `{"$regex": "^(a+)+$"}` would burn database CPU on every project,
    # unauthenticated. Only a plain string is a usable API key.
    if not isinstance(issuer, str) or not issuer:
        logger.warning("webhook JWT issuer is not a string: %r", type(issuer).__name__)
        return None
    return issuer


def _receiver_for(project: Project):
    """Cache one WebhookReceiver per credential pair."""
    from livekit.api import TokenVerifier, WebhookReceiver

    cache_key = f"{project.api_key}|{project.api_secret}"
    receiver = _receivers.get(cache_key)
    if receiver is None:
        # Always pass the credentials explicitly. TokenVerifier's no-argument
        # form reads LIVEKIT_API_KEY/_SECRET from the environment, which would
        # let one project's webhook validate against another's credentials.
        receiver = WebhookReceiver(TokenVerifier(project.api_key, project.api_secret))
        _receivers[cache_key] = receiver
    return receiver


def clear_receiver_cache() -> None:
    _receivers.clear()


def _to_dict(event) -> dict:
    """Convert the protobuf WebhookEvent to a plain dict."""
    from google.protobuf.json_format import MessageToDict

    return MessageToDict(
        event,
        preserving_proto_field_name=True,
        always_print_fields_with_no_presence=True,
    )


async def verify(db, body: bytes, auth_header: str) -> tuple[Project, dict]:
    """Authenticate a webhook delivery and return (project, event dict)."""
    token = (auth_header or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise WebhookVerificationError("missing Authorization header")

    issuer = _unverified_issuer(token)
    if not issuer:
        raise WebhookVerificationError("token has no issuer claim")

    candidates = await project_service.get_by_api_key(db, issuer)
    if not candidates:
        raise WebhookVerificationError(f"no project uses API key {issuer!r}")

    try:
        payload = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WebhookVerificationError(f"body is not valid UTF-8: {exc}") from exc

    last_error: Optional[Exception] = None
    for project in candidates:
        try:
            event = _receiver_for(project).receive(payload, token)
        except Exception as exc:
            last_error = exc
            continue
        return project, _to_dict(event)

    raise WebhookVerificationError(
        f"signature did not verify against any of {len(candidates)} candidate "
        f"project(s) for API key {issuer!r}: {last_error}"
    )
