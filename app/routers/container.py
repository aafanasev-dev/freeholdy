"""
container.py — project-level lifecycle for dockerfile-mode projects.

A dockerfile project is a single container, so these endpoints operate on the
Project row itself (no parts). Replaces the old per-part router.
"""

import os

from fastapi import APIRouter, Depends, HTTPException, WebSocket
from sqlalchemy.orm import Session

from app.models.database import get_db, SessionLocal
from app.models.orm import Project
from app.models.schemas import (
    DockerJobStatusResponse,
    SslResponse,
    SetDomainRequest,
    ProjectResponse,
)
from app.auth import require_auth
from app.services import docker_service, nginx_service, compose_service, interactive_service, ws_session, deploy_service

router = APIRouter()


def _get_dockerfile_project(project_name: str, db: Session) -> Project:
    project = db.query(Project).filter(Project.name == project_name).first()
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")
    if project.deploy_mode != "dockerfile":
        raise HTTPException(
            status_code=400,
            detail=f"Project '{project_name}' is a compose project — use the /compose endpoints",
        )
    return project


def _job_response(job_key: str, launched_message: str) -> DockerJobStatusResponse:
    job = docker_service.get_job(job_key)
    if job is None:
        return DockerJobStatusResponse(status="no_job", message="No job found")
    return DockerJobStatusResponse(
        status=job.status,
        operation=job.operation,
        message=launched_message,
        logs=docker_service.get_job_logs(job_key),
        exit_code=job.exit_code,
    )


# Dockerfile / build-context uploads now go through POST /projects/{name}/upload
# (see app/routers/projects.py), which writes files into the one per-project directory
# and auto-detects the Dockerfile to set container_port + WebSocket headers.


# ── Stop / status / exec ────────────────────────────────────────────────────────
#
# Build + run is launched automatically by the upload deploy flow (POST
# /projects/{name}/upload → WS /projects/{name}/deploy); re-upload to redeploy. The
# former per-mode /build and /start endpoints are gone — these remain for control.

@router.post("/{project_name}/stop", response_model=DockerJobStatusResponse,
             summary="Stop the project's container — poll /status")
def stop_container(project_name: str, db: Session = Depends(get_db), _=Depends(require_auth)):
    project = _get_dockerfile_project(project_name, db)
    # Stop the active version's container, but track the job under the stable deploy key
    # (project.container_name is version-scoped and changes on every blue/green deploy).
    job_key = deploy_service.deploy_job_key(project_name)
    docker_service.stop_container(project.container_name, job_key)
    return _job_response(job_key, f"Stop issued for '{project.container_name}' — poll /status")


@router.get("/{project_name}/status", response_model=DockerJobStatusResponse,
            summary="Status + logs of the last docker operation for the project")
def get_docker_status(project_name: str, db: Session = Depends(get_db), _=Depends(require_auth)):
    project = _get_dockerfile_project(project_name, db)
    job_key = deploy_service.deploy_job_key(project_name)
    job = docker_service.get_job(job_key)
    if job is None:
        return DockerJobStatusResponse(status="no_job", message="No docker operation has been run for this project yet")
    return DockerJobStatusResponse(
        status=job.status,
        operation=job.operation,
        message=f"Last operation: {job.operation}",
        logs=docker_service.get_job_logs(job_key),
        exit_code=job.exit_code,
    )


@router.post("/{project_name}/abort", response_model=DockerJobStatusResponse,
             summary="Abort the currently running docker operation for the project")
def abort_docker_job(project_name: str, db: Session = Depends(get_db), _=Depends(require_auth)):
    project = _get_dockerfile_project(project_name, db)
    job_key = deploy_service.deploy_job_key(project_name)
    success, message = docker_service.abort_job(job_key)
    if not success:
        raise HTTPException(status_code=400, detail=message)
    job = docker_service.get_job(job_key)
    return DockerJobStatusResponse(
        status="aborted",
        operation=job.operation if job else None,
        message=message,
        logs=docker_service.get_job_logs(job_key),
        exit_code=job.exit_code if job else None,
    )


# ── Interactive exec shell over WebSocket ────────────────────────────────────────
#
# Replaces the old POST /exec + /status polling: the client connects, sends an auth
# frame, and gets a live pty bridged to `docker exec -it`. Frame protocol matches the
# install session (auth → ready → stdin/stdout/resize → exit; see ws_session +
# interactive_service). An optional ?cmd= query overrides the default shell (sh).

@router.websocket("/{project_name}/exec")
async def exec_session(websocket: WebSocket, project_name: str, cmd: str = ""):
    await websocket.accept()
    if not await ws_session.authenticate(websocket):
        return

    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.name == project_name).first()
        if project is None or project.deploy_mode != "dockerfile":
            await ws_session.reject(websocket, 4404, f"no dockerfile project '{project_name}'")
            return
        container_name = project.container_name
        project_dir = os.path.abspath(compose_service.project_dir(project_name))
    finally:
        db.close()

    await interactive_service.run_exec_session(
        websocket, container_name, project_dir, f"exec:{project_name}", cmd,
    )


# ── SSL (manual retry) ──────────────────────────────────────────────────────────

@router.post("/{project_name}/ssl", response_model=SslResponse,
             summary="(Re)issue the Let's Encrypt SSL certificate for the project")
def issue_ssl(project_name: str, db: Session = Depends(get_db), _=Depends(require_auth)):
    project = _get_dockerfile_project(project_name, db)
    success, message = nginx_service.issue_cert(project.effective_domain)
    if success:
        project.ssl_enabled = True
        db.commit()
        nginx_service.write_ssl_config(project_name, [{
            "subdomain": project.effective_domain, "local_port": project.local_port,
            "websocket": bool(project.websocket),
        }])
    return SslResponse(
        status="ok" if success else "error",
        message=message,
        ssl_enabled=bool(project.ssl_enabled),
    )


# ── Custom domain ─────────────────────────────────────────────────────────────────

@router.post("/{project_name}/domain", response_model=ProjectResponse,
             summary="Set or clear the project's custom domain (re-runs nginx + certbot)")
def set_domain(project_name: str, request: SetDomainRequest,
               db: Session = Depends(get_db), _=Depends(require_auth)):
    """Point this dockerfile project at a custom domain instead of its auto subdomain.
    Pass `custom_domain: null` (or empty) to revert to the subdomain. Rewrites the nginx
    config and issues a Let's Encrypt cert for the effective domain; if DNS doesn't yet
    point here the project stays HTTP-only (ssl_enabled=False) and SSL can be retried."""
    # Local imports avoid a circular import at module load (projects imports service fns).
    from app.routers.projects import assert_domain_available, project_response

    project = _get_dockerfile_project(project_name, db)
    if not project.subdomain or not project.local_port:
        raise HTTPException(status_code=400, detail="Project is not provisioned yet — upload a Dockerfile first")

    if request.custom_domain:
        assert_domain_available(db, request.custom_domain, exclude_project_id=project.id)
    project.custom_domain = request.custom_domain
    db.commit()

    ssl_result = nginx_service.setup_nginx(project_name, [{
        "subdomain": project.effective_domain,
        "local_port": project.local_port,
        "websocket": bool(project.websocket),
    }])
    project.ssl_enabled = bool(ssl_result["ssl"].get(project.effective_domain, {}).get("success"))
    db.commit()
    db.refresh(project)
    return project_response(project)
