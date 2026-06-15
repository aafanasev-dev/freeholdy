"""
ws_session.py — shared WebSocket handshake helpers.

Every freeholdy WebSocket (interactive install, exec shell, build-log stream) opens
the same way: the client's first frame must be {"type":"auth","token":…} because
browsers can't set an Authorization header on a WebSocket. These helpers centralise
that handshake and the rejection shape so the routers don't each re-implement it.
"""

import asyncio

from fastapi import WebSocket, WebSocketDisconnect

from app.config import settings
from app.models.database import SessionLocal
from app.models.orm import Token
from app.auth import hash_token


def token_valid(token: str) -> bool:
    """True if the bearer token maps to an active Token row (or DEBUG bypasses auth)."""
    if settings.DEBUG:
        return True
    if not token:
        return False
    db = SessionLocal()
    try:
        return (
            db.query(Token)
            .filter(Token.token_hash == hash_token(token), Token.active == True)
            .first()
            is not None
        )
    finally:
        db.close()


async def reject(websocket: WebSocket, code: int, message: str) -> None:
    """Send an error frame and close with a custom code (4401 auth, 4404 not found, 4409 busy)."""
    try:
        await websocket.send_json({"type": "error", "message": message})
        await websocket.close(code=code)
    except (WebSocketDisconnect, RuntimeError):
        pass


async def authenticate(websocket: WebSocket, timeout: float = 10) -> bool:
    """Read and validate the mandatory first auth frame on an already-accepted socket.

    Returns True on success; on failure it has already sent an error frame + closed
    (4401), so the caller should just return. Returns False without closing only if the
    client disconnected before sending anything.
    """
    try:
        msg = await asyncio.wait_for(websocket.receive_json(), timeout=timeout)
    except WebSocketDisconnect:
        return False
    except (asyncio.TimeoutError, ValueError):
        await reject(websocket, 4401, "expected an auth frame within 10s")
        return False
    if msg.get("type") != "auth" or not token_valid(str(msg.get("token") or "")):
        await reject(websocket, 4401, "invalid or inactive token")
        return False
    return True
