"""
versions.py — blue/green version management for dockerfile-mode projects.

Each dockerfile deploy creates a ProjectVersion (see deploy_service): one `active`
(running, nginx points at it), at most one `inactive` (previous version, stopped but kept
for fast rollback), and zero-or-more `archived` (image kept, container gone) up to
Project.backup_limit. These endpoints list versions, change the backup limit, and roll back
to an earlier version.
"""

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


def _get_dockerfile_project(project_name: str, db: Session) -> Project:
    project = db.query(Project).filter(Project.name == project_name).first()
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")
    if project.deploy_mode != "dockerfile":
        raise HTTPException(
            status_code=400,
            detail=f"Project '{project_name}' is not a dockerfile project — versions apply to "
                   "single-container projects only",
        )
    return project


def _versions_response(project: Project) -> VersionsResponse:
    counts = {"active": 0, "inactive": 0, "archived": 0}
    infos: list[VersionInfo] = []
    for v in sorted(project.versions, key=lambda x: x.version, reverse=True):
        counts[v.status] = counts.get(v.status, 0) + 1
        infos.append(VersionInfo(
            version=v.version,
            status=v.status,
            image_name=v.image_name,
            container_name=v.container_name,
            local_port=v.local_port,
            container_status=docker_service.get_container_status(v.container_name),
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
    project = _get_dockerfile_project(project_name, db)
    return _versions_response(project)


@router.put("/{project_name}/backup-limit", response_model=VersionsResponse,
            summary="Set how many archived versions to keep (oldest pruned immediately)")
def set_backup_limit(project_name: str, request: SetBackupLimitRequest,
                     db: Session = Depends(get_db), _=Depends(require_auth)):
    project = _get_dockerfile_project(project_name, db)
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
    """Make an earlier version active again: its container is brought up (docker start for an
    inactive version, docker run from the retained image for an archived one), nginx switches
    to it, and statuses re-shuffle. The response carries a `ws_path` — connect to
    `WS /projects/{name}/deploy` to stream the rollback log live."""
    project = _get_dockerfile_project(project_name, db)
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
