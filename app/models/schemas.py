from pydantic import BaseModel, field_validator
from typing import Optional, List
from datetime import datetime
from enum import Enum


class ProjectType(str, Enum):
    user = "user"       # created directly by the user via POST /projects
    plugin = "plugin"   # created from a (normal) plugin
    system = "system"   # created from a system plugin — hidden from the web UI


def validate_project_slug(v: str) -> str:
    """Project names must be DNS-safe slugs (used in subdomains + container names)."""
    import re
    if not re.match(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$', v) and len(v) > 1:
        raise ValueError("name must be lowercase alphanumeric with hyphens (no leading/trailing hyphens)")
    return v


def validate_custom_domain(v: str) -> str:
    """A custom domain must be a valid fully-qualified hostname (e.g. app.acme.com).

    Lowercased; ≤253 chars; at least one dot; each label 1-63 chars, alphanumeric or
    hyphen with no leading/trailing hyphen. This is the hostname used verbatim for nginx
    server_name + the Let's Encrypt cert path."""
    import re
    v = v.strip().lower().rstrip(".")
    label = r'(?!-)[a-z0-9-]{1,63}(?<!-)'
    if len(v) > 253 or not re.match(rf'^{label}(\.{label})+$', v):
        raise ValueError("custom_domain must be a valid fully-qualified domain (e.g. app.acme.com)")
    return v


# ── Requests ──────────────────────────────────────────────────────────────────

class DeployMode(str, Enum):
    dockerfile = "dockerfile"   # single container, one Dockerfile (the default)
    compose = "compose"         # multi-container, one docker-compose.yml


class ProjectCreateRequest(BaseModel):
    name: str
    # No deploy_mode here: a project is created empty ("pending") and its mode is
    # auto-detected from the first upload (Dockerfile vs docker-compose.yml in the root).

    @field_validator("name")
    @classmethod
    def name_must_be_slug(cls, v: str) -> str:
        return validate_project_slug(v)


class ExecRequest(BaseModel):
    command: str


class SetDomainRequest(BaseModel):
    """Set or clear a component's custom domain. None/empty clears it (reverts to the
    auto-generated subdomain)."""
    custom_domain: Optional[str] = None

    @field_validator("custom_domain")
    @classmethod
    def domain_must_be_fqdn(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v.strip() == "":
            return None
        return validate_custom_domain(v)


class PluginAddRequest(BaseModel):
    project_name: str

    @field_validator("project_name")
    @classmethod
    def name_must_be_slug(cls, v: str) -> str:
        return validate_project_slug(v)


# ── Responses ─────────────────────────────────────────────────────────────────

class ContainerInfo(BaseModel):
    """The single container of a dockerfile-mode project."""
    subdomain: Optional[str] = None        # effective hostname served (custom domain if set, else auto subdomain)
    custom_domain: Optional[str] = None    # the override, when one is set
    local_port: Optional[int] = None
    container_port: Optional[int] = None
    image_name: Optional[str] = None
    container_name: Optional[str] = None
    ssl_enabled: bool = False
    websocket: bool = False
    container_status: str = "not_found"   # running | exited | not_found | no_image | error


class ServiceInfo(BaseModel):
    """One exposed service of a compose-mode project."""
    name: str
    subdomain: str                         # effective hostname served (custom domain if set, else auto subdomain)
    custom_domain: Optional[str] = None    # the override, when one is set
    local_port: int
    container_port: int
    container_name: str
    ssl_enabled: bool = False
    websocket: bool = False
    container_status: str = "not_found"


class ProjectResponse(BaseModel):
    name: str
    type: str
    deploy_mode: str                       # dockerfile | compose
    created_at: datetime
    container: Optional[ContainerInfo] = None   # dockerfile mode
    services: List[ServiceInfo] = []            # compose mode


class UploadResponse(BaseModel):
    status: str                    # ok | error
    message: str
    count: int                     # files written this upload
    files: List[str] = []          # relative paths written
    deploy_mode: str               # pending | dockerfile | compose (after autodetect)
    provisioned: bool = False      # whether a manifest was found and the project wired up
    project: Optional[ProjectResponse] = None  # refreshed project view when provisioned


class DockerJobStatusResponse(BaseModel):
    """Returned by every async docker endpoint and by GET /status."""
    status: str                     # running | done | error | aborted | no_job | waiting_interactive
    operation: Optional[str] = None # build | start | stop | exec | provision | install
    message: str
    logs: str = ""
    exit_code: Optional[int] = None


class PluginResponse(BaseModel):
    name: str
    description: str
    about: str = ""     # long-form Markdown (ABOUT.md); empty when the plugin ships none
    deploy_mode: str = "dockerfile"       # dockerfile | compose
    container_port: Optional[int] = None  # dockerfile-mode only
    has_install: bool   # whether the plugin ships an install.sh
    interactive: bool = False  # install.sh runs interactively over a WebSocket session
    type: str           # project type this plugin creates (user | plugin | system); "system" is hidden in the web UI


class PluginAddResponse(BaseModel):
    status: str          # ok | error
    message: str
    project: ProjectResponse
    job: DockerJobStatusResponse
    # Set when job.status == "waiting_interactive": the WebSocket path the client must
    # connect to in order to run install.sh interactively (see routers/plugins.py).
    ws_path: Optional[str] = None


class ContainerResponse(BaseModel):
    status: str
    message: str


class ExecResponse(BaseModel):
    status: str
    output: str
    exit_code: int


class SslResponse(BaseModel):
    status: str
    message: str
    ssl_enabled: bool


class ProjectDeleteResponse(BaseModel):
    status: str           # ok | partial
    message: str
    details: List[str]    # per-step log of what was done / skipped
