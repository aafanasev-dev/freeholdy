import asyncio
import os
import subprocess
import threading
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from typing import List, Optional, Tuple

from app.models.database import get_db, SessionLocal
from app.models.orm import Project, ComposeService, Token
from app.models.schemas import (
    PluginResponse,
    PluginAddRequest,
    PluginAddResponse,
    DockerJobStatusResponse,
)
from app.auth import require_auth, hash_token
from app.config import settings
from app.services import (
    docker_service,
    plugin_service,
    compose_service,
    interactive_service,
    scan,
    nginx_service,
)
from app.routers.projects import provision_dockerfile, project_response
from app.routers.compose import provision_compose

router = APIRouter()


@router.get("/", response_model=List[PluginResponse])
def list_plugins(_=Depends(require_auth)):
    return [
        {
            "name": p["name"],
            "description": p["description"],
            "about": p["about"],
            "deploy_mode": p["deploy_mode"],
            "container_port": p["container_port"],
            "has_install": p["has_install"],
            "interactive": p["interactive"],
            "type": p["type"],
        }
        for p in plugin_service.list_plugins()
    ]


def _plugin_slug(plugin: dict) -> str:
    """The plugin's directory name — the identifier used in /plugins/{name}/... paths
    (the manifest "name" may differ)."""
    return os.path.basename(plugin["dir"])


def _waiting_interactive_response(plugin: dict, project: Project) -> PluginAddResponse:
    """Response for an interactive plugin: provisioning stops here until the client
    connects to ws_path and drives install.sh (see install_session)."""
    ws_path = f"/plugins/{_plugin_slug(plugin)}/install/{project.name}"
    return PluginAddResponse(
        status="ok",
        message=f"Project '{project.name}' created from plugin '{plugin['name']}' — "
                f"interactive install pending",
        project=project_response(project),
        job=DockerJobStatusResponse(
            status="waiting_interactive",
            operation="install",
            message=f"install.sh is interactive — connect WebSocket {ws_path} to run it",
            logs="",
        ),
        ws_path=ws_path,
    )


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

    # 1.+2. Create the project row, wire nginx/SSL, stage the Dockerfile (synchronous).
    project = _create_dockerfile_project(plugin, request.project_name, db)

    # Interactive plugins stop here: install.sh needs a user on the other end, so the
    # client must connect to the returned WebSocket path to continue (build+run follow
    # automatically once install.sh exits 0).
    if plugin["interactive"]:
        return _waiting_interactive_response(plugin, project)

    # 3. Launch the async provision job: install.sh → build → run.
    project_dir = compose_service.project_dir(project.name)
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


def _create_dockerfile_project(plugin: dict, project_name: str, db: Session) -> Project:
    """Create the single-container project row, wire nginx/SSL, and stage the plugin's
    Dockerfile into the build context (everything synchronous that must happen before
    install.sh/build can run). The project's type comes straight from the plugin
    manifest's "type" (default "plugin"); a "system" plugin yields a project hidden
    from the web UI."""
    if db.query(Project).filter(Project.name == project_name).first():
        raise HTTPException(status_code=409, detail=f"Project '{project_name}' already exists")
    project = Project(name=project_name, type=plugin["type"], deploy_mode="pending")
    db.add(project)
    db.flush()
    project = provision_dockerfile(
        db,
        project,
        container_port=plugin["container_port"],
        domain_prefix=plugin["domain_prefix"],
    )

    # Stage the plugin's Dockerfile into the build context + scan it for WebSockets.
    project_dir = compose_service.project_dir(project.name)
    project.dockerfile_path = plugin_service.stage_dockerfile(plugin, project_dir)
    with open(project.dockerfile_path) as f:
        if scan.uses_websocket(f.read()):
            project.websocket = True
            if project.ssl_enabled:
                nginx_service.write_ssl_config(project.name, [{
                    "subdomain": project.effective_domain, "local_port": project.local_port, "websocket": True,
                }])
    db.commit()
    db.refresh(project)
    return project


def _build_install_env(plugin: dict, project_name: str, project_dir: str) -> dict:
    """Env for a compose plugin's install.sh (both phases get the same base; per-service
    ports are added after provisioning). Pure function of its inputs so an interactive
    session can rebuild it statelessly on (re)connect."""
    return {
        **os.environ,
        "PLUGIN_DIR": plugin["dir"],
        "PROJECT_DIR": project_dir,
        "PROJECT_NAME": project_name,
        "PROJECTS_DIR": os.path.abspath(settings.PROJECTS_DIR),
        "DOCKERFILES_DIR": os.path.abspath(settings.DOCKERFILES_DIR),
        "BASE_DOMAIN": settings.BASE_DOMAIN,
    }


