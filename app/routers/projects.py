import os
import shutil
import subprocess

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from typing import List

from app.models.database import get_db
from app.models.orm import Project, ComposeService
from app.models.schemas import (
    ProjectCreateRequest,
    ProjectResponse,
    ProjectDeleteResponse,
    UploadResponse,
    ProjectType,
)
from app.auth import require_auth
from app.config import settings
from app.services import docker_service, nginx_service, compose_service, scan

router = APIRouter()


def _next_port(db: Session, reserved: set[int]) -> int:
    """First free loopback port, considering both dockerfile projects and compose services."""
    used = {row[0] for row in db.query(Project.local_port).filter(Project.local_port.isnot(None)).all()}
    used |= {row[0] for row in db.query(ComposeService.local_port).all()}
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
    }


def _service_info(svc: ComposeService) -> dict:
    return {
        "name": svc.name,
        "subdomain": svc.effective_domain,
        "custom_domain": svc.custom_domain,
        "local_port": svc.local_port,
        "container_port": svc.container_port,
        "container_name": svc.container_name,
        "ssl_enabled": bool(svc.ssl_enabled),
        "websocket": bool(svc.websocket),
        "container_status": _container_status(svc.container_name, None),
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
def list_projects(db: Session = Depends(get_db), _=Depends(require_auth)):
    projects = db.query(Project).order_by(Project.name).all()
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


@router.post("/", response_model=ProjectResponse, status_code=201)
def create_project(
    request: ProjectCreateRequest,
    db: Session = Depends(get_db),
    _=Depends(require_auth),
):
    """Create an empty project. Its deploy mode stays "pending" until the first upload, where
    a Dockerfile / docker-compose.yml in the uploaded root selects dockerfile vs compose and
    triggers nginx/port provisioning."""
    if db.query(Project).filter(Project.name == request.name).first():
        raise HTTPException(status_code=409, detail=f"Project '{request.name}' already exists")
    project = Project(name=request.name, type=ProjectType.user.value, deploy_mode="pending")
    db.add(project)
    db.commit()
    db.refresh(project)
    return project_response(project)


def _teardown_compose(project: Project, details: list[str], errors: list[str]) -> None:
    """Stop+remove a compose stack's containers/images and drop its on-disk directory."""
    name = project.name
    docker_service.abort_job(f"compose:{name}")
    cdir = os.path.abspath(compose_service.project_dir(name))

    # Preferred path: let docker compose tear the whole stack down (containers, networks, images).
    if os.path.exists(compose_service.override_file_path(name)):
        result = subprocess.run(
            docker_service._compose_cmd(name, cdir, "down", "--rmi", "all"),
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

    if os.path.isdir(cdir):
        shutil.rmtree(cdir, ignore_errors=True)
        details.append(f"Compose directory '{cdir}' removed")


def _teardown_dockerfile(project: Project, details: list[str], errors: list[str]) -> None:
    """Stop+remove a single-container project's container/image and drop its files dir.

    Also handles `pending` projects (no container/image yet): only the files dir is removed."""
    if project.container_name:
        docker_service.abort_job(project.container_name)
        ok, msg = docker_service.remove_container(project.container_name)
        details.append(msg)
        if not ok:
            errors.append(msg)
    if project.image_name:
        ok, msg = docker_service.remove_image(project.image_name)
        details.append(msg)
        if not ok:
            errors.append(msg)

    # Drop the project's files dir too — compose mode already does this; keep both modes symmetric.
    pdir = os.path.abspath(compose_service.project_dir(project.name))
    if os.path.isdir(pdir):
        shutil.rmtree(pdir, ignore_errors=True)
        details.append(f"Project directory '{pdir}' removed")


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
    _=Depends(require_auth),
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

    # 3. DB row (cascades to ComposeService).
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
    _=Depends(require_auth),
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
    plain file sync (the mode stays as-is / "pending")."""
    project = db.query(Project).filter(Project.name == project_name).first()
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    base_dir = _project_files_dir(project)
    os.makedirs(base_dir, exist_ok=True)

    written: list[str] = []
    for f in files:
        dest = _safe_join(base_dir, f.filename or "")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as out:
            out.write(await f.read())
        written.append(os.path.relpath(dest, os.path.abspath(base_dir)))

    detected_mode, manifest_path = _detect_manifest(base_dir)

    provisioned = False
    if detected_mode:
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
        provisioned = True

    if provisioned:
        message = (f"Uploaded {len(written)} file(s); detected {detected_mode} project "
                   f"and provisioned '{project_name}'")
    elif project.deploy_mode in ("dockerfile", "compose"):
        message = f"Uploaded {len(written)} file(s) to '{project_name}'"
    else:
        message = (f"Uploaded {len(written)} file(s); no Dockerfile or docker-compose.yml found "
                   f"in the root yet — '{project_name}' is not deployed")

    db.refresh(project)
    return UploadResponse(
        status="ok",
        message=message,
        count=len(written),
        files=sorted(written),
        deploy_mode=project.deploy_mode,
        provisioned=provisioned,
        project=project_response(project) if provisioned else None,
    )
