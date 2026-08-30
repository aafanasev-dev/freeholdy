"""
backup_service.py — build, ship, import and restore backup archives.

A **backup** is one self-contained tar holding everything needed to bring a project back on a
different box: its version's image(s), its volumes' contents, its env files, its project tree
and a manifest describing all of it. The freeholdy database gets the same treatment under the
*system scope* (`project_id IS NULL`), which is why every function here takes an optional
project.

    manifest.json
    images/{slug}.tar.gz        docker save | gzip  (compose: one per service)
    volumes/{volume}.tar.gz     volume_service.tar_volume output, gzipped
    env/_project.env, env/svc-{svc}.env
    project/…                   the project tree (compose: the version's snapshot)

The two invariants worth knowing before reading further:

  * **Importing a backup mints a version.** `import_backup` loads the archive's images under
    the *next* version number, writes an `archived` `ProjectVersion` (plus, for compose, the
    file snapshot the rollback path restores from) and stops. Activating it is then the
    ordinary, unmodified `deploy_service` rollback — so the Versions panel remains the single
    answer to "which build is live", and a backup needs no second activation mechanism.

  * **Job keys differ by direction.** *Creating* a backup only reads docker (`docker save`, a
    `tar` in the volume helper container), exactly like the existing volume *download*, so it
    runs under its own `backup:{name}` key and may overlap a deploy. *Importing* or
    *restoring data* recreates containers, so it registers under the project's own deploy key
    — the trick `deploy_service.volume_restore` uses, which makes `/status`, `/abort` and
    `WS /projects/{name}/deploy` work on it for free and makes it mutually exclusive with a
    deploy.

One caveat to state plainly wherever this is surfaced: **volumes are captured as they are at
backup time**, not as they were when the version was deployed. Docker has no per-version
volume state, so a backup of an older version pairs that version's image with today's data.
"""

import gzip
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import threading
from datetime import datetime

from app.config import settings
from app.models.database import SessionLocal
from app.models.orm import Backup, BackupConfig, Project, ProjectVersion
from app.services import (
    backup_targets, compose_service, deploy_service, docker_service, env_service,
    volume_service,
)

FORMAT_VERSION = 1
SYSTEM_SCOPE = "_system"          # directory + job-key name for the database scope
MANIFEST = "manifest.json"
DB_MEMBER = "freeholdy.db.gz"


# ── Paths and keys ──────────────────────────────────────────────────────────────

def scope_name(project: Project | None) -> str:
    return project.name if project is not None else SYSTEM_SCOPE


def backup_dir(scope: str) -> str:
    d = os.path.join(settings.DATA_DIR, "backups", scope)
    os.makedirs(d, exist_ok=True)
    return d


def backup_path(scope: str, filename: str) -> str:
    """Absolute path of an archive. `filename` always comes from a DB row this server wrote,
    but it is basenamed anyway so a hand-edited row can never escape the directory."""
    return os.path.join(backup_dir(scope), os.path.basename(filename))


def create_job_key(scope: str) -> str:
    """Creating a backup is read-only w.r.t. docker, so it gets its own key and may run
    alongside a deploy — unlike an import, which uses the project's deploy key."""
    return f"backup:{scope}"


def restore_job_key(project: Project) -> str:
    """Imports and data restores recreate containers, so they take the project's own deploy
    key and become mutually exclusive with a deploy."""
    if project.deploy_mode == "compose":
        return deploy_service.compose_job_key(project.name)
    return deploy_service.deploy_job_key(project.name)


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-._" else "-" for c in text)


def _timestamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d-%H%M%S")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _freeholdy_version() -> str:
    try:
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(repo_root, "version.json")) as f:
            return json.load(f).get("version", "")
    except (OSError, ValueError):
        return ""


# ── Config rows ─────────────────────────────────────────────────────────────────

def get_config(db, project: Project | None) -> BackupConfig:
    """The scope's config row, created with defaults on first access. SQLite treats NULLs as
    distinct in a UNIQUE index, so the system row's uniqueness is maintained here rather than
    by the constraint."""
    project_id = project.id if project is not None else None
    query = db.query(BackupConfig)
    query = (query.filter(BackupConfig.project_id == project_id) if project_id is not None
             else query.filter(BackupConfig.project_id.is_(None)))
    config = query.first()
    if config is None:
        config = BackupConfig(project_id=project_id, keep_local=settings.DEFAULT_BACKUP_KEEP)
        db.add(config)
        db.flush()
    return config


def list_backups(db, project: Project | None) -> list:
    project_id = project.id if project is not None else None
    query = db.query(Backup)
    query = (query.filter(Backup.project_id == project_id) if project_id is not None
             else query.filter(Backup.project_id.is_(None)))
    return query.order_by(Backup.created_at.desc(), Backup.id.desc()).all()


