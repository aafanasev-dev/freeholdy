"""
ws_session.py — shared WebSocket handshake helpers.

Every freeholdy WebSocket (interactive install, exec shell, build-log stream) opens
the same way: the client's first frame must be {"type":"auth","token":…} because
browsers can't set an Authorization header on a WebSocket. These helpers centralise
that handshake, the role check and the rejection shape so the routers don't each
re-implement them.

`authenticate` is default-deny: with no `project=` it admits admin tokens only, which is
the right answer for the exec and plugin-install sockets. Project-scoped sockets (the
deploy log stream) pass `project=` to also admit that project's guest token — the
WebSocket mirror of `auth.require_project_access`.
"""

import asyncio
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect

from app.config import settings
from app.models.database import SessionLocal
from app.models.orm import Token
from app.auth import hash_token, ROLE_ADMIN


def authorize(raw_token: str, project: Optional[str]) -> bool:
    """True if `raw_token` is an active token allowed on this socket.

    `project is None` → admin only; otherwise admin, or the guest bound to that project.
    DEBUG bypasses auth entirely, as it does for HTTP.
    """
    if settings.DEBUG:
        return True
    if not raw_token:
        return False
    db = SessionLocal()
    try:
        token = (
            db.query(Token)
            .filter(Token.token_hash == hash_token(raw_token), Token.active == True)
            .first()
        )
        if token is None:
            return False
        if token.role == ROLE_ADMIN:
            return True
        if project is None:
            return False
        return token.project is not None and token.project.name == project
    finally:
        db.close()


async def reject(websocket: WebSocket, code: int, message: str) -> None:
    """Send an error frame and close with a custom code (4401 auth, 4403 forbidden,
    4404 not found, 4409 busy)."""
    try:
        await websocket.send_json({"type": "error", "message": message})
        await websocket.close(code=code)
    except (WebSocketDisconnect, RuntimeError):
        pass


async def authenticate(
    websocket: WebSocket,
    *,
    project: Optional[str] = None,
    timeout: float = 10,
) -> bool:
    """Read and validate the mandatory first auth frame on an already-accepted socket.

    `project=None` (the default) admits admin tokens only; pass a project name to also
    admit the guest token bound to it.

    Returns True on success; on failure it has already sent an error frame + closed
    (4401 unknown/inactive token, 4403 valid token without permission), so the caller
    should just return. Returns False without closing only if the client disconnected
    before sending anything.
    """
    try:
        msg = await asyncio.wait_for(websocket.receive_json(), timeout=timeout)
    except WebSocketDisconnect:
        return False
    except (asyncio.TimeoutError, ValueError):
        await reject(websocket, 4401, "expected an auth frame within 10s")
        return False
    if msg.get("type") != "auth":
        await reject(websocket, 4401, "invalid or inactive token")
        return False
    raw = str(msg.get("token") or "")
    if authorize(raw, project):
        return True
    # Distinguish "not a token" from "a real token that may not do this", so a guest
    # client can tell a scope error from a revoked credential.
    if _is_known_token(raw):
        await reject(
            websocket, 4403,
            "this token is not permitted here"
            + (f" — it is not bound to project '{project}'" if project else " — admin only"),
        )
        return False
    await reject(websocket, 4401, "invalid or inactive token")
    return False


def _is_known_token(raw_token: str) -> bool:
    """True if the token exists and is active (regardless of role/scope)."""
    if not raw_token:
        return False
    db = SessionLocal()
    try:
        return (
            db.query(Token)
            .filter(Token.token_hash == hash_token(raw_token), Token.active == True)
            .first()
            is not None
        )
    finally:
        db.close()
