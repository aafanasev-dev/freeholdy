"""
backups.py — backup archives for projects and for the freeholdy database.

Two routers in one module, because they are the same feature at two scopes:

  * `router` is mounted under `/projects` alongside the other project routers and covers one
    project's archives, its automatic-backup config, and importing an uploaded archive.
  * `system_router` is mounted at `/backups` and covers the destinations declared in the
    server's `.env` plus the freeholdy database's own archives — the same service functions
    with `project=None`.

The transfer shape is deliberately the volume routes': a download is fetched as raw
`[offset, offset+length)` pieces, and an upload is assembled by sparse writes at offsets.
Large archives therefore survive nginx's body limit and any proxy in between, and both
directions are resumable.

Authorization follows the rule the rest of the API uses. Reading, creating and downloading a
project's backups is `require_project_access` — a guest already holds that project's data.
Anything that names a *source* or changes policy is admin: importing an archive (it decides
what the project's next version contains) and writing the backup config. Every system route
is admin-only.
"""

import os
import re
import tarfile
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, WebSocket
from fastapi import WebSocketDisconnect
from sqlalchemy.orm import Session

from app.auth import require_admin, require_project_access
from app.config import settings
from app.models.database import SessionLocal, get_db
from app.models.orm import Backup, Project
from app.models.schemas import (
    BackupConfigResponse,
    BackupCreateResponse,
    BackupImportResponse,
    BackupInfo,
    BackupTargetInfo,
    BackupTargetTestResponse,
    BackupTargetsResponse,
    BackupUploadCompleteRequest,
    BackupsResponse,
    CreateBackupRequest,
    DockerJobStatusResponse,
    SetBackupConfigRequest,
)
from app.services import (
    backup_service, backup_targets, docker_service, interactive_service, ws_session,
)

router = APIRouter()
system_router = APIRouter()

_ID_RE = re.compile(r"^[0-9a-f]{8,}$")
STAGING_TTL = 6 * 3600          # an abandoned upload is swept after six hours
MAX_CHUNK = 8 * 1024 * 1024


# ── Shared helpers ──────────────────────────────────────────────────────────────

def _get_project(project_name: str, db: Session) -> Project:
    project = db.query(Project).filter(Project.name == project_name).first()
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")
    return project


def _get_backup(db: Session, project: Project | None, backup_id: int) -> Backup:
    """Resolve a backup id **within one scope** — the authorization boundary for every route
    here. A backup belonging to another project is indistinguishable from one that does not
    exist, exactly like `volume_service.find_volume`."""
    project_id = project.id if project is not None else None
    query = db.query(Backup).filter(Backup.id == backup_id)
    query = (query.filter(Backup.project_id == project_id) if project_id is not None
             else query.filter(Backup.project_id.is_(None)))
    row = query.first()
    if row is None:
        scope = f"project '{project.name}'" if project is not None else "the database"
        raise HTTPException(status_code=404, detail=f"Backup {backup_id} not found for {scope}")
    return row


def _info(row: Backup) -> BackupInfo:
    return BackupInfo(
        id=row.id,
        project=row.project.name if row.project is not None else None,
        version=row.version, kind=row.kind, imported=bool(row.imported),
        filename=row.filename, size_bytes=row.size_bytes, sha256=row.sha256,
        has_images=bool(row.has_images), has_volumes=bool(row.has_volumes),
        has_env=bool(row.has_env), has_project=bool(row.has_project),
        status=row.status, message=row.message or "",
        target_name=row.target_name, remote_status=row.remote_status,
        remote_path=row.remote_path, created_at=row.created_at,
    )


def _backups_response(db: Session, project: Project | None) -> BackupsResponse:
    rows = backup_service.list_backups(db, project)
    return BackupsResponse(
        project=project.name if project is not None else None,
        backups=[_info(r) for r in rows],
        total_bytes=sum(r.size_bytes or 0 for r in rows),
    )


