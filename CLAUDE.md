# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A FastAPI service (`app/`) that orchestrates Docker containers behind nginx + Let's Encrypt on a single VPS, exposing each as `*.your_domain.com`. A standalone CLI (`cli/fhcli.py`) wraps the HTTP API; it has its own venv and `.env` and is not imported by the server.


## System components vs pet projects

freeholdy manages two distinct layers:

**System infrastructure** — started outside the freeholdy API, provisioned by `install.sh` (the one-command bootstrap) and managed by the OS:
- `nginx` (system service)
- `certbot` (system service)

**Managed pet projects** — created via `POST /projects` or `fhcli plugin-add`, tracked in SQLite, proxied by nginx. Includes `type: "system"` projects (webui) that are hidden from the web UI but are otherwise normal managed projects.

## Common commands

Server (run from repo root, venv active):
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload   # dev
python scripts/generate_token.py generate --name "label"   # mint API token (printed once)
python scripts/generate_token.py list | revoke --id N
```

CLI (separate package, see `cli/README.md`):
```bash
cd cli && source venv/bin/activate
./fhcli.py health | projects | create NAME
./fhcli.py plugins                      # list available plugins
./fhcli.py plugin-add PLUGIN PROJECT    # create a project from a plugin
./fhcli.py upload PROJECT PATH          # upload a file or folder → auto-detect + provision
```

No test suite, linter, or formatter is configured — do not invent commands for them.

## Architecture

**Upload → detect → DB → Docker → nginx → certbot.** `POST /projects` just creates an empty (`deploy_mode:"pending"`) row; the wiring happens on the **first `POST /projects/{name}/upload`**, which writes files into the one per-project directory and then auto-detects the deploy mode. Understanding the upload flow (`projects.py::upload`) is the fastest way into the codebase.

The deploy mode is **not** chosen at create — it is auto-detected by scanning the uploaded project root (`projects.py::_detect_manifest`): a `docker-compose.yml` (also `compose.yaml`/`compose.yml`) selects compose mode and **wins over** a `Dockerfile`; a bare `Dockerfile` selects dockerfile mode. Uploading the other manifest type onto an already-provisioned project is rejected (400 — remove + recreate to change mode).

**`dockerfile`** — a single container per project (detected from a `Dockerfile`):
1. `projects.py::provision_dockerfile` allocates one free port from `PORT_RANGE_START..END`, assigns subdomain `{name}.{domain}` (plugins may override via `domain_prefix`), and sets deterministic Docker names `freeholdy_{name}` / `freeholdy_{name}:latest`. It is idempotent — re-uploads keep the existing port/subdomain.
2. `app/services/nginx_service.py::setup_nginx` runs a **three-pass nginx setup**: HTTP config → nginx reload (ACME challenge) → `certbot certonly --nginx` per subdomain → SSL config → nginx reload. SSL success is stored on `Project.ssl_enabled`.
3. The container port comes from the Dockerfile's `EXPOSE` (`scan.exposed_port`; the upload is rejected 400 if absent). `scan.uses_websocket` scans the same text; if WebSocket usage is detected, `Project.websocket` is set and the nginx SSL config is regenerated with upgrade headers.
4. **Build + run is launched automatically by the provisioning upload** (`projects.py::launch_deploy` → `docker_service.provision_from_plugin(install_script=None)`, job_key = container name) and streamed over `WS /projects/{name}/deploy` — the same unified deploy path git uses; re-upload to redeploy. There are **no** `/build` or `/start` endpoints. The remaining `app/routers/container.py` calls (`/projects/{name}/stop|status|abort|ssl|domain`) operate on the `Project` row via `app/services/docker_service.py`. **Exec is a WebSocket, not REST:** `WS /projects/{name}/exec` bridges an interactive `docker exec -it` shell on a pty (`interactive_service.run_exec_session`; auth → ready → `stdin`/`stdout`/`resize` frames → exit). An optional `?cmd=` overrides the default shell.

**`compose`** — a multi-container stack (detected from a `docker-compose.yml`):
1. The unified upload calls `compose.py::provision_compose`, which parses the compose file, allocates a loopback port per exposed service, creates `ComposeService` rows, writes a `docker-compose.override.yml` pinning container names + port bindings, and runs nginx/SSL setup for each exposed service. A service is exposed iff it publishes a **TCP** port; UDP-only services (e.g. the jitsi-meet plugin's JVB on UDP 10000) are not exposed — nginx can't proxy UDP — and their original host bindings pass through the compose merge untouched. WebSocket detection runs per-service (scans that service's YAML block + name).
2. **`docker compose up -d` is launched automatically by the provisioning upload** (`projects.py::launch_deploy` → `docker_service.compose_up`, job_key = `compose:{name}`) and streamed over `WS /projects/{name}/deploy`; re-upload to redeploy. There are **no** `/compose/build` or `/compose/up` endpoints. The remaining lifecycle calls (`/compose/down|status|abort`) run `docker compose -p {name} -f docker-compose.yml -f docker-compose.override.yml`.
3. Per-service exec — `WS /projects/{name}/services/{service}/exec` bridges an interactive `docker exec -it` shell into one service's `freeholdy_{name}_{service}` container (same `interactive_service.run_exec_session` as the dockerfile route), keyed `exec:{name}:{service}` so it never clobbers the stack's `compose:{name}` lifecycle job.

**Plugins** — pre-packaged project templates in `plugins/{name}/`:
- Each plugin has a `plugin.json` manifest: `name`, `description`, `deploy_mode`, `container_port` (dockerfile only), `type` (`user`|`plugin`|`system`), optional `domain_prefix`.
- A plugin may ship an optional `ABOUT.md` (Markdown) — long-form detail shown in the web UI's plugin panel. It is read by `plugin_service` into the plugin's `about` field, surfaced on `GET /plugins`, and is **not** staged into the build context. Absent → the UI falls back to `description`.
- `type: "system"` plugins (e.g. `webui`) are hidden from the web UI but are otherwise normal managed projects — created/deleted via the standard API.
- `POST /plugins/{name}/add` provisions the project (nginx/SSL via `provision_dockerfile` / `provision_compose` — the same service functions the unified upload calls, not the HTTP upload endpoint) then launches an async build+run job. The response always carries a `ws_path` — clients connect to `WS /plugins/{plugin}/install/{project}` to **stream the build log live** (`interactive_service.stream_job` tails the running job and sends an `exit` frame when it finishes; the build itself is a normal background job, so it survives a client disconnect and a reconnect re-streams it). A plugin may ship an `install.sh` that runs in two phases: `pre` (synchronous, before compose up — generate secrets) and `post` (background thread, after containers start — configure via REST API).
- **Interactive installs** — `"interactive": true` in `plugin.json` (requires an `install.sh`; e.g. `ws-chat`) makes the install prompt the user over the same install WebSocket. `POST /plugins/{name}/add` stops after staging (returns `job.status: "waiting_interactive"` + `ws_path`); the client connects to `WS /plugins/{plugin}/install/{project}`, authenticates with a first frame `{"type":"auth","token":…}`, and exchanges `stdin`/`stdout` JSON frames while install.sh runs on a pty (`app/services/interactive_service.py`; echo disabled — clients echo locally). On exit 0 the server launches the follow-up provision job (dockerfile: build+run; compose: `provision_compose` + up + `post`) and **streams it over the same socket**, so the `exit` frame reports the *build* result (no status polling). Disconnecting mid-install.sh kills the script and leaves the project pre-install; reconnecting re-runs install.sh if nothing is built yet, otherwise re-streams the running build (`fhcli plugin-add` resumes on 409). Only the `pre` phase is interactive for compose plugins.
- **nginx extras** — a plugin (or any project) may ship an `nginx-extra.conf`: staging copies it into the project dir like any other file, and `nginx_service.generate_ssl_config` inlines its contents into **every SSL `server {}` block** of that project at render time (`_load_extra_config` re-reads it on each regeneration — `/ssl` re-issue, websocket regen, custom domains all keep it). HTTP (port 80) blocks never get it. Used by `nextcloud` for `client_max_body_size 10G`, HSTS, and the `.well-known/{card,cal}dav` redirects.
- Discovery and staging live in `app/services/plugin_service.py`; the router is `app/routers/plugins.py`.

Key invariants that aren't obvious from a single file:

- **No parts.** The `Part` table and `/parts/{type}/...` endpoints are gone. Dockerfile projects store everything directly on the `Project` row; compose projects use `ComposeService` rows. The `parts` key never appears in API responses.
- **Two ways in, one provisioning path.** Files reach a project either via the legacy `POST /projects/{name}/upload` (`projects.py::upload`, `List[UploadFile]` multipart — each filename carries its path relative to the project root) or via the **chunked upload** trio used by `fhcli upload` and the web UI for large files/folders: `POST …/upload/chunk?upload_id=&offset=` (raw 1 MiB octet-stream pieces written at their offset into a staged zip under `{DATA_DIR}/uploads/{project}/{upload_id}.zip`), `POST …/upload/complete` (safe-unzip into the project dir — guarded member-by-member by `_safe_join` against zip-slip — then provision), and `DELETE …/upload/{upload_id}` (abort/cleanup). 1 MiB pieces stay under nginx's 1 MB default body limit (the API vhost also sets `client_max_body_size 8m`). Both entry points recreate the tree under the single per-project dir (`PROJECTS_DIR/{name}`) and end in the shared `projects.py::_autoprovision` (`_detect_manifest` → `provision_compose` / `_provision_from_dockerfile`; compose wins over Dockerfile). When a manifest is provisioned the upload then **auto-launches build + run** via `projects.py::launch_deploy` (the single `deploy_mode →` docker_service dispatch, shared with git deploy and the non-interactive plugin install) and returns a `ws_path` (`WS /projects/{name}/deploy`, served by the shared `deploy_stream_session`) for the client to stream the deploy log live — exactly like `POST /git/add`. Both return `UploadResponse` (now carrying `ws_path` + `job`); an upload with no manifest is a plain file sync (mode stays `pending`, no `ws_path`). There are no `/dockerfile`, `/compose`, or `/context` upload endpoints, and no per-mode `/build` or `/start` (`/compose/build`, `/compose/up`) endpoints — re-upload to redeploy.
- **Git is a third on-ramp** (`app/routers/git.py`). `POST /git/add` (body `{name, git_url, branch?}`) creates the project row, `git clone --depth 1`s the repo into `PROJECTS_DIR/{name}` (`app/services/git_service.py`; HTTPS or `git@host:path`/`ssh://` URLs), then runs the **same** `projects.py::_autoprovision` as the upload flow (no manifest in the repo root → 400, project rolled back + dir removed). Unlike upload, it then auto-launches the build+run job — dockerfile: `docker_service.provision_from_plugin(install_script=None)`; compose: `compose_up` — and returns a `PluginAddResponse` with `ws_path: /git/deploy/{name}`. `WS /git/deploy/{project}` is a read-only stream (no interactive prompts) that tails that job via `interactive_service.stream_job`, mirroring the non-interactive plugin install socket. Driven by `fhcli git-add` and the web UI's "+ git project" form (which reuses `InstallPane`). The `.git` dir is kept on disk (enables a future pull/update).
- **SSH keys for private git repos** (`git_service.get_or_create_github_key`). `GET /git/key` returns the server's GitHub SSH public key so a user can add it to GitHub and clone private repos over `git@github.com:…`. It's read-or-create: the key is keyed off the `Host github.com` block in the server user's `~/.ssh/config` (one global key, `/root/.ssh` in production); when absent, an ed25519 keypair (`~/.ssh/freeholdy_github`) is generated via `ssh-keygen` and a matching config block written. The response (`GitKeyResponse`) carries the public key + GitHub "add this key" `instructions`. When a `POST /git/add` clone fails an SSH-auth check (`git_service.is_auth_failure`), the 400 detail is augmented to point the user at this endpoint — surfaced by both `fhcli get-git-key` and the web UI header's "git key" button (`GitKeyModal`).
- **One per-project directory.** Both modes store files under `PROJECTS_DIR/{name}` (resolved by `compose_service.project_dir`); the mode can't be known until the root is scanned, so files can't be split across mode-specific dirs.
- **`DELETE /projects/{name}` is a full teardown.** For dockerfile projects: `docker rm -f` the container + `docker rmi -f` the image. For compose projects: `docker compose down --rmi all` (removes containers, networks, *and* all images) + `shutil.rmtree` the compose directory. Both modes then remove the nginx config, reload nginx, and delete the DB row (cascades to `ComposeService`).
- **Port allocation unions both tables.** `projects.py::_next_port` checks both `Project.local_port` and `ComposeService.local_port` so the two modes never collide.
- **Container port binding is `127.0.0.1:{local_port}` only** (`docker_service.start_container`). Public traffic must go through nginx; do not bind to `0.0.0.0`. Exception: non-HTTP ports a compose file binds itself (mailserver's SMTP/IMAP, jitsi-meet's UDP 10000) survive the override merge — the generated override only *adds* the loopback binding for exposed services, it never strips original `ports:` entries.
- **Restart policy is `unless-stopped`** — containers come back after host reboot without freeholdy doing anything.
- **Auth tokens are stored as SHA-256 hashes** (`app/auth.py::hash_token`). The plaintext token only exists at generation time in `scripts/generate_token.py`; there is no recovery path.
- **The service writes to `/etc/nginx/sites-{available,enabled}` and shells out to `nginx -s reload` and `certbot`.** In production it must run as root or with sudoers carve-outs; locally, expect `PermissionError` from `_write_config` unless those dirs are writable.
- **Nginx config filenames are namespaced `freeholdy_{project}.conf`** — one file per project. Dockerfile `/ssl` and Dockerfile-upload (if cert exists) both call `nginx_service.write_ssl_config` which rewrites the whole file.
- **WebSocket nginx headers** (`proxy_http_version 1.1; Upgrade $http_upgrade; Connection "upgrade"`) are emitted only for endpoints where `websocket=True` — set automatically by scanning the manifest text, not by a user flag.

