"""
volume_service.py — docker volumes as a derived, project-scoped resource.

Volumes are never stored in the DB. Docker is the source of truth, and a copy could only
drift: a volume can be created by a `docker compose up`, by an image's `VOLUME` instruction
at `docker run` time, or by hand. So a project's volumes are recomputed from three sources
on every request (see `list_project_volumes`), which also means this feature needs no
migration.

**All volume I/O runs inside a helper container** (`settings.VOLUME_HELPER_IMAGE`), never on
the host filesystem. A volume's Mountpoint lives under /var/lib/docker/volumes and is
readable by root only, so a host-path implementation would work when freeholdy runs as root
and fail everywhere else; `docker run --rm -v {volume}:/v …` works identically in both cases
and needs nothing but the docker socket freeholdy already uses.
"""

import json
import os
import re
import subprocess
import threading
import time
from typing import Optional

import yaml

from app.config import settings
from app.services import compose_service

# Docker's own volume-name grammar, applied to anything that arrives in a URL path.
VOLUME_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
# An anonymous volume (created by an image's VOLUME instruction) is a 64-char hex id.
_ANON_RE = re.compile(r"^[0-9a-f]{64}$")

INSPECT_TIMEOUT = 15        # docker inspect / volume ls
SIZE_TIMEOUT = 120          # `du -sb` inside the helper container
ARCHIVE_TIMEOUT = 3600      # tar of a whole volume

# Sizing a volume costs a container start (~0.5s for a small volume, longer for a big one),
# and GET /projects is refetched after every UI action — so sizes are cached, and a miss is
# filled in the background rather than blocking the list. See `volume_size`.
SIZE_CACHE_TTL = 60
_size_cache: dict[str, tuple[float, Optional[int]]] = {}   # name → (computed_at, bytes|None)
_size_pending: set[str] = set()
_size_lock = threading.Lock()


# ── helper-container plumbing ───────────────────────────────────────────────────

def helper_cmd(volume: str, *args: str, read_only: bool = True, stdin: bool = False) -> list:
    """`docker run` argv that mounts one volume at /v inside the helper image.

    Never call this for a volume that does not exist: `docker run -v name:/v` *creates* a
    missing volume, which would turn a typo into a new empty volume."""
    mount = f"{volume}:/v:ro" if read_only else f"{volume}:/v"
    return [
        "docker", "run", "--rm", *(["-i"] if stdin else []),
        "-v", mount, settings.VOLUME_HELPER_IMAGE, *args,
    ]


def _docker(args: list, timeout: int = INSPECT_TIMEOUT) -> tuple[bool, str]:
    """Run a short docker command and return (ok, stdout-or-stderr)."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return False, str(e)
    return result.returncode == 0, (result.stdout if result.returncode == 0 else result.stderr).strip()


# ── discovery ───────────────────────────────────────────────────────────────────

def existing_volumes() -> set[str]:
    """Every volume name the daemon currently knows."""
    ok, out = _docker(["docker", "volume", "ls", "--format", "{{.Name}}"])
    return set(out.split()) if ok else set()


def container_volume_mounts(container_name: str) -> list[dict]:
    """The volume-type mounts of one container: [{name, target}, …].

    This is the only way a dockerfile project's volumes are visible at all — an image's
    `VOLUME` instruction creates an anonymous volume when the container is created, and
    nothing about it appears in freeholdy's own state."""
    ok, out = _docker(["docker", "inspect", "-f", "{{json .Mounts}}", container_name])
    if not ok or not out:
        return []
    try:
        mounts = json.loads(out)
    except (ValueError, TypeError):
        return []
    return [
        {"name": m.get("Name"), "target": m.get("Destination") or ""}
        for m in mounts or []
        if m.get("Type") == "volume" and m.get("Name")
    ]


def _compose_labelled_volumes(project_name: str) -> dict[str, str]:
    """Volumes docker attributes to this compose project: {volume name → compose key}.

    `docker compose` stamps com.docker.compose.project / .volume on every volume it
    creates, so the friendly key comes back without parsing any YAML. Catches volumes whose
    containers are currently down (a stack that was `compose down`ed still has its data)."""
    ok, out = _docker([
        "docker", "volume", "ls",
        "--filter", f"label=com.docker.compose.project={project_name}",
        "--format", '{{.Name}}\t{{.Label "com.docker.compose.volume"}}',
    ])
    if not ok:
        return {}
    found: dict[str, str] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        name, _, key = line.partition("\t")
        found[name.strip()] = key.strip() or name.strip()
    return found


