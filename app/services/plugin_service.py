"""
plugin_service.py
Discovery and staging for the plugin catalog.

A plugin is a directory under settings.PLUGINS_DIR containing:
  plugin.json   — manifest: {name?, description, deploy_mode, container_port, domain_prefix?}
  Dockerfile    — required; copied into the build context at add time
  install.sh    — optional; populates the build context (see provision_from_plugin)
  <assets>      — any files install.sh / the Dockerfile reference

Plugins are trusted, in-repo content — there is no upload path. The API only lists
and instantiates what ships with freeholdy.
"""

import json
import os
import shutil
from typing import Optional

from app.config import settings

_VALID_PROJECT_TYPES = {"user", "plugin", "system"}
_VALID_DEPLOY_MODES = {"dockerfile", "compose"}
_DEFAULT_PROJECT_TYPE = "plugin"


def _plugins_root() -> str:
    return os.path.abspath(settings.PLUGINS_DIR)


def _load_manifest(plugin_dir: str, name: str) -> Optional[dict]:
    """Read + normalise a plugin's manifest. Returns None if the dir isn't a valid plugin.

    Two flavours, selected by the manifest's "deploy_mode":
      - "dockerfile" (default): single container; requires a Dockerfile; carries container_port.
      - "compose": requires a docker-compose.yml; services + ports come from that file,
                   so container_port is unused (None).
    """
    manifest_path = os.path.join(plugin_dir, "plugin.json")
    if not os.path.isfile(manifest_path):
        return None

    try:
        with open(manifest_path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    deploy_mode = str(data.get("deploy_mode", "dockerfile"))
    if deploy_mode not in _VALID_DEPLOY_MODES:
        return None

    dockerfile_path = os.path.join(plugin_dir, "Dockerfile")
    compose_path = os.path.join(plugin_dir, "docker-compose.yml")

    container_port: Optional[int]
    if deploy_mode == "compose":
        if not os.path.isfile(compose_path):
            return None
        container_port = None
    else:
        if not os.path.isfile(dockerfile_path):
            return None
        try:
            container_port = int(data.get("container_port", 80))
        except (TypeError, ValueError):
            return None

    # The project created from this plugin takes its type straight from the manifest's
    # "type" field (defaults to "plugin"). Unknown values fall back to the default.
    project_type = str(data.get("type", _DEFAULT_PROJECT_TYPE))
    if project_type not in _VALID_PROJECT_TYPES:
        project_type = _DEFAULT_PROJECT_TYPE

    plugin_name = str(data.get("name") or name)

    # Subdomain label override; None means "not set" (caller uses its own default):
    #   "domain_prefix" absent          -> None (dockerfile: use project name; compose: {svc}.{project}.{domain})
    #   "domain_prefix": "" (or null)   -> "" (pins to base domain)
    #   "domain_prefix": "<value>"      -> that value
    if "domain_prefix" in data:
        dp = data["domain_prefix"]
        domain_prefix = "" if dp is None else str(dp)
    else:
        domain_prefix = None

    install_path = os.path.join(plugin_dir, "install.sh")
    has_install = os.path.isfile(install_path)
    return {
        "name": plugin_name,
        "description": str(data.get("description", "")),
        "deploy_mode": deploy_mode,
        "container_port": container_port,
        "has_install": has_install,
        "type": project_type,
        "domain_prefix": domain_prefix,
        "dir": plugin_dir,
        "dockerfile": dockerfile_path if deploy_mode == "dockerfile" else None,
        "compose_file": compose_path if deploy_mode == "compose" else None,
        "install": install_path if has_install else None,
    }


def list_plugins() -> list[dict]:
    """Return manifests for every valid plugin directory, sorted by name."""
    root = _plugins_root()
    if not os.path.isdir(root):
        return []
    plugins = []
    for entry in sorted(os.listdir(root)):
        plugin_dir = os.path.join(root, entry)
        if not os.path.isdir(plugin_dir):
            continue
        manifest = _load_manifest(plugin_dir, entry)
        if manifest:
            plugins.append(manifest)
    return plugins


def get_plugin(name: str) -> Optional[dict]:
    """Resolve a plugin by directory name. Rejects path traversal; returns None if unknown."""
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None
    root = _plugins_root()
    plugin_dir = os.path.join(root, name)
    # Guard against any traversal that slipped through.
    if os.path.commonpath([root, os.path.abspath(plugin_dir)]) != root:
        return None
    if not os.path.isdir(plugin_dir):
        return None
    return _load_manifest(plugin_dir, name)


def stage_dockerfile(plugin: dict, project_dir: str) -> str:
    """Copy the plugin's Dockerfile into the project build context. Returns the dest path."""
    os.makedirs(project_dir, exist_ok=True)
    dest = os.path.join(project_dir, "Dockerfile")
    with open(plugin["dockerfile"], "rb") as src, open(dest, "wb") as out:
        out.write(src.read())
    return dest


def stage_compose(plugin: dict, project_dir: str) -> str:
    """Copy a compose plugin's whole source tree into the compose project dir so the
    docker-compose.yml and its build: contexts (frontend/, backend/, …) sit together.
    Skips plugin.json. Returns the staged docker-compose.yml path."""
    os.makedirs(project_dir, exist_ok=True)
    for entry in os.listdir(plugin["dir"]):
        if entry == "plugin.json":
            continue
        src = os.path.join(plugin["dir"], entry)
        dst = os.path.join(project_dir, entry)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
    return os.path.join(project_dir, "docker-compose.yml")
