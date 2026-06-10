"""
compose.py — project-level endpoints for compose-mode projects.

A compose project is created empty (POST /projects with deploy_mode="compose"),
then a single docker-compose.yml is uploaded here. Every service that publishes
a port becomes a tracked ComposeService exposed at {service}.{project}.{domain};
lifecycle (build/up/down) runs against the whole stack via `docker compose`.
"""

import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.models.database import get_db
from app.models.orm import Project, ComposeService
from app.models.schemas import DockerJobStatusResponse, SetDomainRequest, ProjectResponse
from app.auth import require_auth
from app.config import settings
from app.services import docker_service, nginx_service, compose_service
from app.routers.projects import _next_port, assert_domain_available, project_response

router = APIRouter()


def _get_compose_project(project_name: str, db: Session) -> Project:
    project = db.query(Project).filter(Project.name == project_name).first()
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")
    if project.deploy_mode != "compose":
        raise HTTPException(
            status_code=400,
            detail=f"Project '{project_name}' is not a compose project (deploy_mode={project.deploy_mode})",
        )
    return project


def _job_key(project_name: str) -> str:
    return f"compose:{project_name}"


def _job_response(project_name: str, launched_message: str) -> DockerJobStatusResponse:
    key = _job_key(project_name)
    job = docker_service.get_job(key)
    if job is None:
        return DockerJobStatusResponse(status="no_job", message="No job found")
    return DockerJobStatusResponse(
        status=job.status,
        operation=job.operation,
        message=launched_message,
        logs=docker_service.get_job_logs(key),
        exit_code=job.exit_code,
    )


def _abs_project_dir(project_name: str) -> str:
    return os.path.abspath(compose_service.project_dir(project_name))


# ── Upload ──────────────────────────────────────────────────────────────────────

def provision_compose(
    db: Session,
    project: Project,
    compose_text: str,
    domain_prefix: Optional[str] = None,
) -> list[dict]:
    """Wire up a compose project from its compose file: (re)create service rows,
    allocate loopback ports, write the override, and run nginx/SSL setup. Commits.

    Shared by the upload endpoint and the plugin add-flow. Each exposed service's dict
    is augmented in place with `local_port`, `subdomain`, and `ssl_enabled`. Raises
    HTTPException(400) on a bad compose file and HTTPException(500) on nginx permission errors.

    `domain_prefix` overrides the default `{service}.{project}.{domain}` subdomain scheme:
      - single exposed service  → `{domain_prefix}.{domain}` (or just `{domain}` if empty)
      - multiple exposed services → `{service}.{domain_prefix}.{domain}`
    When None the default scheme applies.
    """
    project_name = project.name
    try:
        services = compose_service.parse_services(compose_text)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Re-provision: capture any custom domains keyed by service name so they survive,
    # then drop existing service rows first so their ports are freed.
    prior_custom_domains = {s.name: s.custom_domain for s in project.services if s.custom_domain}
    for old in list(project.services):
        db.delete(old)
    db.flush()

    exposed_count = sum(1 for s in services if s["exposed"])

    reserved: set[int] = set()
    endpoints: list[dict] = []
    rows_by_name: dict[str, ComposeService] = {}

    for svc in services:
        if not svc["exposed"]:
            continue
        port = _next_port(db, reserved)
        reserved.add(port)
        if domain_prefix is not None:
            if exposed_count == 1:
                subdomain = ".".join(s for s in [domain_prefix, settings.BASE_DOMAIN] if s)
            else:
                subdomain = ".".join(s for s in [svc["name"], domain_prefix, settings.BASE_DOMAIN] if s)
        else:
            subdomain = f"{svc['name']}.{project_name}.{settings.BASE_DOMAIN}"
        custom_domain = prior_custom_domains.get(svc["name"])
        effective = custom_domain or subdomain
        svc["local_port"] = port      # augment for write_files + response
        svc["subdomain"] = subdomain
        row = ComposeService(
            project_id=project.id,
            name=svc["name"],
            subdomain=subdomain,
            custom_domain=custom_domain,
            local_port=port,
            container_port=svc["container_port"],
            container_name=f"freeholdy_{project_name}_{svc['name']}",
            websocket=svc["websocket"],
        )
        db.add(row)
        rows_by_name[svc["name"]] = row
        endpoints.append({"subdomain": effective, "local_port": port, "websocket": svc["websocket"]})

    db.flush()

    # Persist the compose file + generated override.
    compose_path, _ = compose_service.write_files(project_name, compose_text, services)
    project.compose_path = compose_path

    # nginx + SSL for the exposed services.
    if endpoints:
        try:
            ssl_result = nginx_service.setup_nginx(project_name, endpoints)
        except PermissionError:
            db.rollback()
            nginx_service.remove_config(project_name)
            raise HTTPException(
                status_code=500,
                detail="Permission denied writing nginx config — run freeholdy with sudo or grant write access to nginx dirs",
            )
        if ssl_result.get("error"):
            # nginx -t rejected the generated config: roll back so we don't leave a broken
            # nginx config on disk paired with "live" service rows.
            db.rollback()
            nginx_service.remove_config(project_name)
            raise HTTPException(status_code=500, detail=ssl_result["error"])
        for name, row in rows_by_name.items():
            if ssl_result["ssl"].get(row.effective_domain, {}).get("success"):
                row.ssl_enabled = True

    db.commit()

    for svc in services:
        svc["ssl_enabled"] = rows_by_name[svc["name"]].ssl_enabled if svc["name"] in rows_by_name else False
    return services


