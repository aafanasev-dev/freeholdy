"""
tokens.py — mint, list and revoke API tokens.

The counterpart of `scripts/generate_token.py` for operators who work over HTTP rather
than by SSH-ing to the box. Its reason to exist is guest tokens: an admin provisions one
or more projects, then mints a token scoped to them and hands that to a third party (a
CI/CD runner), which can redeploy and roll back those projects and nothing else.

Everything here is admin-only except `GET /tokens/me`, which lets any caller — including
a guest — discover its own role and binding. The web UI and `fhcli whoami` use it to
decide what to show.

Plaintext tokens exist only in the response to POST /tokens: the DB keeps a SHA-256 hash
(`auth.hash_token`), so a lost token can only be revoked and replaced, never recovered.
"""

import secrets
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import hash_token, require_admin, require_token
from app.models.database import get_db
from app.models.orm import Project, Token
from app.models.schemas import (
    CreateTokenRequest,
    SetTokenProjectsRequest,
    TokenCreateResponse,
    TokenResponse,
    TokenRole,
)

router = APIRouter()


def _resolve_projects(db: Session, names: List[str]) -> List[Project]:
    """Map project names to rows, 404-ing on the first one that does not exist."""
    rows = []
    for name in names:
        project = db.query(Project).filter(Project.name == name).first()
        if project is None:
            raise HTTPException(
                status_code=404,
                detail=(f"Project '{name}' not found — deploy it first, then scope a guest "
                        "token to it"),
            )
        rows.append(project)
    return rows


def _token_response(token: Token) -> dict:
    """Shared shape for every token endpoint — never the hash, never the plaintext."""
    return {
        "id": token.id,
        "name": token.name,
        "role": token.role,
        "projects": sorted(p.name for p in token.projects),
        "active": bool(token.active),
        "created_at": token.created_at,
    }


@router.post("", response_model=TokenCreateResponse, status_code=201,
             summary="Mint an API token (admin only)")
@router.post("/", response_model=TokenCreateResponse, status_code=201, include_in_schema=False)
def create_token(
    request: CreateTokenRequest,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """Create a token and return its plaintext **once**.

    A `guest` token must name at least one existing project. Deleting a project later
    unbinds it from every token that covered it; the tokens themselves survive, since one
    may still cover others. Change the scope afterwards with PUT /tokens/{id}/projects."""
    projects = _resolve_projects(db, request.projects)

    plaintext = secrets.token_urlsafe(32)
    token = Token(
        name=request.name,
        token_hash=hash_token(plaintext),
        role=request.role.value,
    )
    token.projects = projects
    db.add(token)
    db.commit()
    db.refresh(token)

    return TokenCreateResponse(**_token_response(token), token=plaintext)


@router.get("", response_model=List[TokenResponse], summary="List tokens (admin only)")
@router.get("/", response_model=List[TokenResponse], include_in_schema=False)
def list_tokens(db: Session = Depends(get_db), _=Depends(require_admin)):
    """Every token, revoked ones included. Hashes are never returned."""
    tokens = db.query(Token).order_by(Token.id).all()
    return [TokenResponse(**_token_response(t)) for t in tokens]


@router.get("/me", response_model=TokenResponse,
            summary="The calling token's own role and scope")
def whoami(token: Optional[Token] = Depends(require_token)):
    """What this token is allowed to do — the only token endpoint a guest may call.

    In DEBUG mode auth is disabled and there is no row, so report a synthetic admin
    identity rather than 401-ing a caller the rest of the API is letting through."""
    if token is None:
        return TokenResponse(id=0, name="debug", role=TokenRole.admin, projects=[], active=True)
    return TokenResponse(**_token_response(token))


@router.put("/{token_id}/projects", response_model=TokenResponse,
            summary="Replace a guest token's project scope (admin only)")
def set_token_projects(
    token_id: int,
    request: SetTokenProjectsRequest,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """Replace the set of projects a guest token may act on, so a CI credential can be
    widened or narrowed without re-minting it (the third party keeps the same secret).

    An empty list is allowed and leaves the token able to authenticate but not to act."""
    token = db.query(Token).filter(Token.id == token_id).first()
    if token is None:
        raise HTTPException(status_code=404, detail=f"Token {token_id} not found")
    if token.role != TokenRole.guest.value:
        raise HTTPException(
            status_code=400,
            detail="Only guest tokens carry a project scope — an admin token already reaches every project",
        )
    token.projects = _resolve_projects(db, request.projects)
    db.commit()
    db.refresh(token)
    return TokenResponse(**_token_response(token))


@router.delete("/{token_id}", response_model=TokenResponse,
               summary="Revoke a token (admin only)")
def revoke_token(
    token_id: int,
    db: Session = Depends(get_db),
    caller: Optional[Token] = Depends(require_admin),
):
    """Deactivate a token. Rows are kept (soft revoke), matching
    `scripts/generate_token.py revoke`, so the audit trail survives."""
    token = db.query(Token).filter(Token.id == token_id).first()
    if token is None:
        raise HTTPException(status_code=404, detail=f"Token {token_id} not found")
    if caller is not None and caller.id == token.id:
        # Revoking your own credential mid-request would lock you out of the API.
        raise HTTPException(
            status_code=400,
            detail="Cannot revoke the token you are authenticating with",
        )
    token.active = False
    db.commit()
    db.refresh(token)
    return TokenResponse(**_token_response(token))