def _config_response(db: Session, project: Project | None) -> BackupConfigResponse:
    config = backup_service.get_config(db, project)
    db.commit()
    return BackupConfigResponse(
        project=project.name if project is not None else None,
        enabled=bool(config.enabled), schedule_cron=config.schedule_cron,
        on_deploy=bool(config.on_deploy), keep_local=config.keep_local,
        keep_remote=config.keep_remote, target_name=config.target_name,
        include_volumes=bool(config.include_volumes),
        last_run_at=config.last_run_at, last_status=config.last_status,
        last_message=config.last_message or "",
    )


def _apply_config(db: Session, project: Project | None,
                  request: SetBackupConfigRequest) -> BackupConfigResponse:
    config = backup_service.get_config(db, project)
    if request.enabled is not None:
        config.enabled = request.enabled
    if request.schedule_cron is not None:
        config.schedule_cron = request.schedule_cron.strip() or None
    if request.on_deploy is not None:
        config.on_deploy = request.on_deploy
    if request.keep_local is not None:
        config.keep_local = request.keep_local
    if request.keep_remote is not None:
        config.keep_remote = request.keep_remote
    if request.target_name is not None:
        name = request.target_name.strip()
        if name and backup_targets.get_target(name) is None:
            raise HTTPException(
                status_code=400,
                detail=f"No backup target '{name}' is declared in the server's .env — "
                       f"see GET /backups/targets for the configured destinations",
            )
        config.target_name = name or None
    if request.include_volumes is not None:
        config.include_volumes = request.include_volumes
    db.commit()
    # A lowered keep_local should take effect now, not at the next backup.
    backup_service.prune_local(db, project, lambda _m: None)
    db.commit()
    return _config_response(db, project)


