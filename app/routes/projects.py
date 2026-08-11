"""Project management: CRUD, connectivity checks, and the active-project switcher."""

import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import mongo
from app.middleware.project_context import invalidate_list_cache
from app.security.basic_auth import get_current_user, requires_admin
from app.security.csrf import get_csrf_token, verify_csrf_token
from app.services import audit_log, lk_pool
from app.services import projects as project_service
from app.services.projects import ProjectError
from app.utils.flash import flash, get_flash

logger = logging.getLogger(__name__)

router = APIRouter()


def _webhook_url(request: Request) -> str:
    """The URL to paste into the LiveKit server's `webhook.urls` config."""
    return str(request.base_url).rstrip("/") + "/webhooks/livekit"


async def _render_index(request: Request, *, status_code: int = 200) -> HTMLResponse:
    db = mongo.get_database()
    projects = await project_service.list_projects(db)
    env = project_service.env_project()
    flash_message, flash_type = get_flash(request)

    return request.app.state.templates.TemplateResponse(
        request,
        "projects/index.html.j2",
        {
            "request": request,
            "projects": projects,
            "env_project": env,
            "can_import_env": bool(env) and not any(p.source == "mongo" for p in projects),
            "storage_available": db is not None,
            "db_health": mongo.health(),
            "webhook_url": _webhook_url(request),
            "current_user": get_current_user(request),
            "csrf_token": get_csrf_token(request),
            "flash_message": flash_message,
            "flash_type": flash_type,
        },
        status_code=status_code,
    )


@router.get("/projects", response_class=HTMLResponse, dependencies=[Depends(requires_admin)])
async def list_projects_page(request: Request):
    return await _render_index(request)


@router.post("/projects", dependencies=[Depends(requires_admin)])
async def create_project(
    request: Request,
    csrf_token: str = Form(""),
    name: str = Form(...),
    livekit_url: str = Form(...),
    api_key: str = Form(...),
    api_secret: str = Form(...),
    prometheus_url: str = Form(""),
    sip_enabled: str = Form(""),
):
    await verify_csrf_token(request)

    try:
        project = await project_service.create_project(
            mongo.get_database(),
            name=name,
            livekit_url=livekit_url,
            api_key=api_key,
            api_secret=api_secret,
            sip_enabled=sip_enabled == "on",
            prometheus_url=prometheus_url,
            created_by=get_current_user(request) or "",
        )
    except ProjectError as exc:
        flash(request, str(exc), "danger")
        return RedirectResponse("/projects", status_code=303)

    invalidate_list_cache()
    await audit_log.log_action(
        "project.create", project.slug, get_current_user(request), {"url": project.livekit_url}
    )
    flash(request, f"Project '{project.name}' created.", "success")
    return RedirectResponse("/projects", status_code=303)


# NOTE: these two must be declared before the /projects/{project_id} routes.
# FastAPI matches in declaration order, so a later static path would be
# swallowed by the earlier parameterised one.
@router.post("/projects/switch", dependencies=[Depends(requires_admin)])
async def switch_project(
    request: Request,
    csrf_token: str = Form(""),
    project_id: str = Form(...),
    next: str = Form("/"),
):
    """Change which project the dashboard is scoped to."""
    await verify_csrf_token(request)

    project = await project_service.get_project(mongo.get_database(), project_id)
    if project is None:
        flash(request, "Project not found.", "danger")
        return RedirectResponse("/", status_code=303)

    request.session["project_id"] = project_id

    from app.security.session_auth import safe_next
    return RedirectResponse(safe_next(next), status_code=303)


@router.post("/projects/import-env", dependencies=[Depends(requires_admin)])
async def import_env(request: Request, csrf_token: str = Form("")):
    await verify_csrf_token(request)
    try:
        project = await project_service.import_from_environment(
            mongo.get_database(), created_by=get_current_user(request) or ""
        )
    except ProjectError as exc:
        flash(request, str(exc), "danger")
        return RedirectResponse("/projects", status_code=303)

    invalidate_list_cache()
    flash(request, f"Imported '{project.name}' from the environment.", "success")
    return RedirectResponse("/projects", status_code=303)