# ── Archive assembly ────────────────────────────────────────────────────────────

def _gzip_stream_to(cmd: list, dest_path: str, log) -> bool:
    """Run `cmd` with its stdout gzipped into `dest_path`. Used for `docker save`, whose
    output is a tar we never want to hold in memory or unzipped on disk."""
    try:
        with gzip.open(dest_path, "wb") as out:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            while True:
                chunk = proc.stdout.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
            proc.stdout.close()
            stderr = proc.stderr.read().decode(errors="replace")
            proc.stderr.close()
            code = proc.wait()
    except OSError as exc:
        log(f"  ! {exc}")
        return False
    if code != 0:
        log(f"  ! {' '.join(cmd[:3])} failed: {stderr.strip()}")
        return False
    return True


def _gzip_file(src_path: str, dest_path: str) -> None:
    with open(src_path, "rb") as src, gzip.open(dest_path, "wb") as out:
        shutil.copyfileobj(src, out, 1024 * 1024)


def _gunzip_file(src_path: str, dest_path: str) -> None:
    with gzip.open(src_path, "rb") as src, open(dest_path, "wb") as out:
        shutil.copyfileobj(src, out, 1024 * 1024)


def _version_images(project: Project, version_row: ProjectVersion | None) -> list:
    """The image tags an archive of this version should hold, as `[(service_or_None, tag)]`.

    Dockerfile: the version's own `freeholdy_{name}:v{N}`. Compose: every retained
    `freeholdy_{name}_{svc}:v{N}` tag, discovered the same way pruning discovers them (by
    listing docker, not by reading rows — per-service tags are deterministic but not stored).
    """
    if project.deploy_mode == "compose":
        if version_row is None:
            return []
        tags = deploy_service._compose_version_image_tags(project.name, version_row.version)
        out = []
        for tag in tags:
            service = tag.split(":")[0][len(f"freeholdy_{project.name}_"):]
            out.append((service, tag))
        return sorted(out)
    if version_row is not None and version_row.image_name:
        return [(None, version_row.image_name)]
    if project.image_name:
        return [(None, project.image_name)]
    return []


def _resolve_version(project: Project, version: int | None) -> ProjectVersion | None:
    if version is not None:
        return next((v for v in project.versions if v.version == version), None)
    active = next((v for v in project.versions if v.status == "active"), None)
    if active is not None:
        return active
    return max(project.versions, key=lambda v: v.version, default=None)


def _project_source_dir(project: Project, version_row: ProjectVersion | None) -> str:
    """What to archive as `project/`. A compose version keeps a file snapshot — that is the
    tree its rollback restores, so it is the honest thing to capture; anything else (and any
    dockerfile project) archives the live project dir."""
    if project.deploy_mode == "compose" and version_row is not None:
        snap = deploy_service.compose_snapshot_dir(project.name, version_row.version)
        if os.path.isdir(snap):
            return snap
    return compose_service.project_dir(project.name)