def _mount_source(entry) -> Optional[str]:
    """The volume a service's `volumes:` entry refers to, or None for a bind mount."""
    if isinstance(entry, dict):
        if entry.get("type") not in (None, "volume"):
            return None
        source = entry.get("source")
        return str(source) if source else None
    text = str(entry)
    source = text.split(":")[0]
    # A bind mount's source is a path ("./data:/data", "/srv/x:/x"); a named volume is not.
    if not source or source.startswith(("/", ".", "~", "$")):
        return None
    return source


def _declared_compose_volumes(project_name: str) -> dict[str, dict]:
    """Top-level `volumes:` of the project's compose file → {real name: {...}}.

    Adds what docker cannot yet know: a volume declared but never created (the stack has
    not been up), plus `external: true` (never deleted by a teardown) and an explicit
    `name:` override. Also maps each volume to the services that mount it."""
    path = compose_service.compose_file_path(project_name)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            doc = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(doc, dict):
        return {}

    declared = doc.get("volumes")
    if not isinstance(declared, dict):
        return {}

    # key (as written in the compose file) → services mounting it
    users: dict[str, list[str]] = {}
    services = doc.get("services")
    if isinstance(services, dict):
        for svc_name, spec in services.items():
            for entry in (spec or {}).get("volumes") or []:
                source = _mount_source(entry)
                if source:
                    users.setdefault(source, []).append(str(svc_name))

    out: dict[str, dict] = {}
    for key, spec in declared.items():
        spec = spec or {}
        external = bool(spec.get("external")) if not isinstance(spec.get("external"), dict) else True
        if spec.get("name"):
            real = str(spec["name"])
        elif external:
            real = str(key)              # an external volume is referenced by its own name
        else:
            real = f"{project_name}_{key}"
        out[real] = {
            "label": str(key),
            "external": external,
            "services": users.get(str(key), []),
        }
    return out


def list_project_volumes(project, with_sizes: bool = True, wait: bool = False) -> list[dict]:
    """Every docker volume belonging to `project` (an ORM Project), newest information first.

    Union of three sources, docker before configuration:
      1. the volume-type mounts of the project's own containers — the service mapping, and
         the only source for a dockerfile project's anonymous volumes;
      2. volumes labelled with this compose project (survives a stopped stack);
      3. the compose file's top-level `volumes:` — declared-but-not-created ones, `external`
         flags and `name:` overrides.

    `with_sizes` fills size_bytes/size_status (see `volume_size`); `wait` computes a missing
    size synchronously instead of in the background.
    """
    found: dict[str, dict] = {}

    def touch(name: str) -> dict:
        return found.setdefault(name, {
            "name": name, "label": name, "external": False, "services": [], "exists": True,
        })

    # 1. The project's own containers.
    if project.deploy_mode == "compose":
        containers = [(s.container_name, s.name) for s in project.services if s.container_name]
    else:
        containers = [(project.container_name, project.name)] if project.container_name else []
    for container_name, service_name in containers:
        for mount in container_volume_mounts(container_name):
            entry = touch(mount["name"])
            if service_name not in entry["services"]:
                entry["services"].append(service_name)
            if _ANON_RE.match(mount["name"]) and mount["target"]:
                entry["label"] = mount["target"]          # anonymous → name it by where it lands
            entry["anonymous"] = bool(_ANON_RE.match(mount["name"]))

    # 2. Volumes docker attributes to this compose project.
    if project.deploy_mode == "compose":
        for name, key in _compose_labelled_volumes(project.name).items():
            entry = touch(name)
            if entry["label"] == name:
                entry["label"] = key

    # 3. The compose file's declarations.
    if project.deploy_mode == "compose":
        live = existing_volumes()
        for name, spec in _declared_compose_volumes(project.name).items():
            entry = touch(name)
            entry["label"] = spec["label"]
            entry["external"] = spec["external"]
            entry["exists"] = name in live
            for svc in spec["services"]:
                if svc not in entry["services"]:
                    entry["services"].append(svc)

    volumes = []
    for name in sorted(found):
        entry = found[name]
        entry.setdefault("anonymous", bool(_ANON_RE.match(name)))
        # A volume that does not exist yet has no size to report, and neither does one
        # listed with with_sizes=False (the teardown/lookup paths) — both are "unknown"
        # rather than a zero that would read as "empty".
        if with_sizes and entry["exists"]:
            size, status = volume_size(name, wait=wait)
        else:
            size, status = None, "unknown"
        entry["size_bytes"] = size
        entry["size_status"] = status
        volumes.append(entry)
    return volumes