def _launch_backup(db: Session, project: Project | None,
                   request: CreateBackupRequest) -> BackupCreateResponse:
    scope = backup_service.scope_name(project)
    job_key = backup_service.create_job_key(scope)
    running = docker_service.get_job(job_key)
    if running is not None and running.status == "running":
        raise HTTPException(status_code=409,
                            detail=f"A backup of {scope} is already running")
    try:
        backup_service.create_backup(
            project.id if project is not None else None,
            kind="manual", version=request.version,
            include_volumes=request.include_volumes, upload=request.upload,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    job = docker_service.get_job(job_key)
    latest = backup_service.list_backups(db, project)
    ws_path = f"/projects/{project.name}/backup" if project is not None else None
    return BackupCreateResponse(
        status="ok",
        message=f"Backing up {scope}" + (f" — stream {ws_path}" if ws_path else ""),
        backup=_info(latest[0]) if latest else None,
        job=DockerJobStatusResponse(
            status=job.status if job else "no_job",
            operation=job.operation if job else None,
            message="Backup launched",
            logs=docker_service.get_job_logs(job_key),
            exit_code=job.exit_code if job else None,
        ),
        ws_path=ws_path,
    )


def _serve_chunk(path: str, offset: int, length: int) -> Response:
    """Raw `[offset, offset+length)` bytes of an archive — the volume download's shape, minus
    the staging step (a backup archive is already a file on disk)."""
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Backup archive is missing from disk")
    total = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(length)
    return Response(
        content=data, media_type="application/octet-stream",
        headers={"X-Total-Size": str(total), "X-Chunk-Offset": str(offset),
                 "X-Chunk-Length": str(len(data))},
    )


def _delete_backup(db: Session, project: Project | None, row: Backup, remote: bool) -> dict:
    details = []
    path = backup_service.backup_path(backup_service.scope_name(project), row.filename)
    try:
        os.remove(path)
        details.append(f"removed {row.filename}")
    except OSError:
        details.append(f"{row.filename} was already gone from disk")
    if remote and row.target_name and row.remote_status == "ok":
        target = backup_targets.get_target(row.target_name)
        if target is None:
            details.append(f"target '{row.target_name}' is no longer declared — remote copy kept")
        else:
            ok, message = backup_targets.delete_remote(target, row.filename)
            details.append(message if ok else f"remote delete failed: {message}")
    db.delete(row)
    db.commit()
    return {"status": "ok", "details": details}


# ── Upload staging (shared by the import route) ─────────────────────────────────

def _staging_dir(scope: str) -> str:
    d = os.path.join(settings.DATA_DIR, "backups", scope, "_staging")
    os.makedirs(d, exist_ok=True)
    return d


def _staging_path(scope: str, upload_id: str) -> str:
    """Path of an in-flight upload. The hex-only id is what keeps it inside the directory."""
    if not _ID_RE.match(upload_id or ""):
        raise HTTPException(status_code=400, detail=f"Invalid upload_id '{upload_id}'")
    return os.path.join(_staging_dir(scope), f"up-{upload_id}.tar")


def _sweep_staging(scope: str) -> None:
    now = time.time()
    d = _staging_dir(scope)
    for entry in os.listdir(d):
        path = os.path.join(d, entry)
        try:
            if os.path.isfile(path) and now - os.path.getmtime(path) > STAGING_TTL:
                os.remove(path)
        except OSError:
            pass


# ════════════════════════════════════════════════════════════════════════════════
# Project scope — mounted under /projects
# ════════════════════════════════════════════════════════════════════════════════

@router.get("/{project_name}/backups", response_model=BackupsResponse,
            summary="List the project's backup archives (newest first)")
def list_project_backups(project_name: str, db: Session = Depends(get_db),
                         _=Depends(require_project_access)):
    return _backups_response(db, _get_project(project_name, db))


@router.post("/{project_name}/backups", response_model=BackupCreateResponse,
             summary="Back the project up now — stream WS /projects/{name}/backup")
def create_project_backup(project_name: str, request: CreateBackupRequest = CreateBackupRequest(),
                          db: Session = Depends(get_db), _=Depends(require_project_access)):
    """Archives the version's image(s), the project's volumes, its env files and its project
    tree into one tar under `{DATA_DIR}/backups/{project}/`, then ships it when a destination
    is configured.

    Read-only towards docker, so it runs on its own job key and may overlap a deploy.
    **Volumes are captured as they are now** — docker keeps no per-version volume state, so
    backing up an older version pairs that version's image with today's data.
    """
    return _launch_backup(db, _get_project(project_name, db), request)


@router.get("/{project_name}/backups/{backup_id}/download",
            summary="Fetch one piece of a backup archive")
def download_project_backup(project_name: str, backup_id: int,
                            offset: int = Query(0, ge=0),
                            length: int = Query(1024 * 1024, ge=1, le=MAX_CHUNK),
                            db: Session = Depends(get_db), _=Depends(require_project_access)):
    project = _get_project(project_name, db)
    row = _get_backup(db, project, backup_id)
    return _serve_chunk(backup_service.backup_path(project.name, row.filename), offset, length)


@router.delete("/{project_name}/backups/{backup_id}",
               summary="Delete a backup archive (and optionally its remote copy)")
def delete_project_backup(project_name: str, backup_id: int,
                          remote: bool = Query(False, description="Also delete the copy at the target"),
                          db: Session = Depends(get_db), _=Depends(require_project_access)):
    project = _get_project(project_name, db)
    return _delete_backup(db, project, _get_backup(db, project, backup_id), remote)


@router.post("/{project_name}/backups/upload/chunk",
             summary="Append one piece of a backup archive at the given byte offset")
async def upload_backup_chunk(project_name: str, request: Request, upload_id: str, offset: int,
                              db: Session = Depends(get_db), _=Depends(require_admin)):
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")
    project = _get_project(project_name, db)
    path = _staging_path(project.name, upload_id)
    chunk = await request.body()
    # os.open + lseek so a write past EOF leaves a sparse hole — order-independent and
    # idempotent, the same technique the project and volume uploads use.
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        os.write(fd, chunk)
    finally:
        os.close(fd)
    return {"status": "ok", "received": len(chunk)}


@router.post("/{project_name}/backups/upload/complete", response_model=BackupImportResponse,
             summary="Import the uploaded archive as a new archived version")
def complete_backup_upload(project_name: str, request: BackupUploadCompleteRequest,
                           db: Session = Depends(get_db), _=Depends(require_admin)):
    """Loads the archive's images under the project's **next** version number and writes an
    `archived` `ProjectVersion` for it (compose additionally gets that version's file
    snapshot). Nothing is started: the version shows up in the versions list, and activating
    it is the ordinary `POST /projects/{name}/rollback` — add `restore_data: true` there to
    put the archive's volume contents and env back at the same time.

    Admin-only: an archive decides what the project's next version contains, so it belongs
    with the other deploy endpoints that name a source.
    """
    project = _get_project(project_name, db)
    _sweep_staging(project.name)
    path = _staging_path(project.name, request.upload_id)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="No staged upload for this upload_id")
    if request.total_size is not None and os.path.getsize(path) != request.total_size:
        os.remove(path)
        raise HTTPException(status_code=400, detail="Staged upload is incomplete (size mismatch)")
    if not tarfile.is_tarfile(path):
        os.remove(path)
        raise HTTPException(status_code=400, detail="Uploaded data is not a tar archive")
    try:
        manifest = backup_service.read_manifest(path)
    except ValueError as exc:
        os.remove(path)
        raise HTTPException(status_code=400, detail=str(exc))
    if manifest.get("scope") != "project":
        os.remove(path)
        raise HTTPException(
            status_code=400,
            detail="This is a freeholdy database backup, not a project backup — "
                   "restore it with scripts/restore_db.sh",
        )
    if manifest.get("deploy_mode") and project.deploy_mode not in ("pending", manifest["deploy_mode"]):
        os.remove(path)
        raise HTTPException(
            status_code=400,
            detail=f"Archive is a {manifest['deploy_mode']} project but '{project.name}' is "
                   f"{project.deploy_mode} — import it into a fresh project name instead",
        )

    job_key = backup_service.restore_job_key(project)
    running = docker_service.get_job(job_key)
    if running is not None and running.status == "running":
        os.remove(path)
        raise HTTPException(status_code=409,
                            detail=f"Project '{project.name}' is busy with a "
                                   f"{running.operation} job — try again when it finishes")

    version = project.version_counter + 1
    try:
        backup_service.import_backup(project.id, path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return BackupImportResponse(
        status="ok",
        message=f"Importing the archive as v{version} of '{project.name}' — it will appear "
                f"as an archived version; roll back to it to activate",
        version=version, project=project.name,
        job=DockerJobStatusResponse(
            status="running", operation="backup_import",
            message="Import launched", logs=docker_service.get_job_logs(job_key),
        ),
        ws_path=f"/projects/{project.name}/deploy",
    )


@router.get("/{project_name}/backup-config", response_model=BackupConfigResponse,
            summary="The project's automatic-backup settings")
def get_project_backup_config(project_name: str, db: Session = Depends(get_db),
                              _=Depends(require_project_access)):
    return _config_response(db, _get_project(project_name, db))


@router.put("/{project_name}/backup-config", response_model=BackupConfigResponse,
            summary="Change the project's automatic-backup settings")
def set_project_backup_config(project_name: str, request: SetBackupConfigRequest,
                              db: Session = Depends(get_db), _=Depends(require_admin)):
    """Omitted fields keep their stored value. `schedule_cron: ""` clears the timer and
    `target_name: ""` clears the destination. Lowering `keep_local` prunes immediately."""
    return _apply_config(db, _get_project(project_name, db), request)


@router.websocket("/{project_name}/backup")
async def backup_stream_session(websocket: WebSocket, project_name: str):
    """Read-only tail of the project's `backup:{name}` job — the deploy socket's shape, on
    the key a backup actually runs under (its own, so it can overlap a deploy)."""
    await websocket.accept()
    if not await ws_session.authenticate(websocket, project=project_name):
        return

    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.name == project_name).first()
        if project is None:
            await ws_session.reject(websocket, 4404, f"project '{project_name}' not found")
            return
        job_key = backup_service.create_job_key(project.name)
    finally:
        db.close()

    if docker_service.get_job(job_key) is None:
        await ws_session.reject(websocket, 4404,
                                f"no backup job for '{project_name}' — start one first")
        return

    try:
        await websocket.send_json({"type": "ready"})
        exit_code = await interactive_service.stream_job(websocket, job_key)
        if exit_code is None:
            return  # client went away; the backup keeps running
        await websocket.send_json({"type": "exit", "code": exit_code})
        await websocket.close(code=1000)
    except WebSocketDisconnect:
        pass


