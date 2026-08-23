# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

The React control panel for freeholdy (`webui/`). It is a pure browser-side client of the
FastAPI server documented in `../CLAUDE.md` — it ships no backend of its own and talks to the
live API over HTTPS. The web UI does file/folder upload over the API via the unified
`UploadModal` / `DeployForm`; the CLI's `fhcli deploy` deploys over the same API.

## Commands

```bash
npm install
npm run dev       # Vite dev server on http://localhost:5173
npm run build     # static bundle → dist/
npm run preview   # serve the built bundle locally
```

No tests, linter, or formatter are configured. Production deploy is a static copy of `dist/`
behind nginx (`ui.your_domain.com`); see `README.md`.

## Architecture

**The entire application is one file: `src/App.jsx` (~930 lines).** `main.jsx` only mounts it.
There is no router, no component directory, and no CSS files. When adding UI, add it to `App.jsx`
following the existing `// ── Section ──` comment dividers and component conventions below — do not
introduce new files or a styling library unless asked.

Things that require reading the whole file to understand:

- **API base URL:** `const BASE = import.meta.env.VITE_API_URL || "https://api.your_domain.com"` at
  the top of `App.jsx`. `VITE_API_URL` is baked in at build time (the webui plugin's `install.sh`
  writes it into `.env`); fall back is the production API.
- **`mkApi(token)` is the single HTTP layer.** Every request goes through it; `get/post/del` send
  JSON with a `Bearer` header, `form` sends `FormData` (used only for file uploads, where the
  `Content-Type` header is deliberately omitted so the browser sets the multipart boundary).
  `unwrap` throws `Error(detail)` on non-2xx, so all callers just `try/catch` and surface
  `e.message`.
- **Auth is a token in `localStorage["freeholdy_token"]`.** `App` gates on its presence; `LoginScreen`
  validates by calling `/health` before storing. Logout clears the key. There is no refresh/expiry
  handling — a rejected request just shows an error, it does not force re-login.
