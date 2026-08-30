from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean, Table, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime
from app.models.database import Base


# A guest token may cover several projects, and a project may be covered by several guest
# tokens — hence a plain association table rather than a column on either side. Deleting a
# project drops its rows here (SQLAlchemy clears the secondary table) but leaves the tokens
# themselves alive: a token that loses its last project still authenticates, it just has
# nothing it may act on.
token_projects = Table(
    "token_projects",
    Base.metadata,
    Column("token_id", Integer, ForeignKey("tokens.id"), primary_key=True),
    Column("project_id", Integer, ForeignKey("projects.id"), primary_key=True),
)


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False, index=True)
    type = Column(String, nullable=False, default="user")  # user | plugin | system (system projects are hidden in the web UI)
    deploy_mode = Column(String, nullable=False, default="pending")  # pending (created, nothing uploaded yet) | dockerfile (single container) | compose (multi-container)
    created_at = Column(DateTime, default=datetime.utcnow)

    # ── dockerfile mode: the project IS a single container (these fields unused for compose) ──
    subdomain = Column(String, nullable=True)         # auto-generated, e.g. myapp.your_domain.com
    custom_domain = Column(String, nullable=True)     # optional override; when set it wins over subdomain
    local_port = Column(Integer, nullable=True, unique=True)   # VPS-side loopback port
    container_port = Column(Integer, nullable=True, default=80)  # port inside the container
    dockerfile_path = Column(String, nullable=True)
    image_name = Column(String, nullable=True)        # freeholdy_{name}:latest
    container_name = Column(String, nullable=True)    # freeholdy_{name}
    ssl_enabled = Column(Boolean, default=False)
    websocket = Column(Boolean, default=False)        # detected from the Dockerfile → nginx upgrade headers

    # ── compose mode ──
    compose_path = Column(String, nullable=True)      # path to the uploaded docker-compose.yml

    # ── git origin (set by a git deploy, cleared by a file upload) ──
    # These record how the project was LAST deployed, which is what makes POST /{name}/redeploy
    # honest: a NULL git_url means "not git-backed", never a stale repo from an earlier deploy.
    git_url = Column(String, nullable=True)           # clone URL, verbatim (may embed credentials)
    git_branch = Column(String, nullable=True)        # NULL = the repo's default branch

    # ── blue/green versioning (dockerfile mode only) ──
    backup_limit = Column(Integer, nullable=False, default=5)      # max archived versions kept
    version_counter = Column(Integer, nullable=False, default=0)   # last version number assigned

    services = relationship("ComposeService", back_populates="project", cascade="all, delete-orphan")
    versions = relationship("ProjectVersion", back_populates="project", cascade="all, delete-orphan")
    env_files = relationship("ProjectEnvFile", back_populates="project", cascade="all, delete-orphan")
    backups = relationship("Backup", back_populates="project", cascade="all, delete-orphan")
    backup_config = relationship("BackupConfig", back_populates="project",
                                 cascade="all, delete-orphan", uselist=False)
    # Guest tokens scoped to this project. Deleting the project unbinds them (the association
    # rows go) but never deletes the tokens — one may still cover other projects.
    tokens = relationship("Token", secondary=token_projects, back_populates="projects")

    @property
    def effective_domain(self) -> str | None:
        """The hostname actually served: the custom domain when set, else the auto subdomain."""
        return self.custom_domain or self.subdomain


