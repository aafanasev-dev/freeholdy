# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

The React control panel for freeholdy (`webui/`). It is a pure browser-side client of the
FastAPI server documented in `../CLAUDE.md` — it ships no backend of its own and talks to the
live API over HTTPS. The web UI does file/folder upload over the API via the unified
`UploadModal`; the CLI's `fhcli upload` uploads over the same API.

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
- **All styling is inline.** Colors come from the `C` object; there are no class names except the
  one global `<style>` block in `App` (font import, scrollbar, resets, input focus ring). Match
  this — pass `style` props, reuse `C`, do not write CSS.
- **Light "Hostinger-style" theme.** `C` is a light palette: lavender-white surfaces (`bg/s1/s2/s3`),
  a royal-purple brand/primary accent (`C.purple`), dark-navy text (`C.txt`), and a soft `C.shadow`
  for cards. `C.ff` is the UI sans font (DM Sans); `C.mono` (JetBrains Mono) is **only** for
  log/code output (LogPane, status/SSL output, the login token command). Green/amber/red/blue stay
  reserved for status semantics (`SC`/`SI`/`Tag`), not branding.
- **Container/job status is a fixed vocabulary** rendered by the `SC` (color) and `SI` (glyph)
  maps and the `<Tag>` component: `running | done | exited | aborted | error | no_image |
  not_found | no_job`. These mirror the server's synthesized states — keep the maps in sync if the
  API adds a status.

## How it drives the server (endpoint contract)

The UI assumes these endpoints and is the place this contract is exercised from the client side:

- `GET /health`, `GET /projects`, `POST /projects` (name only — no deploy_mode), `DELETE /projects/{name}`
- `GET /plugins` — each item carries `name`, `description`, `about` (Markdown from the plugin's
  `ABOUT.md`, empty when none), `deploy_mode`, `container_port`, `has_install`, `type`. `PluginPanel`
  is a master-detail view: a ~25% name list on the left, a ~75% detail pane on the right that renders
  `about` (falling back to `description`) via the tiny inline `Markdown` component, with a solid-green
  **install** button (`Btn v="green"`) in the pane's top-right. `system`-type plugins are filtered out.
- One upload endpoint: `POST /projects/{name}/upload` (multipart `files[]`; each file's multipart
  filename carries its relative path). Writes the tree under the project dir, then the server
  auto-detects a `Dockerfile`/`docker-compose.yml` in the root and provisions (compose wins).
  Returns `{ status, message, count, files, deploy_mode, provisioned, project }`.
- Dockerfile (single-container) actions, all project-level:
  `POST /projects/{name}/{build|start|stop|ssl|abort}` and `GET /projects/{name}/status`
- Compose lifecycle: `.../compose/{build|up|down|abort}`, `GET .../compose/status`
- **Exec is a WebSocket, not REST:** `WS /projects/{name}/exec` (dockerfile) and
  `WS /projects/{name}/services/{service}/exec` (compose) bridge an interactive `docker exec -it`
  shell. `ExecTerminal` renders an xterm.js terminal (`@xterm/xterm` + `@xterm/addon-fit`): auth
  frame first, then `stdin`/`stdout`/`resize` frames; closing the modal closes the socket (which
  kills the exec server-side). An optional `?cmd=` query overrides the default shell.
- **Install streams over a WebSocket:** `POST /plugins/{name}/add` returns a `ws_path`; the client
  connects to `WS /plugins/{plugin}/install/{project}` and `InstallPane` streams the build log live
  (interactive plugins also drive `install.sh` via an input row). The `exit` frame reports the build
  result — no `/status` polling during install.
- **Git projects:** `POST /git/add` (body `{ name, git_url, branch? }`) clones a repo, auto-detects
  a Dockerfile/compose, provisions nginx + SSL, then builds + runs it. Same response shape as
  `/plugins/{name}/add` (`{ project, job, ws_path }`); the build streams over
  `WS /git/deploy/{project}` (read-only — git deploys never prompt). `GitProjectForm` posts this and
  hands the response to the same `handleInstalled` → `InstallPane` path as plugins (interactive is
  always false). The "+ git project" toolbar button toggles the form alongside "new project"/"add plugin".
- **Git SSH key:** `GET /git/key` returns the server's GitHub SSH public key (`{ public_key, created,
  key_path, instructions }`), creating an ed25519 keypair server-side on first use. The header's
  "git key" button opens `GitKeyModal`, which fetches it and shows the key (with a copy button) plus
  GitHub instructions — so a user can add the key to GitHub and clone private repos over
  `git@github.com:…`. A `POST /git/add` clone that fails SSH auth returns a 400 whose detail points
  the user here, surfaced in `GitProjectForm`'s `<Err>`.

There are **no `/parts/{type}/...`, `/dockerfile`, `/compose`, or `/context` upload endpoints** — a
project starts as `deploy_mode: "pending"` and the first `upload` makes it either one container
(`deploy_mode: "dockerfile"`, fields under `project.container`) or a compose stack
(`project.services[]`).

`UploadModal` (opened from the **upload** button on every `ProjectCard` header, both modes and
pending) posts to `/projects/{name}/upload`. It offers a file picker and a folder picker; the folder
input is a `<input webkitdirectory>` whose non-standard attributes are set via a ref on mount (React
won't pass them through). Each `File.webkitRelativePath` has its leading folder segment stripped
(`stripRoot`) and is sent as the multipart filename via `fd.append("files", file, relPath)`.

Two row components render the project table; a `pending` project (created, not yet uploaded) renders
neither — `ProjectCard` shows an "awaiting upload" placeholder and a `pending` chip in the header:
- `ContainerRow` — dockerfile mode, one row, drives the project-level lifecycle endpoints above.
- `ServiceRow` — compose mode (name/subdomain/port/ssl/status) plus a per-service **exec** button
  (and the custom-domain button); the stack's build/up/down live on the `ProjectCard` header and
  stream at the compose level. The exec button opens an `ExecTerminal` for that service's container.

Operation flow (`Dashboard` + `ContainerRow`):
- Action buttons call the endpoint, then push an `activeLog` into the bottom `LogPane`.
- If the action returns `status === "running"`, `Dashboard` **polls status every `POLL_MS`
  (1000ms)** via a single shared `pollRef` interval (`/projects/{name}/status`, or
  `/projects/{name}/compose/status` when `log.kind === "compose"`), streaming logs until status
  leaves `running`, then refetches the project list. Only one operation is polled at a time.
  (Exec and plugin installs bypass this — they stream over their own WebSockets: `ExecTerminal` and
  `InstallPane` respectively.)
- `abort` posts to the matching `.../abort` and stops the poll.

## Project = subdomain; mode + port auto-detected from the upload

A project's name is its subdomain label — a dockerfile project is served at `{name}.your_domain.com`
(`CreateForm` previews this), compose services at `{service}.{name}.your_domain.com`. `CreateForm`
takes **only a name** — there is no deploy-mode or port input. The deploy mode is auto-detected
server-side from the first `upload` (a `docker-compose.yml` wins over a `Dockerfile`), and a
dockerfile project's container port is read from the Dockerfile's `EXPOSE` instruction, so the
Dockerfile must declare one (the upload is rejected otherwise).
