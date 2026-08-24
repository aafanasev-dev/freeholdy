import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.models.database import init_db, SessionLocal
from app.services import compose_service, deploy_service
from app.routers import projects, container, plugins, compose, git, versions, env, logs


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Backfill rows for compose services that predate unexposed-service tracking
    # (schema columns are added by migrate_db.sh; this fills the missing rows).
    db = SessionLocal()
    try:
        added = compose_service.backfill_unexposed_services(db)
        if added:
            logging.getLogger("uvicorn").info(
                "Backfilled %d unexposed compose service row(s)", added
            )
    finally:
        db.close()
    # Backfill compose projects that predate compose versioning as active v1 (retag their
    # running images + snapshot their files) so they get an immediate rollback point.
    backfilled = deploy_service.backfill_compose_versions()
    if backfilled:
        logging.getLogger("uvicorn").info(
            "Backfilled %d compose project(s) as active v1", backfilled
        )
    if settings.DEBUG:
        logging.getLogger("uvicorn").warning(
            "DEBUG mode: bearer-token auth is DISABLED — do not run like this in production"
        )
    yield


app = FastAPI(
    title="freeholdy",
    description="Docker + Nginx orchestrator for pet projects on your_domain.com",
    version="0.1.0",
    lifespan=lifespan,
)

# The web UI is served from a different origin (your_domain.com) than the API
# (api.your_domain.com), so the browser issues CORS preflight requests. Auth is via
# the Authorization header (no cookies), so credentials are not allowed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(projects.router,  prefix="/projects", tags=["projects"])
app.include_router(container.router, prefix="/projects", tags=["container"])
app.include_router(versions.router,  prefix="/projects", tags=["versions"])
app.include_router(compose.router,   prefix="/projects", tags=["compose"])
app.include_router(env.router,       prefix="/projects", tags=["env"])
app.include_router(logs.router,      prefix="/projects", tags=["logs"])
app.include_router(plugins.router,   prefix="/plugins",  tags=["plugins"])
app.include_router(git.router,       prefix="/git",      tags=["git"])


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok"}


@app.get("/version", tags=["system"])
def version():
    version_file = Path(__file__).resolve().parent.parent / "version.json"
    return json.loads(version_file.read_text())


def main():
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="Run freeholdy API server")
    parser.add_argument("--host", default=settings.HOST, help=f"Listen address (default: {settings.HOST})")
    parser.add_argument("--port", type=int, default=settings.PORT, help=f"Listen port (default: {settings.PORT})")
    parser.add_argument("--reload", action="store_true", help="Enable uvicorn auto-reload")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Disable bearer-token auth — all requests are accepted. Local dev only.",
    )
    args = parser.parse_args()

    if args.debug:
        settings.DEBUG = True

    uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
