"""
backup_targets.py — remote destinations for backup archives, declared in the server's `.env`.

Credentials must not reach the database, an API response or the web UI, so a *target* is not
a table: it is a named block of keys in freeholdy's own `.env`, and the DB stores nothing but
the target's name.

    BACKUP_TARGET_offsite_TYPE=rsync
    BACKUP_TARGET_offsite_HOST=backup.example.com
    BACKUP_TARGET_offsite_USER=freeholdy
    BACKUP_TARGET_offsite_SSH_KEY=/root/.ssh/backup_ed25519
    BACKUP_TARGET_offsite_PATH=/srv/backups

    BACKUP_TARGET_ftpbox_TYPE=ftp
    BACKUP_TARGET_ftpbox_HOST=ftp.example.com
    BACKUP_TARGET_ftpbox_USER=alexey
    BACKUP_TARGET_ftpbox_PASSWORD=…
    BACKUP_TARGET_ftpbox_PATH=/backups

`Settings` (pydantic-settings) ignores keys it does not declare and never exports them into
the process environment, so these are read from the file directly — through `env_service.parse`,
which already understands comments, `export ` and quoting. `os.environ` is overlaid on top so
a systemd `Environment=` line or a container env var can override a file value.

Two transports, both dependency-free:
  * `rsync` — shells out to `rsync -az` over ssh (the ssh key is a path on the server).
  * `ftp` / `ftps` — stdlib `ftplib`.

Every function returns `(ok: bool, message: str)` like the other service modules, so callers
decide the HTTP shape and a failed upload can be recorded without failing the backup.
"""

import ftplib
import os
import re
import subprocess

from app.config import settings
from app.services import env_service

# BACKUP_TARGET_{name}_{FIELD}. The name is whatever is between the prefix and the last
# underscore-separated field, so `BACKUP_TARGET_my_box_HOST` names the target `my_box`.
FIELDS = ("TYPE", "HOST", "PORT", "USER", "PASSWORD", "SSH_KEY", "PATH", "TLS")
_KEY_RE = re.compile(
    r"^BACKUP_TARGET_(?P<name>.+?)_(?P<field>" + "|".join(FIELDS) + r")$"
)

TYPES = ("rsync", "ftp", "ftps")
SECRET_FIELDS = ("password", "ssh_key")


def env_file_path() -> str:
    """The `.env` targets are read from — the same file `app/config.py` loads settings from,
    resolved against the repo root so a relative CWD (systemd, a test runner) cannot miss it.
    `BACKUP_ENV_FILE` overrides it."""
    override = os.environ.get("BACKUP_ENV_FILE")
    if override:
        return override
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(repo_root, ".env")


def _raw_values() -> dict:
    """Every BACKUP_TARGET_* key/value, `.env` first and `os.environ` winning."""
    values: dict = {}
    path = env_file_path()
    try:
        with open(path) as f:
            content = f.read()
    except OSError:
        content = ""
    pairs, _errors = env_service.parse(content)
    for key, value in pairs:
        if key.startswith("BACKUP_TARGET_"):
            values[key] = value
    for key, value in os.environ.items():
        if key.startswith("BACKUP_TARGET_"):
            values[key] = value
    return values


def load_targets() -> dict:
    """All declared targets as `{name: {type, host, port, user, password, ssh_key, path, tls}}`.

    A block missing `TYPE` or naming an unknown one is dropped rather than raising — a typo in
    `.env` must not take the API down; `GET /backups/targets` simply won't list it.
    """
    grouped: dict = {}
    for key, value in _raw_values().items():
        m = _KEY_RE.match(key)
        if not m:
            continue
        grouped.setdefault(m.group("name"), {})[m.group("field").lower()] = value

    targets = {}
    for name, fields in grouped.items():
        kind = (fields.get("type") or "").strip().lower()
        if kind not in TYPES:
            continue
        targets[name] = {
            "name": name,
            "type": kind,
            "host": (fields.get("host") or "").strip(),
            "port": (fields.get("port") or "").strip(),
            "user": (fields.get("user") or "").strip(),
            "password": fields.get("password") or "",
            "ssh_key": (fields.get("ssh_key") or "").strip(),
            "path": (fields.get("path") or "").strip(),
            "tls": (fields.get("tls") or "").strip().lower() in ("1", "true", "yes", "on"),
        }
    return targets


def get_target(name: str) -> dict | None:
    if not name:
        return None
    return load_targets().get(name)


def public(target: dict) -> dict:
    """The form safe to return over the API: everything but the secrets."""
    return {
        "name": target["name"],
        "type": target["type"],
        "host": target["host"],
        "port": target["port"] or None,
        "user": target["user"] or None,
        "path": target["path"] or None,
        "has_password": bool(target["password"]),
        "has_ssh_key": bool(target["ssh_key"]),
    }


def list_targets() -> list:
    return [public(t) for t in sorted(load_targets().values(), key=lambda t: t["name"])]


# ── rsync ───────────────────────────────────────────────────────────────────────

def _ssh_command(target: dict) -> str:
    parts = ["ssh"]
    if target["ssh_key"]:
        parts += ["-i", target["ssh_key"]]
    if target["port"]:
        parts += ["-p", target["port"]]
    # accept-new pins the host key on first contact but still refuses a *changed* one, which
    # is the right trade-off for an unattended backup to a box the operator just configured.
    parts += ["-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes"]
    return " ".join(parts)


