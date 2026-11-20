import glob
import json
import logging
import os
import subprocess
from typing import Tuple
from jinja2 import Environment, FileSystemLoader
from app.config import settings
from app.services import compose_service

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
_jinja = Environment(loader=FileSystemLoader(_TEMPLATES_DIR), trim_blocks=True, lstrip_blocks=True)
log = logging.getLogger(__name__)


# ── Config generation ─────────────────────────────────────────────────────────

def _config_filename(project_name: str) -> str:
    return f"freeholdy_{project_name}.conf"


def generate_http_config(project_name: str, parts: list[dict]) -> str:
    """HTTP-only config — used first so certbot can complete the ACME challenge.

    `parts` must already be filtered to the endpoints that should be proxied
    (callers drop `database` parts; compose passes only its exposed services)."""
    template = _jinja.get_template("nginx_http.conf.j2")
    return template.render(parts=parts, webroot=settings.CERTBOT_WEBROOT)


_EXTRA_CONF_NAME = "nginx-extra.conf"


def _load_extra_config(project_name: str) -> str:
    """Optional per-project nginx snippet, inlined into every SSL server block.

    Plugins ship an `nginx-extra.conf` next to their compose file; staging copies
    it into the project dir with the rest of the tree, so it is re-read on every
    config regeneration and removed with the project. Empty string when absent."""
    path = os.path.join(compose_service.project_dir(project_name), _EXTRA_CONF_NAME)
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def generate_ssl_config(project_name: str, parts: list[dict]) -> str:
    """Full HTTPS config — written after certs have been issued.

    `parts` must already be filtered to the endpoints that should be proxied."""
    template = _jinja.get_template("nginx_ssl.conf.j2")
    return template.render(parts=parts, webroot=settings.CERTBOT_WEBROOT,
                           extra_config=_load_extra_config(project_name))


def _write_config(project_name: str, content: str) -> str:
    filename = _config_filename(project_name)

    # Local backup
    os.makedirs(settings.NGINX_CONFIGS_DIR, exist_ok=True)
    with open(os.path.join(settings.NGINX_CONFIGS_DIR, filename), "w") as f:
        f.write(content)

    # nginx sites-available
    available = os.path.join(settings.NGINX_SITES_AVAILABLE, filename)
    with open(available, "w") as f:
        f.write(content)

    # Symlink sites-enabled
    enabled = os.path.join(settings.NGINX_SITES_ENABLED, filename)
    if not os.path.exists(enabled):
        os.symlink(available, enabled)

    return available


def remove_config(project_name: str):
    filename = _config_filename(project_name)
    for path in [
        os.path.join(settings.NGINX_SITES_ENABLED, filename),
        os.path.join(settings.NGINX_SITES_AVAILABLE, filename),
        os.path.join(settings.NGINX_CONFIGS_DIR, filename),
    ]:
        if os.path.exists(path) or os.path.islink(path):
            os.remove(path)
    _remove_stream(project_name)


# ── Stream (raw TCP) endpoints ────────────────────────────────────────────────
#
# A compose service labelled `freeholdy.proxy: tcp` is not proxied as HTTP. Its port-80
# vhost serves only the ACME challenge (so the usual HTTP-01 flow still issues a Let's
# Encrypt cert for its hostname) and there is no 443 server. Instead it gets nginx
# `stream` servers, written to NGINX_STREAMS_DIR (included from nginx's main context by
# scripts/setup_nginx_stream.sh):
#
#   * SNI   — a per-project TLS terminator on a unix socket, using the LE cert; the shared
#             listener on STREAM_SNI_PORT (ssl_preread) routes the still-encrypted
#             connection there by hostname through a per-project map file in sni/.
#             The shared listener file exists only while at least one map exists.
#   * port  — a plain passthrough on a dedicated public port; the service does its own TLS.
#
# Which of the two (or both) a project uses comes from `nginx-stream.json` in its project
# dir (written by the plugin's install.sh), re-read on every render like nginx-extra.conf.

STREAM_SETTINGS_NAME = "nginx-stream.json"
_SNI_LISTENER_NAME = "00-freeholdy-sni-listener.conf"
_DEFAULT_STREAM_SETTINGS = {"sni": True, "public_port": None, "alpn": None}


def stream_supported() -> bool:
    """True once scripts/setup_nginx_stream.sh has wired nginx's stream module in."""
    return os.path.isdir(settings.NGINX_STREAMS_DIR)


def load_stream_settings(project_name: str) -> dict:
    """`{sni: bool, public_port: int | None, alpn: str | None}` from the project dir's
    nginx-stream.json. Missing or unreadable → SNI only. Values are type-checked here;
    range/collision checks are the provisioning router's job."""
    out = dict(_DEFAULT_STREAM_SETTINGS)
    path = os.path.join(compose_service.project_dir(project_name), STREAM_SETTINGS_NAME)
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return out
    if not isinstance(data, dict):
        return out
    out["sni"] = bool(data.get("sni", True))
    port = data.get("public_port")
    try:
        out["public_port"] = int(port) if port not in (None, "", 0, "0") else None
    except (TypeError, ValueError):
        out["public_port"] = None
    alpn = data.get("alpn")
    out["alpn"] = str(alpn) if alpn and str(alpn).replace("-", "").replace(".", "").isalnum() else None
    return out