def _build_project_archive(work_dir: str, project: Project, version_row, include_volumes: bool,
                           log) -> dict:
    """Fill `work_dir` with the archive members and return the manifest dict."""
    manifest = {
        "format_version": FORMAT_VERSION,
        "scope": "project",
        "project": project.name,
        "deploy_mode": project.deploy_mode,
        "version": version_row.version if version_row else None,
        "created_at": datetime.utcnow().isoformat(),
        "freeholdy_version": _freeholdy_version(),
        "container_port": project.container_port,
        "subdomain": project.subdomain,
        "custom_domain": project.custom_domain,
        "websocket": bool(project.websocket),
        "git_url": project.git_url,
        "git_branch": project.git_branch,
        "services": [
            {"name": s.name, "exposed": bool(s.exposed), "container_port": s.container_port,
             "websocket": bool(s.websocket)}
            for s in project.services
        ],
        "images": [],
        "volumes": [],
        "env": [],
    }

    # ── images ──
    images = _version_images(project, version_row)
    if images:
        os.makedirs(os.path.join(work_dir, "images"), exist_ok=True)
        for service, tag in images:
            if not docker_service.image_exists(tag):
                log(f"  ~ image {tag} is gone — skipped")
                continue
            member = f"images/{_slug(tag)}.tar.gz"
            log(f"── saving image {tag} ──")
            if not _gzip_stream_to(["docker", "save", tag], os.path.join(work_dir, member), log):
                raise RuntimeError(f"docker save failed for {tag}")
            manifest["images"].append({
                "service": service, "tag": tag, "file": member,
                "bytes": os.path.getsize(os.path.join(work_dir, member)),
            })
    else:
        log("── no images to save (project has never been built) ──")

    # ── volumes ──
    if include_volumes:
        vols = [v for v in volume_service.list_project_volumes(project, with_sizes=False)
                if v["exists"] and not v["external"]]
        if vols:
            os.makedirs(os.path.join(work_dir, "volumes"), exist_ok=True)
        for vol in vols:
            member = f"volumes/{_slug(vol['name'])}.tar.gz"
            raw = os.path.join(work_dir, f".{_slug(vol['name'])}.tar")
            log(f"── archiving volume {vol['name']} ──")
            ok, message = volume_service.tar_volume(vol["name"], raw)
            if not ok:
                log(f"  ! {message}")
                if os.path.exists(raw):
                    os.remove(raw)
                raise RuntimeError(message)
            _gzip_file(raw, os.path.join(work_dir, member))
            os.remove(raw)
            manifest["volumes"].append({
                "name": vol["name"], "label": vol["label"], "file": member,
                # An anonymous volume's name is a random 64-hex id docker minted for the
                # container that created it, so it means nothing anywhere else — a restore
                # matches those by mount target instead. See `match_volumes`.
                "anonymous": bool(vol["anonymous"]),
                "bytes": os.path.getsize(os.path.join(work_dir, member)),
            })
        log(f"── {len(manifest['volumes'])} volume(s) archived ──")
    else:
        log("── volumes skipped (include_volumes=false) ──")

    # ── env ──
    env_source = env_service.env_dir(project.name)
    if os.path.isdir(env_source):
        dest = os.path.join(work_dir, "env")
        os.makedirs(dest, exist_ok=True)
        for entry in sorted(os.listdir(env_source)):
            src = os.path.join(env_source, entry)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(dest, entry))
                manifest["env"].append({"file": f"env/{entry}"})
        if manifest["env"]:
            log(f"── {len(manifest['env'])} env file(s) included ──")

    # ── project tree ──
    source = _project_source_dir(project, version_row)
    if os.path.isdir(source):
        shutil.copytree(source, os.path.join(work_dir, "project"), symlinks=True,
                        ignore=shutil.ignore_patterns(".git"))
        manifest["has_project"] = True
        log(f"── project files copied from {source} ──")
    else:
        manifest["has_project"] = False

    return manifest


def _build_database_archive(work_dir: str, log) -> dict:
    db_path = os.path.join(settings.DATA_DIR, "freeholdy.db")
    if not os.path.exists(db_path):
        raise RuntimeError(f"database not found at {db_path}")
    staged = os.path.join(work_dir, "freeholdy.db")
    # `.backup` takes a consistent copy of a live database; a plain file copy can catch it
    # mid-write. Fall back when the sqlite3 CLI is absent (it is not a hard dependency).
    used = "sqlite3 .backup"
    try:
        result = subprocess.run(["sqlite3", db_path, f".backup '{staged}'"],
                                capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip())
    except (FileNotFoundError, subprocess.TimeoutExpired, RuntimeError) as exc:
        log(f"  ~ sqlite3 .backup unavailable ({exc}) — falling back to a file copy")
        shutil.copy2(db_path, staged)
        used = "file copy"
    _gzip_file(staged, os.path.join(work_dir, DB_MEMBER))
    os.remove(staged)
    log(f"── database captured via {used} ──")
    return {
        "format_version": FORMAT_VERSION,
        "scope": "database",
        "created_at": datetime.utcnow().isoformat(),
        "freeholdy_version": _freeholdy_version(),
        "database": DB_MEMBER,
        "method": used,
    }


def _tar_work_dir(work_dir: str, dest_path: str) -> None:
    with tarfile.open(dest_path, "w") as tar:
        for entry in sorted(os.listdir(work_dir)):
            tar.add(os.path.join(work_dir, entry), arcname=entry)


# ── Retention ───────────────────────────────────────────────────────────────────

def prune_local(db, project: Project | None, log) -> None:
    """Delete `ok` archives beyond `keep_local`, oldest first — the same shape as
    `deploy_service._prune_archived`. Rows in `error` are kept: they carry the failure
    message a user still needs to read."""
    config = get_config(db, project)
    scope = scope_name(project)
    kept = [b for b in list_backups(db, project) if b.status == "ok"]
    for old in sorted(kept, key=lambda b: (b.created_at or datetime.min, b.id))[:max(0, len(kept) - config.keep_local)]:
        log(f"── pruning local backup {old.filename} (keep_local {config.keep_local}) ──")
        try:
            os.remove(backup_path(scope, old.filename))
        except OSError:
            pass
        db.delete(old)


