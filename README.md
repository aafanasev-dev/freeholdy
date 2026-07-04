# freeholdy

Docker + Nginx orchestrator for pet projects on **your_domain.com**.

REST API to deploy, manage, and expose Docker containers via HTTPS subdomains — no manual nginx or certbot work needed after setup.

---

## How it works

A **deploy** creates the project (no separate create step): you **deploy** a local file/folder or a
git repo, and the server auto-creates the project row if it's new, then scans the deployed root for a
`Dockerfile` or `docker-compose.yml`: that selects the deploy mode automatically (compose wins if both
are present), allocates the subdomain(s), writes the nginx config, and issues the Let's Encrypt cert.
The lifecycle endpoints then operate on that project. Re-deploying redeploys (blue/green).

```
POST   /projects/{name}/upload/complete →  finish a chunked upload (a file/folder); auto-creates the
                                          project if new, auto-detects Dockerfile or docker-compose.yml
                                          in the root → provision ({name}.your_domain.com + nginx config
                                          + Let's Encrypt cert), then build + run it. Returns a ws_path.
POST   /git/add                         →  deploy from a git URL {name, git_url, branch?}: clone →
                                          same auto-create + autodetect + provision + build/run.
                                          Idempotent — an existing name re-clones + redeploys.
WS     /projects/{name}/deploy         →  stream the build + run log (read-only); re-deploy to redeploy
DELETE /projects/{name}                →  stop containers, remove images, delete nginx config, remove from DB

# dockerfile mode (detected from a Dockerfile) — one container per project:
# (build + run are launched automatically by the deploy above — no /build or /start)
POST   /projects/{name}/stop        →  docker stop   (async, poll /status)
WS     /projects/{name}/exec         →  interactive docker exec shell
GET    /projects/{name}/status      →  logs + status of last docker op
POST   /projects/{name}/abort       →  kill running docker subprocess
POST   /projects/{name}/ssl         →  (re)issue the Let's Encrypt cert
POST   /projects/{name}/domain      →  set/clear a custom domain (re-runs nginx + certbot)

# compose mode (detected from a docker-compose.yml) — multi-container stack:
# (build + up are launched automatically by the deploy above — no /compose/build or /compose/up)
POST   /projects/{name}/compose/down                  →  tear the stack down (async, poll /compose/status)
GET    /projects/{name}/compose/status                →  logs + status of last compose op
POST   /projects/{name}/compose/abort                 →  kill running compose subprocess
POST   /projects/{name}/services/{service}/domain     →  set/clear a service's custom domain
```