def _stream_conf_path(project_name: str) -> str:
    return os.path.join(settings.NGINX_STREAMS_DIR, _config_filename(project_name))


def _sni_map_path(project_name: str) -> str:
    return os.path.join(settings.NGINX_STREAMS_DIR, "sni", f"freeholdy_{project_name}.map")


def _stream_backup_path(project_name: str) -> str:
    return os.path.join(settings.NGINX_CONFIGS_DIR, f"stream_{_config_filename(project_name)}")


def _socket_path(project_name: str, service: str) -> str:
    return os.path.join(settings.NGINX_STREAM_SOCKET_DIR, f"freeholdy_{project_name}_{service}.sock")


def generate_stream_config(project_name: str, tcp_parts: list[dict], cert_ok: set[str]) -> tuple[str, str]:
    """Render (stream conf, sni map) for a project's TCP endpoints. An SNI terminator is
    emitted only for a hostname whose cert exists (`cert_ok`); a public-port passthrough
    needs no cert. Either string is "" when it has nothing to say."""
    st = load_stream_settings(project_name)
    parts, map_lines = [], []
    for p in tcp_parts:
        service = p.get("service") or "main"
        sni_active = bool(st["sni"]) and p["subdomain"] in cert_ok
        socket = _socket_path(project_name, service)
        parts.append({
            "subdomain": p["subdomain"],
            "cert_name": p["subdomain"],
            "local_port": p["local_port"],
            "sni_active": sni_active,
            "socket": socket,
            "alpn": st["alpn"],
            "public_port": st["public_port"],
        })
        if sni_active:
            map_lines.append(f"{p['subdomain']} unix:{socket};")
    if not any(x["sni_active"] or x["public_port"] for x in parts):
        return "", ""
    conf = _jinja.get_template("nginx_stream.conf.j2").render(
        project=project_name, parts=parts, sni_port=settings.STREAM_SNI_PORT)
    smap = ""
    if map_lines:
        smap = f"# SNI routes for {project_name} — generated by freeholdy. Do not edit manually.\n" \
               + "\n".join(map_lines) + "\n"
    return conf, smap


def _write_or_remove(path: str, content: str):
    if content:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
    elif os.path.exists(path) or os.path.islink(path):
        os.remove(path)


def _sync_sni_listener():
    """Write the shared SNI listener while any project routes by hostname; remove it
    otherwise, so STREAM_SNI_PORT is bound only when something actually uses it."""
    path = os.path.join(settings.NGINX_STREAMS_DIR, _SNI_LISTENER_NAME)
    has_maps = bool(glob.glob(os.path.join(settings.NGINX_STREAMS_DIR, "sni", "*.map")))
    content = ""
    if has_maps:
        content = _jinja.get_template("nginx_stream_sni.conf.j2").render(
            streams_dir=settings.NGINX_STREAMS_DIR, sni_port=settings.STREAM_SNI_PORT)
    _write_or_remove(path, content)


def _write_stream(project_name: str, tcp_parts: list[dict], cert_ok: set[str]) -> bool:
    """(Re)write — or remove, when there is nothing to serve — a project's stream files.
    Returns whether any stream file now exists for the project."""
    conf, smap = generate_stream_config(project_name, tcp_parts, cert_ok)
    _write_or_remove(_stream_conf_path(project_name), conf)
    _write_or_remove(_sni_map_path(project_name), smap)
    try:
        _write_or_remove(_stream_backup_path(project_name), conf)
    except OSError:
        pass  # the local backup copy is a convenience, never a reason to fail
    _sync_sni_listener()
    return bool(conf)


def _remove_stream(project_name: str):
    """Drop a project's stream conf + SNI map (and the shared listener if it was the last)."""
    for path in (_stream_conf_path(project_name), _sni_map_path(project_name),
                 _stream_backup_path(project_name)):
        if os.path.exists(path) or os.path.islink(path):
            os.remove(path)
    if stream_supported():
        _sync_sni_listener()


# ── nginx commands ────────────────────────────────────────────────────────────

def test_config() -> Tuple[bool, str]:
    result = subprocess.run(["sudo", "nginx", "-t"], capture_output=True, text=True)
    return result.returncode == 0, result.stderr.strip()


def reload() -> Tuple[bool, str]:
    result = subprocess.run(["sudo", "nginx", "-s", "reload"], capture_output=True, text=True)
    return result.returncode == 0, result.stderr.strip()


# ── SSL ───────────────────────────────────────────────────────────────────────

