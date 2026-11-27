"""
image_service.py — static inspection of an uploaded Docker image archive.

The image-archive deploy transport (`fhcli deploy NAME ./app.tar`, the web UI's "image
archive" tab) needs the same facts a Dockerfile scan gives a dockerfile project — which
port the container serves, whether it speaks WebSocket — before anything is loaded into
docker, because provisioning (port, subdomain, nginx) happens before the deploy job starts.
All of it is in the archive's image config JSON, so we read the tar directly with
`tarfile` rather than `docker load`ing it first:

  - `config.ExposedPorts`  → candidate container ports (includes base-image EXPOSEs, which
                              the Dockerfile text scan cannot see).
  - `history[].created_by` + Labels/Env/Cmd/Entrypoint → WebSocket *hint*
                              (`scan.uses_websocket`); the user confirms it.
  - `architecture` / `os`   → refuse an image the host cannot execute.

Both `docker save` layouts are supported: the classic `manifest.json` (still written by
Docker >= 25 next to the OCI files) and a pure OCI layout (`index.json` → manifest blob →
config blob). Plain, gzip, bzip2 and xz tars open via `tarfile`'s "r:*".
"""

import json
import subprocess
import tarfile

from app.services import scan

# Upper bound for any JSON member we read (manifests/configs are a few KiB).
_MAX_JSON = 16 * 1024 * 1024

_ARCH_ALIASES = {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "arm", "i386": "386", "i686": "386"}


def _norm_arch(arch: str | None) -> str | None:
    if not arch:
        return None
    arch = arch.strip().lower()
    return _ARCH_ALIASES.get(arch, arch)


def _read_json(tf: tarfile.TarFile, name: str) -> dict | list | None:
    if name.startswith("./"):
        name = name[2:]
    if not name:
        return None
    try:
        member = tf.getmember(name)
    except KeyError:
        try:
            member = tf.getmember("./" + name)
        except KeyError:
            return None
    if not member.isfile() or member.size > _MAX_JSON:
        return None
    f = tf.extractfile(member)
    if f is None:
        return None
    try:
        return json.loads(f.read().decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _blob_path(digest: str) -> str:
    algo, _, hexpart = digest.partition(":")
    return f"blobs/{algo}/{hexpart}"


def _oci_config(tf: tarfile.TarFile, index: dict) -> tuple[dict, list[str]]:
    """Follow an OCI index to the single image's config. Nested indexes (multi-platform)
    are descended; with several platform manifests we cannot pick for the user, so that
    is rejected like a multi-image archive."""
    manifests = index.get("manifests") or []
    tags = [m.get("annotations", {}).get("io.containerd.image.name")
            or m.get("annotations", {}).get("org.opencontainers.image.ref.name")
            for m in manifests]
    tags = [t for t in tags if t]
    depth = 0
    while True:
        if len(manifests) != 1:
            raise ValueError(
                f"The archive holds {len(manifests)} images/platforms — upload an archive of exactly one "
                f"image (docker save one tag, or build for a single --platform)")
        doc = _read_json(tf, _blob_path(manifests[0].get("digest", "")))
        if not isinstance(doc, dict):
            raise ValueError("The archive's OCI manifest blob is missing or unreadable")
        if "manifests" in doc and depth < 4:   # nested index
            manifests = doc.get("manifests") or []
            depth += 1
            continue
        cfg_digest = (doc.get("config") or {}).get("digest", "")
        config = _read_json(tf, _blob_path(cfg_digest))
        if not isinstance(config, dict):
            raise ValueError("The archive's image config blob is missing or unreadable")
        return config, tags


def inspect_archive(path: str) -> dict:
    """Read an image archive's config without loading it into docker.

    Returns `{ports, udp_ports, websocket, architecture, os, repo_tags}` where `ports` are
    the TCP ports the image EXPOSEs (sorted). Raises ValueError with a user-facing message
    when the file is not a usable single-image archive."""
    try:
        tf = tarfile.open(path, "r:*")
    except (tarfile.TarError, OSError, EOFError):
        raise ValueError("Uploaded data is not a docker image archive (expected `docker save` output: .tar or .tar.gz)")
    try:
        repo_tags: list[str] = []
        manifest = _read_json(tf, "manifest.json")
        if isinstance(manifest, list) and manifest:
            if len(manifest) != 1:
                raise ValueError(
                    f"The archive holds {len(manifest)} images — upload an archive of exactly one image")
            entry = manifest[0]
            repo_tags = [t for t in (entry.get("RepoTags") or []) if t]
            config = _read_json(tf, entry.get("Config", ""))
            if not isinstance(config, dict):
                raise ValueError("The archive's image config is missing or unreadable")
        else:
            index = _read_json(tf, "index.json")
            if not isinstance(index, dict):
                raise ValueError("Not a docker image archive: no manifest.json or index.json inside "
                                 "(expected `docker save` output)")
            config, repo_tags = _oci_config(tf, index)
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ValueError(f"The image archive is corrupt or truncated: {exc}")
    finally:
        tf.close()

    cfg = config.get("config") or config.get("Config") or {}
    tcp, udp = set(), set()
    for key in (cfg.get("ExposedPorts") or {}):
        port, _, proto = str(key).partition("/")
        if port.isdigit():
            (udp if proto.lower() == "udp" else tcp).add(int(port))

    text_parts = [h.get("created_by", "") for h in (config.get("history") or []) if isinstance(h, dict)]
    text_parts += [f"{k}={v}" for k, v in (cfg.get("Labels") or {}).items()]
    text_parts += list(cfg.get("Env") or [])
    text_parts += list(cfg.get("Cmd") or []) + list(cfg.get("Entrypoint") or [])

    return {
        "ports": sorted(tcp),
        "udp_ports": sorted(udp),
        "websocket": scan.uses_websocket("\n".join(str(p) for p in text_parts)),
        "architecture": _norm_arch(config.get("architecture")),
        "os": config.get("os"),
        "repo_tags": repo_tags,
    }


def host_architecture() -> str | None:
    """The docker daemon's architecture, normalized to Go/OCI names (amd64, arm64…).
    None when docker cannot be asked — the caller then skips the check."""
    try:
        res = subprocess.run(["docker", "info", "--format", "{{.Architecture}}"],
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _norm_arch(res.stdout) if res.returncode == 0 and res.stdout.strip() else None


def check_runnable(info: dict) -> None:
    """Raise ValueError if the image cannot run on this host (wrong OS or CPU arch)."""
    if info.get("os") and info["os"] != "linux":
        raise ValueError(f"The image is built for '{info['os']}', but this host runs linux containers")
    host = host_architecture()
    img = info.get("architecture")
    if host and img and img != host:
        raise ValueError(
            f"The image is built for {img}, but this server is {host} — rebuild it with "
            f"`docker build --platform linux/{host}`")
