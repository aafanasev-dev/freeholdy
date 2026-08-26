"""
auth.py — bearer-token authentication and role-based authorization.

Two separate steps, deliberately kept apart:

  * `require_token` **authenticates**: it resolves the bearer token to an active `Token`
    row (or `None` when settings.DEBUG disables auth). It grants nothing on its own.
  * `require_admin` / `require_project_access` **authorize**: they are the dependencies a
    route actually declares.

Every route must declare one of the two authorization dependencies — a route that only
declares `require_token` is open to guest tokens. `require_token` is for the handful of
routes that need the caller's identity (GET /projects/, POST /git/add, /tokens/me) and
do their own check via `authorize_project`.

Roles:
  * `admin` — everything (the default; every pre-roles token is one).
  * `guest` — bound to a single project (`Token.project_id`): redeploy, restart, env,
    logs/status, versions and rollback for that project only.

WebSocket routes can't use FastAPI dependencies for this (the token arrives in the first
frame, not a header) — the mirror-image checks live in `app/services/ws_session.py`.
"""

import hashlib
from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
from app.config import settings
from app.models.database import get_db
from app.models.orm import Token

security = HTTPBearer(auto_error=False)

ROLE_ADMIN = "admin"
ROLE_GUEST = "guest"
ROLES = (ROLE_ADMIN, ROLE_GUEST)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def is_admin(token: Optional[Token]) -> bool:
    """True for an admin token and for DEBUG mode (where `token` is None)."""
    return token is None or token.role == ROLE_ADMIN


def guest_project_name(token: Optional[Token]) -> Optional[str]:
    """The project a guest token is bound to, or None for an admin/DEBUG caller.

    A guest whose project row is gone (the project was deleted) resolves to None here, and
    `authorize_project` denies it — its token rows are cascade-deleted with the project, so
    this is only reachable inside a request that raced the deletion.
    """
    if is_admin(token):
        return None
    return token.project.name if token.project is not None else None


async def require_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> Optional[Token]:
    """Authenticate only — resolve the bearer token to an active Token row.

    Returns None in DEBUG mode (auth disabled), which every caller must treat as admin.
    This grants no permission by itself: routes declare `require_admin` or
    `require_project_access` instead.
    """
    if settings.DEBUG:
        return None

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token_hash = hash_token(credentials.credentials)
    token = (
        db.query(Token)
        .filter(Token.token_hash == token_hash, Token.active == True)
        .first()
    )
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or inactive token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


def authorize_project(token: Optional[Token], project_name: str) -> Optional[Token]:
    """Raise 403 unless `token` may act on `project_name`. Returns the token for chaining.

    The plain-function form of `require_project_access`, for routes where the project name
    arrives in the request body rather than the path (POST /git/add).
    """
    if is_admin(token):
        return token

    bound = guest_project_name(token)
    if bound is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This guest token is not bound to an existing project",
        )
    if bound != project_name:
        # Name only the project the caller already knows about — never confirm or deny
        # that the project it asked for exists.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"This guest token is limited to project '{bound}'",
        )
    return token


async def require_admin(token: Optional[Token] = Depends(require_token)) -> Optional[Token]:
    """Admin-only route. DEBUG (token is None) counts as admin."""
    if not is_admin(token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires an admin token",
        )
    return token


async def require_project_access(
    project_name: str,
    token: Optional[Token] = Depends(require_token),
) -> Optional[Token]:
    """Admin, or the guest token bound to exactly this project.

    `project_name` is picked up from the route's path parameter, so this works uniformly
    across every project-scoped endpoint.
    """
    return authorize_project(token, project_name)
