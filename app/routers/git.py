import os
import shutil

from fastapi import APIRouter, Depends, HTTPException, WebSocket
from sqlalchemy.orm import Session

from app.models.database import get_db
from app.models.orm import Project
from app.models.schemas import (
    GitAddRequest,
    GitKeyResponse,
    PluginAddResponse,
    DockerJobStatusResponse,
    ProjectType,
)
from app.auth import require_auth
from app.services import docker_service, compose_service, git_service
from app.routers.projects import (
    project_response,
    _autoprovision,
    apply_deploy_env,
    launch_deploy,
    deploy_stream_session,
)

router = APIRouter()


def _deploy_ws_path(project_name: str) -> str:
    """The WebSocket path clients connect to to watch the build + run (see deploy_session)."""
    return f"/git/deploy/{project_name}"


@router.post(
    "/add",
    response_model=PluginAddResponse,
    status_code=201,
    summary="Create a project from a git repo: clone, auto-detect, build + run (async)",
)
def add_git_project(
    request: GitAddRequest,
    db: Session = Depends(get_db),
    _=Depends(require_auth),
):
    """Deploy a project from a git clone URL. Clones the repo into the project dir, scans
    the root for a Dockerfile / docker-compose.yml (compose wins) and wires nginx/SSL/ports
    exactly like an upload, then launches an async build+run job. The response carries a
    `ws_path` — connect to `WS /git/deploy/{name}` to stream the build log live.

    Idempotent: a new name creates the project; an existing name re-clones + redeploys it
    (a fresh blue/green version), so `deploy NAME <git-url>` behaves like a re-upload."""
    name = request.name
    existing = db.query(Project).filter(Project.name == name).first()
    if existing:
        project = existing
    else:
        project = Project(name=name, type=ProjectType.user.value, deploy_mode="pending")
        db.add(project)
        db.flush()

    # Store any env supplied with the deploy before provisioning, so the first container
    # start already has it (provision_compose bakes env_file: entries into the override).
    # Flush only — the provisioning commit persists it on success, and the rollbacks below
    # discard it along with the project row on failure.
    apply_deploy_env(db, project, request.env)

    project_dir = os.path.abspath(compose_service.project_dir(name))
    # git clone needs a clean target; clear the existing tree (a prior deploy or a stale dir
    # from a failed attempt) so the clone lands fresh.
    if os.path.isdir(project_dir):
        shutil.rmtree(project_dir, ignore_errors=True)

    # 1. Clone the repo into the project dir.
    ok, message = git_service.clone(request.git_url, project_dir, request.branch)
    if not ok:
        db.rollback()
        shutil.rmtree(project_dir, ignore_errors=True)
        detail = f"git clone failed: {message}"
        if git_service.is_auth_failure(message):
            detail += (
                " — this looks like an SSH auth problem. Run `fhcli get-git-key` (or click "
                "'git key' in the web UI) to get the server's public key, add it to GitHub, "
                "then retry."
            )
        raise HTTPException(status_code=400, detail=detail)

    # 2. Provision nginx/SSL/ports (the same path the upload endpoint runs). On any
    # failure, roll back and drop the cloned tree so a retry starts clean.
    try:
        provisioned, detected_mode = _autoprovision(db, project, name, project_dir)
    except HTTPException:
        db.rollback()
        shutil.rmtree(project_dir, ignore_errors=True)
        raise
    if not provisioned:
        db.rollback()
        shutil.rmtree(project_dir, ignore_errors=True)
        raise HTTPException(
            status_code=400,
            detail="no Dockerfile or docker-compose.yml found in the repository root",
        )
    db.refresh(project)

    # 3. Launch the async build + run job (no install.sh) — same dispatch the upload flow uses.
    job_key = launch_deploy(project)

    verb = "Redeploying" if existing else "Cloned and provisioning"
    job = docker_service.get_job(job_key)
    job_resp = DockerJobStatusResponse(
        status=job.status if job else "no_job",
        operation=job.operation if job else None,
        message=f"{verb} '{name}' ({detected_mode}) — stream {_deploy_ws_path(name)}",
        logs=docker_service.get_job_logs(job_key),
        exit_code=job.exit_code if job else None,
    )

    return PluginAddResponse(
        status="ok",
        message=(f"Project '{name}' redeployed from git repo" if existing
                 else f"Project '{name}' created from git repo"),
        project=project_response(project),
        job=job_resp,
        ws_path=_deploy_ws_path(name),
    )


@router.get(
    "/key",
    response_model=GitKeyResponse,
    summary="Get the server's GitHub SSH public key (creates one on first use)",
)
def get_git_key(_=Depends(require_auth)):
    """Return the server's GitHub SSH public key so it can be added to GitHub for cloning
    private repos over SSH. If no `Host github.com` key exists yet, an ed25519 keypair is
    generated and a matching ~/.ssh/config block is written; subsequent calls return the
    same key (`created: false`)."""
    try:
        public_key, created = git_service.get_or_create_github_key()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=f"could not provision git key: {exc}")
    return GitKeyResponse(
        public_key=public_key,
        created=created,
        key_path=git_service.github_key_path(),
        instructions=git_service.github_instructions(public_key),
    )


# ── Deploy over WebSocket ───────────────────────────────────────────────────────
#
# POST /git/add launches the build+run job and returns a ws_path; the client connects
# here to stream the build log live (no status polling). Read-only — a git deploy never
# prompts. Same frame protocol as the plugin install socket:
#   client -> server : {"type": "auth", "token": "..."}   first frame, always
#                      {"type": "abort"}                    cancel the running build
#   server -> client : {"type": "ready"}                   auth ok, streaming starts
#                      {"type": "stdout", "data": "..."}   build + run output
#                      {"type": "exit", "code": N}          job finished
# A reconnect re-streams a running job and replays a finished one (stream_job).


@router.websocket("/deploy/{project_name}")
async def deploy_session(websocket: WebSocket, project_name: str):
    # Same read-only build+run stream as the upload deploy route (routers/projects.py).
    await deploy_stream_session(websocket, project_name)