def find_volume(project, name: str) -> Optional[dict]:
    """The project's volume called `name`, or None.

    Every route resolves the path parameter through here before touching docker: it is what
    stops a token reaching a volume outside the project it has access to."""
    if not VOLUME_NAME_RE.match(name or ""):
        return None
    return next((v for v in list_project_volumes(project, with_sizes=False) if v["name"] == name), None)


# ── sizing ──────────────────────────────────────────────────────────────────────

def _measure(name: str) -> Optional[int]:
    """`du -sb /v` inside the helper container → bytes, or None when it fails."""
    ok, out = _docker(helper_cmd(name, "du", "-sb", "/v"), timeout=SIZE_TIMEOUT)
    if not ok:
        return None
    try:
        return int(out.split()[0])
    except (IndexError, ValueError):
        return None


def _measure_into_cache(name: str) -> None:
    size = _measure(name)
    with _size_lock:
        _size_cache[name] = (time.monotonic(), size)
        _size_pending.discard(name)


def volume_size(name: str, wait: bool = False, refresh: bool = False) -> tuple[Optional[int], str]:
    """(size_bytes, status) for one volume — `ok`, `pending` or `unknown`.

    A fresh cache entry (< SIZE_CACHE_TTL) is returned as-is. On a miss: `wait` measures
    inline; otherwise a background thread fills the cache and the caller gets `pending`, so
    the project list never blocks on a multi-gigabyte volume. Callers that need a number
    (GET /volumes) pass wait=True.
    """
    now = time.monotonic()
    with _size_lock:
        cached = _size_cache.get(name)
        if cached and not refresh and now - cached[0] < SIZE_CACHE_TTL:
            return cached[1], "ok" if cached[1] is not None else "unknown"
        if not wait:
            if name in _size_pending:
                return (cached[1] if cached else None), "pending"
            _size_pending.add(name)

    if not wait:
        threading.Thread(target=_measure_into_cache, args=(name,), daemon=True).start()
        return (cached[1] if cached else None), "pending"

    size = _measure(name)
    with _size_lock:
        _size_cache[name] = (time.monotonic(), size)
        _size_pending.discard(name)
    return size, "ok" if size is not None else "unknown"


def forget_sizes(names) -> None:
    """Drop cached sizes — after a restore, and after a volume is removed."""
    with _size_lock:
        for name in names:
            _size_cache.pop(name, None)


# ── archive / restore / remove ──────────────────────────────────────────────────

def tar_volume(name: str, dest_path: str) -> tuple[bool, str]:
    """Write the whole volume to `dest_path` as an uncompressed tar (members relative to
    the volume root, so `tar tf` shows `./chat.db`)."""
    try:
        with open(dest_path, "wb") as out:
            result = subprocess.run(
                helper_cmd(name, "tar", "cf", "-", "-C", "/v", "."),
                stdout=out, stderr=subprocess.PIPE, text=False, timeout=ARCHIVE_TIMEOUT,
            )
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"Archiving volume '{name}' failed: {e}"
    if result.returncode != 0:
        return False, (result.stderr or b"").decode(errors="replace").strip() or "docker run failed"
    return True, f"Archived volume '{name}' ({os.path.getsize(dest_path)} bytes)"


# Wipe first, then extract: a restore reproduces the archive exactly rather than merging
# into whatever the volume held. `.[!.]*` and `..?*` cover dotfiles, which `*` misses.
_RESTORE_SH = 'rm -rf /v/* /v/.[!.]* /v/..?* 2>/dev/null; exec tar xf - -C /v'


def restore_cmd(name: str) -> list:
    """argv that wipes the volume and extracts a tar arriving on stdin."""
    return helper_cmd(name, "sh", "-c", _RESTORE_SH, read_only=False, stdin=True)


def remove_volumes(names) -> tuple[list[str], list[str]]:
    """`docker volume rm` each name. Returns (details, errors) — never raises, so a
    teardown always continues."""
    details: list[str] = []
    errors: list[str] = []
    for name in names:
        ok, out = _docker(["docker", "volume", "rm", name])
        if ok:
            details.append(f"Volume '{name}' removed")
        else:
            # An in-use or already-gone volume is a warning, not a failure of the delete.
            errors.append(f"volume rm {name}: {out}")
            details.append(f"Volume '{name}' NOT removed ({out})")
    forget_sizes(names)
    return details, errors
