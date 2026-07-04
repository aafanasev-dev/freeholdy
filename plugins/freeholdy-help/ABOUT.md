# freeholdy help

A deployable **overview page for freeholdy itself** — a single **static HTML page** served
by Python's built-in `http.server` that lays out what freeholdy can do: folder deploy, git
deploy, plugins, and versions/rollback. No build step, no dependencies, nothing to
configure — it also doubles as a smoke test that provisioning, nginx, and SSL all work.

## What you get

- A polished freeholdy overview page, live within seconds of installing.
- A minimal, readable example of the `dockerfile` deploy mode.
- HTTPS out of the box at `freeholdy-help.{your-domain}`.

## How it works

- **Deploy mode:** `dockerfile` (a single container).
- **Container port:** `80`, served by `python -m http.server`.
- **Subdomain:** published under the `freeholdy-help` prefix.

## Good for

- A landing page that shows off freeholdy's capabilities.
- A smoke test for a fresh freeholdy install.
- A blank canvas — replace the HTML with your own and re-upload.
