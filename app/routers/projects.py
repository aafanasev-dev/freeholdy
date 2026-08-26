import os
import re
import shutil
import subprocess
import zipfile

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    UploadFile,
    File,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from sqlalchemy.orm import Session, object_session
from typing import List

from app.models.database import get_db, SessionLocal
from app.models.orm import Project, ComposeService, ProjectVersion
from app.models.schemas import (
    ProjectResponse,
    ProjectDeleteResponse,
    UploadResponse,
    UploadCompleteRequest,
    DockerJobStatusResponse,
    ProjectType,
    validate_project_slug,
)
from app.auth import guest_project_name, require_admin, require_project_access, require_token
from app.config import settings
from app.services import (
    docker_service,
    nginx_service,
    compose_service,
    env_service,
    scan,
    interactive_service,
    ws_session,
)

router = APIRouter()


def _next_port(db: Session, reserved: set[int]) -> int:
    """First free loopback port, considering both dockerfile projects and compose services."""
    used = {row[0] for row in db.query(Project.local_port).filter(Project.local_port.isnot(None)).all()}
    used |= {row[0] for row in db.query(ComposeService.local_port).filter(ComposeService.local_port.isnot(None)).all()}
    used |= {row[0] for row in db.query(ProjectVersion.local_port).filter(ProjectVersion.local_port.isnot(None)).all()}
    used |= reserved
    for port in range(settings.PORT_RANGE_START, settings.PORT_RANGE_END):
        if port not in used:
            return port
    raise HTTPException(status_code=500, detail="No free ports available in configured range")


def assert_domain_available(
    db: Session,
    domain: str,
    *,
    exclude_project_id: int | None = None,
    exclude_service_id: int | None = None,
) -> None:
    """Raise 409 if `domain` is already claimed by another component's custom domain or
    auto subdomain. The current component is excluded so re-setting its own domain is fine."""
    pq = db.query(Project).filter(
        (Project.custom_domain == domain) | (Project.subdomain == domain)
    )
    if exclude_project_id is not None:
        pq = pq.filter(Project.id != exclude_project_id)
    sq = db.query(ComposeService).filter(
        (ComposeService.custom_domain == domain) | (ComposeService.subdomain == domain)
    )
    if exclude_service_id is not None:
        sq = sq.filter(ComposeService.id != exclude_service_id)
    if pq.first() or sq.first():
        raise HTTPException(status_code=409, detail=f"Domain '{domain}' is already in use by another component")


# ── Status enrichment ───────────────────────────────────────────────────────────

def _container_status(container_name: str | None, image_name: str | None) -> str:
    if not container_name:
        return "not_found"
    status = docker_service.get_container_status(container_name)
    if status == "not_found" and image_name and not docker_service.image_exists(image_name):
        status = "no_image"
    return status


def _env_count(project: Project, service: str | None = None) -> int:
    """How many variables the project's (or one service's) stored env file defines — a
    cheap badge for clients, without echoing any values."""
    scope = service or env_service.PROJECT_SCOPE
    row = next((e for e in project.env_files if e.service_name == scope), None)
    return len(env_service.keys(row.content)) if row else 0


def _container_info(project: Project) -> dict:
    return {
        "subdomain": project.effective_domain,
        "custom_domain": project.custom_domain,
        "local_port": project.local_port,
        "container_port": project.container_port,
        "image_name": project.image_name,
        "container_name": project.container_name,
        "ssl_enabled": bool(project.ssl_enabled),
        "websocket": bool(project.websocket),
        "container_status": _container_status(project.container_name, project.image_name),
        "env_count": _env_count(project),
    }


def _service_info(svc: ComposeService) -> dict:
    return {
        "name": svc.name,
        "exposed": bool(svc.exposed),
        "subdomain": svc.effective_domain,
        "custom_domain": svc.custom_domain,
        "local_port": svc.local_port,
        "container_port": svc.container_port,
        "container_name": svc.container_name,
        "ssl_enabled": bool(svc.ssl_enabled),
        "websocket": bool(svc.websocket),
        "container_status": _container_status(svc.container_name, None),
        "env_count": _env_count(svc.project, svc.name),
    }