- **All styling is inline.** Colors come from the `C` object; the only class names are the
  handful the one global `<style>` block in `App` needs — the responsive nav-rail media query
  (`.fh-rail`, `.fh-main`, `.fh-burger`), the `.fh-nav` / `.fh-active` nav-item hover rule
  (`NavItem` can't express `:hover` inline), plus the font import/scrollbar/reset/focus-ring
  rules. Match this — pass `style` props, reuse `C`, do not write CSS beyond that block.
- **Dark blue-slate theme, with three separate accent roles.** `C` is a cool blue-slate palette:
  chrome `bg #0F1421` with panels `s1/s2/s3` (`#161C2B`/`#1C2334`/`#242D40`), light text
  (`C.txt #E9EDF5`), and translucent **tint tokens** (`C.moneyFill/moneyBd`, `blueFill/blueBd`,
  `greenFill/greenBd`, `amberFill/amberBd`, `redFill/redBd`, `purpleFill/purpleBd`) that variant
  buttons (`Btn`), chips, badges, and `Err`/`Ok` use so they read on dark. The accents do **not**
  overlap:
  - **`C.brand` coral `#FF6B4A` is identity only** — the nav-rail wordmark, the `LoginScreen`
    wordmark, and the favicon in `index.html`. Nothing else may use it. (`C.brandFill/brandBd`
    are kept in `C` for completeness but are currently unreferenced.)
  - **`C.money` `#4EB06B` is primary action + active state** — the solid-filled `Btn v="primary"`
    (dark `C.moneyInk` text: every deploy/install/submit button), the active `NavItem`, the
    selected source segment in the `DeployForm`/`UploadModal` `tab()` helpers, and the selected
    row in `PluginPanel`. Hover comes from the global `button:hover { filter: brightness(1.12) }`
    rule, so `C.moneyH/moneyA` exist for completeness and may be unreferenced.
  - **`C.blue` `#5B9EFF` is links, the focus ring, and informational chips** (`compose`, `custom`,
    plugin mode chips).
  - **`C.green` mint `#34D399` / `amber` / `red` stay reserved for status semantics** (`SC`/`SI`/
    `Tag`, `Ok`, danger buttons). Mint and money green are deliberately distinct — mint never
    fills a button, money never marks a status.

  Titles, project names, and inline emphasis are plain `C.txt`. `C.ff` is Inter; `C.mono`
  (JetBrains Mono / Roboto Mono, both fetched by the `@import`) is **only** for log/code output
  (LogPane, ExecTerminal, status/SSL output, the login token command). Corner radii follow a
  scale: buttons/inputs/segments/nav items `10px`, cards/panels/modals/log panes `14px`,
  chips/badges/inline code `7px`, progress bars `999px`.
- **`Dashboard` layout is a fixed left nav rail + shifted main column.** A `<nav className="fh-rail">`
  (220px, `C.s2`) holds the `freeholdy` brand + version badge, the `NavItem` entries (**Projects**
  clears the panels, **Deploy**/**Plugins** toggle `showDeploy`/`showPlugins` with a money-green
  active state, **Git key** opens `GitKeyModal`), and a bottom **Refresh**/**Logout** footer. The content
  sits in `.fh-main` (`margin-left:220px`) under a slim sticky top bar (section title + `api ●`
  health dot + `DOMAIN`). Below 820px the rail becomes an off-canvas overlay toggled by `railOpen`
  (the `.fh-burger` hamburger + a backdrop); the media query lives in the global `<style>` block.
- **Container/job status is a fixed vocabulary** rendered by the `SC` (color) and `SI` (glyph)
  maps and the `<Tag>` component: `running | done | exited | aborted | error | no_image |
  not_found | no_job`. These mirror the server's synthesized states — keep the maps in sync if the
  API adds a status.

## How it drives the server (endpoint contract)

The UI assumes these endpoints and is the place this contract is exercised from the client side:

- `GET /health`, `GET /projects`, `DELETE /projects/{name}` (there is **no** `POST /projects`
  create endpoint — a deploy auto-creates the row; see below)
- `GET /plugins` — each item carries `name`, `description`, `about` (Markdown from the plugin's
  `ABOUT.md`, empty when none), `deploy_mode`, `container_port`, `has_install`, `type`. `PluginPanel`
  is a master-detail view: a ~25% name list on the left, a ~75% detail pane on the right that renders
  `about` (falling back to `description`) via the tiny inline `Markdown` component, with a solid
  money-green **install** button (`Btn v="primary"`) in the pane's top-right. `system`-type plugins
  are filtered out.
- Upload (chunked): `POST /projects/{name}/upload/chunk` then `.../upload/complete`. **Auto-creates
  the project row if it doesn't exist yet** (no separate create call), reassembles + unzips the tree
  under the project dir, auto-detects a `Dockerfile`/`docker-compose.yml` in the root and provisions
  (compose wins), then **auto-launches build + run and returns a `ws_path`** — the same unified deploy
  path git/plugins use. Returns `{ status, message, count, files, deploy_mode, provisioned, project,
  ws_path, job }`. The shared `chunkedDeploy(api, project, entries, onProgress)` helper implements the
  zip → chunk → complete round-trip; both `UploadModal` (per-card redeploy) and `DeployForm` (new
  project) call it. A provisioned response is handed to `handleInstalled` → `InstallPane` to stream the
  deploy log live (a no-manifest sync has no `ws_path` and just shows its result). Re-upload to redeploy.
- Dockerfile (single-container) actions, all project-level:
  `POST /projects/{name}/{stop|ssl|abort}` and `GET /projects/{name}/status` (no `/build` or `/start`)
- **Blue/green versions** (both modes): `GET /projects/{name}/versions` (active/inactive/archived
  list + counts + backup limit; compose versions are active/archived only and carry null
  `image_name`/`container_name`/`local_port`), `PUT /projects/{name}/backup-limit` (`{limit}`), and
  `POST /projects/{name}/rollback` (`{version}`, returns `{job, ws_path}`). The **versions** button
  lives on `ContainerRow` for dockerfile projects and on the `ProjectCard` header for compose ones;
  both open the shared `VersionsModal` (backup-limit control + versions table). A **rollback** streams
  over `WS /projects/{name}/deploy` — the modal hands the returned `ws_path` up via `onStream`
  (`Dashboard.handleDeployStream`) to the shared `InstallPane`, exactly like a deploy. `mkApi` now also
  has a `put`.
- Compose lifecycle: `.../compose/{down|abort}`, `GET .../compose/status` (no `/build` or `/up`)
- **Environment variables** (both modes): `GET|PUT|DELETE /projects/{name}/env` for the project-level
  `.env` file (a dockerfile project's container env; a compose stack's shared file) and
  `GET|PUT|DELETE /projects/{name}/services/{service}/env` for one service's own file, whose values
  override the shared ones. `EnvResponse` carries `{ project, service, content, keys, updated_at,
  applied, status, message }`. Saving is **save-only** — the server never restarts anything, so
  `applied: false` means the running container still has the old values; `EnvModal` then shows an
  amber banner with a **restart now** button. `POST /projects/{name}/restart` recreates the
  container(s) from the images they already run (no rebuild) and reports like `stop`/`down`, so it
  flows through the normal `handleOperation` → `LogPane` polling (compose needs `kind: "compose"`).
  `ContainerInfo`/`ServiceInfo` gained `env_count`, rendered as the count on the `env` button.
  `EnvModal` uses the `TextArea` primitive (mono, `whiteSpace: "pre"`, resizable) added next to
  `TextIn`; the global `<style>` block's placeholder/focus rules cover `input, textarea`.
  A **PUT with malformed dotenv returns a FastAPI 422**, whose `detail` is a *list* of
  `{loc, msg}` objects rather than a string — `mkApi`'s `unwrap` flattens that to newline-joined
  `msg` text (and strips pydantic's `"Value error, "` prefix), so `e.message` never renders
  `[object Object]`; `Err` is `white-space: pre-wrap` to keep the per-line breakdown readable.
- **Exec is a WebSocket, not REST:** `WS /projects/{name}/exec` (dockerfile) and
  `WS /projects/{name}/services/{service}/exec` (compose) bridge an interactive `docker exec -it`
  shell. `ExecTerminal` renders an xterm.js terminal (`@xterm/xterm` + `@xterm/addon-fit`): auth
  frame first, then `stdin`/`stdout`/`resize` frames; closing the modal closes the socket (which
  kills the exec server-side). An optional `?cmd=` query overrides the default shell.
- **Install streams over a WebSocket:** `POST /plugins/{name}/add` returns a `ws_path`; the client
  connects to `WS /plugins/{plugin}/install/{project}` and `InstallPane` streams the build log live
  (interactive plugins also drive `install.sh` via an input row). The `exit` frame reports the build
  result — no `/status` polling during install.
- **Git deploy:** `POST /git/add` (body `{ name, git_url, branch? }`) auto-creates-or-reuses the row
  (idempotent: new name creates, existing name redeploys), clones a repo, auto-detects a
  Dockerfile/compose, provisions nginx + SSL, then builds + runs it. Same response shape as
  `/plugins/{name}/add` (`{ project, job, ws_path }`); the build streams over
  `WS /git/deploy/{project}` (read-only — git deploys never prompt). `DeployForm`'s **git tab** posts
  this and hands the response to the same `handleInstalled` → `InstallPane` path as plugins (interactive
  is always false).
- **Git SSH key:** `GET /git/key` returns the server's GitHub SSH public key (`{ public_key, created,
  key_path, instructions }`), creating an ed25519 keypair server-side on first use. The header's
  "git key" button opens `GitKeyModal`, which fetches it and shows the key (with a copy button) plus
  GitHub instructions — so a user can add the key to GitHub and clone private repos over
  `git@github.com:…`. A `POST /git/add` clone that fails SSH auth returns a 400 whose detail points
  the user here, surfaced in `DeployForm`'s `<Err>` (git tab).

`DeployForm` is the single new-project entry point (toolbar **+ deploy project** button, `showDeploy`
state), alongside **+ add plugin**. It takes a project name plus a **files/folder ⇄ git URL** toggle:
the files tab calls `chunkedDeploy`, the git tab posts `/git/add`; a provisioned response is handed to
`onDeployed` (= `handleInstalled`) to stream in the `InstallPane`, a manifest-less file sync calls
`onSynced` (= `fetchProjects`). It replaces the former `CreateForm` (which posted the now-removed
`POST /projects`) and `GitProjectForm`.

There are **no `/parts/{type}/...`, `/dockerfile`, `/compose`, `/context`, or `POST /projects`
endpoints** — a deploy auto-creates the row as `deploy_mode: "pending"` and the manifest it detects
makes it either one container (`deploy_mode: "dockerfile"`, fields under `project.container`) or a
compose stack (`project.services[]`).

`UploadModal` (opened from the **upload** button on every `ProjectCard` header, both modes and
pending) has the same **files/folder ⇄ git URL** tabs as `DeployForm`: the files tab chunk-uploads
to `/projects/{name}/upload/chunk` + `.../upload/complete`, the git tab posts `/git/add` (idempotent
redeploy of the same project name). It offers a file picker and a folder picker; the folder input is
a `<input webkitdirectory>` whose non-standard attributes are set via a ref on mount (React won't pass
them through). Each `File.webkitRelativePath` has its leading folder segment stripped (`stripRoot`)
and becomes its zip entry name. A provisioned response (carrying `ws_path`) is handed to `onDeploy`
(= `handleInstalled`) so the deploy streams in the `InstallPane`; the modal closes. The card's primary
button reads **deploy** for a provisioned project (re-uploading redeploys) and **upload** while pending.

**Deploy sources are remembered** in `localStorage["freeholdy_deploy_history"]` (helpers
`loadDeployHistory`/`getProjectDeploy`/`getRecentGitUrls`/`saveProjectDeploy` near the top of
`App.jsx`): `{ projects: { [name]: { srcKind, gitUrl, branch, label, ts } }, recentGitUrls: [...] }`.
A **git** source is fully reusable — `UploadModal` pre-selects the git tab and pre-fills the URL +
branch of a project's last deploy, and `DeployForm` offers recent git URLs as clickable "recent:"
chips. A **files/folder** source can only be remembered as a hint (browsers never expose a file path
and can't re-read files without a fresh user pick): the last selection's `label` (e.g.
`folder 'x' · 42 files`) is shown, but re-picking is still required. History is saved at the deploy
call sites in `UploadModal`/`DeployForm` (the source isn't in the deploy response), not in `Dashboard`.

Two row components render the project table; a `pending` project (created, not yet uploaded) renders
neither — `ProjectCard` shows an "awaiting upload" placeholder and a `pending` chip in the header:
- `ContainerRow` — dockerfile mode, one row, drives the remaining project-level control endpoints
  (`stop`, `ssl`, `domain`, `env`, `restart`, `status`, `exec`). Build + run happen via the
  deploy/upload flow, not a button.
- `ServiceRow` — compose mode (name/subdomain/port/ssl/status) plus a per-service **exec** and
  **env** button (and the custom-domain button, exposed services only); the stack's **env**,
  **restart** and **down** live on the `ProjectCard` header (build + up happen via the
  deploy/upload flow). The exec button opens an `ExecTerminal` for that service's container.

Note both rows render their modals as `<div>`s directly under `<tbody>` (`ContainerRow`) — React
logs a `validateDOMNesting` warning for that. It predates the env work (`DomainModal`,
`VersionsModal` and `ExecTerminal` all do it); `EnvModal` follows the same placement.

Operation flow (`Dashboard` + `ContainerRow`):
- Control buttons (`stop`, compose `down`) call the endpoint, then push an `activeLog` into the
  bottom `LogPane`.
- If the action returns `status === "running"`, `Dashboard` **polls status every `POLL_MS`
  (1000ms)** via a single shared `pollRef` interval (`/projects/{name}/status`, or
  `/projects/{name}/compose/status` when `log.kind === "compose"`), streaming logs until status
  leaves `running`, then refetches the project list. Only one operation is polled at a time.
  (Deploys/installs and exec bypass this — they stream over their own WebSockets: `InstallPane`
  and `ExecTerminal` respectively.)
- `abort` posts to the matching `.../abort` and stops the poll.

## Project = subdomain; mode + port auto-detected from the deploy

A project's name is its subdomain label — a dockerfile project is served at `{name}.your_domain.com`
(`DeployForm` previews this), compose services at `{service}.{name}.your_domain.com`. `DeployForm`
takes **only a name** (plus the files/git source) — there is no deploy-mode or port input. The deploy
mode is auto-detected server-side from the deployed source (a `docker-compose.yml` wins over a
`Dockerfile`), and a dockerfile project's container port is read from the Dockerfile's `EXPOSE`
instruction, so the Dockerfile must declare one (the deploy is rejected otherwise).
