# fhold CLI

Command-line wrapper for the freeholdy API.

## Setup

```bash
cd cli/
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env   # set TOKEN and BASE_DOMAIN
```

`.env` (stays in this directory, never committed):
```
TOKEN=your_api_token_here
BASE_DOMAIN=your_domain.com

# SFTP — for fhold sftp-upload (SFTPGo on port 2022)
SFTP_USER=your_sftpgo_username
SFTP_PASSWORD=your_sftpgo_password
# Or use a key instead of password:
# SFTP_KEY_PATH=~/.ssh/id_rsa
# SFTP_HOST defaults to BASE_DOMAIN; override if different
# SFTP_PORT defaults to 2022
```

## Make `fhold` available system-wide (optional)

```bash
# Option A — symlink into /usr/local/bin
sudo ln -s "$(pwd)/fhold.py" /usr/local/bin/fhold

# Option B — shell alias in ~/.bashrc
alias fhold="$(pwd)/venv/bin/python $(pwd)/fhold.py"
```

## Commands

| Command | Description |
|---|---|
| `fhold health` | Check API is reachable |
| `fhold projects` | List all projects (incl. `system`) with live container status + type |
| `fhold plugins` | List available plugins in the catalog (incl. system plugins) |
| `fhold plugin-add PLUGIN PROJECT` | Create a project from a plugin, then build + run it |
| `fhold create NAME` | Create an empty project (deploy mode decided at upload time) |
| `fhold upload PROJECT PATH [--dest DIR]` | Upload a file or folder → auto-detect Dockerfile/compose + provision |
| `fhold build PROJECT [--no-follow]` | Build the Docker image (dockerfile mode) |
| `fhold start PROJECT` | Start the container |
| `fhold stop PROJECT` | Stop the container |
| `fhold exec PROJECT "COMMAND"` | Run a command inside the container |
| `fhold ssl PROJECT` | Issue / retry the SSL certificate |
| `fhold compose-build PROJECT` | `docker compose build` (compose mode) |
| `fhold compose-up PROJECT` | `docker compose up -d` |
| `fhold compose-down PROJECT` | `docker compose down` |
| `fhold compose-status PROJECT` | Last compose operation's status + logs |
| `fhold status PROJECT [--follow]` | Status + logs of the last docker op |
| `fhold abort PROJECT` | Abort the running docker op |
| `fhold remove PROJECT [--yes]` | Delete the project (containers, images, nginx, DB row) |
| `fhold sftp-upload PROJECT FILE... [--dest DIR]` | Raw SFTP transfer with progress bar (no provisioning) |

The deploy mode is **auto-detected** from your upload: a `docker-compose.yml` in the
uploaded root makes it a compose project (it wins over a `Dockerfile`), a bare
`Dockerfile` makes it a single-container project. A Dockerfile must `EXPOSE` its port.

## Dockerfile workflow example

```bash
# Check connectivity
fhold health

# Create an empty project, then upload its folder (must contain a Dockerfile that EXPOSEs a port)
fhold create myapp
fhold upload myapp ./myapp        # detects the Dockerfile, reads EXPOSE, wires nginx + SSL

# Build + start
fhold build myapp
fhold start myapp

# Inspect / operate
fhold projects
fhold exec myapp "python manage.py migrate"
fhold stop myapp

# Retry SSL if it failed during the upload
fhold ssl myapp
```

## Compose workflow example

For multi-service projects described by a single `docker-compose.yml`. Every
service that publishes a port is exposed at `{service}.{project}.{base_domain}`;
services without `ports:` (databases, caches) stay internal.

```bash
# Create an empty project, then upload a folder whose root has a docker-compose.yml
fhold create myapp
fhold upload myapp ./myapp        # detects compose, sets up nginx + SSL per exposed service

# Build + start the whole stack
fhold compose-build myapp
fhold compose-up myapp

# Inspect
fhold projects                 # myapp shows "· compose" + service endpoints
fhold compose-status myapp

# Tear the stack down
fhold compose-down myapp
```

## Install from a plugin

A plugin bundles a Dockerfile (+ optional `install.sh` and assets). `plugin-add`
creates the project, runs `install.sh`, builds the image, and starts the container
in one step, streaming the combined log:

```bash
# See what's available
fhold plugins

# Deploy the hello-world plugin as project "mysite"
fhold plugin-add hello-world mysite

# Don't wait for build/run to finish
fhold plugin-add hello-world mysite --no-follow
fhold status mysite          # check progress later
```
