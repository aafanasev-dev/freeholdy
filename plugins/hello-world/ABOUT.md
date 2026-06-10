# Hello World

The simplest possible freeholdy plugin: a **static HTML page** served by Python's
built-in `http.server`. No build step, no dependencies, nothing to configure — it is the
fastest way to confirm that provisioning, nginx, and SSL are all working.

## What you get

- A single static page live within seconds of installing.
- A minimal, readable example of the `dockerfile` deploy mode.
- HTTPS out of the box at `hello-world.{your-domain}`.

## How it works

- **Deploy mode:** `dockerfile` (a single container).
- **Container port:** `80`, served by `python -m http.server`.
- **Subdomain:** published under the `hello-world` prefix.

## Good for

- A smoke test for a fresh freeholdy install.
- Learning the upload → detect → build → run → nginx → certbot flow.
- A blank canvas — replace the HTML with your own and re-upload.