def issue_cert(subdomain: str) -> Tuple[bool, str]:
    """Issue a Let's Encrypt cert for `subdomain` via certbot's webroot authenticator.

    We deliberately use `--webroot` (not `--nginx`): freeholdy owns the nginx config
    files (sites dirs are root:nginx-managers 1775), and the `--nginx` plugin rewrites
    those files in place with a checkpoint/revert dance that fights that ownership and,
    if interrupted, leaves a poison checkpoint that bricks all later issuance. With
    webroot, certbot only drops the challenge token under CERTBOT_WEBROOT (served by the
    `/.well-known/acme-challenge/` location both nginx templates emit) and never touches
    nginx config. The webroot is created by install.sh; we also try here best-effort."""
    try:
        os.makedirs(os.path.join(settings.CERTBOT_WEBROOT, ".well-known", "acme-challenge"), exist_ok=True)
    except OSError:
        pass  # /var/www is root-owned; install.sh creates it. certbot (as root) fills it in.
    result = subprocess.run(
        [
            "sudo", "certbot", "certonly",
            "--webroot", "-w", settings.CERTBOT_WEBROOT,
            "--non-interactive",
            "--agree-tos",
            "--email", settings.CERTBOT_EMAIL,
            "-d", subdomain,
        ],
        capture_output=True,
        text=True,
    )
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output


# ── High-level project setup ──────────────────────────────────────────────────

def setup_nginx(project_name: str, endpoints: list[dict]) -> dict:
    """
    Full nginx + SSL setup for an explicit list of proxied endpoints.

    Each endpoint dict needs `subdomain`, `local_port`, `websocket`. The caller
    is responsible for excluding anything that should not be proxied (compose
    passes only services that publish a port).
    Steps:
      1. Write HTTP config → nginx reload  (enables ACME challenge)
      2. Issue cert per subdomain
      3. Write SSL config → nginx reload
    Returns dict with per-subdomain ssl results.
    """
    results: dict[str, dict] = {}

    # TCP endpoints (nginx stream) need the stream module wired in; refuse before
    # writing anything rather than leave a half-configured project.
    tcp_parts = [p for p in endpoints if p.get("proxy") == "tcp"]
    if tcp_parts and not stream_supported():
        return {"success": False, "ssl": {},
                "error": "nginx stream support is not installed — run "
                         "`sudo bash scripts/setup_nginx_stream.sh` on the server first"}

    # Step 1: HTTP config
    http_cfg = generate_http_config(project_name, endpoints)
    _write_config(project_name, http_cfg)
    ok, msg = test_config()
    if not ok:
        return {"success": False, "error": f"nginx config test failed: {msg}", "ssl": {}}
    reload()

    # Step 2: Issue certs
    all_ok = True
    for p in endpoints:
        success, msg = issue_cert(p["subdomain"])
        results[p["subdomain"]] = {"success": success, "message": msg}
        if not success:
            all_ok = False

    # Step 3: SSL config (only for subdomains where cert succeeded)
    successful = {sub for sub, r in results.items() if r["success"]}
    ssl_parts = [p for p in endpoints if p["subdomain"] in successful]

    if ssl_parts:
        ssl_cfg = generate_ssl_config(project_name, ssl_parts)
        _write_config(project_name, ssl_cfg)

    # Step 3b: stream (TCP) servers. Always synced when stream support exists, so a
    # re-upload that dropped the `freeholdy.proxy: tcp` label also drops stale files.
    wrote_stream = False
    if stream_supported():
        wrote_stream = _write_stream(project_name, tcp_parts, successful)

    if ssl_parts or wrote_stream:
        ok, msg = test_config()
        if ok:
            reload()
        elif wrote_stream:
            # Most likely the stream side (a port nginx can't bind is NOT caught by -t,
            # but a bad directive or module is): pull it so nginx stays loadable.
            _remove_stream(project_name)
            ok2, _ = test_config()
            if ok2:
                reload()
            return {"success": False, "ssl": results,
                    "error": f"nginx stream config test failed: {msg}"}

    return {"success": all_ok, "ssl": results}


def write_ssl_config(project_name: str, endpoints: list[dict]) -> bool:
    """Rewrite a project's HTTPS config from its current endpoints and reload nginx.
    Assumes certs already exist (does not run certbot). Used to apply a freshly
    detected `websocket` flag or a manual /ssl re-issue. Returns whether nginx reloaded."""
    cfg = generate_ssl_config(project_name, endpoints)
    _write_config(project_name, cfg)
    ok, _ = test_config()
    if ok:
        reload()
    return ok


def write_http_config(project_name: str, endpoints: list[dict]) -> bool:
    """Rewrite a project's HTTP-only config from its current endpoints and reload nginx.
    The SSL counterpart of write_ssl_config for projects with no cert yet — used by the
    blue/green switch to re-point nginx at a new loopback port when ssl is not enabled.
    Returns whether nginx reloaded."""
    cfg = generate_http_config(project_name, endpoints)
    _write_config(project_name, cfg)
    ok, _ = test_config()
    if ok:
        reload()
    return ok
