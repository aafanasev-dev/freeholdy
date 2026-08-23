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
| `fhcli deploy NAME SOURCE [--branch B] [--dest DIR] [--no-follow]` | **Deploy a project** — auto-creates it if new, redeploys if it exists. `SOURCE` is a local file/folder (zipped + streamed in 1 MiB chunks) **or** a git clone URL (cloned server-side). Either way the server auto-detects Dockerfile/compose, provisions nginx + SSL, then builds + runs it (deploy log streams live). `--branch` is git-only; `--dest` is local-upload-only. |
| `fhcli stop PROJECT` | Stop the container |
| `fhcli restart PROJECT [--no-follow]` | Recreate the container(s) from the images they already run — no rebuild. How edited environment variables take effect |
| `fhcli env-get PROJECT [-s SERVICE] [-o FILE]` | Download the project's (or one compose service's) `.env` file — prints to stdout, or writes `FILE` |
| `fhcli env-set PROJECT FILE [-s SERVICE]` | Upload `FILE` as that `.env` file (`-` reads stdin). Saved only — run `restart` to apply |
| `fhcli env-clear PROJECT [-s SERVICE]` | Delete that `.env` file |
| `fhcli exec PROJECT "COMMAND"` | Run a command inside the container |
| `fhcli ssl PROJECT` | Issue / retry the SSL certificate |
| `fhcli compose-down PROJECT` | `docker compose down` |
| `fhcli compose-status PROJECT` | Last compose operation's status + logs |
| `fhcli status PROJECT [--follow]` | Status + logs of the last docker op |
| `fhcli abort PROJECT` | Abort the running docker op |
| `fhcli remove PROJECT [--yes]` | Delete the project (containers, images, nginx, DB row) |

The deploy mode is **auto-detected** from your source: a `docker-compose.yml` in the
deployed root makes it a compose project (it wins over a `Dockerfile`), a bare
`Dockerfile` makes it a single-container project. A Dockerfile must `EXPOSE` its port.
There is no separate `create` step — `deploy` creates the project on first use.

## Dockerfile workflow example

```bash
# Check connectivity
fhcli health

# Deploy a folder (must contain a Dockerfile that EXPOSEs a port) — the project is
# created automatically on first deploy.
fhcli deploy myapp ./myapp        # detects the Dockerfile, reads EXPOSE, wires nginx + SSL,
                                  # then builds + runs it — streaming the deploy log live.
                                  # Re-run `fhcli deploy myapp ./myapp` to redeploy.

# Inspect / operate
fhcli projects
fhcli exec myapp "python manage.py migrate"
fhcli stop myapp

# Retry SSL if it failed during the deploy
fhcli ssl myapp
```

## Compose workflow example

For multi-service projects described by a single `docker-compose.yml`. Every
service that publishes a port is exposed at `{service}.{project}.{base_domain}`;
services without `ports:` (databases, caches) stay internal.

```bash
# Deploy a folder whose root has a docker-compose.yml (the project is auto-created)
fhcli deploy myapp ./myapp        # detects compose, sets up nginx + SSL per exposed service,
                                  # then `docker compose up -d` — streaming the deploy log live.
                                  # Re-run `fhcli deploy myapp ./myapp` to redeploy.

# Inspect
fhcli projects                 # myapp shows "· compose" + service endpoints
fhcli compose-status myapp

# Tear the stack down
fhcli compose-down myapp
```

## Environment variables

freeholdy stores a `.env`-format file per project in its database and passes those
variables to the container when it starts. A compose project also gets one file per
service, whose values override the shared project-level ones.

Editing is **save-only**: `env-set` stores the file and returns. Nothing is rebuilt and
the running container keeps its current environment until `restart` recreates it from the
image it is already running (a container's environment is fixed when it is created, so
this is what it takes). `env-get` warns when the stored file is ahead of what is running.

```bash
# Round-trip a project's environment
fhcli env-get myapp > .env
$EDITOR .env
fhcli env-set myapp .env
fhcli restart myapp            # now the container has them

# Straight from stdin
printf 'DEBUG=1\nLOG_LEVEL=info\n' | fhcli env-set myapp -

# Compose: a shared file plus per-service overrides
fhcli env-set mystack shared.env          # every service gets these
fhcli env-set mystack db.env -s db        # only `db`, and these win on a key clash
fhcli restart mystack                     # only services whose env changed are recreated

fhcli env-clear mystack -s db
```

The file is ordinary dotenv: `KEY=value` per line, `#` comments and blank lines kept,
surrounding quotes stripped. Values are passed through verbatim (no shell expansion) and
must stay on one line. This is separate from any `.env` a plugin writes into the project
directory for compose `${VAR}` interpolation — freeholdy never touches that file.

## Install from a plugin

A plugin bundles a Dockerfile (+ optional `install.sh` and assets). `plugin-add`
creates the project, runs `install.sh`, builds the image, and starts the container
in one step, streaming the combined log:

```bash
# See what's available
fhcli plugins

# Deploy the freeholdy-help plugin as project "mysite"
fhcli plugin-add freeholdy-help mysite

# Don't wait for build/run to finish
fhcli plugin-add freeholdy-help mysite --no-follow
fhcli status mysite          # check progress later
```

## Deploy from a git repository

Pass a git clone URL as the `deploy` SOURCE (instead of a local path): the server clones
the repo, auto-detects a `Dockerfile` or `docker-compose.yml` in the root (compose wins),
wires up nginx + SSL, then builds and runs it — streaming the build log live. The project
is created on first deploy and re-cloned + redeployed on subsequent ones. For **private**
repos, add the server's key first (`fhcli get-git-key`).

```bash
# Deploy a public repo as project "mysite"
fhcli deploy mysite https://github.com/owner/repo.git

# A specific branch (SSH URLs are accepted; they use the server's own SSH keys)
fhcli deploy mysite git@github.com:owner/repo.git --branch dev

# Fire-and-forget
fhcli deploy mysite https://github.com/owner/repo.git --no-follow
fhcli status mysite          # check progress later
```
