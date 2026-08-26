"""
env.py — environment variables for a project and, in compose mode, for each of its services.

A project has one `.env`-format file at project level and, for compose projects, one per
service; the DB holds them (see `ProjectEnvFile`) and `env_service` renders them onto disk
for docker. Project-level values apply to every container; a compose service's own file wins
over the shared one on a key collision.

These endpoints are **save-only**. Writing a file never touches a running container — the
values are picked up the next time it starts, either on the next deploy or via
`POST /projects/{name}/restart` (which recreates containers from the images they already
run, so nothing is rebuilt). `EnvResponse.applied` says whether the running container is
using what is stored.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.models.database import get_db
from app.models.orm import ComposeService, Project
from app.models.schemas import EnvResponse, SetEnvRequest
from app.auth import require_project_access
from app.services import env_service

router = APIRouter()

_APPLY_HINT = "restart or redeploy the project to apply"


def _get_project(project_name: str, db: Session) -> Project:
    project = db.query(Project).filter(Project.name == project_name).first()
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")
    return project


def _get_service(project: Project, service_name: str, db: Session) -> ComposeService:
    if project.deploy_mode != "compose":
        raise HTTPException(
            status_code=400,
            detail=f"Project '{project.name}' is not a compose project (deploy_mode="
                   f"{project.deploy_mode}) — use /projects/{project.name}/env instead",
        )
    svc = (
        db.query(ComposeService)
        .filter(ComposeService.project_id == project.id, ComposeService.name == service_name)
        .first()
    )
    if not svc:
        raise HTTPException(
            status_code=404,
            detail=f"Service '{service_name}' not found in project '{project.name}'",
        )
    return svc


def _response(db: Session, project: Project, service: str | None, message: str = "") -> EnvResponse:
    row = env_service.get_row(db, project, service)
    content = row.content if row else ""
    applied = env_service.is_applied(db, project, service)
    if not message:
        message = "" if applied else f"Saved but not live yet — {_APPLY_HINT}"
    return EnvResponse(
        project=project.name,
        service=service,
        content=content,
        keys=env_service.keys(content),
        updated_at=row.updated_at if row else None,
        applied=applied,
        status="ok",
        message=message,
    )


# ── Project-level ───────────────────────────────────────────────────────────────

@router.get("/{project_name}/env", response_model=EnvResponse,
            summary="Read the project's .env file (dockerfile: the container's environment; "
                    "compose: the file shared by every service)")
def get_env(project_name: str, db: Session = Depends(get_db), _=Depends(require_project_access)):
    project = _get_project(project_name, db)
    return _response(db, project, None)


@router.put("/{project_name}/env", response_model=EnvResponse,
            summary="Replace the project's .env file — saved only, applied on next container start")
def set_env(project_name: str, request: SetEnvRequest,
            db: Session = Depends(get_db), _=Depends(require_project_access)):
    project = _get_project(project_name, db)
    env_service.set_content(db, project, request.content)
    db.commit()
    db.refresh(project)
    n = len(env_service.keys(request.content))
    return _response(db, project, None, f"Stored {n} variable(s) — {_APPLY_HINT}")


@router.delete("/{project_name}/env", response_model=EnvResponse,
               summary="Clear the project's .env file — applied on next container start")
def clear_env(project_name: str, db: Session = Depends(get_db), _=Depends(require_project_access)):
    project = _get_project(project_name, db)
    removed = env_service.delete_row(db, project)
    db.commit()
    db.refresh(project)
    return _response(db, project, None,
                     f"Environment cleared — {_APPLY_HINT}" if removed else "No environment was set")


# ── Per compose service ─────────────────────────────────────────────────────────

@router.get("/{project_name}/services/{service_name}/env", response_model=EnvResponse,
            summary="Read one compose service's own .env file")
def get_service_env(project_name: str, service_name: str,
                    db: Session = Depends(get_db), _=Depends(require_project_access)):
    project = _get_project(project_name, db)
    svc = _get_service(project, service_name, db)
    return _response(db, project, svc.name)


@router.put("/{project_name}/services/{service_name}/env", response_model=EnvResponse,
            summary="Replace one compose service's .env file (wins over the project-level one)")
def set_service_env(project_name: str, service_name: str, request: SetEnvRequest,
                    db: Session = Depends(get_db), _=Depends(require_project_access)):
    project = _get_project(project_name, db)
    svc = _get_service(project, service_name, db)
    env_service.set_content(db, project, request.content, svc.name)
    db.commit()
    db.refresh(project)
    n = len(env_service.keys(request.content))
    return _response(db, project, svc.name, f"Stored {n} variable(s) — {_APPLY_HINT}")


@router.delete("/{project_name}/services/{service_name}/env", response_model=EnvResponse,
               summary="Clear one compose service's .env file")
def clear_service_env(project_name: str, service_name: str,
                      db: Session = Depends(get_db), _=Depends(require_project_access)):
    project = _get_project(project_name, db)
    svc = _get_service(project, service_name, db)
    removed = env_service.delete_row(db, project, svc.name)
    db.commit()
    db.refresh(project)
    return _response(db, project, svc.name,
                     f"Environment cleared — {_APPLY_HINT}" if removed else "No environment was set")
