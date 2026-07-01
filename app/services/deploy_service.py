"""
deploy_service.py — blue/green deploys with archived backups for dockerfile projects.

Every dockerfile deploy builds a new version `freeholdy_{name}:v{N}` in a container
`freeholdy_{name}_v{N}` on its own loopback port, runs it, and only then switches nginx
to it (zero downtime; a failed build never takes the running site down). The previous
version is kept as a stopped `inactive` container for fast rollback; older ones are
`archived` (image kept, container removed, port freed) up to Project.backup_limit.

Build + run happens as a docker subprocess job; the nginx switch + version bookkeeping run
as Python post-steps in a background thread. Both stream to ONE log registered under the
stable base job key `freeholdy_{name}` via docker_service's external-job registry, so the
existing `WS /projects/{name}/deploy` (interactive_service.stream_job) shows the whole thing
and abort_job() cancels the build. The base key is version-independent, so a reconnecting
deploy socket always finds the job regardless of which vN is building.
"""

import os
import shlex
import subprocess
import tempfile
import threading
from typing import Callable, Optional

from app.models.database import SessionLocal
from app.models.orm import Project, ProjectVersion
from app.services import docker_service, nginx_service, compose_service


def deploy_job_key(name: str) -> str:
    """The stable, version-independent job key a dockerfile deploy/rollback registers under
    (and that `WS /projects/{name}/deploy` reconnects to). Never collides with a version
    container `freeholdy_{name}_v{N}` or an exec key `exec:{name}`."""
    return f"freeholdy_{name}"


def version_names(name: str, n: int) -> tuple[str, str]:
    """(image_name, container_name) for version N of a project."""
    return f"freeholdy_{name}:v{n}", f"freeholdy_{name}_v{n}"


# ── Build/run + rollback scripts ────────────────────────────────────────────────

def _deploy_script(
    project_dir: str,
    install_script: Optional[str],
    plugin_dir: Optional[str],
    image_name: str,
    container_name: str,
    local_port: int,
    container_port: int,
) -> list:
    """optional install.sh → docker build → docker run, as one bash job (mirrors
    docker_service.provision_from_plugin, but with version-scoped names)."""
    q = shlex.quote
    dockerfile = os.path.join(project_dir, "Dockerfile")
    lines = ["set -e"]
    if install_script and os.path.exists(install_script):
        lines += [
            'echo "── running install.sh ──"',
            f"cd {q(project_dir)}",
            f"PLUGIN_DIR={q(plugin_dir or project_dir)} PROJECT_DIR={q(project_dir)} bash {q(install_script)}",
        ]
    lines += [
        f'echo "── docker build {image_name} ──"',
        f"docker build --progress=plain -t {q(image_name)} -f {q(dockerfile)} {q(project_dir)}",
        f'echo "── docker run {container_name} ──"',
        f"docker rm -f {q(container_name)} >/dev/null 2>&1 || true",
        f"docker run --detach --name {q(container_name)} --restart unless-stopped "
        f"-p 127.0.0.1:{int(local_port)}:{int(container_port)} {q(image_name)}",
    ]
    return ["bash", "-c", "\n".join(lines)]


def _rollback_script(
    target_status: str,
    image_name: str,
    container_name: str,
    local_port: int,
    container_port: int,
) -> list:
    """Bring a rollback target's container up: `docker start` a stopped inactive container,
    or `docker run` a fresh one from an archived image (its container was removed)."""
    q = shlex.quote
    if target_status == "inactive":
        lines = ["set -e", f'echo "── starting {container_name} ──"', f"docker start {q(container_name)}"]
    else:  # archived — recreate from the retained image
        lines = [
            "set -e",
            f'echo "── recreating {container_name} from {image_name} ──"',
            f"docker rm -f {q(container_name)} >/dev/null 2>&1 || true",
            f"docker run --detach --name {q(container_name)} --restart unless-stopped "
            f"-p 127.0.0.1:{int(local_port)}:{int(container_port)} {q(image_name)}",
        ]
    return ["bash", "-c", "\n".join(lines)]