def project_response(project: Project) -> dict:
    """Build the API representation of a project (container for dockerfile, services for compose)."""
    return {
        "name": project.name,
        "type": project.type,
        "deploy_mode": project.deploy_mode,
        "created_at": project.created_at,
        "container": _container_info(project) if project.deploy_mode == "dockerfile" else None,
        "services": [_service_info(s) for s in project.services] if project.deploy_mode == "compose" else [],
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/", response_model=List[ProjectResponse])
def list_projects(db: Session = Depends(get_db), token=Depends(require_token)):
    """Every project for an admin token; only its own project for a guest token — a guest
    must not learn that the server's other projects exist."""
    query = db.query(Project)
    bound = guest_project_name(token)
    if bound is not None:
        query = query.filter(Project.name == bound)
    projects = query.order_by(Project.name).all()
    return [project_response(p) for p in projects]


def provision_dockerfile(
    db: Session,
    project: Project,
    *,
    container_port: int | None = None,
    domain_prefix: str | None = None,
) -> Project:
    """Wire an *existing* project as a single-container (dockerfile-mode) project: set the
    deploy mode, allocate a loopback port + subdomain + docker names (only if not already
    set), and run nginx/SSL setup. Idempotent — safe to call again on re-upload, keeping the
    existing port/subdomain. Shared by the unified upload endpoint and the plugins router.

    `container_port` is filled from the Dockerfile's EXPOSE by the upload endpoint, or passed
    from a plugin's manifest. `domain_prefix` overrides the subdomain label (plugins; "" pins
    to the apex). Raises HTTPException(500) on nginx permission errors. Commits."""
    name = project.name
    project.deploy_mode = "dockerfile"
    if project.local_port is None:
        project.local_port = _next_port(db, set())
    if not project.subdomain:
        label = name if domain_prefix is None else domain_prefix
        project.subdomain = ".".join(seg for seg in (label, settings.BASE_DOMAIN) if seg)
    project.image_name = project.image_name or f"freeholdy_{name}:latest"
    project.container_name = project.container_name or f"freeholdy_{name}"
    if container_port is not None:
        project.container_port = container_port
    db.flush()

    endpoints = [{
        "subdomain": project.effective_domain,
        "local_port": project.local_port,
        "websocket": bool(project.websocket),
    }]
    try:
        ssl_result = nginx_service.setup_nginx(name, endpoints)
    except PermissionError:
        db.rollback()
        nginx_service.remove_config(name)
        raise HTTPException(
            status_code=500,
            detail="Permission denied writing nginx config — run freeholdy with sudo or grant write access to nginx dirs",
        )
    if ssl_result.get("error"):
        # nginx -t rejected the generated config: roll back so we don't leave a broken
        # nginx config on disk paired with a "live" project row.
        db.rollback()
        nginx_service.remove_config(name)
        raise HTTPException(status_code=500, detail=ssl_result["error"])
    if ssl_result["ssl"].get(project.effective_domain, {}).get("success"):
        project.ssl_enabled = True

    db.commit()
    db.refresh(project)
    return project


def _validate_project_name(name: str) -> None:
    """Slug-validate a project name (used before we create a row or stage a chunked upload
    under a per-project dir), turning the schema's ValueError into an HTTP 422."""
    try:
        validate_project_slug(name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


def _get_or_create_project(db: Session, name: str) -> Project:
    """Return the project row named `name`, creating an empty ("pending") one if it does not
    exist yet. This is what makes `deploy` auto-create: the first upload/complete for a new
    name provisions from scratch, later ones redeploy. Flushes (caller commits)."""
    project = db.query(Project).filter(Project.name == name).first()
    if project:
        return project
    _validate_project_name(name)
    project = Project(name=name, type=ProjectType.user.value, deploy_mode="pending")
    db.add(project)
    # Commit immediately (like the former create endpoint did) so a manifest-less deploy — which
    # never reaches a provision commit — still persists the empty "pending" row, and `get_db`
    # (which does not commit) can't discard it on session close.
    db.commit()
    db.refresh(project)
    return project


def apply_deploy_env(db: Session, project: Project, env: str | None) -> bool:
    """Store dotenv text supplied with a deploy request. Returns whether anything was stored.

    A deploy is one call that creates the row, provisions, and launches the build, so there
    is no window for a separate `PUT /projects/{name}/env` before the first container is
    created. Calling this early is what makes those values reach that **first** start:
    it must run *before* `_autoprovision` (compose materializes env there, writing the
    `env_file:` entries into the generated override) and *before* `launch_deploy`
    (`bluegreen_deploy` materializes inside its own session).

    Blank or omitted text leaves any stored env untouched, so redeploying with an empty env
    box never silently wipes a project's environment; clearing is DELETE .../env.

    Flushes; the **caller commits** (same convention as `_get_or_create_project`). That is
    what lets git deploy roll the env back together with the project row it also created
    when a clone or provision fails, instead of leaving an orphan `pending` project holding
    an env file.

    Shared by the chunked upload (`upload_complete`) and git deploy (`routers/git.py`)."""
    if not (env or "").strip():
        return False
    env_service.set_content(db, project, env)
    return True


def _teardown_compose(project: Project, details: list[str], errors: list[str]) -> None:
    """Stop+remove a compose stack's containers/images and drop its on-disk directory."""
    name = project.name
    docker_service.abort_job(f"compose:{name}")
    cdir = os.path.abspath(compose_service.project_dir(name))

    # Preferred path: let docker compose tear the whole stack down (containers, networks, images).
    # The env file is passed so ${VAR} interpolation resolves the same way it did on `up` —
    # otherwise a compose file referencing a stored variable could fail to render here.
    if os.path.exists(compose_service.override_file_path(name)):
        result = subprocess.run(
            docker_service._compose_cmd(
                name, cdir, "down", "--rmi", "all",
                env_file=env_service.project_env_file(object_session(project), project),
            ),
            capture_output=True, text=True,
        )
        details.append(f"docker compose down ({'ok' if result.returncode == 0 else 'warning'})")
        if result.returncode != 0:
            errors.append(f"compose down: {result.stderr.strip()}")

    # Fallback: remove any tracked container compose left behind (e.g. a missing override file),
    # so we never delete the nginx config while a container is still running.
    for svc in project.services:
        if docker_service.get_container_status(svc.container_name) != "not_found":
            ok, msg = docker_service.remove_container(svc.container_name)
            details.append(msg)
            if not ok:
                errors.append(msg)

    # Blue/green version artifacts: retained per-version image tags + file snapshots
    # (`compose down --rmi all` only removes images the *current* compose files reference).
    # Tags are matched by prefix rather than iterating version rows, so nothing lingers
    # even if the DB and docker drifted apart.
    from app.services import deploy_service  # lazy
    removed_tags = deploy_service.remove_all_compose_version_artifacts(name)
    if removed_tags or project.versions:
        details.append(f"Removed {removed_tags} retained version image tag(s) + snapshots")

    if os.path.isdir(cdir):
        shutil.rmtree(cdir, ignore_errors=True)
        details.append(f"Compose directory '{cdir}' removed")

    env_service.remove_project(name)  # DB rows cascade off Project


def _teardown_dockerfile(project: Project, details: list[str], errors: list[str]) -> None:
    """Stop+remove a single-container project's container/image and drop its files dir.

    Also handles `pending` projects (no container/image yet): only the files dir is removed.
    Removes every blue/green version's container + image (active, inactive, and archived),
    not just the currently-active one."""
    docker_service.abort_job(deploy_job_key(project))

    # Every version's container + image (superset of the active container/image below).
    seen_containers: set[str] = set()
    seen_images: set[str] = set()
    for v in project.versions:
        if v.container_name and v.container_name not in seen_containers:
            seen_containers.add(v.container_name)
            ok, msg = docker_service.remove_container(v.container_name)
            details.append(msg)
            if not ok:
                errors.append(msg)
        if v.image_name and v.image_name not in seen_images:
            seen_images.add(v.image_name)
            ok, msg = docker_service.remove_image(v.image_name)
            details.append(msg)
            if not ok:
                errors.append(msg)

    if project.container_name and project.container_name not in seen_containers:
        ok, msg = docker_service.remove_container(project.container_name)
        details.append(msg)
        if not ok:
            errors.append(msg)
    if project.image_name and project.image_name not in seen_images:
        ok, msg = docker_service.remove_image(project.image_name)
        details.append(msg)
        if not ok:
            errors.append(msg)

    # Drop the project's files dir too — compose mode already does this; keep both modes symmetric.
    pdir = os.path.abspath(compose_service.project_dir(project.name))
    if os.path.isdir(pdir):
        shutil.rmtree(pdir, ignore_errors=True)
        details.append(f"Project directory '{pdir}' removed")

    env_service.remove_project(project.name)  # DB rows cascade off Project


def _teardown_nginx(project_name: str, details: list[str], errors: list[str]) -> None:
    """Remove a project's nginx config and reload — runs regardless of the docker outcome,
    so a project never ends up with its nginx config lingering after its container is gone."""
    try:
        nginx_service.remove_config(project_name)
        details.append(f"Nginx config 'freeholdy_{project_name}.conf' removed")
        ok, msg = nginx_service.test_config()
        if ok:
            nginx_service.reload()
            details.append("Nginx reloaded")
        else:
            errors.append(f"Nginx config test failed after removal: {msg}")
    except Exception as e:
        errors.append(f"Failed to remove nginx config: {e}")


@router.delete("/{project_name}", response_model=ProjectDeleteResponse)
def delete_project(
    project_name: str,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """Full teardown: stop+remove container(s)/image(s), drop the files dir, remove the nginx
    config, and delete the DB row. Every phase runs even if an earlier one fails, so a project
    is never left half-deleted (e.g. an nginx config still pointing at an already-removed
    container)."""
    project = db.query(Project).filter(Project.name == project_name).first()
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")

    details: list[str] = []
    errors: list[str] = []

    # 1. Docker resources (mode-specific, best-effort — failures must not block nginx/DB cleanup).
    try:
        if project.deploy_mode == "compose":
            _teardown_compose(project, details, errors)
        else:
            _teardown_dockerfile(project, details, errors)
    except Exception as e:
        errors.append(f"Docker teardown error: {e}")

    # 2. nginx config + reload (always runs).
    _teardown_nginx(project_name, details, errors)

    # 3. DB row (cascades to ComposeService and to any guest tokens bound to it).
    db.delete(project)
    db.commit()
    details.append(f"Project '{project_name}' deleted from database")

    status = "ok" if not errors else "partial"
    message = (
        f"Project '{project_name}' deleted successfully"
        if not errors
        else f"Project '{project_name}' deleted with {len(errors)} warning(s)"
    )
    return ProjectDeleteResponse(status=status, message=message, details=details)


# ── Folder upload ───────────────────────────────────────────────────────────────

def _project_files_dir(project: Project) -> str:
    """The on-disk directory that holds a project's files (one dir for both modes)."""
    return compose_service.project_dir(project.name)


def _safe_join(base_dir: str, rel_name: str) -> str:
    """Resolve a client-supplied relative path under base_dir, rejecting traversal.

    Mirrors the commonpath guard in plugin_service.get_plugin. Raises HTTPException(400)
    on absolute paths, `..` escapes, or empty names."""
    if not rel_name:
        raise HTTPException(status_code=400, detail="Empty filename in upload")
    rel = os.path.normpath(rel_name.replace("\\", "/"))
    if os.path.isabs(rel) or rel.startswith(".."):
        raise HTTPException(status_code=400, detail=f"Unsafe path '{rel_name}'")
    base_abs = os.path.abspath(base_dir)
    dest_abs = os.path.abspath(os.path.join(base_abs, rel))
    if os.path.commonpath([base_abs, dest_abs]) != base_abs:
        raise HTTPException(status_code=400, detail=f"Unsafe path '{rel_name}'")
    return dest_abs


# docker-compose.yml takes precedence over a Dockerfile when both are in the root.
_COMPOSE_MANIFESTS = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
_DOCKERFILE_MANIFEST = "Dockerfile"


def _detect_manifest(base_dir: str) -> tuple[str | None, str | None]:
    """Scan a project's root directory for a deploy manifest.

    Returns `(deploy_mode, manifest_path)`: ("compose", path) if a compose file is present
    (it wins over a Dockerfile), ("dockerfile", path) for a bare Dockerfile, or (None, None)
    when neither is found."""
    for name in _COMPOSE_MANIFESTS:
        path = os.path.join(base_dir, name)
        if os.path.isfile(path):
            return "compose", path
    dockerfile = os.path.join(base_dir, _DOCKERFILE_MANIFEST)
    if os.path.isfile(dockerfile):
        return "dockerfile", dockerfile
    return None, None


def _provision_from_dockerfile(db: Session, project: Project, dockerfile_path: str) -> None:
    """Wire a project as dockerfile-mode from a detected Dockerfile: nginx/port setup, then
    EXPOSE → container_port (400 if absent) and WebSocket detection. Commits."""
    valid, message = docker_service.validate_dockerfile(dockerfile_path)
    if not valid:
        raise HTTPException(status_code=400, detail=message)

    with open(dockerfile_path, encoding="utf-8", errors="replace") as f:
        text = f.read()

    port = scan.exposed_port(text)
    if port is None:
        raise HTTPException(
            status_code=400,
            detail="Dockerfile must EXPOSE a port (e.g. `EXPOSE 8080`) so the container "
                   "port can be determined.",
        )

    provision_dockerfile(db, project, container_port=port)
    project.dockerfile_path = dockerfile_path

    ws = scan.uses_websocket(text)
    if ws != bool(project.websocket):
        project.websocket = ws
        if project.ssl_enabled:
            nginx_service.write_ssl_config(project.name, [{
                "subdomain": project.effective_domain, "local_port": project.local_port, "websocket": ws,
            }])
    db.commit()
    db.refresh(project)


@router.post(
    "/{project_name}/upload",
    response_model=UploadResponse,
    summary="Upload a file or folder, then auto-detect a Dockerfile/compose file and provision",
)
async def upload(
    project_name: str,
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
    _=Depends(require_project_access),
):
    """Single entry point for getting files into a project. Each upload's multipart filename
    carries its path relative to the project root; the tree is recreated under the one
    per-project directory. After writing, the root is scanned for a manifest:

      - `docker-compose.yml` (or compose.yaml / compose.yml) → compose mode (wins over a
        Dockerfile), provisioned via `provision_compose`.
      - `Dockerfile` → dockerfile mode: nginx/port setup + EXPOSE → container_port + WebSocket
        detection.

    A project's mode is fixed by its first provisioning upload; uploading the other manifest
    type later is rejected (remove + recreate to change mode). Uploads with no manifest are a
    plain file sync (the mode stays as-is / "pending"). The project is created on first
    upload if it does not exist yet (deploy auto-creates)."""
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")
    project = _get_or_create_project(db, project_name)

    base_dir = _project_files_dir(project)
    os.makedirs(base_dir, exist_ok=True)

    written: list[str] = []
    for f in files:
        dest = _safe_join(base_dir, f.filename or "")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as out:
            out.write(await f.read())
        written.append(os.path.relpath(dest, os.path.abspath(base_dir)))

    provisioned, detected_mode = _autoprovision(db, project, project_name, base_dir)

    db.refresh(project)
    return _upload_response(project, project_name, written, provisioned, detected_mode)


def _autoprovision(
    db: Session, project: Project, project_name: str, base_dir: str
) -> tuple[bool, str | None]:
    """Scan base_dir's root for a manifest and provision the project if one is found.

    Shared by the legacy multipart upload and the chunked-upload `complete` endpoint.
    Returns `(provisioned, detected_mode)`. Raises 400 if the detected mode conflicts
    with the project's already-fixed deploy mode."""
    detected_mode, manifest_path = _detect_manifest(base_dir)
    if not detected_mode:
        return False, None

    if project.deploy_mode in ("dockerfile", "compose") and project.deploy_mode != detected_mode:
        raise HTTPException(
            status_code=400,
            detail=f"Project '{project_name}' is already a {project.deploy_mode} project — "
                   f"remove and recreate it to change the deploy mode.",
        )
    if detected_mode == "compose":
        # Lazy import avoids a circular import (compose router imports _next_port from here).
        from app.routers.compose import provision_compose
        with open(manifest_path, encoding="utf-8", errors="replace") as f:
            provision_compose(db, project, f.read())
    else:
        _provision_from_dockerfile(db, project, manifest_path)
    return True, detected_mode


def launch_deploy(project: Project) -> str:
    """Launch the async build + run job for an already-provisioned project, dispatching on
    its deploy mode, and return the job_key the client streams over a deploy WebSocket.

    The single place that maps (deploy_mode → deploy_service call); shared by the upload
    flow (`WS /projects/{name}/deploy`), git deploy (`WS /git/deploy/{name}`), and the
    non-interactive plugin install. Both modes run a blue/green versioned deploy: compose
    via `deploy_service.compose_bluegreen_deploy` (build-first, brief down/up switch),
    dockerfile via `deploy_service.bluegreen_deploy` (build + run + nginx switch)."""
    from app.services import deploy_service  # lazy: deploy_service imports _next_port from here

    if project.deploy_mode == "compose":
        return deploy_service.compose_bluegreen_deploy(project.id)
    return deploy_service.bluegreen_deploy(project.id)


def launch_restart(db: Session, project: Project) -> tuple[str, str]:
    """Recreate the project's container(s) from the images they already run, so the current
    environment (and nothing else) is picked up. Returns `(job_key, message)`.

    This is how an edited env file goes live: env is baked into a container when it is
    created, so it cannot change under a running one, and `docker start` on a stopped
    container reuses the environment frozen at creation. Recreating from the existing image
    avoids a rebuild entirely — no build context is read, no new version is cut, and the
    blue/green version rows are untouched.

    dockerfile → `docker rm -f` + `docker run` the active version's image on its current
    port, under the stable deploy job key so `/status`, `/abort` and `WS /{name}/deploy`
    keep working. compose → `up -d --no-build`, which recreates exactly the services whose
    `env_file` content changed, under the `compose:{name}` key used by `/compose/status`.
    """
    from app.services import deploy_service  # lazy: deploy_service imports _next_port from here

    env_files = env_service.materialize(db, project)
    env_file = env_service.project_env_file(db, project)
    project_dir = os.path.abspath(compose_service.project_dir(project.name))

    if project.deploy_mode == "compose":
        # The override is where a compose service's env is wired in, so regenerate it from
        # the service rows before `up` — otherwise a stack deployed before the env was
        # edited would come back with the old `env_file:` list (or none at all). Only the
        # override is rewritten: ports, subdomains and nginx are left exactly as provisioned.
        compose_service.write_override(
            project.name,
            [{"name": s.name, "exposed": bool(s.exposed),
              "local_port": s.local_port, "container_port": s.container_port}
             for s in project.services],
            env_files,
        )
        job_key = deploy_service.compose_restart(project.name, project_dir, env_file=env_file)
        return job_key, f"Recreating the '{project.name}' stack — poll /compose/status"

    if not project.container_name or not project.image_name:
        raise HTTPException(
            status_code=400,
            detail=f"Project '{project.name}' has no container yet — deploy it first",
        )
    job_key = deploy_service.deploy_job_key(project.name)
    docker_service.start_container(
        project.container_name, project.image_name,
        project.local_port, project.container_port, job_key, env_file=env_file,
    )
    return job_key, f"Recreating container '{project.container_name}' — poll /status"


@router.post("/{project_name}/restart", response_model=DockerJobStatusResponse,
             summary="Recreate the project's container(s) from their existing images "
                     "(no rebuild) so edited environment variables take effect")
def restart_project(project_name: str, db: Session = Depends(get_db), _=Depends(require_project_access)):
    project = db.query(Project).filter(Project.name == project_name).first()
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")
    if project.deploy_mode not in ("dockerfile", "compose"):
        raise HTTPException(
            status_code=400,
            detail=f"Project '{project_name}' has no deploy yet (deploy_mode="
                   f"{project.deploy_mode}) — nothing to restart",
        )
    job_key, message = launch_restart(db, project)
    return _deploy_job_response(job_key, message)


def deploy_job_key(project: Project) -> str:
    """The job_key a deploy WebSocket reconnects to — mirrors `launch_deploy` without
    launching anything (compose → `compose:{name}`, dockerfile → the stable base key)."""
    from app.services import deploy_service  # lazy
    return f"compose:{project.name}" if project.deploy_mode == "compose" else deploy_service.deploy_job_key(project.name)


def _deploy_job_response(job_key: str, message: str) -> DockerJobStatusResponse:
    job = docker_service.get_job(job_key)
    return DockerJobStatusResponse(
        status=job.status if job else "no_job",
        operation=job.operation if job else None,
        message=message,
        logs=docker_service.get_job_logs(job_key),
        exit_code=job.exit_code if job else None,
    )


def _upload_message(
    project: Project, project_name: str, count: int, provisioned: bool, detected_mode: str | None
) -> str:
    """Human-readable result line shared by both upload endpoints."""
    if provisioned:
        return (f"Uploaded {count} file(s); detected {detected_mode} project "
                f"and provisioned '{project_name}'")
    if project.deploy_mode in ("dockerfile", "compose"):
        return f"Uploaded {count} file(s) to '{project_name}'"
    return (f"Uploaded {count} file(s); no Dockerfile or docker-compose.yml found "
            f"in the root yet — '{project_name}' is not deployed")


def _deploy_ws_path(project_name: str) -> str:
    """The WebSocket path clients connect to to stream an upload's build + run."""
    return f"/projects/{project_name}/deploy"


def _upload_response(
    project: Project,
    project_name: str,
    written: list[str],
    provisioned: bool,
    detected_mode: str | None,
) -> UploadResponse:
    """Build the shared upload response. When a manifest was provisioned, auto-launch the
    build + run job (like git deploy) and hand back a `ws_path` the client streams; uploads
    with no manifest stay a plain file sync (no job, no ws_path)."""
    ws_path = None
    job = None
    if provisioned:
        job_key = launch_deploy(project)
        ws_path = _deploy_ws_path(project_name)
        job = _deploy_job_response(
            job_key, f"Provisioning '{project_name}' ({detected_mode}) — stream {ws_path}"
        )

    return UploadResponse(
        status="ok",
        message=_upload_message(project, project_name, len(written), provisioned, detected_mode),
        count=len(written),
        files=sorted(written),
        deploy_mode=project.deploy_mode,
        provisioned=provisioned,
        project=project_response(project) if provisioned else None,
        ws_path=ws_path,
        job=job,
    )


# ── Chunked upload (large files/folders) ─────────────────────────────────────────
#
# The client zips its selection, splits the zip into ~1 MiB pieces, and POSTs them one
# by one (raw octet-stream bodies, so each request stays under nginx's 1 MB default and
# never 413s). `complete` reassembles, safely unzips into the project dir, and provisions
# — reusing `_safe_join` (zip-slip guard) and `_autoprovision` (the same flow the legacy
# multipart upload runs).

_UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{8,}$")


def _staging_zip_path(project_name: str, upload_id: str) -> str:
    """Path of the reassembled zip for a chunked upload — outside the project dir so
    partial data is never scanned or served. Rejects a non-hex upload_id and a non-slug
    project name (both would otherwise allow path traversal via the staging dir). The name
    is slug-validated here rather than via a project-row lookup, because a chunked upload can
    now stage bytes for a not-yet-created project (deploy auto-creates on `complete`)."""
    if not _UPLOAD_ID_RE.match(upload_id):
        raise HTTPException(status_code=400, detail=f"Invalid upload_id '{upload_id}'")
    _validate_project_name(project_name)
    staging_dir = os.path.join(settings.DATA_DIR, "uploads", project_name)
    os.makedirs(staging_dir, exist_ok=True)
    return os.path.join(staging_dir, f"{upload_id}.zip")


@router.post(
    "/{project_name}/upload/chunk",
    summary="Append one piece of a chunked upload at the given byte offset",
)
async def upload_chunk(
    project_name: str,
    request: Request,
    upload_id: str,
    offset: int,
    db: Session = Depends(get_db),
    _=Depends(require_project_access),
):
    """Write one raw chunk (the request body) into the staged zip at `offset`. Offset
    addressing makes this idempotent and order-independent. The project need not exist yet —
    `_staging_zip_path` slug-validates the name; the row is created on `complete`."""
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")
    path = _staging_zip_path(project_name, upload_id)
    chunk = await request.body()
    # os.open + lseek so seeking past EOF (sparse) works on a freshly created file.
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        os.write(fd, chunk)
    finally:
        os.close(fd)
    return {"status": "ok", "received": len(chunk)}


@router.post(
    "/{project_name}/upload/complete",
    response_model=UploadResponse,
    summary="Reassemble + unzip a chunked upload, then auto-detect and provision",
)
async def upload_complete(
    project_name: str,
    request: UploadCompleteRequest,
    db: Session = Depends(get_db),
    _=Depends(require_project_access),
):
    """Finalize a chunked upload: validate the staged zip, extract every member under the
    project dir (guarded against zip-slip by `_safe_join`), provision, and clean up. Creates
    the project row if it does not exist yet (deploy auto-creates).

    An optional `env` (dotenv text) is stored before provisioning, so the first container
    this deploy starts already has those variables."""
    zip_path = _staging_zip_path(project_name, request.upload_id)
    if not os.path.isfile(zip_path):
        raise HTTPException(status_code=404, detail="No staged upload for this upload_id")
    if request.total_size is not None and os.path.getsize(zip_path) != request.total_size:
        os.remove(zip_path)
        raise HTTPException(status_code=400, detail="Staged upload is incomplete (size mismatch)")

    project = _get_or_create_project(db, project_name)
    # Before _autoprovision + launch_deploy, so the first container start already has it.
    # Committed here rather than left to the provisioning commit, because a manifest-less
    # deploy (a plain file sync) never reaches one and `get_db` does not commit on close.
    if apply_deploy_env(db, project, request.env):
        db.commit()
    base_dir = _project_files_dir(project)
    os.makedirs(base_dir, exist_ok=True)

    written: list[str] = []
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue
                dest = _safe_join(base_dir, member.filename)  # zip-slip guard
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(member) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                written.append(os.path.relpath(dest, os.path.abspath(base_dir)))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Uploaded data is not a valid zip archive")
    finally:
        if os.path.isfile(zip_path):
            os.remove(zip_path)

    provisioned, detected_mode = _autoprovision(db, project, project_name, base_dir)

    db.refresh(project)
    return _upload_response(project, project_name, written, provisioned, detected_mode)


@router.delete(
    "/{project_name}/upload/{upload_id}",
    summary="Abort a chunked upload and discard its staged data",
)
async def upload_abort(
    project_name: str,
    upload_id: str,
    db: Session = Depends(get_db),
    _=Depends(require_project_access),
):
    # The project row may not exist yet (aborting before `complete`); `_staging_zip_path`
    # slug-validates the name, which is all we need to safely locate the staged data.
    zip_path = _staging_zip_path(project_name, upload_id)
    if os.path.isfile(zip_path):
        os.remove(zip_path)
    return {"status": "ok"}


# ── Deploy over WebSocket ───────────────────────────────────────────────────────
#
# An upload that provisions a manifest auto-launches the build+run job and returns a
# `ws_path`; the client connects here to stream the build log live (no status polling).
# Read-only — a basic deploy never prompts. Same frame protocol as the git deploy and
# plugin install sockets:
#   client -> server : {"type": "auth", "token": "..."}   first frame, always
#                      {"type": "abort"}                    cancel the running build
#   server -> client : {"type": "ready"}                   auth ok, streaming starts
#                      {"type": "stdout", "data": "..."}   build + run output
#                      {"type": "exit", "code": N}          job finished
# A reconnect re-streams a running job and replays a finished one (stream_job).


async def deploy_stream_session(websocket: WebSocket, project_name: str) -> None:
    """Read-only WebSocket that tails a project's build+run job to exit. Shared by the
    upload deploy route below and the git deploy route (`routers/git.py`)."""
    await websocket.accept()

    # First frame must be auth (browsers can't set an Authorization header on a WebSocket).
    # Project-scoped: the admin tokens, plus this project's guest token (CI watching its
    # own redeploy). The exec and plugin-install sockets stay admin-only.
    if not await ws_session.authenticate(websocket, project=project_name):
        return

    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.name == project_name).first()
        if project is None:
            await ws_session.reject(
                websocket, 4404,
                f"project '{project_name}' not found — upload it first",
            )
            return
        job_key = deploy_job_key(project)
    finally:
        db.close()

    if docker_service.get_job(job_key) is None:
        await ws_session.reject(
            websocket, 4404,
            f"no deploy job for '{project_name}' — upload it first",
        )
        return

    try:
        await websocket.send_json({"type": "ready"})
        exit_code = await interactive_service.stream_job(websocket, job_key)
        if exit_code is None:
            return  # client disconnected mid-stream; the job keeps running
        await websocket.send_json({"type": "exit", "code": exit_code})
        await websocket.close(code=1000)
    except WebSocketDisconnect:
        pass


@router.websocket("/{project_name}/deploy")
async def deploy_session(websocket: WebSocket, project_name: str):
    await deploy_stream_session(websocket, project_name)
