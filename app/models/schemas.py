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
        # Spell out the likely cause — the name becomes a subdomain + container name,
        # so it must be a DNS-safe slug: lowercase letters, digits and hyphens only.
        problems = []
        if v != v.lower():
            problems.append("no uppercase letters")
        if "_" in v:
            problems.append("no underscores (use '-')")
        if " " in v:
            problems.append("no spaces (use '-')")
        if v[:1] == "-" or v[-1:] == "-":
            problems.append("no leading/trailing hyphen")
        detail = ("name '%s' is not a valid project name — it must be a DNS-safe slug: "
                  "lowercase letters, digits and hyphens only" % v)
        if problems:
            detail += " (" + "; ".join(problems) + ")"
        raise ValueError(detail)
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

def validate_env_content(v: str) -> str:
    """Reject .env text freeholdy could not turn into a KEY=value file docker can read.

    Delegates to env_service.parse, which reports each problem with its line number."""
    from app.services import env_service  # lazy: services import schemas

    _, errors = env_service.parse(v or "")
    if errors:
        raise ValueError("invalid .env content:\n  - " + "\n  - ".join(errors))
    return v


class DeployMode(str, Enum):
    dockerfile = "dockerfile"   # single container, one Dockerfile (the default)
    compose = "compose"         # multi-container, one docker-compose.yml


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


def validate_git_url(v: str) -> str:
    """Accept an HTTP(S), ssh://, or scp-like (git@host:path) clone URL.

    The URL is handed to `git clone` as argv (never a shell string), so this is format
    validation, not injection defense. SSH URLs are accepted but rely on the server's
    own SSH keys — credential management is a future improvement."""
    import re
    v = v.strip()
    https = r"^https?://[^\s]+$"
    ssh = r"^ssh://[^\s]+$"
    scp = r"^[^\s/@]+@[^\s/:]+:[^\s]+$"   # git@github.com:owner/repo.git
    if not (re.match(https, v) or re.match(ssh, v) or re.match(scp, v)):
        raise ValueError("git_url must be an http(s)://, ssh://, or git@host:path clone URL")
    return v


class GitAddRequest(BaseModel):
    name: str
    git_url: str
    branch: Optional[str] = None
    # Optional dotenv text stored before the project is provisioned, so the FIRST container
    # start already has it (see `projects.py::apply_deploy_env`). Omitted/blank leaves any
    # stored env untouched — clearing is DELETE /projects/{name}/env.
    env: Optional[str] = None

    @field_validator("name")
    @classmethod
    def name_must_be_slug(cls, v: str) -> str:
        return validate_project_slug(v)

    @field_validator("git_url")
    @classmethod
    def git_url_must_be_valid(cls, v: str) -> str:
        return validate_git_url(v)

    @field_validator("env")
    @classmethod
    def env_must_parse(cls, v: Optional[str]) -> Optional[str]:
        return v if v is None else validate_env_content(v)


class GitKeyResponse(BaseModel):
    """The server's GitHub SSH public key (GET /git/key). Add the key to GitHub to clone
    private repos over SSH."""
    public_key: str
    created: bool          # True if the keypair was generated by this call
    key_path: str          # server-side path of the private key
    instructions: str      # human-readable "add to GitHub" steps


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
    env_count: int = 0                    # variables in the project's stored .env file


class ServiceInfo(BaseModel):
    """One service of a compose-mode project. `exposed` services publish a TCP port and
    have a subdomain/port/nginx endpoint; unexposed ones (host networking, UDP-only, or
    internal-only) have those fields null and are tracked only for status + exec."""
    name: str
    exposed: bool = True
    subdomain: Optional[str] = None        # effective hostname served (custom domain if set, else auto subdomain); null when unexposed
    custom_domain: Optional[str] = None    # the override, when one is set
    local_port: Optional[int] = None       # null when unexposed
    container_port: Optional[int] = None   # null when unexposed
    container_name: str
    ssl_enabled: bool = False
    websocket: bool = False
    container_status: str = "not_found"
    env_count: int = 0                     # variables in this service's own .env file


class ProjectResponse(BaseModel):
    name: str
    type: str
    deploy_mode: str                       # dockerfile | compose
    created_at: datetime
    container: Optional[ContainerInfo] = None   # dockerfile mode
    services: List[ServiceInfo] = []            # compose mode


