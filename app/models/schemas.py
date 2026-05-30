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


class PluginAddRequest(BaseModel):
    project_name: str

    @field_validator("project_name")
    @classmethod
    def name_must_be_slug(cls, v: str) -> str:
        return validate_project_slug(v)


# ── Responses ─────────────────────────────────────────────────────────────────

class ContainerInfo(BaseModel):
    """The single container of a dockerfile-mode project."""
    subdomain: Optional[str] = None
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
    subdomain: str
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
    status: str                     # running | done | error | aborted | no_job
    operation: Optional[str] = None # build | start | stop | exec | provision
    message: str
    logs: str = ""
    exit_code: Optional[int] = None


class PluginResponse(BaseModel):
    name: str
    description: str
    deploy_mode: str = "dockerfile"       # dockerfile | compose
    container_port: Optional[int] = None  # dockerfile-mode only
    has_install: bool   # whether the plugin ships an install.sh
    type: str           # project type this plugin creates (user | plugin | system); "system" is hidden in the web UI


class PluginAddResponse(BaseModel):
    status: str          # ok | error
    message: str
    project: ProjectResponse
    job: DockerJobStatusResponse


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