def prune_remote(db, project: Project | None, log) -> None:
    """Trim the destination down to `keep_remote` (0 = never prune there). Only files this
    server named are considered, so a shared destination holding other things is safe."""
    config = get_config(db, project)
    if not config.target_name or config.keep_remote < 1:
        return
    target = backup_targets.get_target(config.target_name)
    if target is None:
        return
    ok, listing = backup_targets.list_remote(target)
    if not ok:
        log(f"  ~ cannot list remote for pruning: {listing}")
        return
    prefix = f"{scope_name(project)}-"
    ours = sorted(n for n in listing if n.startswith(prefix) and n.endswith(".tar"))
    for name in ours[:max(0, len(ours) - config.keep_remote)]:
        removed, message = backup_targets.delete_remote(target, name)
        log(f"── pruning remote {name}: {message}" if removed
            else f"  ~ remote prune failed for {name}: {message}")


# ── Create ──────────────────────────────────────────────────────────────────────

def create_backup(project_id: int | None, kind: str = "manual", version: int | None = None,
                  include_volumes: bool | None = None, upload: bool | None = None) -> str:
    """Build an archive for a project (or the database when `project_id` is None) and,
    when a target is configured, ship it. Returns the job key to stream over
    `WS /projects/{name}/backup`.

    Read-only towards docker, so it registers under its own `backup:{scope}` key and may run
    while a deploy is in flight.
    """
    db = SessionLocal()
    try:
        project = (db.query(Project).filter(Project.id == project_id).first()
                   if project_id is not None else None)
        if project_id is not None and project is None:
            raise ValueError(f"project {project_id} not found")
        scope = scope_name(project)
        config = get_config(db, project)
        if include_volumes is None:
            include_volumes = bool(config.include_volumes)
        if upload is None:
            upload = bool(config.target_name)
        version_row = _resolve_version(project, version) if project is not None else None
        resolved_version = version_row.version if version_row is not None else None
        if project is not None and version is not None and version_row is None:
            raise ValueError(f"version {version} not found for project '{project.name}'")
        # A project deploy needs its env on disk to be archived; materialize is idempotent.
        if project is not None:
            env_service.materialize(db, project)
        suffix = f"-v{resolved_version}" if resolved_version else ""
        filename = f"{scope}{suffix}-{_timestamp()}.fhbak.tar"
        row = Backup(
            project_id=project_id, version=resolved_version, kind=kind, filename=filename,
            status="creating", target_name=config.target_name if upload else None,
            remote_status="pending" if upload and config.target_name else "none",
        )
        db.add(row)
        db.commit()
        backup_id = row.id
    finally:
        db.close()

    job_key = create_job_key(scope)
    log_path = deploy_service._new_log_file()
    docker_service.register_external_job(
        job_key, "backup", ["backup", scope], None, log_path,
    )
    threading.Thread(
        target=_create_job,
        args=(project_id, backup_id, scope, filename, resolved_version, include_volumes,
              upload, job_key, log_path),
        daemon=True,
    ).start()
    return job_key


def _create_job(project_id, backup_id, scope, filename, version, include_volumes, upload,
                job_key, log_path) -> None:
    def log(msg: str) -> None:
        deploy_service._append_log(log_path, msg)

    work_dir = os.path.join(backup_dir(scope), f".work-{os.urandom(8).hex()}")
    dest = backup_path(scope, filename)
    os.makedirs(work_dir, exist_ok=True)
    db = SessionLocal()
    try:
        row = db.query(Backup).filter(Backup.id == backup_id).first()
        project = (db.query(Project).filter(Project.id == project_id).first()
                   if project_id is not None else None)
        log(f"── backup of {scope} → {filename} ──")
        if project is not None:
            version_row = _resolve_version(project, version)
            manifest = _build_project_archive(work_dir, project, version_row,
                                              include_volumes, log)
        else:
            manifest = _build_database_archive(work_dir, log)

        with open(os.path.join(work_dir, MANIFEST), "w") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
        log("── packing archive ──")
        _tar_work_dir(work_dir, dest)

        row.size_bytes = os.path.getsize(dest)
        row.sha256 = _sha256(dest)
        row.has_images = bool(manifest.get("images"))
        row.has_volumes = bool(manifest.get("volumes"))
        row.has_env = bool(manifest.get("env"))
        row.has_project = bool(manifest.get("has_project") or manifest.get("database"))
        row.status = "ok"
        row.message = f"{row.size_bytes} bytes"
        db.commit()
        log(f"── archive complete: {row.size_bytes} bytes, sha256 {row.sha256[:16]}… ──")

        if upload and row.target_name:
            target = backup_targets.get_target(row.target_name)
            if target is None:
                row.remote_status = "error"
                row.message = f"target '{row.target_name}' is not declared in .env"
                log(f"  ! {row.message}")
            else:
                log(f"── uploading to {target['type']} target '{target['name']}' ──")
                ok, result = backup_targets.upload(target, dest, filename)
                row.remote_status = "ok" if ok else "error"
                if ok:
                    row.remote_path = result
                    log(f"── uploaded → {result} ──")
                else:
                    row.message = result
                    log(f"  ! {result}")
            db.commit()

        prune_local(db, project, log)
        prune_remote(db, project, log)
        db.commit()
        docker_service.finish_external_job(job_key, 0)
        log("── backup done ──")
    except Exception as exc:               # a backup must never take the server down
        db.rollback()
        row = db.query(Backup).filter(Backup.id == backup_id).first()
        if row is not None:
            row.status = "error"
            row.message = str(exc)[:2000]
            db.commit()
        log(f"── backup failed: {exc} ──")
        try:
            os.remove(dest)
        except OSError:
            pass
        docker_service.finish_external_job(job_key, 1)
    finally:
        db.close()
        shutil.rmtree(work_dir, ignore_errors=True)