class ComposeService(Base):
    """One service of a compose project. Compose-only; dockerfile projects keep their
    single container's fields on Project itself. `exposed` services publish a TCP port
    and get a loopback binding, subdomain, and nginx vhost; unexposed services (host
    networking, UDP-only, or internal-only) have NULL port/subdomain/container_port and
    no nginx endpoint — they are still tracked for status display and exec."""
    __tablename__ = "compose_services"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    name = Column(String, nullable=False)             # the compose service name
    exposed = Column(Boolean, nullable=False, default=True)  # publishes a TCP port → nginx endpoint
    subdomain = Column(String, nullable=True)         # auto-generated, {service}.{project}.your_domain.com (exposed only)
    custom_domain = Column(String, nullable=True)     # optional override; when set it wins over subdomain
    local_port = Column(Integer, nullable=True, unique=True)  # NULL for unexposed services
    container_port = Column(Integer, nullable=True)   # NULL for unexposed services
    container_name = Column(String, nullable=False)   # freeholdy_{project}_{service}
    ssl_enabled = Column(Boolean, default=False)
    websocket = Column(Boolean, default=False)        # detected from this service's compose block

    project = relationship("Project", back_populates="services")

    @property
    def effective_domain(self) -> str:
        """The hostname actually served: the custom domain when set, else the auto subdomain."""
        return self.custom_domain or self.subdomain


class ProjectVersion(Base):
    """One deployed version of a project, for blue/green deploys with archived backups.

    Dockerfile mode: image + container per version. At any time a project has at most one
    `active` (running, nginx points at it), at most one `inactive` (previous version,
    container stopped but kept for fast rollback), and zero-or-more `archived` (image
    kept, container removed, port freed) versions capped by Project.backup_limit.

    Compose mode: image_name/container_name/local_port stay NULL — per-service image tags
    are deterministic (`freeholdy_{project}_{service}:v{N}`) and ports live on
    ComposeService rows. Statuses are `active` | `archived` only (no stopped-container
    "inactive" tier: `compose down` removes containers); each version also keeps a file
    snapshot under `{DATA_DIR}/versions/{project}/v{N}/`."""
    __tablename__ = "project_versions"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    version = Column(Integer, nullable=False)          # the Project.version_counter value at deploy
    image_name = Column(String, nullable=True)         # freeholdy_{name}:v{N} (dockerfile; NULL for compose)
    container_name = Column(String, nullable=True)     # freeholdy_{name}_v{N} (dockerfile; NULL for compose)
    local_port = Column(Integer, nullable=True)        # loopback port; NULL once archived (freed) and for compose
    status = Column(String, nullable=False)            # active | inactive | archived
    created_at = Column(DateTime, default=datetime.utcnow)

    project = relationship("Project", back_populates="versions")


class ProjectEnvFile(Base):
    """A .env-format file for a project, stored in the DB (the source of truth).

    `service_name == ""` is the project-level file: for a dockerfile project that IS the
    container's environment; for a compose project it is the shared file applied to every
    service. A non-empty `service_name` is that one compose service's own file, and its
    values win over the project-level ones.

    Content is kept verbatim as the user typed it (comments and blank lines included);
    `env_service` re-renders a normalized KEY=value file under `{DATA_DIR}/env/{project}/`
    at deploy/restart time. Nothing is written into PROJECTS_DIR — git redeploys and
    compose rollbacks wipe that directory, and the plugin-owned `{project_dir}/.env`
    (compose ${VAR} interpolation) must never be clobbered."""
    __tablename__ = "project_env_files"
    __table_args__ = (UniqueConstraint("project_id", "service_name"),)

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    service_name = Column(String, nullable=False, default="")   # "" = project-level
    content = Column(Text, nullable=False, default="")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    project = relationship("Project", back_populates="env_files")