def _stage_compose_plugin(plugin: dict, project_name: str, db: Session) -> Tuple[Project, str]:
    """Create the compose project row and stage the plugin's source tree + seed .env.
    Commits the row so a later WebSocket session (separate DB session) can see it.
    Returns (project, absolute project_dir)."""
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
    with open(env_file, "w") as f:
        f.write(f"PROJECTS_DIR={os.path.abspath(settings.PROJECTS_DIR)}\n")
        f.write(f"DOCKERFILES_DIR={os.path.abspath(settings.DOCKERFILES_DIR)}\n")

    # Stage the whole plugin tree (docker-compose.yml + assets + install.sh) into the
    # compose project dir so all files sit together.
    plugin_service.stage_compose(plugin, project_dir)

    db.commit()
    db.refresh(project)
    return project, project_dir


def _finish_compose_plugin(
    plugin: dict,
    project: Project,
    project_dir: str,
    install_env: dict,
    install_script: Optional[str],
    db: Session,
) -> Optional[docker_service.DockerJob]:
    """Everything after install.sh's pre phase: provision nginx/SSL per exposed service,
    `docker compose up -d`, and run the post phase in the background."""
    with open(os.path.join(project_dir, "docker-compose.yml")) as f:
        compose_text = f.read()

    services = provision_compose(db, project, compose_text, domain_prefix=plugin.get("domain_prefix"))
    db.refresh(project)

    # Add per-service local ports so install.sh post phase can reach the container's REST API.
    for svc in services:
        if svc.get("exposed"):
            key = f"SERVICE_{svc['name'].upper()}_LOCAL_PORT"
            install_env[key] = str(svc["local_port"])

    # Launch the stack.
    job_key = f"compose:{project.name}"
    docker_service.compose_up(project.name, project_dir, job_key)

    # Post phase: runs in the background; polls until the container API is ready.
    if install_script and os.path.exists(install_script):
        def _run_post():
            subprocess.run(["bash", install_script, "post"], cwd=project_dir, env=install_env, check=False)
        threading.Thread(target=_run_post, daemon=True).start()

    return docker_service.get_job(job_key)


def _add_compose_plugin(plugin: dict, project_name: str, db: Session) -> PluginAddResponse:
    """Provision a compose plugin: create a compose project, stage the plugin's source
    (compose file + build contexts), wire up nginx, then `docker compose up -d`.

    If the plugin has install.sh it is invoked in two phases:
      - "pre"  (synchronous, before compose_up): populate .env with generated secrets
      - "post" (background thread, after compose_up): call APIs once the container is live

    Interactive plugins stop after staging — the pre phase needs a user on stdin, so it
    runs inside the WebSocket session (install_session) and the rest follows from there.
    """
    project, project_dir = _stage_compose_plugin(plugin, project_name, db)

    if plugin["interactive"]:
        return _waiting_interactive_response(plugin, project)

    install_env = _build_install_env(plugin, project_name, project_dir)

    # Pre phase: let the plugin generate secrets and append them to .env.
    install_script = os.path.join(project_dir, "install.sh") if plugin.get("has_install") else None
    if install_script and os.path.exists(install_script):
        subprocess.run(["bash", install_script, "pre"], cwd=project_dir, env=install_env, check=False)

    job = _finish_compose_plugin(plugin, project, project_dir, install_env, install_script, db)

    job_resp = DockerJobStatusResponse(
        status=job.status if job else "no_job",
        operation=job.operation if job else None,
        message=f"Provisioning '{project_name}' from plugin '{plugin['name']}' — "
                f"poll /projects/{project_name}/compose/status",
        logs=docker_service.get_job_logs(f"compose:{project_name}"),
        exit_code=job.exit_code if job else None,
    )

    return PluginAddResponse(
        status="ok",
        message=f"Project '{project_name}' created from plugin '{plugin['name']}'",
        project=project_response(project),
        job=job_resp,
    )


# ── Interactive install over WebSocket ─────────────────────────────────────────
#
# Two-step flow for plugins with "interactive": true — POST /plugins/{name}/add stages
# everything but runs nothing, then the client connects here to drive install.sh:
#
#   client -> server : {"type": "auth", "token": "..."}     first frame, always
#                      {"type": "stdin", "data": "line\n"}  user input
#   server -> client : {"type": "ready"}                    auth ok, session starting
#                      {"type": "stdout", "data": "..."}    install.sh output
#                      {"type": "exit", "code": N}          script finished
#                      {"type": "error", "message": "..."}  protocol/validation failure
#
# Close codes: 4401 bad auth, 4404 unknown plugin/project, 4409 busy/already provisioned.
# On exit 0 the server finishes provisioning (dockerfile: build+run job; compose:
# nginx/SSL + compose up + post phase) BEFORE sending the exit frame, so clients can
# fall back to the normal status-polling endpoints immediately after.
# Disconnecting mid-script kills it and leaves the project pre-install; reconnecting
# re-runs install.sh, and DELETE /projects/{name} remains the escape hatch.


