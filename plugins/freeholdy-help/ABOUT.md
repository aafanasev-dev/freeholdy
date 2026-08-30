# freeholdy help

A deployable **user guide for freeholdy itself** — a single **static HTML page** served by
Python's built-in `http.server`, with a minimal header and a left-hand navigation menu.
It covers: what freeholdy is, installation, deploying apps (folder / git / plugins), the
plugin catalog, versions & rollback, backups (manual, automatic, and off-server
destinations), environment variables, container logs & shell access, the REST API, token
roles (including guest tokens for CI/CD), the `fhcli` command reference, and the `fhdeploy`
Claude Code AI integration.
No build step, no dependencies, nothing to configure — it also doubles as a smoke test that
provisioning, nginx, and SSL all work.

## What you get

- The full freeholdy user guide, live within seconds of installing.
- A minimal, readable example of the `dockerfile` deploy mode.
- HTTPS out of the box at `freeholdy-help.{your-domain}`.

## How it works

- **Deploy mode:** `dockerfile` (a single container).
- **Container port:** `80`, served by `python -m http.server`.
- **Subdomain:** published under the `freeholdy-help` prefix.

## Good for

- Onboarding: hand new users one URL that explains the whole workflow.
- A smoke test for a fresh freeholdy install.
- A blank canvas — replace the HTML with your own and re-upload.