class Token(Base):
    """An API bearer token, stored as a SHA-256 hash (the plaintext exists only at mint time).

    `role` is the authorization level:
      * `admin` — full API access. The default, and what every token issued before roles
        existed is (migrate_db.sh backfills them via the column DEFAULT).
      * `guest` — scoped to the projects in `projects`: redeploy (from the project's own
        stored git origin), restart, env, logs/status, versions and rollback for those
        projects and nothing else. Meant to be handed to a third party (CI/CD) that must
        not see the rest of the server. The set may be empty, in which case the token
        authenticates but may do nothing.

    Enforcement lives in `app/auth.py` (HTTP) and `app/services/ws_session.py` (WebSockets)."""
    __tablename__ = "tokens"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    token_hash = Column(String, nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    active = Column(Boolean, default=True)
    role = Column(String, nullable=False, default="admin")   # admin | guest

    # Guest scope. Always empty for an admin token, which needs no scoping.
    projects = relationship("Project", secondary=token_projects, back_populates="tokens")


class Backup(Base):
    """One backup archive — a self-contained `.tar` holding a project's images, volumes,
    env files and project tree (or, for the system scope, the freeholdy database).

    `project_id IS NULL` is the **system scope**: the freeholdy SQLite database itself. That
    is what lets the database backup reuse the same service functions, retention logic and
    UI as a project backup instead of growing a parallel implementation.

    `version` is the `ProjectVersion.version` the archive captured. For an imported archive
    (`imported=True`) it is the version number *minted at import time* — importing a backup
    loads its images under `freeholdy_{name}:v{N}` and writes an `archived` ProjectVersion,
    so activating a backup is the ordinary rollback path and the Versions panel stays the
    single answer to "which build is live".

    The archive lives at `{DATA_DIR}/backups/{project}/{filename}` (`{DATA_DIR}/backups/
    _system/` for the database). `remote_*` records the copy shipped to the configured
    `backup_targets` destination; a failed upload never fails the backup — the local copy
    still exists, and `remote_status` says what happened."""
    __tablename__ = "backups"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True)  # NULL = the freeholdy DB
    version = Column(Integer, nullable=True)           # ProjectVersion.version captured (NULL for the DB scope)
    kind = Column(String, nullable=False, default="manual")   # manual | scheduled | deploy
    imported = Column(Boolean, nullable=False, default=False)  # arrived by upload rather than made here
    filename = Column(String, nullable=False)
    size_bytes = Column(Integer, nullable=True)
    sha256 = Column(String, nullable=True)
    has_images = Column(Boolean, nullable=False, default=False)
    has_volumes = Column(Boolean, nullable=False, default=False)
    has_env = Column(Boolean, nullable=False, default=False)
    has_project = Column(Boolean, nullable=False, default=False)
    status = Column(String, nullable=False, default="creating")  # creating | ok | error
    message = Column(Text, nullable=False, default="")
    target_name = Column(String, nullable=True)        # the .env-declared destination it was shipped to
    remote_status = Column(String, nullable=False, default="none")  # none | pending | ok | error
    remote_path = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    project = relationship("Project", back_populates="backups")


class BackupConfig(Base):
    """Automatic-backup settings for one project, or for the freeholdy database.

    One row per scope (`project_id IS NULL` = the database), enforced by a unique constraint
    — SQLite treats NULLs as distinct in a UNIQUE index, so the system row's uniqueness is
    maintained by `backup_service.get_config` doing a get-or-create under one session rather
    than by the constraint.

    Two independent triggers, both optional: `schedule_cron` (a five-field cron expression
    evaluated once a minute by `backup_scheduler`) and `on_deploy` (a backup taken after
    every successful deploy, the same shape as creating a version). `keep_local` caps
    archives on disk exactly the way `Project.backup_limit` caps archived versions;
    `keep_remote` does the same at the destination (0 = never prune there).

    `target_name` names a destination declared in the server's `.env` (see
    `app/services/backup_targets.py`). Credentials never live here."""
    __tablename__ = "backup_configs"
    __table_args__ = (UniqueConstraint("project_id"),)

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True)  # NULL = the freeholdy DB
    enabled = Column(Boolean, nullable=False, default=False)
    schedule_cron = Column(String, nullable=True)      # five-field cron, NULL = no timer
    on_deploy = Column(Boolean, nullable=False, default=False)
    keep_local = Column(Integer, nullable=False, default=5)
    keep_remote = Column(Integer, nullable=False, default=0)   # 0 = keep everything remotely
    target_name = Column(String, nullable=True)
    include_volumes = Column(Boolean, nullable=False, default=True)
    last_run_at = Column(DateTime, nullable=True)
    last_status = Column(String, nullable=True)        # ok | error
    last_message = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    project = relationship("Project", back_populates="backup_config")
