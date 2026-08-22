"""
LiveKit Dashboard - Main Application
Stateless SSR dashboard for LiveKit server management
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Without this, module-level `logging.getLogger(__name__)` calls throughout
# the app (e.g. app.db.mongo's connection logging) have no handler attached
# and are silently dropped instead of reaching Docker/Coolify's log output.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.routes import overview, rooms, egress, ingress, sip, settings, sandbox, auth, agents, homer, search, views, alerts, audit, diagnostics, events, projects, webhooks, billing
from app.security.csrf import get_csrf_token
from app.middleware.project_context import ProjectContextMiddleware
from app.security.session_auth import AuthMiddleware
from app.utils.formatters import format_duration, format_pct, status_color, format_number
from app.db import mongo
from app.services import lk_pool, prom_collector, usage
from app.services.projects import URL_SCHEMES, normalize_livekit_url
from app.services.users import bootstrap_admin, using_default_password


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown events"""
    # Startup
    print("🚀 LiveKit Dashboard starting up...")
    raw_url = os.environ.get("LIVEKIT_URL", "")
    # Show what will actually be dialed, not the raw env text.
    print(f"   LiveKit URL: {normalize_livekit_url(raw_url) if raw_url else 'Not set'}")
    print(f"   SIP Enabled: {os.environ.get('ENABLE_SIP', 'false')}")
    print(f"   Homer Enabled: {os.environ.get('ENABLE_HOMER', 'false')}")

    # Verify required environment variables
    required_vars = ["LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"]
    missing_vars = [var for var in required_vars if not os.environ.get(var)]

    if missing_vars:
        print(f"⚠️  WARNING: Missing required environment variables: {', '.join(missing_vars)}")
    elif not normalize_livekit_url(raw_url).startswith(URL_SCHEMES):
        print(f"⚠️  WARNING: LIVEKIT_URL is malformed: {raw_url!r}")
        print("    Expected e.g. wss://your-project.livekit.cloud or http://localhost:7880")
    else:
        print("✅ All required environment variables are set")

    await mongo.connect()
    db_health = mongo.health()
    if not db_health["enabled"]:
        print("   MongoDB: not configured (running without persistence)")
    elif db_health["connected"]:
        print(f"   MongoDB: connected (db={db_health['db']})")
        if db_health["index_errors"]:
            print(f"⚠️  WARNING: {len(db_health['index_errors'])} index(es) failed to create")
        try:
            created = await bootstrap_admin(mongo.get_database())
            if created:
                print(f"   Created admin account '{created.username}' from environment")
        except Exception as exc:
            print(f"⚠️  WARNING: could not bootstrap the admin account: {exc}")
    else:
        print(f"⚠️  WARNING: MongoDB unreachable: {db_health['error']}")

    if using_default_password():
        print("⚠️  WARNING: ADMIN_PASSWORD is still the default 'changeme' — change it")

    # Background workers only make sense with somewhere to write.
    background: list[asyncio.Task] = []
    if mongo.get_database() is not None and not _in_test_mode():
        background.append(asyncio.create_task(prom_collector.collector_loop()))
        background.append(asyncio.create_task(_sweep_loop()))

    yield

    # Shutdown
    print("👋 LiveKit Dashboard shutting down...")
    for task in background:
        task.cancel()
    for task in background:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    await lk_pool.close_all()
    await mongo.disconnect()


def _in_test_mode() -> bool:
    return os.environ.get("PYTEST_CURRENT_TEST") is not None


async def _sweep_loop() -> None:
    """Periodically close usage sessions whose end event never arrived.

    Without this a crashed LiveKit node leaves sessions open forever and their
    minutes are never counted, so the month silently under-reports.
    """
    interval = int(os.environ.get("USAGE_SWEEP_INTERVAL", "900"))
    max_age = int(os.environ.get("USAGE_SESSION_MAX_HOURS", "24"))
    while True:
        await asyncio.sleep(interval)
        try:
            await usage.sweep_stale_sessions(mongo.get_database(), max_age_hours=max_age)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"⚠️  usage sweep failed: {exc}")


# Create FastAPI app
app = FastAPI(
    title="LiveKit Dashboard",
    description="Self-hosted SSR dashboard for LiveKit server management",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,  # Disable Swagger UI in production
    redoc_url=None,  # Disable ReDoc in production
)

# ---------------------------------------------------------------------------
# Middleware
#
# IMPORTANT: `add_middleware` inserts at the front of the stack, so the LAST
# one registered is the OUTERMOST. They are therefore registered
# innermost-first below, giving this execution order:
#
#   SecurityHeaders -> CORS -> Session -> Auth -> ProjectContext -> ReadOnly
#     -> CSRF -> router
#
# The CSRF middleware must sit INSIDE SessionMiddleware because it stores the
# per-browser CSRF secret in the session. Do not reorder these without
# updating tests/test_middleware_order.py, which pins the arrangement.
# ---------------------------------------------------------------------------

# Paths that stay writable in readonly mode: authentication, and inbound
# webhooks (which are machine-to-machine and authenticate themselves).
READONLY_EXEMPT_EXACT = {"/login", "/logout", "/projects/switch"}
READONLY_EXEMPT_PREFIXES = ("/webhooks/",)


def _readonly_exempt(path: str) -> bool:
    return path in READONLY_EXEMPT_EXACT or path.startswith(READONLY_EXEMPT_PREFIXES)


class CSRFTokenMiddleware(BaseHTTPMiddleware):
    """Ensure every request has a CSRF token available for templates."""

    async def dispatch(self, request: Request, call_next):
        get_csrf_token(request)
        return await call_next(request)