# The docker-compose.yml upload now goes through POST /projects/{name}/upload (see
# app/routers/projects.py): it writes the file into the per-project directory, detects it,
# and calls provision_compose below. provision_compose stays here as the shared service
# function (the plugins router imports it too).


# ── Custom domain ─────────────────────────────────────────────────────────────

@router.post(
    "/{project_name}/services/{service_name}/domain",
    response_model=ProjectResponse,
    summary="Set or clear a compose service's custom domain (re-runs nginx + certbot)",
)
def set_service_domain(
    project_name: str,
    service_name: str,
    request: SetDomainRequest,
    db: Session = Depends(get_db),
    _=Depends(require_auth),
):
    """Point one exposed service at a custom domain instead of its
    `{service}.{project}.{domain}` subdomain. Pass `custom_domain: null` (or empty) to
    revert. Rewrites the whole project's nginx config and (re)issues certs for every
    exposed service; if DNS doesn't yet point here the service stays HTTP-only."""
    project = _get_compose_project(project_name, db)
    target = next((s for s in project.services if s.name == service_name), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"Service '{service_name}' not found in project '{project_name}'")

    if request.custom_domain:
        assert_domain_available(db, request.custom_domain, exclude_service_id=target.id)
    target.custom_domain = request.custom_domain
    db.commit()

    endpoints = [{
        "subdomain": s.effective_domain,
        "local_port": s.local_port,
        "websocket": bool(s.websocket),
    } for s in project.services]
    ssl_result = nginx_service.setup_nginx(project_name, endpoints)
    for s in project.services:
        s.ssl_enabled = bool(ssl_result["ssl"].get(s.effective_domain, {}).get("success"))
    db.commit()
    db.refresh(project)
    return project_response(project)


# ── Lifecycle ─────────────────────────────────────────────────────────────────

def _require_compose_files(project_name: str):
    if not os.path.exists(compose_service.override_file_path(project_name)):
        raise HTTPException(status_code=400, detail="No docker-compose.yml uploaded yet")


@router.post(
    "/{project_name}/compose/build",
    response_model=DockerJobStatusResponse,
    summary="docker compose build — returns immediately, poll /compose/status",
)
def compose_build(project_name: str, db: Session = Depends(get_db), _=Depends(require_auth)):
    _get_compose_project(project_name, db)
    _require_compose_files(project_name)
    docker_service.compose_build(project_name, _abs_project_dir(project_name), _job_key(project_name))
    return _job_response(project_name, "compose build started — poll /compose/status")


@router.post(
    "/{project_name}/compose/up",
    response_model=DockerJobStatusResponse,
    summary="docker compose up -d — returns immediately, poll /compose/status",
)
def compose_up(project_name: str, db: Session = Depends(get_db), _=Depends(require_auth)):
    _get_compose_project(project_name, db)
    _require_compose_files(project_name)
    docker_service.compose_up(project_name, _abs_project_dir(project_name), _job_key(project_name))
    return _job_response(project_name, "compose up started — poll /compose/status")


@router.post(
    "/{project_name}/compose/down",
    response_model=DockerJobStatusResponse,
    summary="docker compose down — returns immediately, poll /compose/status",
)
def compose_down(project_name: str, db: Session = Depends(get_db), _=Depends(require_auth)):
    _get_compose_project(project_name, db)
    _require_compose_files(project_name)
    docker_service.compose_down(project_name, _abs_project_dir(project_name), _job_key(project_name))
    return _job_response(project_name, "compose down started — poll /compose/status")


@router.get(
    "/{project_name}/compose/status",
    response_model=DockerJobStatusResponse,
    summary="Status + logs of the last docker compose operation",
)
def compose_status(project_name: str, db: Session = Depends(get_db), _=Depends(require_auth)):
    _get_compose_project(project_name, db)
    key = _job_key(project_name)
    job = docker_service.get_job(key)
    if job is None:
        return DockerJobStatusResponse(status="no_job", message="No compose operation has been run yet")
    return DockerJobStatusResponse(
        status=job.status,
        operation=job.operation,
        message=f"Last operation: {job.operation}",
        logs=docker_service.get_job_logs(key),
        exit_code=job.exit_code,
    )


@router.post(
    "/{project_name}/compose/abort",
    response_model=DockerJobStatusResponse,
    summary="Abort the currently running docker compose operation",
)
def compose_abort(project_name: str, db: Session = Depends(get_db), _=Depends(require_auth)):
    _get_compose_project(project_name, db)
    key = _job_key(project_name)
    success, message = docker_service.abort_job(key)
    if not success:
        raise HTTPException(status_code=400, detail=message)
    job = docker_service.get_job(key)
    return DockerJobStatusResponse(
        status="aborted",
        operation=job.operation if job else None,
        message=message,
        logs=docker_service.get_job_logs(key),
        exit_code=job.exit_code if job else None,
    )