# ── Job spawning + finalize ─────────────────────────────────────────────────────

def _spawn(job_key: str, cmd: list, verify_container: str,
           promote: Callable[[Callable[[str], None]], None],
           cleanup: Optional[Callable[[], None]]) -> str:
    """Popen `cmd` streaming to one log registered under job_key, then run `promote`
    (nginx switch + DB bookkeeping) in a background thread once the build/run succeeds and
    `verify_container` is running. Returns immediately with job_key."""
    log_fd = tempfile.NamedTemporaryFile(
        mode="w", suffix=".log", prefix="freeholdy_deploy_", delete=False,
    )
    proc = subprocess.Popen(cmd, stdout=log_fd, stderr=subprocess.STDOUT, text=True)
    docker_service.register_external_job(job_key, "deploy", cmd, proc, log_fd.name)
    threading.Thread(
        target=_finalize,
        args=(job_key, proc, log_fd, verify_container, promote, cleanup),
        daemon=True,
    ).start()
    return job_key


def _finalize(job_key, proc, log_fd, verify_container, promote, cleanup) -> None:
    exit_code = proc.wait()
    path = log_fd.name
    try:
        log_fd.flush()
        log_fd.close()
    except Exception:
        pass

    def log(msg: str) -> None:
        try:
            with open(path, "a") as f:
                f.write(msg if msg.endswith("\n") else msg + "\n")
        except OSError:
            pass

    job = docker_service.get_job(job_key)
    if job is not None and job.status == "aborted":
        log("── aborted — current version kept ──")
        if cleanup:
            cleanup()
        docker_service.finish_external_job(job_key, exit_code, aborted=True)
        return
    if exit_code != 0:
        log(f"── failed (exit {exit_code}) — current version kept ──")
        if cleanup:
            cleanup()
        docker_service.finish_external_job(job_key, exit_code)
        return
    status = docker_service.get_container_status(verify_container)
    if status != "running":
        log(f"── container '{verify_container}' is not running ({status}) — keeping current version ──")
        if cleanup:
            cleanup()
        docker_service.finish_external_job(job_key, 1)
        return
    try:
        promote(log)
        docker_service.finish_external_job(job_key, 0)
    except Exception as e:  # never leave the job stuck "running"
        log(f"── post-deploy error: {e} ──")
        docker_service.finish_external_job(job_key, 1)


# ── Version bookkeeping (runs inside promote, own DB session) ────────────────────

def _switch_nginx(project: Project, port: int, log: Callable[[str], None]) -> None:
    endpoint = {
        "subdomain": project.effective_domain,
        "local_port": port,
        "websocket": bool(project.websocket),
    }
    log(f"── switching nginx → 127.0.0.1:{port} ──")
    if project.ssl_enabled:
        nginx_service.write_ssl_config(project.name, [endpoint])
    else:
        nginx_service.write_http_config(project.name, [endpoint])


def _demote_others(db, project: Project, keep_version: int, log: Callable[[str], None]) -> None:
    """Old active → inactive (stopped); old inactive → archived (container removed, port
    freed). Then prune archived beyond backup_limit."""
    for v in list(project.versions):
        if v.version == keep_version:
            continue
        if v.status == "active":
            log(f"── stopping v{v.version} (now inactive) ──")
            docker_service.stop_container_sync(v.container_name)
            v.status = "inactive"
        elif v.status == "inactive":
            log(f"── archiving v{v.version} (removing container, keeping image) ──")
            docker_service.remove_container(v.container_name)
            v.local_port = None
            v.status = "archived"
    _prune_archived(db, project, log)


def _prune_archived(db, project: Project, log: Callable[[str], None]) -> None:
    archived = sorted(
        [v for v in project.versions if v.status == "archived"], key=lambda v: v.version
    )
    excess = len(archived) - project.backup_limit
    for v in archived[: max(0, excess)]:
        log(f"── pruning archived v{v.version} (limit {project.backup_limit}) ──")
        docker_service.remove_container(v.container_name)
        docker_service.remove_image(v.image_name)
        db.delete(v)


