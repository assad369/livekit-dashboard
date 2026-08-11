"""Pin the middleware stack arrangement.

Starlette runs middleware in the inverse of registration order, so the
arrangement in app/main.py is load-bearing and silently breakable: appending
an `add_middleware` call after SessionMiddleware would move that middleware
*outside* the session, and anything reading `request.session` there would
start failing at runtime rather than at import time.
"""

from app.main import app


def _execution_order() -> list[str]:
    """Middleware class names in execution order, outermost first.

    `add_middleware` inserts at position 0, so `user_middleware` already reads
    outermost-first — the inverse of the order the calls appear in main.py.
    """
    return [m.cls.__name__ for m in app.user_middleware]


def test_execution_order_is_pinned():
    assert _execution_order() == [
        "SecurityHeadersMiddleware",
        "CORSMiddleware",
        "SessionMiddleware",
        "AuthMiddleware",
        "ProjectContextMiddleware",
        "ReadOnlyModeMiddleware",
        "CSRFTokenMiddleware",
    ]


def test_auth_middleware_runs_inside_session_middleware():
    """Auth reads the session cookie, so it must sit inside SessionMiddleware."""
    names = _execution_order()
    assert names.index("SessionMiddleware") < names.index("AuthMiddleware")


def test_project_context_runs_after_auth():
    """Project resolution is skipped for anonymous requests, so auth runs first."""
    names = _execution_order()
    assert names.index("AuthMiddleware") < names.index("ProjectContextMiddleware")


def test_csrf_middleware_runs_inside_session_middleware():
    """CSRF stores its per-browser secret in the session, so it must be inner."""
    names = _execution_order()
    assert names.index("SessionMiddleware") < names.index("CSRFTokenMiddleware")


def test_security_headers_are_outermost():
    """Outermost means every response — including errors — gets the headers."""
    assert _execution_order()[0] == "SecurityHeadersMiddleware"


def test_security_headers_applied_to_error_responses(client):
    """A 404 goes through the full stack, so it carries the headers too."""
    resp = client.get("/__definitely_not_a_route__")
    assert resp.status_code == 404
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