# ════════════════════════════════════════════════════════════════════════════════
# System scope — mounted at /backups (admin only)
# ════════════════════════════════════════════════════════════════════════════════

@system_router.get("/targets", response_model=BackupTargetsResponse,
                   summary="The backup destinations declared in the server's .env")
def list_backup_targets(_=Depends(require_admin)):
    """Names, types and hosts only — passwords and key paths never leave the server."""
    return BackupTargetsResponse(targets=[BackupTargetInfo(**t) for t in backup_targets.list_targets()])


@system_router.post("/targets/{name}/test", response_model=BackupTargetTestResponse,
                    summary="Check that a destination is reachable and the credentials work")
def test_backup_target(name: str, _=Depends(require_admin)):
    target = backup_targets.get_target(name)
    if target is None:
        raise HTTPException(status_code=404,
                            detail=f"No backup target '{name}' is declared in the server's .env")
    ok, message = backup_targets.check(target)
    return BackupTargetTestResponse(status="ok" if ok else "error", name=name, message=message)


@system_router.get("/database", response_model=BackupsResponse,
                   summary="List backups of the freeholdy database itself")
def list_database_backups(db: Session = Depends(get_db), _=Depends(require_admin)):
    return _backups_response(db, None)


@system_router.post("/database", response_model=BackupCreateResponse,
                    summary="Back the freeholdy database up now")
