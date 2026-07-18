"""
compose_service.py

Parsing and override generation for compose-mode projects.

A compose project is described by a single user-supplied `docker-compose.yml`.
freeholdy discovers its services, exposes every service that publishes a TCP
port at `{service}.{project}.{base_domain}`, and pins container names +
loopback-only port bindings via a generated `docker-compose.override.yml`.
UDP-only services are never exposed (nginx is HTTP-only and can't proxy UDP);
their original host bindings pass through the compose merge untouched.
Functions here are pure (parsing) or filesystem-only (file writing);
orchestration lives in the compose router.
"""

import os
import yaml

from app.config import settings
from app.models.schemas import validate_project_slug
from app.services import scan


def _extract_container_port(entry) -> int | None:
    """Return the in-container (target) TCP port for one compose `ports:` entry.

    Handles the common short forms ("3000", "8080:80", "127.0.0.1:8080:80",
    "8080:80/tcp", port ranges) and the long dict form ({target: 80, ...}).
    UDP entries ("10000:10000/udp" or {..., protocol: udp}) return None —
    nginx can't proxy UDP, so they never make a service exposed; the override
    leaves them alone and the original host binding survives the compose merge.
    Returns None if no TCP port can be determined.
    """
    # Long form: a mapping with an explicit target.
    if isinstance(entry, dict):
        if str(entry.get("protocol", "tcp")).lower() == "udp":
            return None
        target = entry.get("target")
        return int(target) if target is not None else None

    # Short form: a string/number "host:container" — the container port is last.
    text = str(entry)
    if "/" in text:
        text, proto = text.rsplit("/", 1)
        if proto.strip().lower() == "udp":
            return None
    container = text.split(":")[-1]              # last segment = container port
    container = container.split("-")[0]          # first port of a range
    try:
        return int(container)
    except ValueError:
        return None


def parse_services(compose_text: str) -> list[dict]:
    """Parse a compose file into a list of service descriptors.

    Each descriptor is `{name, exposed: bool, container_port: int | None}`.
    A service is *exposed* (gets an nginx endpoint) iff it declares at least
    one TCP `ports:` entry. UDP-only services are not exposed — no loopback
    rebind, no nginx vhost, no subdomain — and keep their original bindings.
    Raises ValueError on malformed YAML, a missing `services:` block, or a
    service name that is not a DNS-safe slug (it becomes a subdomain label).
    """
    try:
        doc = yaml.safe_load(compose_text)
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML: {e}")

    if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict):
        raise ValueError("Compose file has no 'services' mapping")

    services: list[dict] = []
    for name, spec in doc["services"].items():
        validate_project_slug(str(name))   # raises ValueError if not slug-safe
        spec = spec or {}
        ports = spec.get("ports") or []
        # First entry that yields a TCP port wins; UDP entries are skipped, so a
        # UDP-only service (e.g. a WebRTC media bridge) is never exposed.
        container_port = None
        for entry in ports:
            container_port = _extract_container_port(entry)
            if container_port is not None:
                break
        # Per-service WebSocket detection: scan this service's name + its YAML block only.
        block = f"{name}\n{yaml.safe_dump(spec, default_flow_style=False)}"
        services.append({
            "name": str(name),
            "exposed": container_port is not None,
            "container_port": container_port,
            "websocket": scan.uses_websocket(block),
        })

    if not services:
        raise ValueError("Compose file defines no services")
    return services


def project_dir(project: str) -> str:
    """The single on-disk working directory for a project, used by both deploy modes.

    Uploaded files, the Dockerfile / docker-compose.yml, and the generated override all
    live here, so the deploy mode can be auto-detected by scanning this directory's root.
    """
    return os.path.join(settings.PROJECTS_DIR, project)


def compose_file_path(project: str) -> str:
    return os.path.join(project_dir(project), "docker-compose.yml")


def override_file_path(project: str) -> str:
    return os.path.join(project_dir(project), "docker-compose.override.yml")


def write_files(project: str, compose_text: str, services: list[dict]) -> tuple[str, str]:
    """Persist the uploaded compose file and a generated override.

    `services` is the descriptor list from `parse_services`, augmented with a
    `local_port` key for every exposed service (the allocated loopback port).
    The override pins a deterministic `container_name` and `restart: unless-stopped`
    on every service, and rebinds each exposed service's port to
    `127.0.0.1:{local_port}:{container_port}` so nothing is published beyond loopback.
    Returns `(compose_path, override_path)`.
    """
    d = project_dir(project)
    os.makedirs(d, exist_ok=True)

    compose_path = compose_file_path(project)
    with open(compose_path, "w") as f:
        f.write(compose_text)

    override_services: dict[str, dict] = {}
    for svc in services:
        name = svc["name"]
        cfg: dict = {
            "container_name": f"freeholdy_{project}_{name}",
            "restart": "unless-stopped",
        }
        if svc.get("exposed"):
            cfg["ports"] = [f"127.0.0.1:{svc['local_port']}:{svc['container_port']}"]
        override_services[name] = cfg

    override_path = override_file_path(project)
    with open(override_path, "w") as f:
        f.write("# Generated by freeholdy. Do not edit manually.\n")
        yaml.safe_dump({"services": override_services}, f, default_flow_style=False, sort_keys=False)

    return compose_path, override_path


def backfill_unexposed_services(db) -> int:
    """Insert ComposeService rows for compose services that predate unexposed-service
    tracking (host-networking / UDP-only / internal-only services got no row before).

    Idempotent and cheap: for every compose project whose compose file is on disk, re-parse
    it and add a row (exposed=False, NULL port/subdomain) for any service that has no row yet.
    Runs once at startup (needs YAML parsing, which the sqlite migration can't do). Returns
    the number of rows inserted. Callers commit.
    """
    from app.models.orm import Project, ComposeService  # local import: avoid import cycle

    inserted = 0
    projects = db.query(Project).filter(Project.deploy_mode == "compose").all()
    for project in projects:
        path = compose_file_path(project.name)
        if not os.path.exists(path):
            continue
        try:
            with open(path) as f:
                services = parse_services(f.read())
        except (OSError, ValueError):
            continue  # unreadable/malformed compose — skip; a redeploy will fix it
        existing = {s.name for s in project.services}
        for svc in services:
            if svc["name"] in existing or svc["exposed"]:
                continue
            db.add(ComposeService(
                project_id=project.id,
                name=svc["name"],
                exposed=False,
                container_name=f"freeholdy_{project.name}_{svc['name']}",
                websocket=svc["websocket"],
            ))
            inserted += 1
    if inserted:
        db.commit()
    return inserted
