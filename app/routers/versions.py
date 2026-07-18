"""
versions.py — blue/green version management (dockerfile and compose projects).

Each deploy creates a ProjectVersion (see deploy_service). Dockerfile: one `active`
(running, nginx points at it), at most one `inactive` (previous version, stopped but kept
for fast rollback), and zero-or-more `archived` (image kept, container gone) up to
Project.backup_limit. Compose: `active` | `archived` only (a compose switchover `down`s
the previous stack, so there is no stopped-container tier); each version retains its
per-service image tags + a file snapshot. These endpoints list versions, change the
backup limit, and roll back to an earlier version.
"""

import os

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.models.database import get_db
from app.models.orm import Project
from app.models.schemas import (
    VersionInfo,
    VersionsResponse,
    SetBackupLimitRequest,
    RollbackRequest,
    RollbackResponse,
    DockerJobStatusResponse,
)
from app.auth import require_auth
from app.services import docker_service, deploy_service

router = APIRouter()


def _get_versioned_project(project_name: str, db: Session) -> Project:
    project = db.query(Project).filter(Project.name == project_name).first()
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")
    if project.deploy_mode not in ("dockerfile", "compose"):
        raise HTTPException(
            status_code=400,
            detail=f"Project '{project_name}' has no deploy yet (deploy_mode="
                   f"{project.deploy_mode}) — versions exist after the first deploy",
        )
    return project


def _compose_stack_status(project: Project) -> str:
    """Aggregate container state of a compose project's active version (the running
    stack): running iff every exposed service's container runs; not_found when nothing
    exists; error if any inspect errored; else exited."""
    by_service = {s.name: (s.exposed, docker_service.get_container_status(s.container_name))
                  for s in project.services}
    if not by_service:
        return "not_found"
    statuses = [st for _, st in by_service.values()]
    exposed = [st for is_exposed, st in by_service.values() if is_exposed]
    if exposed and all(st == "running" for st in exposed):
        return "running"
    if all(st == "not_found" for st in statuses):
        return "not_found"
    if any(st == "error" for st in statuses):
        return "error"
    return "exited"


def _versions_response(project: Project) -> VersionsResponse:
    counts = {"active": 0, "inactive": 0, "archived": 0}
    infos: list[VersionInfo] = []
    compose = project.deploy_mode == "compose"
    for v in sorted(project.versions, key=lambda x: x.version, reverse=True):
        counts[v.status] = counts.get(v.status, 0) + 1
        if compose:
            # Only the active version has containers — a compose switchover `down`s the rest.
            container_status = _compose_stack_status(project) if v.status == "active" else "not_found"
        else:
            container_status = docker_service.get_container_status(v.container_name)
        infos.append(VersionInfo(
            version=v.version,
            status=v.status,
            image_name=v.image_name,
            container_name=v.container_name,
            local_port=v.local_port,
            container_status=container_status,
            created_at=v.created_at,
        ))
    return VersionsResponse(
        project=project.name,
        backup_limit=project.backup_limit,
        version_counter=project.version_counter,
        counts=counts,
        versions=infos,
    )


@router.get("/{project_name}/versions", response_model=VersionsResponse,
            summary="List the project's blue/green versions (active / inactive / archived)")
def list_versions(project_name: str, db: Session = Depends(get_db), _=Depends(require_auth)):
    project = _get_versioned_project(project_name, db)
    return _versions_response(project)


@router.put("/{project_name}/backup-limit", response_model=VersionsResponse,
            summary="Set how many archived versions to keep (oldest pruned immediately)")
def set_backup_limit(project_name: str, request: SetBackupLimitRequest,
                     db: Session = Depends(get_db), _=Depends(require_auth)):
    project = _get_versioned_project(project_name, db)
    project.backup_limit = request.limit
    db.commit()
    # Prune archived versions that now exceed the (possibly lowered) limit.
    deploy_service.enforce_backup_limit(project.id)
    db.refresh(project)
    return _versions_response(project)


@router.post("/{project_name}/rollback", response_model=RollbackResponse,
             summary="Roll back to an inactive or archived version — stream WS /deploy")
def rollback(project_name: str, request: RollbackRequest,
             db: Session = Depends(get_db), _=Depends(require_auth)):
    """Make an earlier version active again. Dockerfile: its container is brought up (docker
    start for an inactive version, docker run from the retained image for an archived one)
    and nginx switches to it. Compose: the current stack is downed, the version's file
    snapshot restored + re-provisioned, and its retained images brought back up. Statuses
    re-shuffle either way. The response carries a `ws_path` — connect to
    `WS /projects/{name}/deploy` to stream the rollback log live."""
    project = _get_versioned_project(project_name, db)
    target = next((v for v in project.versions if v.version == request.version), None)
    if target is None:
        raise HTTPException(
            status_code=404,
            detail=f"Version {request.version} not found for project '{project_name}'",
        )
    if target.status == "active":
        raise HTTPException(
            status_code=400,
            detail=f"Version {request.version} is already the active version",
        )

    if project.deploy_mode == "compose":
        snap = deploy_service.compose_snapshot_dir(project_name, request.version)
        if not os.path.isdir(snap):
            raise HTTPException(
                status_code=409,
                detail=f"Version {request.version} of '{project_name}' has no file snapshot "
                       f"on disk ({snap}) — it cannot be restored",
            )
        job_key = deploy_service.compose_rollback_to_version(project.id, request.version)
    else:
        job_key = deploy_service.rollback_to_version(project.id, request.version)
    ws_path = f"/projects/{project_name}/deploy"
    job = docker_service.get_job(job_key)
    return RollbackResponse(
        status="ok",
        message=f"Rolling back '{project_name}' to v{request.version} — stream {ws_path}",
        job=DockerJobStatusResponse(
            status=job.status if job else "no_job",
            operation=job.operation if job else None,
            message=f"Rollback to v{request.version} launched",
            logs=docker_service.get_job_logs(job_key),
            exit_code=job.exit_code if job else None,
        ),
        ws_path=ws_path,
    )