# ── Import (an uploaded archive becomes an archived version) ────────────────────

def read_manifest(tar_path: str) -> dict:
    """The archive's manifest, or a ValueError naming what is wrong with the file."""
    if not tarfile.is_tarfile(tar_path):
        raise ValueError("not a tar archive")
    with tarfile.open(tar_path, "r") as tar:
        try:
            member = tar.extractfile(MANIFEST)
        except KeyError:
            member = None
        if member is None:
            raise ValueError(f"archive has no {MANIFEST} — not a freeholdy backup")
        try:
            manifest = json.loads(member.read().decode())
        except ValueError as exc:
            raise ValueError(f"{MANIFEST} is not valid JSON: {exc}")
    if manifest.get("format_version") != FORMAT_VERSION:
        raise ValueError(
            f"unsupported backup format_version {manifest.get('format_version')} "
            f"(this server reads {FORMAT_VERSION})"
        )
    return manifest


def _safe_extract(tar: tarfile.TarFile, prefix: str, dest_dir: str) -> list:
    """Extract every member under `prefix/` into `dest_dir`, refusing anything whose resolved
    path escapes it — the zip-slip guard the project upload applies, in tar form."""
    written = []
    dest_root = os.path.abspath(dest_dir)
    for member in tar.getmembers():
        if not member.name.startswith(prefix):
            continue
        relative = member.name[len(prefix):].lstrip("/")
        if not relative:
            continue
        out_path = os.path.abspath(os.path.join(dest_root, relative))
        if out_path != dest_root and not out_path.startswith(dest_root + os.sep):
            raise ValueError(f"archive member '{member.name}' escapes the destination")
        if member.isdir():
            os.makedirs(out_path, exist_ok=True)
            continue
        if not (member.isfile() or member.issym() or member.islnk()):
            continue
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        if member.isfile():
            src = tar.extractfile(member)
            if src is None:
                continue
            with open(out_path, "wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
            written.append(out_path)
    return written


def import_backup(project_id: int, tar_path: str) -> str:
    """Load an uploaded archive as a **new archived version** of the project.

    Nothing is started: the images land under the next version number, a compose archive's
    files land in that version's snapshot dir, and an `archived` `ProjectVersion` row appears.
    Activating it is then `POST /projects/{name}/rollback` — the ordinary path, unchanged.

    Registered under the project's deploy job key so it cannot race a deploy; stream it over
    `WS /projects/{name}/deploy`.
    """
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if project is None:
            raise ValueError(f"project {project_id} not found")
        name = project.name
        mode = project.deploy_mode
        version = project.version_counter + 1
        job_key = restore_job_key(project)
    finally:
        db.close()

    log_path = deploy_service._new_log_file()
    docker_service.register_external_job(
        job_key, "backup_import", ["backup-import", name, f"v{version}"], None, log_path,
    )
    threading.Thread(
        target=_import_job,
        args=(project_id, name, mode, version, tar_path, job_key, log_path),
        daemon=True,
    ).start()
    return job_key


def _import_job(project_id, name, mode, version, tar_path, job_key, log_path) -> None:
    def log(msg: str) -> None:
        deploy_service._append_log(log_path, msg)

    work_dir = os.path.join(backup_dir(name), f".import-{os.urandom(8).hex()}")
    os.makedirs(work_dir, exist_ok=True)
    db = SessionLocal()
    loaded_tags: list = []
    try:
        manifest = read_manifest(tar_path)
        log(f"── importing backup of '{manifest.get('project')}' "
            f"(v{manifest.get('version')}, {manifest.get('deploy_mode')}) as v{version} ──")
        if manifest.get("deploy_mode") and manifest["deploy_mode"] != mode:
            raise ValueError(
                f"archive is a {manifest['deploy_mode']} project but '{name}' is {mode} — "
                f"remove the project and import into a fresh name"
            )

        image_name, container_name = deploy_service.version_names(name, version)
        with tarfile.open(tar_path, "r") as tar:
            # ── images: load, then retag under this server's v{N} names ──
            for entry in manifest.get("images", []):
                src = tar.extractfile(entry["file"])
                if src is None:
                    raise ValueError(f"archive is missing {entry['file']}")
                staged = os.path.join(work_dir, "image.tar")
                with gzip.GzipFile(fileobj=src) as gz, open(staged, "wb") as out:
                    shutil.copyfileobj(gz, out, 1024 * 1024)
                log(f"── loading image {entry['tag']} ──")
                result = subprocess.run(["docker", "load", "-i", staged],
                                        capture_output=True, text=True,
                                        timeout=settings.BACKUP_TRANSFER_TIMEOUT)
                os.remove(staged)
                if result.returncode != 0:
                    raise RuntimeError(f"docker load failed: {(result.stderr or result.stdout).strip()}")
                log(result.stdout.strip())
                new_tag = (deploy_service.compose_version_tag(name, entry["service"], version)
                           if mode == "compose" else image_name)
                tag_result = subprocess.run(["docker", "tag", entry["tag"], new_tag],
                                            capture_output=True, text=True, timeout=60)
                if tag_result.returncode != 0:
                    raise RuntimeError(f"docker tag failed: {tag_result.stderr.strip()}")
                loaded_tags.append(new_tag)
                log(f"── tagged {new_tag} ──")

            # ── compose: the version's file snapshot is what its rollback restores ──
            if mode == "compose":
                snap = deploy_service.compose_snapshot_dir(name, version)
                shutil.rmtree(snap, ignore_errors=True)
                os.makedirs(snap, exist_ok=True)
                _safe_extract(tar, "project/", snap)
                if not os.path.exists(os.path.join(snap, "docker-compose.yml")):
                    raise ValueError("archive has no project/docker-compose.yml — "
                                     "a compose version cannot be restored from it")
                # Repoint the snapshot's image pins at the tags just loaded, so an `up` of
                # this version runs the imported images rather than the origin server's.
                deploy_service._write_pin_file(
                    snap, name, version,
                    [e["service"] for e in manifest.get("images", []) if e.get("service")],
                )
                log(f"── snapshot restored to {snap} ──")

        project = db.query(Project).filter(Project.id == project_id).first()
        db.add(ProjectVersion(
            project_id=project.id, version=version, status="archived", local_port=None,
            image_name=None if mode == "compose" else image_name,
            container_name=None if mode == "compose" else container_name,
        ))
        project.version_counter = version
        row = Backup(
            project_id=project.id, version=version, kind="manual", imported=True,
            filename=os.path.basename(tar_path), status="ok",
            size_bytes=os.path.getsize(tar_path), sha256=_sha256(tar_path),
            has_images=bool(manifest.get("images")), has_volumes=bool(manifest.get("volumes")),
            has_env=bool(manifest.get("env")), has_project=bool(manifest.get("has_project")),
            message=f"imported as v{version}",
        )
        db.add(row)
        db.flush()
        # Keep the archive: restoring its volume data later reads it back.
        kept = backup_path(name, f"{name}-imported-v{version}-{_timestamp()}.fhbak.tar")
        shutil.move(tar_path, kept)
        row.filename = os.path.basename(kept)
        db.commit()
        log(f"── v{version} is now available as an archived version — "
            f"roll back to it to activate ──")
        docker_service.finish_external_job(job_key, 0)
    except Exception as exc:
        db.rollback()
        for tag in loaded_tags:
            docker_service.remove_image(tag)
        if mode == "compose":
            shutil.rmtree(deploy_service.compose_snapshot_dir(name, version), ignore_errors=True)
        log(f"── import failed: {exc} ──")
        docker_service.finish_external_job(job_key, 1)
    finally:
        db.close()
        shutil.rmtree(work_dir, ignore_errors=True)
        if os.path.exists(tar_path):
            os.remove(tar_path)


# ── Data restore (volumes + env from an archive) ────────────────────────────────

def find_data_backup(db, project: Project, version: int | None, backup_id: int | None):
    """The archive a data restore should read: the one asked for, else the newest `ok`
    backup of that version. Returns None when there is nothing to restore from."""
    candidates = [b for b in list_backups(db, project) if b.status == "ok"]
    if backup_id is not None:
        return next((b for b in candidates if b.id == backup_id), None)
    matching = [b for b in candidates if version is not None and b.version == version]
    pool = matching or []
    if not pool:
        return None
    return max(pool, key=lambda b: (b.created_at or datetime.min, b.id))


def restore_data_members(tar_path: str, work_dir: str, log) -> tuple:
    """Unpack an archive's volume tars and env files into `work_dir`.

    Returns `([{name, label, anonymous, path}], {scope: content})` — the volume tars are
    gunzipped to plain tars because `volume_service.restore_cmd` feeds them to `tar xf -` on
    stdin. The manifest entry travels with each one because the *name* alone is not enough to
    find the volume to restore into (see `match_volumes`).
    """
    manifest = read_manifest(tar_path)
    volumes, envs = [], {}
    os.makedirs(work_dir, exist_ok=True)
    with tarfile.open(tar_path, "r") as tar:
        for entry in manifest.get("volumes", []):
            src = tar.extractfile(entry["file"])
            if src is None:
                log(f"  ~ archive is missing {entry['file']} — skipped")
                continue
            out_path = os.path.join(work_dir, f"{_slug(entry['name'])}.tar")
            with gzip.GzipFile(fileobj=src) as gz, open(out_path, "wb") as out:
                shutil.copyfileobj(gz, out, 1024 * 1024)
            volumes.append({
                "name": entry["name"], "label": entry.get("label") or "",
                "anonymous": bool(entry.get("anonymous")), "path": out_path,
            })
        for entry in manifest.get("env", []):
            src = tar.extractfile(entry["file"])
            if src is None:
                continue
            base = os.path.basename(entry["file"])
            scope = "" if base == "_project.env" else base[len("svc-"):-len(".env")]
            envs[scope] = src.read().decode(errors="replace")
    return volumes, envs


def match_volumes(project: Project, entries: list, log) -> list:
    """Pair each archived volume with the volume on *this* server it should be restored into.

    A named volume matches by name and needs nothing clever. An **anonymous** one does not:
    docker mints its 64-hex id for the container that creates it, so the id in the archive
    belongs to a container that may no longer exist — and after a rollback it certainly does
    not, because `docker run` on an image with a `VOLUME` instruction attaches a *brand new*
    empty anonymous volume. Restoring by the archived id would then quietly fill the old,
    now-orphaned volume and leave the running container empty.

    So an anonymous entry is matched by **mount target** (`/var/lib/postgresql/data`, `/data`)
    against the project's current volumes — that is the thing that actually identifies it. An
    entry with no match is reported and skipped rather than creating a stray volume nothing
    is mounted on.

    Returns `[(entry, target_volume_name)]`.
    """
    current = volume_service.list_project_volumes(project, with_sizes=False)
    by_name = {v["name"]: v for v in current}
    by_label = {}
    for v in current:
        if v["anonymous"] and v["label"]:
            by_label.setdefault(v["label"], v["name"])

    matched = []
    for entry in entries:
        if not entry["anonymous"] and entry["name"] in by_name:
            matched.append((entry, entry["name"]))
            continue
        target = by_label.get(entry["label"])
        if target is not None:
            if target != entry["name"]:
                log(f"── volume {entry['label']} → {target[:12]}… "
                    f"(anonymous; archived as {entry['name'][:12]}…) ──")
            matched.append((entry, target))
            continue
        if entry["name"] in by_name:
            matched.append((entry, entry["name"]))
            continue
        log(f"  ~ no volume on this server matches "
            f"{entry['label'] or entry['name']} — skipped")
    return matched


def apply_env_from_archive(db, project: Project, envs: dict, log) -> None:
    for scope, content in envs.items():
        env_service.set_content(db, project, content, service=scope or None)
        log(f"── env restored for scope '{scope or 'project'}' ──")
    db.flush()
    env_service.materialize(db, project)


def restore_data(project_id: int, backup_id: int) -> str:
    """Put an archive's volume contents and env files back, then bring the containers up.

    Runs under the project's deploy job key (mutually exclusive with a deploy) and reuses the
    stop → wipe+extract → start shape of `deploy_service._volume_restore_job`; in particular
    the dockerfile branch `docker start`s the same container rather than recreating it, since
    an anonymous volume belongs to the container that created it.
    """
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if project is None:
            raise ValueError(f"project {project_id} not found")
        name, mode = project.name, project.deploy_mode
        container_name = project.container_name
        job_key = restore_job_key(project)
        row = db.query(Backup).filter(Backup.id == backup_id,
                                      Backup.project_id == project_id).first()
        if row is None:
            raise ValueError(f"backup {backup_id} not found for project '{name}'")
        tar_path = backup_path(name, row.filename)
    finally:
        db.close()

    log_path = deploy_service._new_log_file()
    docker_service.register_external_job(
        job_key, "backup_restore", ["backup-restore", name, str(backup_id)], None, log_path,
    )
    threading.Thread(
        target=_restore_data_job,
        args=(project_id, name, mode, container_name, tar_path, job_key, log_path),
        daemon=True,
    ).start()
    return job_key


def _restore_data_job(project_id, name, mode, container_name, tar_path, job_key,
                      log_path) -> None:
    def log(msg: str) -> None:
        deploy_service._append_log(log_path, msg)

    project_dir = os.path.abspath(compose_service.project_dir(name))
    work_dir = os.path.join(backup_dir(name), f".restore-{os.urandom(8).hex()}")
    try:
        run_restore_phases(project_id, name, mode, container_name, tar_path, project_dir,
                           work_dir, job_key, log_path, log)
        docker_service.finish_external_job(job_key, 0)
    except Exception as exc:
        log(f"── data restore failed: {exc} ──")
        docker_service.finish_external_job(job_key, 1)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def run_restore_phases(project_id, name, mode, container_name, tar_path, project_dir,
                       work_dir, job_key, log_path, log) -> None:
    """Stop → restore volumes + env → start. Factored out because the rollback path runs the
    middle phase between its own stop and start rather than adding a second stop/start pair."""
    log(f"── restoring data from {os.path.basename(tar_path)} ──")
    entries, envs = restore_data_members(tar_path, work_dir, log)

    # Resolve which volumes to write into *before* stopping anything, so a mismatch is
    # reported while the project is still up.
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        matched = match_volumes(project, entries, log) if project is not None else []
    finally:
        db.close()

    if mode == "compose":
        deploy_service._run_phase(job_key, log_path,
                                  deploy_service._compose_files_cmd(name, project_dir, "stop"))
    elif container_name:
        deploy_service._run_phase(job_key, log_path,
                                  ["docker", "stop", "--timeout", "10", container_name])

    existing = volume_service.existing_volumes()
    restored = []
    for entry, target in matched:
        if target not in existing:
            log(f"  ~ volume {target} does not exist here — skipped")
            continue
        log(f"── restoring volume {target} ──")
        code = deploy_service._run_stdin_phase(job_key, log_path,
                                               volume_service.restore_cmd(target),
                                               entry["path"])
        if code != 0:
            raise RuntimeError(f"restore of volume '{target}' failed (exit {code})")
        restored.append(target)
    volume_service.forget_sizes(restored)

    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if envs and project is not None:
            apply_env_from_archive(db, project, envs, log)
            db.commit()
    finally:
        db.close()

    if mode == "compose":
        pins = os.path.exists(os.path.join(project_dir, deploy_service.PIN_FILE))
        deploy_service._run_phase(
            job_key, log_path,
            deploy_service._compose_files_cmd(name, project_dir, "up", "-d", "--no-build",
                                              pins=pins),
        )
    elif container_name:
        deploy_service._run_phase(job_key, log_path, ["docker", "start", container_name])
    log(f"── data restore complete ({len(restored)} volume(s), {len(envs)} env file(s)) ──")


# ── Deploy hook ─────────────────────────────────────────────────────────────────

def after_deploy(project_id: int, version: int) -> bool:
    """Take a backup after a successful deploy when the project asked for one — the second
    automatic trigger beside the cron schedule, and the one that mirrors how versions are
    created ("every deploy, capped by a limit").

    Returns whether a backup was started. Called from the deploy promote step; it must never
    raise into it, because a backup that cannot be taken is not a reason to report a deploy
    that already succeeded as failed.
    """
    try:
        db = SessionLocal()
        try:
            project = db.query(Project).filter(Project.id == project_id).first()
            if project is None:
                return False
            config = get_config(db, project)
            db.commit()
            if not (config.enabled and config.on_deploy):
                return False
        finally:
            db.close()
        # Creating a backup takes its own job key, so it does not contend with the deploy
        # that just released the project's — nothing to wait for.
        create_backup(project_id, kind="deploy", version=version)
        return True
    except Exception:
        return False


def remove_scope(scope: str) -> None:
    """Drop a scope's archive directory — called from project teardown. The rows go with the
    project through the relationship cascade."""
    shutil.rmtree(os.path.join(settings.DATA_DIR, "backups", scope), ignore_errors=True)
