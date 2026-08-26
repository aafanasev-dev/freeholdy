from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean, UniqueConstraint
from sqlalchemy.orm import relationship
from datetime import datetime
from app.models.database import Base


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

    # ── blue/green versioning (dockerfile mode only) ──
    backup_limit = Column(Integer, nullable=False, default=5)      # max archived versions kept
    version_counter = Column(Integer, nullable=False, default=0)   # last version number assigned

    services = relationship("ComposeService", back_populates="project", cascade="all, delete-orphan")
    versions = relationship("ProjectVersion", back_populates="project", cascade="all, delete-orphan")
    env_files = relationship("ProjectEnvFile", back_populates="project", cascade="all, delete-orphan")
    # Guest tokens are scoped to exactly one project; deleting the project revokes them.
    tokens = relationship("Token", back_populates="project", cascade="all, delete-orphan")

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
      * `guest` — bound to the single project in `project_id`: redeploy, restart, env,
        logs/status, versions and rollback for that project and nothing else. Meant to be
        handed to a third party (CI/CD) that must not see the rest of the server.

    Enforcement lives in `app/auth.py` (HTTP) and `app/services/ws_session.py` (WebSockets)."""
    __tablename__ = "tokens"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    token_hash = Column(String, nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    active = Column(Boolean, default=True)
    role = Column(String, nullable=False, default="admin")   # admin | guest
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=True)  # guest only; NULL for admin

    project = relationship("Project", back_populates="tokens")