class DockerJobStatusResponse(BaseModel):
    """Returned by every async docker endpoint and by GET /status."""
    status: str                     # running | done | error | aborted | no_job | waiting_interactive
    operation: Optional[str] = None # build | start | stop | exec | provision | install
    message: str
    logs: str = ""
    exit_code: Optional[int] = None


class UploadCompleteRequest(BaseModel):
    """Body of POST /projects/{name}/upload/complete.

    `upload_id`/`total_size` were previously separate embedded Body params; a single model
    accepts the identical JSON object, so existing clients are unaffected."""
    upload_id: str
    total_size: Optional[int] = None
    # Optional dotenv text stored before provisioning, so the FIRST container start already
    # has it. Omitted/blank leaves any stored env untouched.
    env: Optional[str] = None

    @field_validator("env")
    @classmethod
    def env_must_parse(cls, v: Optional[str]) -> Optional[str]:
        return v if v is None else validate_env_content(v)


class UploadResponse(BaseModel):
    status: str                    # ok | error
    message: str
    count: int                     # files written this upload
    files: List[str] = []          # relative paths written
    deploy_mode: str               # pending | dockerfile | compose (after autodetect)
    provisioned: bool = False      # whether a manifest was found and the project wired up
    project: Optional[ProjectResponse] = None  # refreshed project view when provisioned
    # Set when a manifest was provisioned: the build+run job was auto-launched (like git
    # deploy); connect to `ws_path` (WS /projects/{name}/deploy) to stream it. Absent for a
    # plain file sync (no manifest).
    ws_path: Optional[str] = None
    job: Optional[DockerJobStatusResponse] = None


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


# ── Versions / blue-green backups ───────────────────────────────────────────────

class VersionInfo(BaseModel):
    """One deployed version of a project (dockerfile or compose)."""
    version: int
    status: str                          # active | inactive | archived (compose: never inactive)
    image_name: Optional[str] = None     # None for compose (per-service tags are deterministic)
    container_name: Optional[str] = None # None for compose
    local_port: Optional[int] = None     # None once archived (port freed)
    container_status: str = "not_found"  # running | exited | not_found | error
    created_at: datetime


class VersionsResponse(BaseModel):
    project: str
    backup_limit: int
    version_counter: int                 # last version number assigned
    counts: dict                         # {"active": int, "inactive": int, "archived": int}
    versions: List[VersionInfo] = []     # newest first


class SetBackupLimitRequest(BaseModel):
    limit: int

    @field_validator("limit")
    @classmethod
    def limit_must_be_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("limit must be >= 1")
        return v


class RollbackRequest(BaseModel):
    version: int


class RollbackResponse(BaseModel):
    status: str          # ok | error
    message: str
    job: DockerJobStatusResponse
    ws_path: Optional[str] = None   # WS /projects/{name}/deploy — stream the rollback job


class SetEnvRequest(BaseModel):
    """The whole .env file, as text. Stored verbatim (comments and blank lines kept);
    freeholdy renders the normalized KEY=value form when a container starts."""
    content: str = ""

    @field_validator("content")
    @classmethod
    def content_must_parse(cls, v: str) -> str:
        return validate_env_content(v)


class LogsResponse(BaseModel):
    """A snapshot of what a container printed — the last N lines, not a live follow."""
    project: str
    service: Optional[str] = None    # null = project level (the container for dockerfile
                                     # mode; the whole stack for compose)
    container: Optional[str] = None  # null for a compose stack-wide tail
    tail: int                        # lines requested
    lines: int                       # lines actually returned
    content: str = ""
    status: str = "ok"               # ok | error
    message: str = ""


class EnvResponse(BaseModel):
    project: str
    service: Optional[str] = None   # null = project-level (the container's env for dockerfile
                                    # mode; the shared file for compose)
    content: str = ""
    keys: List[str] = []            # variable names only — never the values
    updated_at: Optional[datetime] = None
    applied: bool = True            # False → edited since the container last started;
                                    # POST /projects/{name}/restart to apply
    status: str = "ok"              # ok | error
    message: str = ""
