# fhcli CLI

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
```

## Make `fhcli` available system-wide (optional)

```bash
# Option A — symlink into /usr/local/bin
sudo ln -s "$(pwd)/fhcli.py" /usr/local/bin/fhcli

# Option B — shell alias in ~/.bashrc
alias fhcli="$(pwd)/venv/bin/python $(pwd)/fhcli.py"
```

## Commands

| Command | Description |
|---|---|
| `fhcli health` | Check API is reachable |
| `fhcli projects` | List all projects (incl. `system`) with live container status + type |
| `fhcli plugins` | List available plugins in the catalog (incl. system plugins) |
| `fhcli plugin-add PLUGIN PROJECT` | Create a project from a plugin, then build + run it |
| `fhcli git-add NAME GIT_URL [--branch B] [--no-follow]` | Clone a git repo into a new project, auto-detect Dockerfile/compose, then build + run it (build log streams live) |
| `fhcli create NAME` | Create an empty project (deploy mode decided at upload time) |
| `fhcli upload PROJECT PATH [--dest DIR]` | Zip + stream a file or folder in 1 MiB chunks (progress bar) → server unzips, auto-detects Dockerfile/compose + provisions |
| `fhcli build PROJECT [--no-follow]` | Build the Docker image (dockerfile mode) |
| `fhcli start PROJECT` | Start the container |
| `fhcli stop PROJECT` | Stop the container |
| `fhcli exec PROJECT "COMMAND"` | Run a command inside the container |
| `fhcli ssl PROJECT` | Issue / retry the SSL certificate |
| `fhcli compose-build PROJECT` | `docker compose build` (compose mode) |
| `fhcli compose-up PROJECT` | `docker compose up -d` |
| `fhcli compose-down PROJECT` | `docker compose down` |
| `fhcli compose-status PROJECT` | Last compose operation's status + logs |
| `fhcli status PROJECT [--follow]` | Status + logs of the last docker op |
| `fhcli abort PROJECT` | Abort the running docker op |
| `fhcli remove PROJECT [--yes]` | Delete the project (containers, images, nginx, DB row) |

The deploy mode is **auto-detected** from your upload: a `docker-compose.yml` in the
uploaded root makes it a compose project (it wins over a `Dockerfile`), a bare
`Dockerfile` makes it a single-container project. A Dockerfile must `EXPOSE` its port.

## Dockerfile workflow example

```bash
# Check connectivity
fhcli health

# Create an empty project, then upload its folder (must contain a Dockerfile that EXPOSEs a port)
fhcli create myapp
fhcli upload myapp ./myapp        # detects the Dockerfile, reads EXPOSE, wires nginx + SSL

# Build + start
fhcli build myapp
fhcli start myapp

# Inspect / operate
fhcli projects
fhcli exec myapp "python manage.py migrate"
fhcli stop myapp

# Retry SSL if it failed during the upload
fhcli ssl myapp
```

## Compose workflow example

For multi-service projects described by a single `docker-compose.yml`. Every
service that publishes a port is exposed at `{service}.{project}.{base_domain}`;
services without `ports:` (databases, caches) stay internal.

```bash
# Create an empty project, then upload a folder whose root has a docker-compose.yml
fhcli create myapp
fhcli upload myapp ./myapp        # detects compose, sets up nginx + SSL per exposed service

# Build + start the whole stack
fhcli compose-build myapp
fhcli compose-up myapp

# Inspect
fhcli projects                 # myapp shows "· compose" + service endpoints
fhcli compose-status myapp

# Tear the stack down
fhcli compose-down myapp
```

## Install from a plugin

A plugin bundles a Dockerfile (+ optional `install.sh` and assets). `plugin-add`
creates the project, runs `install.sh`, builds the image, and starts the container
in one step, streaming the combined log:

```bash
# See what's available
fhcli plugins

# Deploy the hello-world plugin as project "mysite"
fhcli plugin-add hello-world mysite

# Don't wait for build/run to finish
fhcli plugin-add hello-world mysite --no-follow
fhcli status mysite          # check progress later
```

## Install from a git repository

`git-add` clones a repo into a new project, auto-detects a `Dockerfile` or
`docker-compose.yml` in the root (compose wins), wires up nginx + SSL, then builds and
runs it — streaming the build log live, just like `plugin-add`:

```bash
# Deploy a public repo as project "mysite"
fhcli git-add mysite https://github.com/owner/repo.git

# A specific branch (SSH URLs are accepted; they use the server's own SSH keys)
fhcli git-add mysite git@github.com:owner/repo.git --branch dev

# Fire-and-forget
fhcli git-add mysite https://github.com/owner/repo.git --no-follow
fhcli status mysite          # check progress later
```
