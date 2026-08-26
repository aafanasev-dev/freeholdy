"""
logs.py — read the last N lines a project's container(s) printed.

`GET /projects/{name}/status` returns the log of the last freeholdy *job* (build/run/stop);
these endpoints return the **container's own** stdout/stderr, which is what you need when a
project builds fine but misbehaves at runtime.

Both deploy modes are served, mirroring how `env.py` handles both:

  - dockerfile  → `docker logs --tail N` on the active version's container
  - compose     → `docker compose logs --tail N` for the whole stack (interleaved), or
                  `docker logs` on one service's container via the /services route

These are read-only synchronous snapshots — **not** a live follow, and they never spawn a
DockerJob, so tailing a log can't collide with a deploy tracked under the same job key.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.models.database import get_db
from app.models.orm import ComposeService, Project
from app.models.schemas import LogsResponse
from app.auth import require_project_access
from app.services import compose_service, docker_service, env_service

router = APIRouter()

DEFAULT_TAIL = 200


def _get_project(project_name: str, db: Session) -> Project:
    project = db.query(Project).filter(Project.name == project_name).first()
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")
    if project.deploy_mode not in ("dockerfile", "compose"):
        raise HTTPException(
            status_code=400,
            detail=f"Project '{project_name}' is not deployed yet (deploy_mode="
                   f"{project.deploy_mode}) — there is no container to read logs from",
        )
    return project


def _get_service(project: Project, service_name: str, db: Session) -> ComposeService:
    if project.deploy_mode != "compose":
        raise HTTPException(
            status_code=400,
            detail=f"Project '{project.name}' is not a compose project (deploy_mode="
                   f"{project.deploy_mode}) — use /projects/{project.name}/logs instead",
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


def _response(project: str, service: str | None, container: str | None,
              tail: int, success: bool, output: str) -> LogsResponse:
    """Turn a (success, output) service-layer result into the HTTP shape, mapping the
    'not_found' sentinel onto a 404."""
    if not success:
        if output == "not_found":
            raise HTTPException(
                status_code=404,
                detail=f"Container '{container}' not found — the project may not be running",
            )
        raise HTTPException(status_code=500, detail=f"docker logs failed: {output}")
    return LogsResponse(
        project=project,
        service=service,
        container=container,
        tail=tail,
        lines=len(output.splitlines()),
        content=output,
        status="ok",
        message="",
    )


@router.get("/{project_name}/logs", response_model=LogsResponse,
            summary="Last N lines the project's container printed (compose: the whole stack)")
def get_logs(project_name: str,
             tail: int = Query(DEFAULT_TAIL, ge=1, le=10000,
                               description="How many trailing lines to return."),
             db: Session = Depends(get_db), _=Depends(require_project_access)):
    project = _get_project(project_name, db)

    if project.deploy_mode == "compose":
        success, output = docker_service.compose_logs(
            project.name,
            compose_service.project_dir(project.name),
            tail,
            env_service.project_env_file(db, project),
        )
        return _response(project.name, None, None, tail, success, output)

    success, output = docker_service.get_container_logs(project.container_name, tail)
    return _response(project.name, None, project.container_name, tail, success, output)


@router.get("/{project_name}/services/{service_name}/logs", response_model=LogsResponse,
            summary="Last N lines one compose service's container printed")
def get_service_logs(project_name: str, service_name: str,
                     tail: int = Query(DEFAULT_TAIL, ge=1, le=10000,
                                       description="How many trailing lines to return."),
                     db: Session = Depends(get_db), _=Depends(require_project_access)):
    project = _get_project(project_name, db)
    svc = _get_service(project, service_name, db)
    success, output = docker_service.get_container_logs(svc.container_name, tail)
    return _response(project.name, svc.name, svc.container_name, tail, success, output)
