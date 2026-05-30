from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Boolean
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
    subdomain = Column(String, nullable=True)         # e.g. myapp.cloudopen.space
    local_port = Column(Integer, nullable=True, unique=True)   # VPS-side loopback port
    container_port = Column(Integer, nullable=True, default=80)  # port inside the container
    dockerfile_path = Column(String, nullable=True)
    image_name = Column(String, nullable=True)        # freeholdy_{name}:latest
    container_name = Column(String, nullable=True)    # freeholdy_{name}
    ssl_enabled = Column(Boolean, default=False)
    websocket = Column(Boolean, default=False)        # detected from the Dockerfile → nginx upgrade headers

    # ── compose mode ──
    compose_path = Column(String, nullable=True)      # path to the uploaded docker-compose.yml

    services = relationship("ComposeService", back_populates="project", cascade="all, delete-orphan")


class ComposeService(Base):
    """One exposed service of a compose project (one per published port). Compose-only;
    dockerfile projects keep their single container's fields on Project itself."""
    __tablename__ = "compose_services"

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    name = Column(String, nullable=False)             # the compose service name
    subdomain = Column(String, nullable=False)        # {service}.{project}.cloudopen.space
    local_port = Column(Integer, nullable=False, unique=True)
    container_port = Column(Integer, nullable=False, default=80)
    container_name = Column(String, nullable=False)   # freeholdy_{project}_{service}
    ssl_enabled = Column(Boolean, default=False)
    websocket = Column(Boolean, default=False)        # detected from this service's compose block

    project = relationship("Project", back_populates="services")


class Token(Base):
    __tablename__ = "tokens"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    token_hash = Column(String, nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    active = Column(Boolean, default=True)