class ReadOnlyModeMiddleware(BaseHTTPMiddleware):
    """Block mutating requests when DASHBOARD_ROLE=readonly."""

    async def dispatch(self, request: Request, call_next):
        if os.environ.get("DASHBOARD_ROLE", "admin").lower() == "readonly":
            if request.method in ("POST", "PUT", "PATCH", "DELETE"):
                if not _readonly_exempt(request.url.path):
                    return Response(
                        "Read-only mode — mutations are disabled.", status_code=403
                    )
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses"""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # Only add HSTS in production with HTTPS
        if os.environ.get("DEBUG", "false").lower() != "true":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )

        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "font-src 'self' https://cdn.jsdelivr.net; "
            "img-src 'self' data: https:; "
            "connect-src 'self';"
        )

        # Disable caching for HTML pages
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"

        return response


# Registered innermost-first — see the note above.
app.add_middleware(CSRFTokenMiddleware)
app.add_middleware(ReadOnlyModeMiddleware)
app.add_middleware(ProjectContextMiddleware)
app.add_middleware(AuthMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("APP_SECRET_KEY", "dev-secret-key-change-in-production"),
    session_cookie="lkdash_session",
    same_site="lax",
    https_only=os.environ.get("SESSION_HTTPS_ONLY", "false").lower() == "true",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],  # No CORS by default for security
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
app.add_middleware(SecurityHeadersMiddleware)

# Setup Jinja2 templates
templates = Jinja2Templates(directory="app/templates")


# Add custom template globals
CSS_VERSION = "0.1.0"

templates.env.globals["css_version"] = CSS_VERSION
templates.env.globals["homer_enabled"] = lambda: os.environ.get("ENABLE_HOMER", "false").lower() == "true"
templates.env.globals["sip_enabled"] = lambda: os.environ.get("ENABLE_SIP", "false").lower() == "true"
templates.env.globals["is_readonly"] = lambda: os.environ.get("DASHBOARD_ROLE", "admin").lower() == "readonly"


def _datetimeformat(value: int) -> str:
    """Format a Unix timestamp (seconds) as a human-readable string."""
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value)


templates.env.filters["datetimeformat"] = _datetimeformat


def _proto_map_tojson(value) -> str:
    """Convert a protobuf ScalarMapContainer (or any dict-like) to a JSON string."""
    import json
    try:
        return json.dumps(dict(value))
    except Exception:
        return "{}"


templates.env.filters["proto_map_tojson"] = _proto_map_tojson

# Formatting helpers (from app.utils.formatters)
templates.env.filters["duration"] = format_duration
templates.env.filters["pct"] = format_pct
templates.env.filters["status_color"] = status_color
templates.env.filters["numformat"] = format_number

# Store templates in app state for route access
app.state.templates = templates

# Mount static files
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Include routers
app.include_router(overview.router, tags=["Overview"])
app.include_router(agents.router, tags=["Agents"])
app.include_router(rooms.router, tags=["Rooms"])
app.include_router(egress.router, tags=["Egress"])
app.include_router(ingress.router, tags=["Ingress"])
app.include_router(sip.router, tags=["SIP"])
app.include_router(settings.router, tags=["Settings"])
app.include_router(sandbox.router, tags=["Sandbox"])
app.include_router(auth.router, tags=["Auth"])
app.include_router(homer.router, tags=["Homer"])
app.include_router(search.router, tags=["Search"])
app.include_router(views.router, tags=["Views"])
app.include_router(alerts.router, tags=["Alerts"])
app.include_router(audit.router, tags=["Audit"])
app.include_router(diagnostics.router, tags=["Diagnostics"])
app.include_router(events.router, tags=["Events"])
app.include_router(projects.router, tags=["Projects"])
app.include_router(webhooks.router)
app.include_router(billing.router, tags=["Billing"])


# Error handlers
@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    """Custom 404 page"""
    return templates.TemplateResponse(request, 
        "base.html.j2",
        {
            "request": request,
            "error": "Page not found",
        },
        status_code=404,
    )


@app.exception_handler(500)
async def server_error_handler(request: Request, exc):
    """Custom 500 page"""
    return templates.TemplateResponse(request, 
        "base.html.j2",
        {
            "request": request,
            "error": "Internal server error",
        },
        status_code=500,
    )


# Health check endpoint (no auth required)
@app.get("/health", response_class=HTMLResponse)
async def health_check():
    """Health check endpoint"""
    return HTMLResponse(content="OK", status_code=200)


@app.get("/health/deep")
async def health_deep(request: Request):
    """Dependency health, including index failures that would break billing.

    Reachable unauthenticated so a probe can see *whether* the app is degraded,
    but the details are withheld: PyMongo's connection errors embed the full
    topology (internal hostnames, ports, replica-set names) and index errors
    enumerate collection names. Sign in to see them.
    """
    db = mongo.health()
    degraded = bool(db["index_errors"]) or (db["enabled"] and not db["connected"])
    status_code = 503 if degraded else 200

    if request.scope.get("state", {}).get("user"):
        body = {"status": "degraded" if degraded else "ok", "mongodb": db}
    else:
        body = {
            "status": "degraded" if degraded else "ok",
            "mongodb": {
                "enabled": db["enabled"],
                "connected": db["connected"],
                "index_errors": len(db["index_errors"]),
            },
        }

    return JSONResponse(body, status_code=status_code)


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    debug = os.environ.get("DEBUG", "false").lower() == "true"

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=debug,
        log_level="info" if debug else "warning",
    )
