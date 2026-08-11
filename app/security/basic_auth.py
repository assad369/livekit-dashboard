"""Route-level authentication helpers.

Historically this implemented HTTP Basic against ADMIN_USERNAME/ADMIN_PASSWORD
with a plaintext comparison. Authentication now happens in
`app.security.session_auth.AuthMiddleware` against a signed session cookie,
and these two functions simply read what it recorded.

The module keeps its old name and exports so the ~60 existing
`dependencies=[Depends(requires_admin)]` declarations and `get_current_user`
calls across the route modules continue to work unchanged. They are now a
cheap second check behind the middleware rather than the only gate.
"""

from typing import Optional

from fastapi import HTTPException, Request, status


def requires_admin(request: Request) -> str:
    """Dependency that requires an authenticated session.

    The middleware normally rejects the request before a route is reached;
    this catches the case where a route is somehow served without it.
    """
    user = request.scope.get("state", {}).get("user")
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return user.get("username", "")


def get_current_user(request: Request) -> Optional[str]:
    """Return the authenticated username, or None."""
    user = request.scope.get("state", {}).get("user")
    return user.get("username") if user else None
