import os
import subprocess
import threading
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from app.models.database import get_db
from app.models.orm import Project
from app.models.schemas import (
    PluginResponse,
    PluginAddRequest,
    PluginAddResponse,
    DockerJobStatusResponse,
)
from app.auth import require_auth
from app.config import settings
from app.services import docker_service, plugin_service, compose_service, scan, nginx_service
from app.routers.projects import provision_dockerfile, project_response
from app.routers.compose import provision_compose

router = APIRouter()


@router.get("/", response_model=List[PluginResponse])
def list_plugins(_=Depends(require_auth)):
    return [
        {
            "name": p["name"],
            "description": p["description"],
            "deploy_mode": p["deploy_mode"],
            "container_port": p["container_port"],
            "has_install": p["has_install"],
            "type": p["type"],
        }
        for p in plugin_service.list_plugins()
    ]


@router.post(
    "/{plugin_name}/add",
    response_model=PluginAddResponse,
    status_code=201,
    summary="Create a project from a plugin, then build + run its container (async)",
)
def add_plugin(
    plugin_name: str,
    request: PluginAddRequest,
    db: Session = Depends(get_db),
    _=Depends(require_auth),
):
    plugin = plugin_service.get_plugin(plugin_name)
    if plugin is None:
        raise HTTPException(status_code=404, detail=f"Plugin '{plugin_name}' not found")

    if plugin["deploy_mode"] == "compose":
        return _add_compose_plugin(plugin, request.project_name, db)

    # 1. Create the single-container project row, then wire nginx/SSL (synchronous).
    #    The project's type comes straight from the plugin manifest's "type" (default "plugin");
    #    a "system" plugin yields a project hidden from the web UI.
    if db.query(Project).filter(Project.name == request.project_name).first():
        raise HTTPException(status_code=409, detail=f"Project '{request.project_name}' already exists")
    project = Project(name=request.project_name, type=plugin["type"], deploy_mode="pending")
    db.add(project)
    db.flush()
    project = provision_dockerfile(
        db,
        project,
        container_port=plugin["container_port"],
        domain_prefix=plugin["domain_prefix"],
    )

    # 2. Stage the plugin's Dockerfile into the build context + scan it for WebSockets.
    project_dir = compose_service.project_dir(project.name)
    project.dockerfile_path = plugin_service.stage_dockerfile(plugin, project_dir)
    with open(project.dockerfile_path) as f:
        if scan.uses_websocket(f.read()):
            project.websocket = True
            if project.ssl_enabled:
                nginx_service.write_ssl_config(project.name, [{
                    "subdomain": project.subdomain, "local_port": project.local_port, "websocket": True,
                }])
    db.commit()
    db.refresh(project)

    # 3. Launch the async provision job: install.sh → build → run.
    docker_service.provision_from_plugin(
        job_key=project.container_name,
        project_dir=os.path.abspath(project_dir),
        plugin_dir=plugin["dir"],
        install_script=plugin["install"],
        image_name=project.image_name,
        container_name=project.container_name,
        local_port=project.local_port,
        container_port=project.container_port,
    )

    job = docker_service.get_job(project.container_name)
    job_resp = DockerJobStatusResponse(
        status=job.status if job else "no_job",
        operation=job.operation if job else None,
        message=f"Provisioning '{project.name}' from plugin '{plugin['name']}' — "
                f"poll /projects/{project.name}/status",
        logs=docker_service.get_job_logs(project.container_name),
        exit_code=job.exit_code if job else None,
    )

    return PluginAddResponse(
        status="ok",
        message=f"Project '{project.name}' created from plugin '{plugin['name']}'",
        project=project_response(project),
        job=job_resp,
    )


def _add_compose_plugin(plugin: dict, project_name: str, db: Session) -> PluginAddResponse:
    """Provision a compose plugin: create a compose project, stage the plugin's source
    (compose file + build contexts), wire up nginx, then `docker compose up -d`.

    If the plugin has install.sh it is invoked in two phases:
      - "pre"  (synchronous, before compose_up): populate .env with generated secrets
      - "post" (background thread, after compose_up): call APIs once the container is live
    """
    if db.query(Project).filter(Project.name == project_name).first():
        raise HTTPException(status_code=409, detail=f"Project '{project_name}' already exists")

    project = Project(name=project_name, type=plugin["type"], deploy_mode="compose")
    db.add(project)
    db.flush()

    project_dir = os.path.abspath(compose_service.project_dir(project_name))
    os.makedirs(project_dir, exist_ok=True)

    # Seed .env with paths docker compose needs for variable substitution.
    # PROJECTS_DIR is the unified per-project files root (e.g. SFTPGo mounts it as /srv/projects);
    # DOCKERFILES_DIR is kept for backward compatibility with older plugin compose files.
    env_file = os.path.join(project_dir, ".env")
    projects_dir_abs = os.path.abspath(settings.PROJECTS_DIR)
    with open(env_file, "w") as f:
        f.write(f"PROJECTS_DIR={projects_dir_abs}\n")
        f.write(f"DOCKERFILES_DIR={os.path.abspath(settings.DOCKERFILES_DIR)}\n")

    # Stage the whole plugin tree (docker-compose.yml + assets + install.sh) into the
    # compose project dir so all files sit together.
    compose_file = plugin_service.stage_compose(plugin, project_dir)

    # Build the env for install.sh (both phases get the same base; ports added after provision).
    install_env = {
        **os.environ,
        "PLUGIN_DIR": plugin["dir"],
        "PROJECT_DIR": project_dir,
        "PROJECT_NAME": project_name,
        "PROJECTS_DIR": projects_dir_abs,
        "DOCKERFILES_DIR": os.path.abspath(settings.DOCKERFILES_DIR),
        "BASE_DOMAIN": settings.BASE_DOMAIN,
    }

    # Pre phase: let the plugin generate secrets and append them to .env.
    install_script = os.path.join(project_dir, "install.sh") if plugin.get("has_install") else None
    if install_script and os.path.exists(install_script):
        subprocess.run(["bash", install_script, "pre"], cwd=project_dir, env=install_env, check=False)

    with open(compose_file) as f:
        compose_text = f.read()

    domain_prefix = plugin.get("domain_prefix")
    services = provision_compose(db, project, compose_text, domain_prefix=domain_prefix)
    db.refresh(project)

    # Add per-service local ports so install.sh post phase can reach the container's REST API.
    for svc in services:
        if svc.get("exposed"):
            key = f"SERVICE_{svc['name'].upper()}_LOCAL_PORT"
            install_env[key] = str(svc["local_port"])

    # Launch the stack.
    job_key = f"compose:{project_name}"
    docker_service.compose_up(project_name, project_dir, job_key)

    # Post phase: runs in the background; polls until the container API is ready.
    if install_script and os.path.exists(install_script):
        def _run_post():
            subprocess.run(["bash", install_script, "post"], cwd=project_dir, env=install_env, check=False)
        threading.Thread(target=_run_post, daemon=True).start()

    job = docker_service.get_job(job_key)
    job_resp = DockerJobStatusResponse(
        status=job.status if job else "no_job",
        operation=job.operation if job else None,
        message=f"Provisioning '{project_name}' from plugin '{plugin['name']}' — "
                f"poll /projects/{project_name}/compose/status",
        logs=docker_service.get_job_logs(job_key),
        exit_code=job.exit_code if job else None,
    )

    return PluginAddResponse(
        status="ok",
        message=f"Project '{project_name}' created from plugin '{plugin['name']}'",
        project=project_response(project),
        job=job_resp,
    )