def _rsync_destination(target: dict, remote_name: str) -> str:
    base = target["path"].rstrip("/")
    remote = f"{base}/{remote_name}" if base else remote_name
    host = f"{target['user']}@{target['host']}" if target["user"] else target["host"]
    return f"{host}:{remote}"


def _rsync_upload(target: dict, local_path: str, remote_name: str) -> tuple:
    dest = _rsync_destination(target, remote_name)
    cmd = ["rsync", "-a", "--partial", "-e", _ssh_command(target), local_path, dest]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=settings.BACKUP_TRANSFER_TIMEOUT)
    except FileNotFoundError:
        return False, "rsync is not installed on this server"
    except subprocess.TimeoutExpired:
        return False, f"rsync timed out after {settings.BACKUP_TRANSFER_TIMEOUT}s"
    if result.returncode != 0:
        return False, f"rsync failed: {(result.stderr or result.stdout).strip()}"
    return True, dest


def _rsync_list(target: dict) -> tuple:
    base = target["path"].rstrip("/") or "."
    host = f"{target['user']}@{target['host']}" if target["user"] else target["host"]
    cmd = _ssh_command(target).split() + [host, f"ls -1 {base}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()
    return True, [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _rsync_delete(target: dict, remote_name: str) -> tuple:
    base = target["path"].rstrip("/")
    remote = f"{base}/{remote_name}" if base else remote_name
    host = f"{target['user']}@{target['host']}" if target["user"] else target["host"]
    # Quoted with single quotes; remote names are freeholdy-generated slugs + timestamps.
    cmd = _ssh_command(target).split() + [host, f"rm -f '{remote}'"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()
    return True, f"removed {remote}"


# ── FTP ─────────────────────────────────────────────────────────────────────────

def _ftp_connect(target: dict):
    cls = ftplib.FTP_TLS if (target["type"] == "ftps" or target["tls"]) else ftplib.FTP
    port = int(target["port"]) if target["port"] else 21
    ftp = cls()
    ftp.connect(target["host"], port, timeout=60)
    ftp.login(target["user"] or "anonymous", target["password"] or "")
    if isinstance(ftp, ftplib.FTP_TLS):
        ftp.prot_p()
    if target["path"]:
        _ftp_chdir(ftp, target["path"])
    return ftp


def _ftp_chdir(ftp, path: str) -> None:
    """cwd into `path`, creating each missing segment — an FTP server has no `mkdir -p`."""
    for segment in path.strip("/").split("/"):
        if not segment:
            continue
        try:
            ftp.cwd(segment)
        except ftplib.error_perm:
            ftp.mkd(segment)
            ftp.cwd(segment)


def _ftp_upload(target: dict, local_path: str, remote_name: str) -> tuple:
    try:
        ftp = _ftp_connect(target)
    except Exception as exc:
        return False, f"FTP connection failed: {exc}"
    try:
        with open(local_path, "rb") as f:
            ftp.storbinary(f"STOR {remote_name}", f)
        return True, f"{target['path'].rstrip('/')}/{remote_name}" if target["path"] else remote_name
    except Exception as exc:
        return False, f"FTP upload failed: {exc}"
    finally:
        try:
            ftp.quit()
        except Exception:
            pass


def _ftp_list(target: dict) -> tuple:
    try:
        ftp = _ftp_connect(target)
    except Exception as exc:
        return False, f"FTP connection failed: {exc}"
    try:
        return True, [n for n in ftp.nlst() if n not in (".", "..")]
    except Exception as exc:
        return False, f"FTP list failed: {exc}"
    finally:
        try:
            ftp.quit()
        except Exception:
            pass


def _ftp_delete(target: dict, remote_name: str) -> tuple:
    try:
        ftp = _ftp_connect(target)
    except Exception as exc:
        return False, f"FTP connection failed: {exc}"
    try:
        ftp.delete(remote_name)
        return True, f"removed {remote_name}"
    except Exception as exc:
        return False, f"FTP delete failed: {exc}"
    finally:
        try:
            ftp.quit()
        except Exception:
            pass


# ── Public dispatch ─────────────────────────────────────────────────────────────

def upload(target: dict, local_path: str, remote_name: str) -> tuple:
    """Ship one archive. Returns `(ok, remote_path_or_error)`."""
    if target["type"] == "rsync":
        return _rsync_upload(target, local_path, remote_name)
    return _ftp_upload(target, local_path, remote_name)


def list_remote(target: dict) -> tuple:
    """`(ok, [names])` — used to prune the destination down to `keep_remote`."""
    if target["type"] == "rsync":
        return _rsync_list(target)
    return _ftp_list(target)


def delete_remote(target: dict, remote_name: str) -> tuple:
    if target["type"] == "rsync":
        return _rsync_delete(target, remote_name)
    return _ftp_delete(target, remote_name)


def check(target: dict) -> tuple:
    """Connectivity probe behind `POST /backups/targets/{name}/test` — a listing is the
    cheapest operation that proves credentials, reachability and the path all work."""
    ok, result = list_remote(target)
    if not ok:
        return False, str(result)
    return True, f"{target['type']} target '{target['name']}' reachable ({len(result)} file(s) at destination)"
