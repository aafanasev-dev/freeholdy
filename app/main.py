import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.models.database import init_db
from app.routers import projects, container, plugins, compose


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
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
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(projects.router,  prefix="/projects", tags=["projects"])
app.include_router(container.router, prefix="/projects", tags=["container"])
app.include_router(compose.router,   prefix="/projects", tags=["compose"])
app.include_router(plugins.router,   prefix="/plugins",  tags=["plugins"])


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok"}


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