## Data & filesystem layout

- SQLite at `{DATA_DIR}/freeholdy.db` (default `data/freeholdy.db`), schema auto-created on startup via `init_db()` in `app/main.py`'s lifespan. No migrations framework — schema changes mean editing `app/models/orm.py` and **deleting the DB** (no alter-table support).
- All project files (both modes) live under `{PROJECTS_DIR}/{project}/` — the uploaded tree, the `Dockerfile` or `docker-compose.yml`, and (compose) the generated `docker-compose.override.yml`. `DOCKERFILES_DIR`/`COMPOSE_DIR` still exist in config but are only referenced by compose-plugin `.env` seeding.
- Local nginx config backups under `{NGINX_CONFIGS_DIR}`, then mirrored to `NGINX_SITES_AVAILABLE` with a symlink in `NGINX_SITES_ENABLED`.

All paths and the port range come from `app/config.py` (pydantic-settings, reads `.env`).

## Conventions worth following

- Routers return Pydantic response schemas from `app/models/schemas.py`; status strings follow `ok | error` for action endpoints and `running | exited | not_found | no_image | error` for container state (synthesized in `projects.py::_container_status`, not stored).
- Service-layer functions (`docker_service`, `nginx_service`) return `(success: bool, message_or_logs: str)` tuples rather than raising — the router decides the HTTP shape.
- Project names are validated as DNS-safe slugs (`^[a-z0-9][a-z0-9-]*[a-z0-9]$`) in `schemas.py`; assume that downstream when constructing subdomains or container names.
- `ProjectResponse` shape: dockerfile projects fill `container: ContainerInfo`, `services: []`; compose projects fill `container: null`, `services: [ServiceInfo, ...]`. A freshly-created project has `deploy_mode: "pending"` with `container: null` and `services: []` until its first provisioning upload.
