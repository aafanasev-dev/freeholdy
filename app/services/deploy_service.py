"""
deploy_service.py — blue/green deploys with archived backups (both deploy modes).

Dockerfile mode: every deploy builds a new version `freeholdy_{name}:v{N}` in a container
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

Compose mode: true side-by-side blue/green is impossible for a stack in general (stacks
bind host ports themselves, some use host networking, named volumes are shared), so compose
deploys are **build-first with a brief switch**: build + pull run while the old stack keeps
serving (a failed build never takes the site down), each service's resolved image is
retagged `freeholdy_{name}_{service}:v{N}` and pinned via a generated
`docker-compose.images.yml`, then a short `down` + `up -d --no-build` switches over
(seconds; images already local). A version keeps its pinned images plus a file snapshot of
the whole project dir under `{DATA_DIR}/versions/{name}/v{N}/` (outside PROJECTS_DIR, which
git redeploys wipe). Statuses are `active` | `archived` only — `down` removes containers,
so there is no stopped-container "inactive" tier — and rollback restores the snapshot,
re-provisions, and brings the pinned images back up. Named volumes are shared across
versions: rollback restores code + images, not data. Compose jobs register under the
existing key `compose:{name}` so all status/abort endpoints and deploy WebSockets work
unchanged.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
from typing import Callable, Iterable, Optional

import yaml
from fastapi import HTTPException

from app.config import settings
from app.models.database import SessionLocal
from app.models.orm import Project, ProjectVersion
from app.services import docker_service, nginx_service, compose_service, env_service


def deploy_job_key(name: str) -> str:
    """The stable, version-independent job key a dockerfile deploy/rollback registers under
    (and that `WS /projects/{name}/deploy` reconnects to). Never collides with a version
    container `freeholdy_{name}_v{N}` or an exec key `exec:{name}`."""
    return f"freeholdy_{name}"


def version_names(name: str, n: int) -> tuple[str, str]:
    """(image_name, container_name) for version N of a project."""
    return f"freeholdy_{name}:v{n}", f"freeholdy_{name}_v{n}"


# ── Build/run + rollback scripts ────────────────────────────────────────────────

def _env_flag(env_file: Optional[str]) -> str:
    """`--env-file <path> ` for a `docker run` command line, or "" when there is no env."""
    if not env_file or not os.path.exists(env_file):
        return ""
    return f"--env-file {shlex.quote(env_file)} "


def _deploy_script(
    project_dir: str,
    install_script: Optional[str],
    plugin_dir: Optional[str],
    image_name: str,
    container_name: str,
    local_port: int,
    container_port: int,
    env_file: Optional[str] = None,
) -> list:
    """optional install.sh → docker build → docker run, as one bash job (mirrors
    docker_service.provision_from_plugin, but with version-scoped names).

    `env_file` is the project's materialized env file (`env_service.materialize`); when
    present it is passed to `docker run --env-file` so the container starts with the
    variables stored in the DB. None → the command is exactly what it was before."""
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
        f"{_env_flag(env_file)}"
        f"-p 127.0.0.1:{int(local_port)}:{int(container_port)} {q(image_name)}",
    ]
    return ["bash", "-c", "\n".join(lines)]


def _rollback_script(
    target_status: str,
    image_name: str,
    container_name: str,
    local_port: int,
    container_port: int,
    env_file: Optional[str] = None,
) -> list:
    """Bring a rollback target's container up by recreating it from its retained image.

    Both tiers take the same path. An `inactive` version's container is merely stopped and
    could be `docker start`ed, but a stopped container's environment is frozen at the
    moment it was created — restarting it that way would silently ignore any env edited
    since. Recreating costs only the container's writable layer (which an `archived`
    rollback already discards) and keeps one rule true everywhere: a container start uses
    the current environment."""
    q = shlex.quote
    what = "restarting" if target_status == "inactive" else "recreating"
    lines = [
        "set -e",
        f'echo "── {what} {container_name} from {image_name} ──"',
        f"docker rm -f {q(container_name)} >/dev/null 2>&1 || true",
        f"docker run --detach --name {q(container_name)} --restart unless-stopped "
        f"{_env_flag(env_file)}"
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


def _backup_after_deploy(project_id: int, version: int,
                         log: Callable[[str], None]) -> None:
    """Take a backup of the version just deployed, when the project asked for one.

    The backup runs on its own job key (it only reads docker), so it is launched and left to
    run rather than awaited — the deploy is already finished and must not be held open, nor
    reported as failed if a backup destination is down."""
    from app.services import backup_service   # lazy: backup_service imports this module
    try:
        if backup_service.after_deploy(project_id, version):
            log(f"── on-deploy backup of v{version} started ──")
    except Exception as exc:
        log(f"── on-deploy backup could not start: {exc} ──")


def _restore_version_data(project_id: int, backup_id: int, job_key: str,
                          log: Callable[[str], None]) -> None:
    """Put a version's volume contents and env back from a backup archive, as the tail of a
    rollback job.

    Runs *inside* the rollback's promote step, so it shares the one job, one log and one exit
    frame the client is already streaming — rather than being a second job under the same key
    that would end the stream early. By this point the rolled-back containers are up, so the
    restore does its own stop → wipe+extract → start; that is also why it reads the project
    row fresh, after promote committed the new active container name.

    Never raises: a rollback that succeeded must not be reported as failed because its
    optional data restore could not find its archive.
    """
    from app.services import backup_service   # lazy: backup_service imports this module

    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        row = next((b for b in project.backups if b.id == backup_id), None) if project else None
        if project is None or row is None or row.status != "ok":
            log(f"── no usable backup {backup_id} — volume data left as it is ──")
            return
        name, mode, container_name = project.name, project.deploy_mode, project.container_name
        tar_path = backup_service.backup_path(name, row.filename)
    finally:
        db.close()

    if not os.path.exists(tar_path):
        log(f"── backup archive is gone from disk ({tar_path}) — volume data left as it is ──")
        return

    job = docker_service.get_job(job_key)
    log_path = job.log_path if job is not None else _new_log_file()
    work_dir = os.path.join(settings.DATA_DIR, "backups", name,
                            f".restore-{os.urandom(8).hex()}")
    project_dir = os.path.abspath(compose_service.project_dir(name))
    try:
        backup_service.run_restore_phases(project_id, name, mode, container_name, tar_path,
                                          project_dir, work_dir, job_key, log_path, log)
    except Exception as exc:
        log(f"── data restore failed: {exc} — the version itself is active ──")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def enforce_backup_limit(project_id: int) -> None:
    """Prune archived versions beyond Project.backup_limit right now (used when the limit
    is lowered via PUT /backup-limit). Synchronous — `docker rmi` the oldest images."""
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if project is not None:
            if project.deploy_mode == "compose":
                _compose_prune_archived(db, project, lambda _msg: None)
            else:
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
        # Write the DB-stored env to disk so the new container starts with it.
        env_service.materialize(db, project)
        env_file = env_service.project_env_file(db, project)
    finally:
        db.close()

    job_key = deploy_job_key(name)
    cmd = _deploy_script(
        project_dir, install_script, plugin_dir,
        image_name, container_name, green_port, container_port,
        env_file=env_file,
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
        _backup_after_deploy(project_id, n, log)

    def cleanup() -> None:
        docker_service.remove_container(container_name)
        docker_service.remove_image(image_name)

    return _spawn(job_key, cmd, container_name, promote, cleanup)


def rollback_to_version(project_id: int, version: int,
                       data_backup_id: Optional[int] = None) -> str:
    """Make an existing inactive/archived version active again: recreate its container from
    the retained image (with the project's current environment), switch nginx to it, and
    re-shuffle statuses (does NOT bump version_counter). Streams over the same
    `WS /projects/{name}/deploy`.

    `data_backup_id` additionally restores that backup archive's volume contents and env
    files once the version is up — the only way a rollback touches data, and off unless the
    caller asks for it."""
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
        env_service.materialize(db, project)
        env_file = env_service.project_env_file(db, project)
    finally:
        db.close()

    job_key = deploy_job_key(name)
    cmd = _rollback_script(target_status, image_name, container_name, target_port,
                           container_port, env_file=env_file)

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
        if data_backup_id is not None:
            _restore_version_data(project_id, data_backup_id, job_key, log)

    return _spawn(job_key, cmd, container_name, promote, cleanup=None)


# ════════════════════════════════════════════════════════════════════════════════
# Compose mode — build-first blue/green with archived backups
# ════════════════════════════════════════════════════════════════════════════════

PIN_FILE = "docker-compose.images.yml"


def compose_job_key(name: str) -> str:
    """The job key compose deploys/rollbacks register under — the same `compose:{name}`
    the compose lifecycle endpoints and deploy WebSockets already use."""
    return f"compose:{name}"


def compose_version_tag(project: str, service: str, n: int) -> str:
    """The retained per-service image tag for version N of a compose project. Project and
    service names are DNS slugs (no `_`), so the `freeholdy_{project}_` prefix never
    collides with another project's tags or with dockerfile-mode `freeholdy_{name}:vN`."""
    return f"freeholdy_{project}_{service}:v{n}"


def compose_versions_root(project: str) -> str:
    return os.path.join(settings.DATA_DIR, "versions", project)


def compose_snapshot_dir(project: str, n: int) -> str:
    """Per-version file snapshot (compose file, override, pin file, .env, bind-mounted
    assets). Lives outside PROJECTS_DIR because git redeploys wipe the project dir."""
    return os.path.join(compose_versions_root(project), f"v{n}")


def _compose_files_cmd(name: str, project_dir: str, *args: str, pins: bool = False,
                       env_file: Optional[str] = None) -> list:
    """A `docker compose` invocation on the project's compose + override files, optionally
    adding the generated image-pin override so `up` runs exactly the retained v{N} tags.

    `env_file` is the project-level materialized env file; it feeds `${VAR}` interpolation
    (see `docker_service.compose_env_flags` for why the project's own `.env` tags along).
    Injection into the containers is separate — the override carries `env_file:` entries."""
    cmd = [
        "docker", "compose", "-p", name,
        *docker_service.compose_env_flags(project_dir, env_file),
        "-f", os.path.join(project_dir, "docker-compose.yml"),
        "-f", os.path.join(project_dir, "docker-compose.override.yml"),
    ]
    if pins:
        cmd += ["-f", os.path.join(project_dir, PIN_FILE)]
    return cmd + list(args)


def _new_log_file() -> str:
    fd = tempfile.NamedTemporaryFile(
        mode="w", suffix=".log", prefix="freeholdy_deploy_", delete=False,
    )
    fd.close()
    return fd.name


def _append_log(path: str, msg: str) -> None:
    try:
        with open(path, "a") as f:
            f.write(msg if msg.endswith("\n") else msg + "\n")
    except OSError:
        pass


def _aborted(job_key: str) -> bool:
    job = docker_service.get_job(job_key)
    return job is not None and job.status == "aborted"


def _run_phase(job_key: str, log_path: str, cmd: list) -> int:
    """Run one subprocess phase of a multi-phase job, appending its output to the shared
    job log and pointing the job at it so abort_job kills the right process."""
    with open(log_path, "a") as log_fd:
        proc = subprocess.Popen(cmd, stdout=log_fd, stderr=subprocess.STDOUT, text=True)
        docker_service.update_job_process(job_key, proc)
        return proc.wait()


def _resolve_compose_services(name: str, project_dir: str,
                              env_file: Optional[str] = None) -> dict:
    """Resolve the stack's services via `docker compose config`: service → {image, build}.

    `image` is the tag the service will run after build/pull: the (env-substituted)
    `image:` from the compose files, or compose v2's default `{project}-{service}` build
    tag when only `build:` is given. Raises RuntimeError on a config failure."""
    result = subprocess.run(
        _compose_files_cmd(name, project_dir, "config", "--format", "json", env_file=env_file),
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"docker compose config failed: {result.stderr.strip()}")
    doc = json.loads(result.stdout)
    services = {}
    for svc, spec in (doc.get("services") or {}).items():
        spec = spec or {}
        services[str(svc)] = {
            "image": spec.get("image") or f"{name}-{svc}",
            "build": "build" in spec,
        }
    if not services:
        raise RuntimeError("docker compose config returned no services")
    return services


def _write_pin_file(project_dir: str, name: str, n: int, service_names: Iterable[str]) -> None:
    """Write docker-compose.images.yml pinning every service to its v{N} tag."""
    pins = {svc: {"image": compose_version_tag(name, svc, n)} for svc in sorted(service_names)}
    with open(os.path.join(project_dir, PIN_FILE), "w") as f:
        f.write("# Generated by freeholdy — pins this deploy's images. Do not edit manually.\n")
        yaml.safe_dump({"services": pins}, f, default_flow_style=False, sort_keys=False)


def _compose_version_image_tags(name: str, version: Optional[int]) -> list:
    """All retained `freeholdy_{name}_{service}:v{version}` tags currently on disk,
    discovered from `docker images` (no stored per-service list is needed). With
    version=None, matches every version's tags for the project."""
    result = subprocess.run(
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    suffix = r"v\d+" if version is None else f"v{int(version)}"
    pattern = re.compile(
        rf"^freeholdy_{re.escape(name)}_[a-z0-9][a-z0-9-]*:{suffix}$"
    )
    return [line.strip() for line in result.stdout.splitlines() if pattern.match(line.strip())]


def remove_compose_version_artifacts(name: str, version: int) -> None:
    """Remove one compose version's retained image tags + file snapshot (prune)."""
    for tag in _compose_version_image_tags(name, version):
        docker_service.remove_image(tag)
    shutil.rmtree(compose_snapshot_dir(name, version), ignore_errors=True)


def remove_all_compose_version_artifacts(name: str) -> int:
    """Project teardown: remove every retained version tag (matched by prefix, so tags
    survive even if DB rows and docker drifted apart) + the whole snapshots dir.
    Returns the number of image tags removed."""
    tags = _compose_version_image_tags(name, None)
    for tag in tags:
        docker_service.remove_image(tag)
    shutil.rmtree(compose_versions_root(name), ignore_errors=True)
    return len(tags)


def _compose_prune_archived(db, project: Project, log: Callable[[str], None]) -> None:
    archived = sorted(
        [v for v in project.versions if v.status == "archived"], key=lambda v: v.version
    )
    excess = len(archived) - project.backup_limit
    for v in archived[: max(0, excess)]:
        log(f"── pruning archived v{v.version} (limit {project.backup_limit}) ──")
        remove_compose_version_artifacts(project.name, v.version)
        db.delete(v)


def _compose_verify(project: Project, log: Callable[[str], None]) -> bool:
    """Post-`up` sanity check: every exposed service's container must be running; an
    unexposed one must at least exist (one-shot helpers may exit by design)."""
    ok = True
    for s in project.services:
        status = docker_service.get_container_status(s.container_name)
        if (s.exposed and status != "running") or (not s.exposed and status == "not_found"):
            log(f"── service '{s.name}' ({s.container_name}) is {status} ──")
            ok = False
    return ok


def _compose_promote(db, project: Project, n: int, project_dir: str,
                     log: Callable[[str], None]) -> None:
    """Version bookkeeping after a successful switch: snapshot the project dir, record
    v{n} active, archive the previous active, prune beyond backup_limit. Commits."""
    snap = compose_snapshot_dir(project.name, n)
    shutil.rmtree(snap, ignore_errors=True)
    os.makedirs(os.path.dirname(snap), exist_ok=True)
    shutil.copytree(project_dir, snap, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    log(f"── snapshot saved to {snap} ──")

    db.add(ProjectVersion(project_id=project.id, version=n, status="active"))
    project.version_counter = max(project.version_counter, n)
    db.flush()
    for v in list(project.versions):
        if v.version != n and v.status in ("active", "inactive"):
            log(f"── archiving v{v.version} (images + snapshot kept) ──")
            v.status = "archived"
            v.local_port = None
    _compose_prune_archived(db, project, log)
    db.commit()


def _compose_recover_previous(name: str, prev_version: int,
                              job_key: str, log_path: str,
                              log: Callable[[str], None]) -> None:
    """Best-effort keep-alive after a failed switchover `up`: bring the previous version
    back up straight from its snapshot dir (its own compose/override/pin files, so bind
    mounts resolve to the snapshot's copies of the old files)."""
    snap = compose_snapshot_dir(name, prev_version)
    if not os.path.isdir(snap):
        log(f"── no snapshot for v{prev_version} — cannot recover automatically ──")
        return
    log(f"── recovering previous v{prev_version} from {snap} ──")
    code = _run_phase(job_key, log_path, _compose_files_cmd(name, snap, "up", "-d", "--no-build", pins=True))
    log(f"── recovery {'succeeded — v%d is serving again' % prev_version if code == 0 else 'FAILED — stack is down'} ──")


def compose_bluegreen_deploy(project_id: int) -> str:
    """Deploy the next version of a compose project: build + pull while the old stack keeps
    serving (a failed build never takes the site down), retag each service's image as
    `freeholdy_{name}_{svc}:v{N}`, then a brief `down` + `up -d --no-build` switchover on
    the pinned images. Registers under the existing `compose:{name}` job key; stream over
    `WS /projects/{name}/deploy`. nginx needs no switch — ports were fixed at provision."""
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        name = project.name
        n = project.version_counter + 1
        prev = next((v for v in project.versions if v.status == "active"), None)
        prev_version = prev.version if prev else None
        # Write the DB-stored env to disk before anything reads the compose files —
        # `docker compose config` (step 1 of the job) resolves ${VAR} from it.
        env_service.materialize(db, project)
        env_file = env_service.project_env_file(db, project)
    finally:
        db.close()

    project_dir = os.path.abspath(compose_service.project_dir(name))
    job_key = compose_job_key(name)
    log_path = _new_log_file()
    docker_service.register_external_job(
        job_key, "deploy", ["compose-bluegreen-deploy", name], None, log_path,
    )
    threading.Thread(
        target=_compose_deploy_job,
        args=(project_id, name, n, prev_version, project_dir, job_key, log_path, env_file),
        daemon=True,
    ).start()
    return job_key


def _compose_deploy_job(project_id: int, name: str, n: int, prev_version: Optional[int],
                        project_dir: str, job_key: str, log_path: str,
                        env_file: Optional[str] = None) -> None:
    def log(msg: str) -> None:
        _append_log(log_path, msg)

    green_tags: list = []

    def fail(exit_code: int, msg: str, cleanup_tags: bool = True) -> None:
        log(msg)
        if cleanup_tags:
            for tag in green_tags:
                docker_service.remove_image(tag)
        docker_service.finish_external_job(job_key, exit_code, aborted=_aborted(job_key))

    try:
        # 1. Resolve the stack's services + target images (env-substituted).
        log(f"── resolving services for v{n} ──")
        services = _resolve_compose_services(name, project_dir, env_file)

        # 2. Build — the old stack keeps serving through the long part.
        log("── docker compose build ──")
        code = _run_phase(job_key, log_path, _compose_files_cmd(name, project_dir, "build", "--progress=plain", env_file=env_file))
        if _aborted(job_key) or code != 0:
            fail(code or 1, f"── build failed (exit {code}) — current version kept ──")
            return

        # 3. Pull registry images for services without a build context.
        pull_images = sorted({s["image"] for s in services.values() if not s["build"]})
        if pull_images:
            log("── docker pull ──")
            script = "\n".join(["set -e"] + [f"docker pull {shlex.quote(img)}" for img in pull_images])
            code = _run_phase(job_key, log_path, ["bash", "-c", script])
            if _aborted(job_key) or code != 0:
                fail(code or 1, f"── pull failed (exit {code}) — current version kept ──")
                return

        # 4. Retag every service's image as v{n} and write the pin override.
        log(f"── tagging images as v{n} ──")
        for svc, spec in services.items():
            tag = compose_version_tag(name, svc, n)
            result = subprocess.run(["docker", "tag", spec["image"], tag], capture_output=True, text=True)
            if result.returncode != 0:
                fail(1, f"── tagging {spec['image']} → {tag} failed: {result.stderr.strip()} — current version kept ──")
                return
            green_tags.append(tag)
        _write_pin_file(project_dir, name, n, services.keys())
        if _aborted(job_key):
            fail(1, "── aborted — current version kept ──")
            return

        # 5. Brief switchover: down the old stack, up the pinned new one.
        log("── switching: docker compose down ──")
        code = _run_phase(job_key, log_path, _compose_files_cmd(name, project_dir, "down", env_file=env_file))
        if _aborted(job_key) or code != 0:
            fail(code or 1, f"── down failed (exit {code}) ──")
            return
        log(f"── switching: docker compose up (v{n}) ──")
        code = _run_phase(job_key, log_path, _compose_files_cmd(name, project_dir, "up", "-d", "--no-build", pins=True, env_file=env_file))
        if _aborted(job_key) or code != 0:
            log(f"── up failed (exit {code}) ──")
            if prev_version is not None:
                _compose_recover_previous(name, prev_version, job_key, log_path, log)
            fail(code or 1, "── deploy failed ──")
            return

        # 6. Verify + promote (snapshot, version rows, prune).
        db = SessionLocal()
        try:
            project = db.query(Project).filter(Project.id == project_id).first()
            healthy = _compose_verify(project, log)
            _compose_promote(db, project, n, project_dir, log)
        finally:
            db.close()
        if not healthy:
            log(f"── v{n} deployed but some services are not running — "
                f"roll back with: fhcli rollback {name} {prev_version if prev_version is not None else '<version>'} ──")
            docker_service.finish_external_job(job_key, 1)
            return
        log(f"── v{n} is now active ──")
        _backup_after_deploy(project_id, n, log)
        docker_service.finish_external_job(job_key, 0)
    except Exception as e:  # never leave the job stuck "running"
        fail(1, f"── deploy error: {e} ──")


def compose_restart(name: str, project_dir: str, env_file: Optional[str] = None) -> str:
    """Recreate a compose stack's containers on the images it already runs, so an edited
    environment is picked up without rebuilding anything. Returns the job key.

    `up -d --no-build` on the generated pin file, exactly like a deploy's switchover `up`:
    compose recreates only the services whose resolved config changed (the `env_file:`
    contents included) and leaves the rest running. Pinning matters — without it `up` would
    re-resolve `image:`/`build:` from the compose file and could pull or build something
    other than the version currently active. A stack deployed before pinning existed has no
    pin file; there `--no-build` alone still guarantees no rebuild."""
    pins = os.path.exists(os.path.join(project_dir, PIN_FILE))
    cmd = _compose_files_cmd(name, project_dir, "up", "-d", "--no-build",
                             pins=pins, env_file=env_file)
    return docker_service.spawn_job(compose_job_key(name), "compose_up", cmd)


# ── Volume restore ──────────────────────────────────────────────────────────────

def _run_stdin_phase(job_key: str, log_path: str, cmd: list, stdin_path: str) -> int:
    """Like `_run_phase`, but feeds a file to the process on stdin — how a staged tar
    reaches `tar xf -` inside the helper container."""
    with open(log_path, "a") as log_fd, open(stdin_path, "rb") as src:
        proc = subprocess.Popen(cmd, stdin=src, stdout=log_fd, stderr=subprocess.STDOUT, text=True)
        docker_service.update_job_process(job_key, proc)
        return proc.wait()


def volume_restore(project_id: int, volume: str, tar_path: str) -> str:
    """Replace a volume's contents with a staged tar: stop the containers, wipe + extract,
    start them again. Returns the job key.

    Registered under the project's own job key (`compose:{name}` / `freeholdy_{name}`), so
    `/status`, `/compose/status`, `/abort` and `WS /projects/{name}/deploy` work on it like
    any deploy, and a restore can never run concurrently with one.

    The stack is stopped first because the archive typically holds a live database file —
    extracting under a running SQLite/Postgres is how you get a corrupt one. The dockerfile
    path deliberately `docker start`s the *same* container rather than recreating it (the
    recreate that `launch_restart` does): an anonymous volume — the kind an image's `VOLUME`
    instruction creates — belongs to the container that created it, so `docker rm` + `run`
    would hand the new container a fresh empty volume and orphan the one just restored.
    """
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        name = project.name
        mode = project.deploy_mode
        container_name = project.container_name
        env_file = env_service.project_env_file(db, project)
    finally:
        db.close()

    project_dir = os.path.abspath(compose_service.project_dir(name))
    job_key = compose_job_key(name) if mode == "compose" else deploy_job_key(name)
    log_path = _new_log_file()
    docker_service.register_external_job(
        job_key, "volume_restore", ["volume-restore", name, volume], None, log_path,
    )
    threading.Thread(
        target=_volume_restore_job,
        args=(name, mode, container_name, volume, tar_path, project_dir, job_key, log_path, env_file),
        daemon=True,
    ).start()
    return job_key


def _volume_restore_job(name: str, mode: str, container_name: Optional[str], volume: str,
                        tar_path: str, project_dir: str, job_key: str, log_path: str,
                        env_file: Optional[str] = None) -> None:
    from app.services import volume_service  # lazy: keeps the import graph one-directional

    def log(msg: str) -> None:
        _append_log(log_path, msg)

    def fail(exit_code: int, msg: str) -> None:
        log(msg)
        docker_service.finish_external_job(job_key, exit_code, aborted=_aborted(job_key))

    try:
        size = os.path.getsize(tar_path)
        log(f"── restoring volume '{volume}' from a {size} byte archive ──")

        # 1. Stop whatever is using the volume, so nothing writes during the swap.
        log("── stopping containers ──")
        if mode == "compose":
            code = _run_phase(job_key, log_path, _compose_files_cmd(name, project_dir, "stop", env_file=env_file))
        elif container_name:
            code = _run_phase(job_key, log_path, ["docker", "stop", "--time", "10", container_name])
        else:
            code = 0
        if _aborted(job_key):
            fail(code or 1, "── aborted before the volume was touched ──")
            return
        if code != 0:
            fail(code, f"── stop failed (exit {code}) — volume left untouched ──")
            return

        # 2. Wipe + extract. Past this point the volume's old contents are gone, so the
        #    containers are brought back up regardless of the outcome.
        log("── wiping volume and extracting archive ──")
        code = _run_stdin_phase(job_key, log_path, volume_service.restore_cmd(volume), tar_path)
        restore_code = code
        volume_service.forget_sizes([volume])
        if code != 0:
            log(f"── extract failed (exit {code}) — starting containers anyway ──")

        # 3. Bring the project back.
        log("── starting containers ──")
        if mode == "compose":
            pins = os.path.exists(os.path.join(project_dir, PIN_FILE))
            code = _run_phase(job_key, log_path,
                              _compose_files_cmd(name, project_dir, "up", "-d", "--no-build",
                                                 pins=pins, env_file=env_file))
        elif container_name:
            code = _run_phase(job_key, log_path, ["docker", "start", container_name])
        else:
            code = 0
        if code != 0:
            fail(code, f"── containers failed to start (exit {code}) ──")
            return
        if restore_code != 0:
            fail(restore_code, f"── restore failed (exit {restore_code}); containers are back up ──")
            return
        log(f"── volume '{volume}' restored ──")
        docker_service.finish_external_job(job_key, 0)
    except Exception as e:   # never leave the job stuck "running"
        fail(1, f"── volume restore error: {e} ──")
    finally:
        try:
            os.unlink(tar_path)
        except OSError:
            pass


def compose_missing_images(name: str, project_dir: str,
                          env_file: Optional[str] = None) -> list[str]:
    """Services whose image is absent — exactly what an `up --no-build` would fail on.

    A restart (and the `up` that ends a volume restore) never builds, so on a project whose
    deploy never got as far as producing images it would take the stack down and leave it
    down, reporting docker's bare `No such image: {project}-{service}:latest`. Callers use
    this to refuse with something a user can act on.

    Resolves what the next `up` would actually use: the pinned `freeholdy_{name}_{svc}:v{N}`
    tags when `PIN_FILE` exists, else each service's resolved image from
    `_resolve_compose_services` (compose's default `{project}-{service}` for a build-only
    service). Checking images rather than "does the project have version rows" is deliberate:
    a stack deployed before versioning existed, whose startup backfill did not run, has no
    rows but perfectly good images, and must keep restarting.

    Never raises — if the stack cannot be resolved at all (a broken compose file, docker
    down), it returns [] and lets the caller proceed exactly as it did before."""
    pin_path = os.path.join(project_dir, PIN_FILE)
    images: dict[str, str] = {}
    try:
        if os.path.exists(pin_path):
            with open(pin_path) as f:
                doc = yaml.safe_load(f) or {}
            for svc, spec in (doc.get("services") or {}).items():
                image = (spec or {}).get("image")
                if image:
                    images[str(svc)] = str(image)
        if not images:
            images = {svc: spec["image"]
                      for svc, spec in _resolve_compose_services(name, project_dir, env_file).items()}
    except (OSError, yaml.YAMLError, RuntimeError, ValueError):
        return []
    return sorted(svc for svc, image in images.items() if not docker_service.image_exists(image))


def compose_rollback_to_version(project_id: int, version: int,
                               data_backup_id: Optional[int] = None) -> str:
    """Make an archived compose version active again: `down` the current stack, restore the
    version's file snapshot into the project dir, re-provision (service rows, ports,
    override, nginx/SSL — subdomains and custom domains are preserved), then `up` on the
    retained v{N} image tags. Streams over the same `WS /projects/{name}/deploy`.
    Does NOT bump version_counter (mirrors the dockerfile rollback).

    `data_backup_id` additionally restores that archive's volume contents and env once the
    stack is up — named-volume data is otherwise deliberately left untouched by a rollback."""
    db = SessionLocal()
    try:
        name = db.query(Project).filter(Project.id == project_id).first().name
    finally:
        db.close()

    project_dir = os.path.abspath(compose_service.project_dir(name))
    job_key = compose_job_key(name)
    log_path = _new_log_file()
    docker_service.register_external_job(
        job_key, "rollback", ["compose-rollback", name, f"v{version}"], None, log_path,
    )
    threading.Thread(
        target=_compose_rollback_job,
        args=(project_id, name, version, project_dir, job_key, log_path, data_backup_id),
        daemon=True,
    ).start()
    return job_key


def _compose_rollback_job(project_id: int, name: str, version: int,
                          project_dir: str, job_key: str, log_path: str,
                          data_backup_id: Optional[int] = None) -> None:
    from app.routers.compose import provision_compose  # lazy: router imports services

    def log(msg: str) -> None:
        _append_log(log_path, msg)

    def fail(exit_code: int, msg: str) -> None:
        log(msg)
        docker_service.finish_external_job(job_key, exit_code, aborted=_aborted(job_key))

    def current_env_file() -> Optional[str]:
        """(Re)materialize the project's env and return the project-level file. Env is not
        rolled back with the code — the DB is always the source of truth — so this is read
        fresh, and again after step 3 re-provisions the service rows."""
        db = SessionLocal()
        try:
            project = db.query(Project).filter(Project.id == project_id).first()
            env_service.materialize(db, project)
            return env_service.project_env_file(db, project)
        finally:
            db.close()

    try:
        snap = compose_snapshot_dir(name, version)
        env_file = current_env_file()

        # 1. Down the current stack (with the current files, while they're still on disk).
        if os.path.exists(compose_service.override_file_path(name)):
            log("── docker compose down (current version) ──")
            code = _run_phase(job_key, log_path, _compose_files_cmd(name, project_dir, "down", env_file=env_file))
            if _aborted(job_key) or code != 0:
                fail(code or 1, f"── down failed (exit {code}) — rollback stopped ──")
                return

        # 2. Restore the snapshot into the project dir.
        log(f"── restoring v{version} snapshot ──")
        shutil.rmtree(project_dir, ignore_errors=True)
        shutil.copytree(snap, project_dir, symlinks=True)

        # 3. Re-provision: rebuild service rows/ports/override + nginx/SSL from the restored
        #    compose file (handles a service set that differed in v{version}).
        log("── re-provisioning services + nginx ──")
        db = SessionLocal()
        try:
            project = db.query(Project).filter(Project.id == project_id).first()
            with open(compose_service.compose_file_path(name)) as f:
                compose_text = f.read()
            provision_compose(db, project, compose_text)
        except HTTPException as e:
            fail(1, f"── re-provision failed: {e.detail} ──")
            return
        finally:
            db.close()

        # 4. Up on the retained v{version} tags (the snapshot carries its own pin file).
        #    Re-provisioning rewrote the override, so pick the env paths up again.
        env_file = current_env_file()
        log(f"── docker compose up (v{version}) ──")
        code = _run_phase(job_key, log_path, _compose_files_cmd(name, project_dir, "up", "-d", "--no-build", pins=True, env_file=env_file))
        if _aborted(job_key) or code != 0:
            fail(code or 1, f"── up failed (exit {code}) ──")
            return

        # 5. Promote: target active, old active archived, prune.
        db = SessionLocal()
        try:
            project = db.query(Project).filter(Project.id == project_id).first()
            healthy = _compose_verify(project, log)
            target = next(v for v in project.versions if v.version == version)
            target.status = "active"
            db.flush()
            for v in list(project.versions):
                if v.version != version and v.status in ("active", "inactive"):
                    log(f"── archiving v{v.version} ──")
                    v.status = "archived"
                    v.local_port = None
            _compose_prune_archived(db, project, log)
            db.commit()
        finally:
            db.close()
        log(f"── rolled back to v{version} ──")
        if data_backup_id is not None:
            _restore_version_data(project_id, data_backup_id, job_key, log)
        docker_service.finish_external_job(job_key, 0 if healthy else 1)
    except Exception as e:  # never leave the job stuck "running"
        fail(1, f"── rollback error: {e} ──")


def backfill_compose_versions() -> int:
    """Give pre-versioning compose projects an immediate rollback point: for each compose
    project with no versions yet whose containers exist, retag each service's current image
    as v1, write the pin file, snapshot the project dir, and record an active v1 row.
    Idempotent + best-effort (skips projects whose containers or files are missing); runs
    once at startup (mirrors migrate_db.sh's dockerfile v1 backfill, which pure SQL can't
    do for compose — this needs docker + the filesystem). Returns projects backfilled."""
    backfilled = 0
    db = SessionLocal()
    try:
        projects = (
            db.query(Project)
            .filter(Project.deploy_mode == "compose", Project.version_counter == 0)
            .all()
        )
        for project in projects:
            try:
                if project.versions or not project.services:
                    continue
                name = project.name
                project_dir = os.path.abspath(compose_service.project_dir(name))
                if not os.path.exists(compose_service.compose_file_path(name)):
                    continue
                # Every service's container must exist so its image can be pinned.
                images = {}
                for s in project.services:
                    result = subprocess.run(
                        ["docker", "inspect", "--format", "{{.Image}}", s.container_name],
                        capture_output=True, text=True,
                    )
                    if result.returncode != 0:
                        images = None
                        break
                    images[s.name] = result.stdout.strip()
                if not images:
                    continue
                for svc, image_id in images.items():
                    result = subprocess.run(
                        ["docker", "tag", image_id, compose_version_tag(name, svc, 1)],
                        capture_output=True, text=True,
                    )
                    if result.returncode != 0:
                        raise RuntimeError(result.stderr.strip())
                _write_pin_file(project_dir, name, 1, images.keys())
                snap = compose_snapshot_dir(name, 1)
                shutil.rmtree(snap, ignore_errors=True)
                os.makedirs(os.path.dirname(snap), exist_ok=True)
                shutil.copytree(project_dir, snap, symlinks=True, ignore=shutil.ignore_patterns(".git"))
                db.add(ProjectVersion(project_id=project.id, version=1, status="active"))
                project.version_counter = 1
                db.commit()
                backfilled += 1
            except Exception:
                db.rollback()  # best-effort: a broken project must not block startup
    finally:
        db.close()
    return backfilled
