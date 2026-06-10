import os
import subprocess
from typing import Tuple
from jinja2 import Environment, FileSystemLoader
from app.config import settings

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
_jinja = Environment(loader=FileSystemLoader(_TEMPLATES_DIR), trim_blocks=True, lstrip_blocks=True)


# ── Config generation ─────────────────────────────────────────────────────────

def _config_filename(project_name: str) -> str:
    return f"freeholdy_{project_name}.conf"


def generate_http_config(project_name: str, parts: list[dict]) -> str:
    """HTTP-only config — used first so certbot can complete the ACME challenge.

    `parts` must already be filtered to the endpoints that should be proxied
    (callers drop `database` parts; compose passes only its exposed services)."""
    template = _jinja.get_template("nginx_http.conf.j2")
    return template.render(parts=parts, webroot=settings.CERTBOT_WEBROOT)


def generate_ssl_config(project_name: str, parts: list[dict]) -> str:
    """Full HTTPS config — written after certs have been issued.

    `parts` must already be filtered to the endpoints that should be proxied."""
    template = _jinja.get_template("nginx_ssl.conf.j2")
    return template.render(parts=parts, webroot=settings.CERTBOT_WEBROOT)


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
        ok, msg = test_config()
        if ok:
            reload()

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