def create_database_backup(request: CreateBackupRequest = CreateBackupRequest(),
                           db: Session = Depends(get_db), _=Depends(require_admin)):
    """Takes a consistent copy with `sqlite3 .backup` (falling back to a file copy when the
    CLI is absent), gzips it into an archive and ships it when a destination is configured.
    The server's `.env` is deliberately **not** included — it holds the target credentials."""
    return _launch_backup(db, None, request)


@system_router.get("/database/{backup_id}/download",
                   summary="Fetch one piece of a database backup archive")
def download_database_backup(backup_id: int, offset: int = Query(0, ge=0),
                             length: int = Query(1024 * 1024, ge=1, le=MAX_CHUNK),
                             db: Session = Depends(get_db), _=Depends(require_admin)):
    row = _get_backup(db, None, backup_id)
    return _serve_chunk(backup_service.backup_path(backup_service.SYSTEM_SCOPE, row.filename),
                        offset, length)


@system_router.delete("/database/{backup_id}",
                      summary="Delete a database backup archive")
def delete_database_backup(backup_id: int,
                           remote: bool = Query(False, description="Also delete the copy at the target"),
                           db: Session = Depends(get_db), _=Depends(require_admin)):
    return _delete_backup(db, None, _get_backup(db, None, backup_id), remote)


@system_router.get("/database/config", response_model=BackupConfigResponse,
                   summary="Automatic-backup settings for the freeholdy database")
def get_database_backup_config(db: Session = Depends(get_db), _=Depends(require_admin)):
    return _config_response(db, None)


@system_router.put("/database/config", response_model=BackupConfigResponse,
                   summary="Change the database's automatic-backup settings")
def set_database_backup_config(request: SetBackupConfigRequest,
                               db: Session = Depends(get_db), _=Depends(require_admin)):
    return _apply_config(db, None, request)