def _ws_token_valid(token: str) -> bool:
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


async def _ws_reject(websocket: WebSocket, code: int, message: str) -> None:
    try:
        await websocket.send_json({"type": "error", "message": message})
        await websocket.close(code=code)
    except (WebSocketDisconnect, RuntimeError):
        pass


def _finish_compose_from_ws(plugin: dict, project_name: str) -> None:
    """Synchronous continuation after an interactive pre phase — runs in a worker thread
    (provision_compose shells out to certbot) with its own DB session."""
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.name == project_name).first()
        project_dir = os.path.abspath(compose_service.project_dir(project_name))
        install_env = _build_install_env(plugin, project_name, project_dir)
        install_script = os.path.join(project_dir, "install.sh")
        _finish_compose_plugin(plugin, project, project_dir, install_env, install_script, db)
    finally:
        db.close()


@router.websocket("/{plugin_name}/install/{project_name}")
async def install_session(websocket: WebSocket, plugin_name: str, project_name: str):
    await websocket.accept()

    # First frame must be auth (browsers can't set an Authorization header on a WebSocket).
    try:
        msg = await asyncio.wait_for(websocket.receive_json(), timeout=10)
    except WebSocketDisconnect:
        return
    except (asyncio.TimeoutError, ValueError):
        await _ws_reject(websocket, 4401, "expected an auth frame within 10s")
        return
    if msg.get("type") != "auth" or not _ws_token_valid(str(msg.get("token") or "")):
        await _ws_reject(websocket, 4401, "invalid or inactive token")
        return

    plugin = plugin_service.get_plugin(plugin_name)
    if plugin is None or not plugin["interactive"]:
        await _ws_reject(websocket, 4404, f"'{plugin_name}' is not an interactive plugin")
        return

    # Validate project state and build the session command with a short-lived DB session.
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.name == project_name).first()
        if project is None:
            await _ws_reject(
                websocket, 4404,
                f"project '{project_name}' not found — POST /plugins/{plugin_name}/add first",
            )
            return

        project_dir = os.path.abspath(compose_service.project_dir(project_name))
        if plugin["deploy_mode"] == "compose":
            job_key = f"compose:{project_name}"
            install_script = os.path.join(project_dir, "install.sh")
            if not os.path.exists(install_script):
                await _ws_reject(
                    websocket, 4404,
                    f"no staged install.sh — POST /plugins/{plugin_name}/add first",
                )
                return
            # Re-running pre on a live stack would re-allocate ports/nginx — refuse.
            already = (
                db.query(ComposeService)
                .filter(ComposeService.project_id == project.id)
                .count()
            )
            if already:
                await _ws_reject(websocket, 4409, "project is already provisioned")
                return
            cmd = ["bash", install_script, "pre"]
            env = _build_install_env(plugin, project_name, project_dir)
            finish_args = None
        else:
            job_key = project.container_name
            running = docker_service.get_job(job_key)
            if running and running.status == "running":
                await _ws_reject(websocket, 4409, "a job is already running for this project")
                return
            cmd = ["bash", plugin["install"]]
            # Mirrors the env provision_from_plugin gives install.sh.
            env = {**os.environ, "PLUGIN_DIR": plugin["dir"], "PROJECT_DIR": project_dir}
            finish_args = dict(
                job_key=job_key,
                project_dir=project_dir,
                plugin_dir=plugin["dir"],
                install_script=None,  # already ran interactively — build + run only
                image_name=project.image_name,
                container_name=project.container_name,
                local_port=project.local_port,
                container_port=project.container_port,
            )
    finally:
        db.close()

    if not interactive_service.try_acquire(job_key):
        await _ws_reject(websocket, 4409, "an install session is already in progress")
        return
    try:
        await websocket.send_json({"type": "ready"})
        exit_code = await interactive_service.run_session(
            websocket, cmd, cwd=project_dir, env=env, job_key=job_key
        )
        if exit_code is None:
            return  # client disconnected; install killed; project left pre-install

        if exit_code == 0:
            # Continue provisioning BEFORE the exit frame so the client's status
            # polling finds the follow-up job instead of no_job.
            if plugin["deploy_mode"] == "compose":
                await websocket.send_json({
                    "type": "stdout",
                    "data": "\n── install.sh done — provisioning nginx/SSL and starting compose ──\n",
                })
                await run_in_threadpool(_finish_compose_from_ws, plugin, project_name)
            else:
                docker_service.provision_from_plugin(**finish_args)

        await websocket.send_json({"type": "exit", "code": exit_code})
        await websocket.close(code=1000)
    except WebSocketDisconnect:
        pass
    finally:
        interactive_service.release(job_key)