def enforce_backup_limit(project_id: int) -> None:
    """Prune archived versions beyond Project.backup_limit right now (used when the limit
    is lowered via PUT /backup-limit). Synchronous — `docker rmi` the oldest images."""
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if project is not None:
            _prune_archived(db, project, lambda _msg: None)
            db.commit()
    finally:
        db.close()


# ── Public entry points ─────────────────────────────────────────────────────────

def bluegreen_deploy(project_id: int, install_script: Optional[str] = None,
                     plugin_dir: Optional[str] = None) -> str:
    """Build + run the next version of a dockerfile project, then switch nginx to it and
    shuffle/prune older versions. Returns the deploy job key (stream over
    `WS /projects/{name}/deploy`). The first deploy reuses the port nginx was already set up
    on (no switch); re-deploys allocate a fresh green port and switch after the build.

    `install_script`/`plugin_dir` come from the plugins router so plugin installs also
    create version 1 through this single path."""
    from app.routers.projects import _next_port  # lazy: projects imports this module

    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        name = project.name
        n = project.version_counter + 1
        first = project.version_counter == 0
        image_name, container_name = version_names(name, n)
        green_port = project.local_port if first else _next_port(db, set())
        project_dir = os.path.abspath(compose_service.project_dir(name))
        container_port = project.container_port
    finally:
        db.close()

    job_key = deploy_job_key(name)
    cmd = _deploy_script(
        project_dir, install_script, plugin_dir,
        image_name, container_name, green_port, container_port,
    )

    def promote(log: Callable[[str], None]) -> None:
        db2 = SessionLocal()
        try:
            p = db2.query(Project).filter(Project.id == project_id).first()
            db2.add(ProjectVersion(
                project_id=p.id, version=n, image_name=image_name,
                container_name=container_name, local_port=green_port, status="active",
            ))
            p.version_counter = n
            if not first:
                _switch_nginx(p, green_port, log)
            p.local_port = green_port
            p.container_name = container_name
            p.image_name = image_name
            db2.flush()
            _demote_others(db2, p, keep_version=n, log=log)
            db2.commit()
            log(f"── v{n} is now active ──")
        finally:
            db2.close()

    def cleanup() -> None:
        docker_service.remove_container(container_name)
        docker_service.remove_image(image_name)

    return _spawn(job_key, cmd, container_name, promote, cleanup)


def rollback_to_version(project_id: int, version: int) -> str:
    """Make an existing inactive/archived version active again: bring its container up
    (`docker start` for inactive, `docker run` from the retained image for archived), switch
    nginx to it, and re-shuffle statuses (does NOT bump version_counter). Streams over the
    same `WS /projects/{name}/deploy`."""
    from app.routers.projects import _next_port  # lazy

    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        name = project.name
        container_port = project.container_port
        target = next((v for v in project.versions if v.version == version), None)
        target_status = target.status
        image_name = target.image_name
        container_name = target.container_name
        # inactive → reuse its reserved port; archived → allocate a fresh one.
        new_port = None
        if target_status == "inactive":
            target_port = target.local_port
        else:
            new_port = _next_port(db, set())
            target_port = new_port
    finally:
        db.close()

    job_key = deploy_job_key(name)
    cmd = _rollback_script(target_status, image_name, container_name, target_port, container_port)

    def promote(log: Callable[[str], None]) -> None:
        db2 = SessionLocal()
        try:
            p = db2.query(Project).filter(Project.id == project_id).first()
            tgt = next(v for v in p.versions if v.version == version)
            if new_port is not None:
                tgt.local_port = new_port
            port = tgt.local_port
            _switch_nginx(p, port, log)
            p.local_port = port
            p.container_name = tgt.container_name
            p.image_name = tgt.image_name
            tgt.status = "active"
            db2.flush()
            _demote_others(db2, p, keep_version=version, log=log)
            db2.commit()
            log(f"── rolled back to v{version} ──")
        finally:
            db2.close()

    return _spawn(job_key, cmd, container_name, promote, cleanup=None)
