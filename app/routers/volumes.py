"""
volumes.py — list a project's docker volumes, download one as a tar, upload one back.

Volumes are the one part of a project freeholdy does not otherwise manage: images are
rebuilt from source, the project dir is re-uploaded, but the data in a volume exists only
once. These endpoints make it visible (name, size, which services mount it) and movable
(tar out, tar back in).

Both transfers are **chunked**, mirroring the project upload in `projects.py`:

  download  POST   …/volumes/{v}/download                → tar into staging, return an id
            GET    …/volumes/{v}/download/{id}?offset=…  → one raw piece
            DELETE …/volumes/{v}/download/{id}           → discard the staged tar
  upload    POST   …/volumes/{v}/upload/chunk?upload_id=&offset=   → one raw piece
            POST   …/volumes/{v}/upload/complete                   → validate + restore

Staged archives live under `{DATA_DIR}/volumes/{project}/`, outside PROJECTS_DIR, exactly
like `{DATA_DIR}/uploads/`. A restore is a **job** (deploy_service.volume_restore), not a
request: it stops the containers, replaces the contents and starts them again, reporting
under the project's normal job key.

Every route resolves the volume through `volume_service.find_volume(project, name)` first,
so a token can only ever reach volumes of a project it already has access to.
"""

import os
import re
import tarfile
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.auth import require_project_access
from app.config import settings
from app.models.database import get_db
from app.models.orm import Project
from app.models.schemas import (
    VolumeDownloadResponse,
    VolumeRestoreResponse,
    VolumeUploadCompleteRequest,
    VolumesResponse,
)
from app.services import volume_service

router = APIRouter()

_ID_RE = re.compile(r"^[0-9a-f]{8,}$")
# Staged archives older than this are swept when a new download is prepared — an aborted
# transfer must not pin disk space forever.
STAGING_TTL = 6 * 3600
MAX_CHUNK = 8 * 1024 * 1024


def _get_project(project_name: str, db: Session) -> Project:
    project = db.query(Project).filter(Project.name == project_name).first()
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")
    return project


def _get_volume(project: Project, volume: str) -> dict:
    """Resolve a volume name against the project's own volumes — the authorization boundary
    for every route here. A volume that belongs to another project is indistinguishable from
    one that does not exist."""
    found = volume_service.find_volume(project, volume)
    if not found:
        raise HTTPException(
            status_code=404,
            detail=f"Volume '{volume}' not found in project '{project.name}'",
        )
    return found


def _staging_dir(project_name: str) -> str:
    d = os.path.join(settings.DATA_DIR, "volumes", project_name)
    os.makedirs(d, exist_ok=True)
    return d


def _staging_path(project_name: str, prefix: str, transfer_id: str) -> str:
    """Path of a staged tar. Rejects a non-hex id so the id can never escape the directory
    (the project name is already known-good — it came from a DB row)."""
    if not _ID_RE.match(transfer_id or ""):
        raise HTTPException(status_code=400, detail=f"Invalid id '{transfer_id}'")
    return os.path.join(_staging_dir(project_name), f"{prefix}-{transfer_id}.tar")


def _sweep_staging(project_name: str) -> None:
    now = time.time()
    d = _staging_dir(project_name)
    for entry in os.listdir(d):
        path = os.path.join(d, entry)
        try:
            if os.path.isfile(path) and now - os.path.getmtime(path) > STAGING_TTL:
                os.remove(path)
        except OSError:
            pass


# ── Listing ─────────────────────────────────────────────────────────────────────

@router.get("/{project_name}/volumes", response_model=VolumesResponse,
            summary="List the project's docker volumes with their measured sizes")
def list_volumes(
    project_name: str,
    refresh: bool = Query(False, description="Re-measure sizes instead of using the cache"),
    db: Session = Depends(get_db),
    _=Depends(require_project_access),
):
    """Unlike the `volumes` field of `GET /projects`, this always returns measured sizes —
    it waits for the `du` rather than reporting `pending`."""
    project = _get_project(project_name, db)
    if refresh:
        volume_service.forget_sizes(
            [v["name"] for v in volume_service.list_project_volumes(project, with_sizes=False)]
        )
    volumes = volume_service.list_project_volumes(project, wait=True)
    total = sum(v["size_bytes"] or 0 for v in volumes)
    return VolumesResponse(project=project_name, volumes=volumes, total_bytes=total)


# ── Download (staged, then fetched in pieces) ───────────────────────────────────

@router.post("/{project_name}/volumes/{volume}/download", response_model=VolumeDownloadResponse,
             summary="Archive a volume into staging and return an id to fetch it with")
