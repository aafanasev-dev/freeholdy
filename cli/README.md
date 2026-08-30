# fhcli CLI

Command-line wrapper for the freeholdy API.

## Setup

The CLI shares the project's single venv — there is no separate one under `cli/`.
From the repo root:

```bash
bash configure.sh          # builds ./venv and installs everything (server + CLI)

cp cli/.env.example cli/.env
nano cli/.env              # set TOKEN and BASE_DOMAIN
```

`fhcli.py` re-execs itself under `./venv/bin/python`, so `./cli/fhcli.py health` works
with no `source venv/bin/activate` — from any directory, and through a symlink. On a
server, `install.sh` has already done both steps for you.

`.env` (stays in this directory, never committed):
```
TOKEN=your_api_token_here
BASE_DOMAIN=your_domain.com
```

## Make `fhcli` available system-wide (optional)

`install.sh` already does this on a server. Elsewhere, one symlink is enough — the
re-exec finds the venv from the script's real path, so it keeps working through the
link:

```bash
sudo ln -s "$(pwd)/cli/fhcli.py" /usr/local/bin/fhcli   # from the repo root
```

## Commands

| Command | Description |
|---|---|
| `fhcli health` | Check API is reachable |
| `fhcli projects` | List all projects (incl. `system`) with live container status + type |
| `fhcli plugins` | List available plugins in the catalog (incl. system plugins) |
| `fhcli plugin-add PLUGIN PROJECT` | Create a project from a plugin, then build + run it |
| `fhcli deploy NAME SOURCE [--branch B] [--dest DIR] [--env FILE] [--no-follow]` | **Deploy a project** — auto-creates it if new, redeploys if it exists. `SOURCE` is a local file/folder (zipped + streamed in 1 MiB chunks) **or** a git clone URL (cloned server-side). Either way the server auto-detects Dockerfile/compose, provisions nginx + SSL, then builds + runs it (deploy log streams live). `--branch` is git-only; `--dest` is local-upload-only; `--env FILE` stores variables **before the first container start**. |
| `fhcli redeploy PROJECT [--no-follow]` | **Rebuild from the project's own git origin** — re-clones the URL and branch recorded at its last git deploy, no arguments. Git-backed projects only; this is the deploy a guest token gets |
| `fhcli stop PROJECT` | Stop the container |
| `fhcli restart PROJECT [--no-follow]` | Recreate the container(s) from the images they already run — no rebuild. How edited environment variables take effect |
| `fhcli env-get PROJECT [-s SERVICE] [-o FILE]` | Download the project's (or one compose service's) `.env` file — prints to stdout, or writes `FILE` |
| `fhcli env-set PROJECT FILE [-s SERVICE]` | Upload `FILE` as that `.env` file (`-` reads stdin). Saved only — run `restart` to apply |
| `fhcli env-clear PROJECT [-s SERVICE]` | Delete that `.env` file |
| `fhcli exec PROJECT "COMMAND"` | Run a command inside the container |
| `fhcli ssl PROJECT` | Issue / retry the SSL certificate |
| `fhcli compose-down PROJECT` | `docker compose down` |
| `fhcli compose-status PROJECT` | Last compose operation's status + logs |
| `fhcli logs PROJECT [-n N] [-s SERVICE] [-o FILE]` | Last `N` lines the **container** printed (default 200). Compose: the whole stack, or one service with `-s`. Log to stdout, summary to stderr |
| `fhcli status PROJECT [--follow]` | Status + logs of the last docker **operation** (build/run/stop) |
| `fhcli abort PROJECT` | Abort the running docker op |
| `fhcli volumes PROJECT` | List the project's docker volumes with measured sizes and which services mount them |
| `fhcli volume-download PROJECT VOLUME [-o FILE]` | Archive a volume as a **tar** and download it in chunks (default `{project}-{volume}.tar`). `VOLUME` is the docker name shown by `fhcli volumes` |
| `fhcli volume-upload PROJECT VOLUME ARCHIVE [--yes] [--no-follow]` | **Replace** a volume's contents with a tar: the project's containers stop, the volume is wiped and extracted into, then they start again |
| `fhcli versions PROJECT` | List the project's blue/green versions (active / inactive / archived) |
| `fhcli rollback PROJECT VERSION [--restore-data] [--backup ID] [--no-follow]` | Activate an earlier version. `--restore-data` also puts that version's volume data + env back from a backup archive |
| `fhcli set-backup-limit PROJECT N` | How many archived **versions** to keep (distinct from a backup archive's `--keep`) |
| `fhcli backup PROJECT [--version N] [--no-volumes] [--upload] [--no-follow]` | **Archive the project** — its version's image(s), volumes, env and project files in one tar (deploy log streams live) |
| `fhcli backups PROJECT` | List the project's backup archives, newest first |
| `fhcli backup-download PROJECT ID [-o FILE]` | Fetch one archive in chunks |
| `fhcli backup-upload PROJECT ARCHIVE [--no-follow]` | **Import an archive as a new archived version** — activate it with `rollback` (admin only) |
| `fhcli backup-delete PROJECT ID [--remote] [--yes]` | Delete an archive (`--remote` also at the destination) |
| `fhcli backup-config PROJECT [--enable\|--disable] [--cron EXPR] [--on-deploy] [--target NAME] [--keep N] [--keep-remote N]` | Show or change automatic backups. No options → show (admin only — reading it too) |
| `fhcli backup-targets` | List the destinations declared in the server's `.env` (never their credentials) (admin only) |
| `fhcli backup-target-test NAME` | Check a destination is reachable and its credentials work (admin only) |
| `fhcli db-backup [--upload]` / `db-backups` / `db-backup-download ID` / `db-backup-delete ID` / `db-backup-config …` | The same, for the **freeholdy database itself** (admin only — the whole scope) |
| `fhcli remove PROJECT [--yes] [--keep-volumes]` | Delete the project (containers, images, nginx, DB row) **and its docker volumes** — `--keep-volumes` leaves the data on disk |
| `fhcli whoami` | What the configured token may do — its role, and the project a guest token is bound to |
| `fhcli tokens` | List every API token (admin only) |
| `fhcli token-create NAME [--role admin\|guest] [-P PROJECT]…` | Mint a token, printed **once**. `--role guest -P a -P b` scopes it to those projects |
| `fhcli token-projects ID [-P PROJECT]…` | Replace which projects a guest token may act on — the token itself is unchanged (admin only) |
| `fhcli token-revoke ID [--yes]` | Revoke a token immediately (admin only) |

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

The exception is a **brand-new project**: `env-set` cannot reach a container that does not
exist yet, so pass `deploy --env` to have the variables stored before the first container is
ever created — useful when the app needs config to boot at all.

```bash
# Set the environment as part of the very first deploy — no restart needed
fhcli deploy myapp ./myapp --env prod.env

# Round-trip an existing project's environment
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

## Volumes (project data)

A project's docker volumes are the one thing freeholdy cannot rebuild: images come from
source and the project directory is re-uploaded on every deploy, but volume data exists only
once. They are listed per project and move in and out as tar archives.

```bash
fhcli volumes ws-chat                                   # name, size, which services mount it
fhcli volume-download ws-chat ws-chat_chat-data -o backup.tar
tar tvf backup.tar                                      # ./chat.db
fhcli volume-upload ws-chat ws-chat_chat-data backup.tar
```

- **Download** archives the volume server-side and streams it back in 1 MiB pieces; the
  containers keep running, so a database being written to is best archived after a `stop`.
- **Upload replaces**: the containers stop, the volume's contents are wiped, the archive is
  extracted, and the containers start again — the volume ends up holding exactly what the tar
  holds, not a merge. The restore is a normal job, so its log streams like a deploy's.
- **`fhcli remove` deletes volumes by default.** The confirmation names each one and its size;
  pass `--keep-volumes` to leave them on disk, where a project deployed under the same name
  later picks them straight back up.
- Compose projects show their named volumes (`external:` ones are listed but never deleted);
  a single-container project shows the anonymous volumes its image's `VOLUME` instruction
  created.

## Backups

A version protects you from a bad deploy; a **backup** protects you from a lost server. One
`.tar` holds the version's image(s), every volume, the stored env and the project files — enough
to bring the project back on a different machine.

```bash
fhcli backup myapp                        # archive it now
fhcli backups myapp                       # list archives
fhcli backup-download myapp 4 -o out.tar  # take it off the box
fhcli backup-upload myapp out.tar         # → imported as v7 (archived)
fhcli rollback myapp 7 --restore-data     # activate it, data and all
```

- **Importing does not overwrite anything.** The archive's images load under the project's next
  version number and appear in `fhcli versions` as *archived*; activating one is the ordinary
  `rollback`, so there is only ever one answer to which build is live.
- `--restore-data` is opt-in: a plain rollback swaps code and images and leaves data alone.
- **Volumes are captured at backup time**, not at the version's deploy time — docker keeps no
  per-version volume state, so backing up an older version pairs its image with today's data.
- Automatic backups have two triggers, a cron schedule and after-every-deploy:
  ```bash
  fhcli backup-config myapp --enable --cron '0 3 * * *' --keep 7
  fhcli backup-config myapp --enable --on-deploy
  ```
- Destinations (`rsync` over ssh, or FTP/FTPS) are declared as `BACKUP_TARGET_*` blocks in the
  **server's** `.env`; `fhcli backup-targets` shows names and hosts, never credentials. A dead
  destination never fails the backup — the local archive is kept and reports `remote: error`.
- The freeholdy database is its own scope: `fhcli db-backup`, `db-backups`, `db-backup-config`.
  Restore it on the server with `scripts/restore_db.sh` (it stops the service first).
- **A guest token gets manual backups only** — `backup`, `backups`, `backup-download` and
  `backup-delete` on the projects it is scoped to. The schedules, the destinations and the
  database scope are admin-only, reading them included.

## Tokens & roles

Every token has a role. **`admin`** (the default) can do everything. **`guest`** is scoped to
a set of projects: for those it may **redeploy**, restart, read logs and status, manage the
environment, list versions, roll back, and take, download or delete **manual** backups — and
no other project. It cannot create or delete projects, upload files, point a project at a repo,
open a shell (`exec`), issue certs, set domains, install plugins, import a backup archive,
read or change the automatic-backup settings (`backup-config`, `backup-targets`), touch the
freeholdy-database scope (`db-backup*`), or manage tokens; any of those returns `403`, as does
naming a project outside its scope.

`redeploy` is the key part: it re-clones the git URL and branch **the server already
recorded** for that project, so the runner never supplies a source. That is what keeps a
guest from replacing a project's contents with something else.

That is the token you give to a CI/CD runner:

```bash
# you, as admin — deploy it from git first, so the project has an origin to re-clone
fhcli deploy myapp https://github.com/me/myapp.git

# mint the token to hand over; it is printed once. Repeat -P for several projects.
fhcli token-create gitlab-ci --role guest --project myapp

fhcli tokens                        # who has what
fhcli token-projects 4 -P myapp -P api   # re-scope without re-minting
fhcli token-revoke 4                # take it back
```

The third party puts `TOKEN` and `BASE_DOMAIN` into their own `cli/.env` (or CI secrets) and
runs `fhcli redeploy myapp` on every push. `fhcli whoami` tells them what they hold.

Two caveats: a guest token can read its projects' environment **values**, so treat it as a
secret of the same weight as those projects' credentials; and deleting a project unbinds it
from every token that covered it — the token keeps working for whatever else it covers, and
is left able to authenticate but not act if that was its last project.

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