A Dockerfile must declare its listening port with `EXPOSE` — that becomes the container port nginx
proxies to (the upload is rejected if it's missing).

Pre-packaged stacks (the web UI, Nextcloud, examples) ship as **plugins** — see `POST /plugins/{name}/add`
and `fhcli plugin-add`.

---

## Prerequisites (VPS)

A fresh Ubuntu VPS with root access. The [installer](#installation) handles every package and
service below — install them manually only for a local/dev setup:

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip \
    nginx certbot python3-certbot-nginx docker.io nodejs npm git

sudo systemctl enable --now nginx docker
sudo usermod -aG docker $USER   # allow current user to use docker socket
```

---

## Installation

A single `install.sh` handles **both** a fresh dedicated VPS and a server that already runs other web apps. It auto-detects which situation it's in, prints what it's about to do, and asks you to confirm before making any change.

**On a fresh Ubuntu VPS, as root:**

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/aafanasev-dev/freeholdy/main/install.sh)
```

**On a server already running other apps**, clone (or copy) the repo and run it from there:

```bash
cd freeholdy
sudo bash install.sh
```

### Two auto-detected modes

The script checks whether `docker` **and** `nginx` are already present (by binary, so an existing `docker-ce` or nginx.org build counts):

- **FRESH** — one or both are missing. The script installs the missing system packages and enables + starts **only the services it installs**. Intended for a dedicated / empty VPS.
- **COEXIST** — both are already present. They are treated as prerequisites: the script never installs, enables, starts, restarts, upgrades, or apt-touches docker or nginx, so other apps, containers, and vhosts are left untouched. In this mode it first runs `nginx -t` and **stops with the full error** if the existing config is already broken, so any later failure can only be its own doing.

After detecting the mode it shows the implications and waits for your confirmation. Pass `-y` to auto-confirm for non-interactive runs.

Regardless of mode, every change is additive and surgical — see [Coexistence safety](#coexistence-safety) below.

### What it does

1. Detects the mode, prints the implications, and asks to proceed
2. Prompts for the service user (default `freeholdy`, reused if it exists), base domain, and Let's Encrypt email
3. Installs supporting apt packages (git, certbot, python, …) — plus docker + nginx **only in FRESH mode**
4. Creates/reuses the service user with docker access, passwordless nginx/certbot sudo, and nginx-config write permission (`nginx-managers` group)
5. Uses the checkout you ran it from (or clones as a fallback) into `/home/<user>/freeholdy` and writes `.env`
6. Picks the API port — default **27182**, auto-bumped to the next free port (with confirmation) if it's taken — and binds the app to `127.0.0.1`
7. Creates the venv and installs Python dependencies
8. Adds an nginx vhost for `api.<domain>` (validating the whole config and reverting on failure), obtains the SSL certificate, and installs a nightly renewal cron
9. Installs + starts the `freeholdy` systemd service
10. Prints your first API token (shown once)

Options: `-u USER` sets the service user; `-y` assumes yes to all confirmations; `-r` wipes `install.log` and redoes every step. Progress is tracked in `install.log`, so re-running is idempotent — it skips completed steps, pulls the latest code, and is also how you enable SSL once DNS has propagated.

### Coexistence safety

These hold in **both** modes, and are what make it safe to run beside other apps:

| Concern | Behaviour |
|---|---|
| docker & nginx | in COEXIST mode never installed/enabled/started/restarted/upgraded; in FRESH mode only the genuinely-missing one is provisioned, and only services it installed are started |
| apt packages | docker.io / nginx are queued **only** in FRESH mode; an existing `docker-ce` or custom nginx is detected by binary and never apt-touched |
| service user | reused if it already exists (password untouched); created only when absent |
| nginx sites dirs | group set on the **directories only**, `1775` with the **sticky bit** (non-recursive) — other apps' config files keep their owner/perms and can't be modified or deleted by freeholdy |
| nginx wiring | if this nginx includes only `conf.d` (e.g. nginx.org packages), an additive bridge is dropped in `conf.d` to load `sites-enabled`; `nginx.conf` is never edited |
| nginx reload | the whole config is validated first; if our change fails the test it is **reverted**, so a running nginx is never left broken |
| API port | default `27182`, auto-bumped to the next free port (with confirmation) if taken; app listens on `127.0.0.1` so public traffic only arrives via nginx |
| systemd / cron | only the `freeholdy` unit is (re)started; the renewal cron is scoped to `api.<domain>`, so other apps' units and cron lines are preserved |

---

## (Optional) SSH access as the `freeholdy` user

The installer creates the `freeholdy` service user but does not set up a way to log in as it. To
manage the service directly (pull code, restart, read logs) without becoming root each time, give
the user your SSH key.

**1. Generate a key pair (skip if you already have one)** — on your local machine:

```bash
ssh-keygen -t ed25519 -C "freeholdy@your-vps" -f ~/.ssh/freeholdy_ed25519
# leave the passphrase empty for unattended use, or set one and load it with ssh-agent
```

This writes the private key `~/.ssh/freeholdy_ed25519` and the public key `~/.ssh/freeholdy_ed25519.pub`.

**2. Authorize the public key for the `freeholdy` user.** From your local machine, if you can
already SSH in as another user:

```bash
ssh-copy-id -i ~/.ssh/freeholdy_ed25519.pub freeholdy@your-vps
```

Or do it manually as root on the VPS:

```bash
install -d -m 700 -o freeholdy -g freeholdy /home/freeholdy/.ssh
nano /home/freeholdy/.ssh/authorized_keys     # paste the contents of freeholdy_ed25519.pub
chmod 600 /home/freeholdy/.ssh/authorized_keys
chown freeholdy:freeholdy /home/freeholdy/.ssh/authorized_keys
```

**3. Add a host entry to `~/.ssh/config`** on your local machine so you can connect with a short
alias:

```ssh-config
Host freeholdy
    HostName your-vps               # IP or domain of the VPS
    User freeholdy
    IdentityFile ~/.ssh/freeholdy_ed25519
    IdentitiesOnly yes
```

**4. Log in directly and work from the repo:**

```bash
ssh freeholdy                       # uses the alias above
cd ~/freeholdy
git pull
sudo systemctl restart freeholdy
journalctl -u freeholdy -f
```

---

## Configuration

```bash
cp .env.example .env
nano .env          # adjust CERTBOT_EMAIL at minimum
```

Key settings:

| Variable | Default | Description |
|---|---|---|
| `BASE_DOMAIN` | `your_domain.com` | Root domain for all projects |
| `CERTBOT_EMAIL` | `admin@your_domain.com` | Let's Encrypt notifications |
| `CERTBOT_WEBROOT` | `/var/www/certbot` | ACME HTTP-01 webroot; project certs are issued with `certbot certonly --webroot` (see below) |
| `PORT_RANGE_START` | `8100` | First local port for containers |
| `PORT_RANGE_END` | `9000` | Last local port |
| `HOST` | `0.0.0.0` | freeholdy listen address (the installer sets `127.0.0.1`) |
| `PORT` | `8000` | freeholdy listen port (the installer defaults to `27182`) |
| `CORS_ORIGINS` | localhost dev ports | Browser origins allowed to call the API; `https://{BASE_DOMAIN}` and `https://ui.{BASE_DOMAIN}` are injected automatically |

---

## Nginx setup for freeholdy itself

freeholdy needs its own nginx reverse proxy so the API is reachable at `https://api.your_domain.com`.

**1. Issue SSL cert:**
```bash
sudo certbot certonly --nginx --non-interactive --agree-tos \
    --email admin@your_domain.com -d api.your_domain.com
```

**2. Create nginx config:**
```bash
sudo nano /etc/nginx/sites-available/freeholdy.conf
```

```nginx
server {
    listen 80;
    server_name api.your_domain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name api.your_domain.com;

    ssl_certificate     /etc/letsencrypt/live/api.your_domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.your_domain.com/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/freeholdy.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo nginx -s reload
```

---

## Generate your first API token

```bash
source venv/bin/activate
python scripts/generate_token.py generate --name "my_laptop"
```

Save the printed token — it is shown only once.

```bash
# List tokens
python scripts/generate_token.py list

# Revoke a token
python scripts/generate_token.py revoke --id 2
```

---

## Running freeholdy

**Development / foreground:**
```bash
source venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

**Production (systemd service):**

```bash
sudo nano /etc/systemd/system/freeholdy.service
```

```ini
[Unit]
Description=freeholdy API
After=network.target docker.service nginx.service

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/freeholdy
ExecStart=/home/YOUR_USER/freeholdy/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now freeholdy
sudo journalctl -u freeholdy -f   # watch logs
```

> **Note:** freeholdy must have write access to `/etc/nginx/sites-available` and `/etc/nginx/sites-enabled`, and must be able to run `certbot` and `nginx -s reload` without a password. See the two sections below for the exact setup.

---

## Nginx write permissions for freeholdy

By default, the Python backend cannot write nginx configs because `/etc/nginx/sites-available` and `/etc/nginx/sites-enabled` are owned by root.  
Fix this by creating a dedicated group, adding the service user to it, and granting group write access:

```bash
sudo groupadd nginx-managers
sudo usermod -aG nginx-managers freeholdy
sudo chown -R root:nginx-managers /etc/nginx/sites-available /etc/nginx/sites-enabled
sudo chmod -R 775 /etc/nginx/sites-available /etc/nginx/sites-enabled
```

> **Note:** Replace `freeholdy` with the actual system user running the service if it differs (i.e. the `User=` value in your systemd unit). The group membership takes effect on the next login / service restart.

---

## Passwordless sudo for nginx and certbot

freeholdy calls `sudo nginx -t`, `sudo nginx -s reload`, and `sudo certbot certonly` at runtime.  
Without a sudoers rule these commands block waiting for a password, which hangs the API.

Create a drop-in sudoers file that grants exactly those three commands — and nothing else:

```bash
sudo tee /etc/sudoers.d/freeholdy << 'EOF'
# Allow freeholdy to run nginx and certbot without a password prompt
freeholdy ALL=(root) NOPASSWD: /usr/sbin/nginx -t
freeholdy ALL=(root) NOPASSWD: /usr/sbin/nginx -s reload
freeholdy ALL=(root) NOPASSWD: /usr/bin/certbot certonly *
EOF

sudo chmod 0440 /etc/sudoers.d/freeholdy

# Validate — must print "parsed OK"
sudo visudo -c -f /etc/sudoers.d/freeholdy
```

Verify it works as the service user before restarting the service:

```bash
sudo -u freeholdy sudo nginx -t
```

> **Paths:** `/usr/sbin/nginx` and `/usr/bin/certbot` are standard on Debian/Ubuntu.  
> Confirm with `which nginx` and `which certbot` if the validation step fails.

---

## SSL certificate renewal (crontab)

The script in `scripts/cert-manager.sh` handles renewal for `api.your_domain.com` and any other fixed domains (it uses `certbot certonly --nginx`).

Certs for **project** subdomains are issued automatically by freeholdy when a project is created (and on the `/ssl` and `/domain` endpoints). For these, freeholdy deliberately uses `certbot certonly --webroot` rather than `--nginx`: it owns the per-project vhost files, so the `--nginx` plugin must not rewrite them. The ACME HTTP-01 challenge is served from `CERTBOT_WEBROOT` (default `/var/www/certbot`); the generated `nginx_http.conf.j2` / `nginx_ssl.conf.j2` vhosts expose `/.well-known/acme-challenge/` from there, and `install.sh` creates the directory. For a manual setup, create it yourself: `sudo mkdir -p /var/www/certbot/.well-known/acme-challenge`.

```bash
sudo crontab -e
```
```
0 3 * * * /home/YOUR_USER/freeholdy/scripts/cert-manager.sh
```

---

## API usage

All endpoints require `Authorization: Bearer <token>`.

### Interactive docs
```
https://api.your_domain.com/docs
```

### Example workflow (dockerfile mode)

```bash
TOKEN="your_token_here"
BASE="https://api.your_domain.com"

# 1. Deploy the project folder (must contain a Dockerfile that EXPOSEs a port). There is no
#    separate create step — the server auto-creates "myapp" on this first upload, detects the
#    Dockerfile, sets the container port from EXPOSE, provisions nginx + SSL for
#    myapp.your_domain.com, then builds + runs the container. The response carries a ws_path;
#    the build streams over WS /projects/myapp/deploy. (This multipart form is the simple path;
#    fhcli / the web UI use the chunked upload/chunk + upload/complete pair for large folders.)
curl -X POST "$BASE/projects/myapp/upload" \
  -H "Authorization: Bearer $TOKEN" \
  -F "files=@./Dockerfile;filename=Dockerfile" \
  -F "files=@./app.py;filename=app.py"

# 2. Watch the deploy (or just poll status)
curl    "$BASE/projects/myapp/status"     -H "Authorization: Bearer $TOKEN"

# 3. Stop
curl -X POST "$BASE/projects/myapp/stop" -H "Authorization: Bearer $TOKEN"

# Re-deploy (re-upload, or POST /git/add again) to redeploy after changing the code.
```

For a multi-container stack, deploy a folder whose root contains a `docker-compose.yml` (it wins over
a Dockerfile); the project becomes compose-mode and the deploy runs `docker compose up -d` for you
(tear it down with `.../compose/down`). The `fhcli` CLI wraps all of this in one command —
`fhcli deploy NAME PATH-OR-GIT-URL` auto-creates, provisions, and deploys in one step — see
`cli/README.md`.

---

## Subdomains & ports

- **dockerfile mode:** the project is served at `{name}.your_domain.com` (one container, one subdomain).
- **compose mode:** each service that publishes a port gets `{service}.{name}.your_domain.com`.
- Plugins may override the subdomain label via `domain_prefix` (e.g. web UI → `ui.`).
- **Custom domains:** point your own FQDN at a project — or at a single compose service — instead of the auto subdomain:
  ```bash
  curl -X POST "$BASE/projects/myapp/domain" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d '{"custom_domain": "app.acme.com"}'        # send {"custom_domain": null} to revert
  # compose service variant: POST /projects/{name}/services/{service}/domain
  ```
  freeholdy rewrites the nginx config and issues a Let's Encrypt cert for the domain. Point its A record
  at this server **first**; if DNS hasn't propagated the component stays HTTP-only (`ssl_enabled: false`)
  and you can POST again later to retry.
- Local container ports are auto-assigned from the `PORT_RANGE_START–PORT_RANGE_END` range and bound to `127.0.0.1` only; public traffic always goes through nginx.

---

## Project layout

```
freeholdy/
├── app/
│   ├── main.py               # FastAPI app entry point + CORS
│   ├── config.py             # Settings (from .env)
│   ├── auth.py               # Bearer token hashing + middleware
│   ├── models/
│   │   ├── database.py       # SQLAlchemy + SQLite setup
│   │   ├── orm.py            # DB models: Project, ComposeService, Token
│   │   └── schemas.py        # Pydantic request/response models
│   ├── routers/
│   │   ├── projects.py       # GET/DELETE /projects + unified /upload (auto-create + autodetect)
│   │   ├── container.py      # dockerfile lifecycle: build / start / stop / exec / ssl / status / abort
│   │   ├── compose.py        # provision_compose + compose lifecycle: build / up / down / status / abort
│   │   └── plugins.py        # list + add plugins
│   ├── services/
│   │   ├── docker_service.py # docker / docker compose subprocess wrapper
│   │   ├── nginx_service.py  # config generation + certbot + reload
│   │   ├── compose_service.py# compose file parsing + override generation
│   │   ├── plugin_service.py # plugin discovery + staging
│   │   └── scan.py           # WebSocket detection in Dockerfiles/compose
│   └── templates/
│       ├── nginx_http.conf.j2  # HTTP-only (for ACME challenge)
│       └── nginx_ssl.conf.j2   # Full HTTPS config
├── plugins/                  # Built-in plugins (webui, freeholdy-help, nextcloud, …)
├── examples/                 # Deployable sample projects (fhcli deploy) + DEPLOY.md
├── cli/                      # Standalone `fhcli` CLI (own venv + .env)
├── webui/                    # React control panel (source for the webui plugin)
├── scripts/
│   ├── generate_token.py     # Token management CLI
│   └── cert-manager.sh       # Cron-based cert renewal
├── data/                     # SQLite DB (gitignored)
├── projects/                 # per-project files for both modes (gitignored)
├── nginx_configs/            # Local config backups (gitignored)
├── install.sh                # Installer — auto-detects fresh VPS vs side-by-side
├── .env.example
├── requirements.txt
└── README.md
```
