from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    BASE_DOMAIN: str = "your_domain.com"
    DATA_DIR: str = "data"
    NGINX_CONFIGS_DIR: str = "nginx_configs"
    NGINX_SITES_AVAILABLE: str = "/etc/nginx/sites-available"
    NGINX_SITES_ENABLED: str = "/etc/nginx/sites-enabled"
    DOCKERFILES_DIR: str = "dockerfiles"
    COMPOSE_DIR: str = "compose"
    # One per-project working directory for both deploy modes. Files uploaded via
    # POST /projects/{name}/upload land here; the root is scanned for a Dockerfile or
    # docker-compose.yml to auto-detect the deploy mode.
    PROJECTS_DIR: str = "projects"
    PLUGINS_DIR: str = "plugins"
    PORT_RANGE_START: int = 8100
    PORT_RANGE_END: int = 9000
    CERTBOT_EMAIL: str = "admin@your_domain.com"
    # ACME HTTP-01 webroot. Certs are issued with `certbot certonly --webroot -w
    # CERTBOT_WEBROOT`, so certbot never rewrites nginx config (freeholdy owns those
    # files). nginx serves /.well-known/acme-challenge/ from here; install.sh creates it.
    CERTBOT_WEBROOT: str = "/var/www/certbot"
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    DEBUG: bool = False
    # Browser origins allowed to call the API (the web UI). Override via .env as a JSON list.
    # https://{BASE_DOMAIN} and https://ui.{BASE_DOMAIN} (the webui plugin) are always
    # injected automatically; add extra origins here.
    CORS_ORIGINS: list[str] = [
        "http://localhost:5173",
        "http://localhost:4173",
        "http://localhost:3000",
    ]

    @model_validator(mode="after")
    def _inject_base_domain_origin(self) -> "Settings":
        # The webui plugin is served at ui.{BASE_DOMAIN}; the apex is kept for any
        # client served from the root domain.
        for origin in (f"https://ui.{self.BASE_DOMAIN}", f"https://{self.BASE_DOMAIN}"):
            if origin not in self.CORS_ORIGINS:
                self.CORS_ORIGINS = [origin] + self.CORS_ORIGINS
        return self

    class Config:
        env_file = ".env"


settings = Settings()
