"""
container.py — project-level lifecycle for dockerfile-mode projects.

A dockerfile project is a single container, so these endpoints operate on the
Project row itself (no parts). Replaces the old per-part router.
"""

import os

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.models.database import get_db
from app.models.orm import Project
from app.models.schemas import (
    DockerJobStatusResponse,
    ExecRequest,
    SslResponse,
)
from app.auth import require_auth
from app.services import docker_service, nginx_service

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


# ── Build / start / stop / exec ─────────────────────────────────────────────────

@router.post("/{project_name}/build", response_model=DockerJobStatusResponse,
             summary="Build (or rebuild) the project's docker image — poll /status")
def build_image(project_name: str, db: Session = Depends(get_db), _=Depends(require_auth)):
    project = _get_dockerfile_project(project_name, db)
    if not project.dockerfile_path or not os.path.exists(project.dockerfile_path):
        raise HTTPException(status_code=400, detail="No Dockerfile uploaded for this project yet")
    docker_service.build_image(project.dockerfile_path, project.image_name, project.container_name)
    return _job_response(project.container_name, f"Build started for image '{project.image_name}' — poll /status")


@router.post("/{project_name}/start", response_model=DockerJobStatusResponse,
             summary="Start the project's container — poll /status")
def start_container(project_name: str, db: Session = Depends(get_db), _=Depends(require_auth)):
    project = _get_dockerfile_project(project_name, db)
    if not docker_service.image_exists(project.image_name):
        raise HTTPException(status_code=400, detail="Docker image not built yet — run /build first")
    if not project.container_port:
        raise HTTPException(status_code=400, detail="No container port set — re-upload the Dockerfile (it must EXPOSE a port)")
    docker_service.start_container(
        project.container_name, project.image_name, project.local_port, project.container_port,
        project.container_name,
    )
    return _job_response(project.container_name, f"Start issued for '{project.container_name}' — poll /status")


@router.post("/{project_name}/stop", response_model=DockerJobStatusResponse,
             summary="Stop the project's container — poll /status")
def stop_container(project_name: str, db: Session = Depends(get_db), _=Depends(require_auth)):
    project = _get_dockerfile_project(project_name, db)
    docker_service.stop_container(project.container_name, project.container_name)
    return _job_response(project.container_name, f"Stop issued for '{project.container_name}' — poll /status")


@router.post("/{project_name}/exec", response_model=DockerJobStatusResponse,
             summary="Execute a command inside the running container — poll /status")
def exec_command(project_name: str, request: ExecRequest, db: Session = Depends(get_db), _=Depends(require_auth)):
    project = _get_dockerfile_project(project_name, db)
    docker_service.exec_in_container(project.container_name, request.command, project.container_name)
    return _job_response(project.container_name, f"Command launched in '{project.container_name}' — poll /status")


@router.get("/{project_name}/status", response_model=DockerJobStatusResponse,
            summary="Status + logs of the last docker operation for the project")
def get_docker_status(project_name: str, db: Session = Depends(get_db), _=Depends(require_auth)):
    project = _get_dockerfile_project(project_name, db)
    job = docker_service.get_job(project.container_name)
    if job is None:
        return DockerJobStatusResponse(status="no_job", message="No docker operation has been run for this project yet")
    return DockerJobStatusResponse(
        status=job.status,
        operation=job.operation,
        message=f"Last operation: {job.operation}",
        logs=docker_service.get_job_logs(project.container_name),
        exit_code=job.exit_code,
    )


@router.post("/{project_name}/abort", response_model=DockerJobStatusResponse,
             summary="Abort the currently running docker operation for the project")
def abort_docker_job(project_name: str, db: Session = Depends(get_db), _=Depends(require_auth)):
    project = _get_dockerfile_project(project_name, db)
    success, message = docker_service.abort_job(project.container_name)
    if not success:
        raise HTTPException(status_code=400, detail=message)
    job = docker_service.get_job(project.container_name)
    return DockerJobStatusResponse(
        status="aborted",
        operation=job.operation if job else None,
        message=message,
        logs=docker_service.get_job_logs(project.container_name),
        exit_code=job.exit_code if job else None,
    )


# ── SSL (manual retry) ──────────────────────────────────────────────────────────

@router.post("/{project_name}/ssl", response_model=SslResponse,
             summary="(Re)issue the Let's Encrypt SSL certificate for the project")
def issue_ssl(project_name: str, db: Session = Depends(get_db), _=Depends(require_auth)):
    project = _get_dockerfile_project(project_name, db)
    success, message = nginx_service.issue_cert(project.subdomain)
    if success:
        project.ssl_enabled = True
        db.commit()
        nginx_service.write_ssl_config(project_name, [{
            "subdomain": project.subdomain, "local_port": project.local_port,
            "websocket": bool(project.websocket),
        }])
    return SslResponse(
        status="ok" if success else "error",
        message=message,
        ssl_enabled=bool(project.ssl_enabled),
    )