@router.get(
    "/projects/{project_id}/edit",
    response_class=HTMLResponse,
    dependencies=[Depends(requires_admin)],
)
async def edit_project_page(request: Request, project_id: str):
    project = await project_service.get_project(mongo.get_database(), project_id)
    if project is None:
        flash(request, "Project not found.", "danger")
        return RedirectResponse("/projects", status_code=303)

    return request.app.state.templates.TemplateResponse(
        request,
        "projects/edit.html.j2",
        {
            "request": request,
            "project": project,
            "webhook_url": _webhook_url(request),
            "current_user": get_current_user(request),
            "csrf_token": get_csrf_token(request),
        },
    )


@router.post("/projects/{project_id}", dependencies=[Depends(requires_admin)])
async def update_project(
    request: Request,
    project_id: str,
    csrf_token: str = Form(""),
    name: str = Form(...),
    livekit_url: str = Form(...),
    api_key: str = Form(...),
    api_secret: str = Form(""),
    prometheus_url: str = Form(""),
    sip_enabled: str = Form(""),
):
    await verify_csrf_token(request)

    try:
        project = await project_service.update_project(
            mongo.get_database(),
            project_id,
            name=name,
            livekit_url=livekit_url,
            api_key=api_key,
            # Blank means "leave the stored secret alone" — the form never
            # renders the real value, so requiring it would force a re-entry
            # on every unrelated edit.
            api_secret=api_secret,
            sip_enabled=sip_enabled == "on",
            prometheus_url=prometheus_url,
        )
    except ProjectError as exc:
        flash(request, str(exc), "danger")
        return RedirectResponse(f"/projects/{project_id}/edit", status_code=303)

    # Credentials may have changed; drop any pooled client built with the old ones.
    await lk_pool.invalidate(project_id)
    invalidate_list_cache()
    await audit_log.log_action("project.update", project.slug, get_current_user(request), {})
    flash(request, f"Project '{project.name}' updated.", "success")
    return RedirectResponse("/projects", status_code=303)


@router.post("/projects/{project_id}/delete", dependencies=[Depends(requires_admin)])
async def delete_project(
    request: Request,
    project_id: str,
    csrf_token: str = Form(""),
    confirm_name: str = Form(""),
    purge_data: str = Form(""),
):
    await verify_csrf_token(request)

    db = mongo.get_database()
    project = await project_service.get_project(db, project_id)
    if project is None:
        flash(request, "Project not found.", "danger")
        return RedirectResponse("/projects", status_code=303)

    # Typing the name is the guard against deleting the wrong project — the
    # usage history goes with it.
    if confirm_name.strip() != project.name:
        flash(request, "Type the project name exactly to confirm deletion.", "warning")
        return RedirectResponse("/projects", status_code=303)

    try:
        await project_service.delete_project(db, project_id, purge_data=purge_data == "on")
    except ProjectError as exc:
        flash(request, str(exc), "danger")
        return RedirectResponse("/projects", status_code=303)

    await lk_pool.invalidate(project_id)
    invalidate_list_cache()
    if request.session.get("project_id") == project_id:
        request.session.pop("project_id", None)

    await audit_log.log_action(
        "project.delete", project.slug, get_current_user(request),
        {"purged": purge_data == "on"},
    )
    flash(request, f"Project '{project.name}' deleted.", "success")
    return RedirectResponse("/projects", status_code=303)


@router.post("/projects/{project_id}/default", dependencies=[Depends(requires_admin)])
async def make_default(request: Request, project_id: str, csrf_token: str = Form("")):
    await verify_csrf_token(request)
    try:
        await project_service.set_default(mongo.get_database(), project_id)
    except ProjectError as exc:
        flash(request, str(exc), "danger")
        return RedirectResponse("/projects", status_code=303)

    invalidate_list_cache()
    flash(request, "Default project updated.", "success")
    return RedirectResponse("/projects", status_code=303)


@router.post(
    "/projects/{project_id}/test",
    response_class=HTMLResponse,
    dependencies=[Depends(requires_admin)],
)
async def test_project(request: Request, project_id: str):
    """HTMX partial: probe the project's LiveKit server and render the result."""
    # This makes the server open an outbound connection, so it is CSRF-checked
    # like every other POST. main.js adds the token to HTMX requests.
    await verify_csrf_token(request)

    project = await project_service.get_project(mongo.get_database(), project_id)
    if project is None:
        result = {"ok": False, "error": "Project not found.", "latency_ms": 0, "rooms": 0}
    else:
        result = await project_service.test_connection(project)

    return request.app.state.templates.TemplateResponse(
        request,
        "projects/_test_result.html.j2",
        {"request": request, "result": result},
    )
