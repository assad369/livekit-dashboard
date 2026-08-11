"""Settings and configuration routes"""

import os
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.db import mongo
from app.services.livekit import LiveKitClient, get_livekit_client
from app.security.basic_auth import requires_admin, get_current_user
from app.security.csrf import get_csrf_token


router = APIRouter()


def _mask_secret(value: str, visible: int = 4) -> str:
    """Mask a secret, showing only the first/last `visible` characters."""
    if not value:
        return "(not set)"
    if len(value) <= visible * 2:
        return "*" * len(value)
    return f"{value[:visible]}{'*' * (len(value) - visible * 2)}{value[-visible:]}"


@router.get("/settings", response_class=HTMLResponse, dependencies=[Depends(requires_admin)])
async def settings_index(
    request: Request,
    lk: LiveKitClient = Depends(get_livekit_client),
):
    """Display settings and configuration"""
    current_user = get_current_user(request)

    # Get server info
    server_info = await lk.get_server_info()

    # Read the credentials off the active project's client, not the
    # environment — under multi-project the env vars belong to a different
    # deployment than the one being displayed.
    project = request.scope.get("state", {}).get("project")

    config = {
        "livekit_url": lk.url,
        "status": server_info.get("status", "unknown"),
        "debug": os.environ.get("DEBUG", "false").lower() == "true",
        "sip_enabled": lk.sip_enabled,
        # Masked before it ever reaches the template.
        "api_key_masked": _mask_secret(lk.key),
        "api_secret_masked": _mask_secret(lk.secret),
        "project_name": project.name if project else "Environment",
        "project_source": project.source if project else "env",
        "mongodb": mongo.health(),
    }

    return request.app.state.templates.TemplateResponse(request, 
        "settings.html.j2",
        {
            "request": request,
            "config": config,
            "current_user": current_user,
            "csrf_token": get_csrf_token(request),
        },
    )