def prepare_download(
    project_name: str,
    volume: str,
    db: Session = Depends(get_db),
    _=Depends(require_project_access),
):
    project = _get_project(project_name, db)
    info = _get_volume(project, volume)
    if not info["exists"]:
        raise HTTPException(
            status_code=400,
            detail=f"Volume '{volume}' is declared but has not been created yet — "
                   f"deploy the project first",
        )

    _sweep_staging(project_name)
    download_id = os.urandom(16).hex()
    path = _staging_path(project_name, "dl", download_id)
    ok, message = volume_service.tar_volume(volume, path)
    if not ok:
        if os.path.exists(path):
            os.remove(path)
        raise HTTPException(status_code=500, detail=message)

    return VolumeDownloadResponse(
        status="ok",
        message=message,
        download_id=download_id,
        filename=f"{project_name}-{info['label'].strip('/').replace('/', '-') or volume}.tar",
        size=os.path.getsize(path),
    )


@router.get("/{project_name}/volumes/{volume}/download/{download_id}",
            summary="Fetch one piece of a staged volume archive")
def download_chunk(
    project_name: str,
    volume: str,
    download_id: str,
    offset: int = Query(0, ge=0),
    length: int = Query(1024 * 1024, ge=1, le=MAX_CHUNK),
    db: Session = Depends(get_db),
    _=Depends(require_project_access),
):
    """Raw `application/octet-stream` bytes `[offset, offset+length)` of the staged tar.
    Offset addressing (rather than a single streaming response) keeps every response small
    enough to survive a proxy, and gives clients a progress bar and resumability."""
    project = _get_project(project_name, db)
    _get_volume(project, volume)
    path = _staging_path(project_name, "dl", download_id)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="No staged archive for this download_id")

    total = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(length)
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "X-Total-Size": str(total),
            "X-Chunk-Offset": str(offset),
            "X-Chunk-Length": str(len(data)),
        },
    )


@router.delete("/{project_name}/volumes/{volume}/download/{download_id}",
               summary="Discard a staged volume archive")
def abort_download(
    project_name: str,
    volume: str,
    download_id: str,
    db: Session = Depends(get_db),
    _=Depends(require_project_access),
):
    project = _get_project(project_name, db)
    _get_volume(project, volume)
    path = _staging_path(project_name, "dl", download_id)
    if os.path.isfile(path):
        os.remove(path)
    return {"status": "ok"}


# ── Upload (chunked, then restored as a job) ────────────────────────────────────

@router.post("/{project_name}/volumes/{volume}/upload/chunk",
             summary="Append one piece of a volume archive at the given byte offset")
async def upload_chunk(
    project_name: str,
    volume: str,
    request: Request,
    upload_id: str,
    offset: int,
    db: Session = Depends(get_db),
    _=Depends(require_project_access),
):
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")
    project = _get_project(project_name, db)
    _get_volume(project, volume)
    path = _staging_path(project_name, "up", upload_id)
    chunk = await request.body()
    # os.open + lseek so seeking past EOF (sparse) works on a freshly created file — same
    # idempotent, order-independent write the project upload uses.
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.lseek(fd, offset, os.SEEK_SET)
        os.write(fd, chunk)
    finally:
        os.close(fd)
    return {"status": "ok", "received": len(chunk)}


@router.post("/{project_name}/volumes/{volume}/upload/complete", response_model=VolumeRestoreResponse,
             summary="Validate the uploaded archive and restore it into the volume")
def complete_upload(
    project_name: str,
    volume: str,
    request: VolumeUploadCompleteRequest,
    db: Session = Depends(get_db),
    _=Depends(require_project_access),
):
    """Replaces the volume's contents with the archive: the containers are stopped, the
    volume is wiped and extracted into, and the containers start again — streamed as a job
    under the project's normal key, so `ws_path` / `/status` report it like a deploy."""
    from app.routers.projects import _deploy_ws_path  # lazy: projects imports schemas widely
    from app.services import deploy_service

    project = _get_project(project_name, db)
    info = _get_volume(project, volume)
    path = _staging_path(project_name, "up", request.upload_id)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="No staged upload for this upload_id")
    if request.total_size is not None and os.path.getsize(path) != request.total_size:
        os.remove(path)
        raise HTTPException(status_code=400, detail="Staged upload is incomplete (size mismatch)")
    # tarfile sniffs gzip/bzip2/xz too, so a .tar.gz an operator made by hand also restores
    # (the helper's `tar xf -` auto-detects compression the same way).
    if not tarfile.is_tarfile(path):
        os.remove(path)
        raise HTTPException(status_code=400, detail="Uploaded data is not a tar archive")
    if not info["exists"]:
        os.remove(path)
        raise HTTPException(
            status_code=400,
            detail=f"Volume '{volume}' has not been created yet — deploy the project first",
        )

    deploy_service.volume_restore(project.id, volume, path)
    status_path = (f"/projects/{project_name}/compose/status"
                   if project.deploy_mode == "compose"
                   else f"/projects/{project_name}/status")
    return VolumeRestoreResponse(
        status="running",
        message=f"Restoring volume '{volume}' — the project's containers are stopped, "
                f"the volume replaced, then started again",
        volume=volume,
        ws_path=_deploy_ws_path(project_name),
        status_path=status_path,
    )
