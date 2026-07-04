# Deploy freeholdy-help with freeholdy

This example runs the simplest possible HTTP server — a single static HTML page
served by Python's built-in `http.server`. No dependencies, no build step.

**What you'll get:** `https://freeholdy-help.your_domain.com`

The folder contains everything freeholdy needs:

```
examples/freeholdy-help/
├── Dockerfile        # FROM python:3.11-alpine, EXPOSE 80, serves /www
└── HelloWorld.html   # copied to /www/index.html in the image
```

---

## Prerequisites

- freeholdy API is running at `https://manager.your_domain.com`
- CLI is set up and `fhcli` is in your PATH (see `cli/README.md`)
- `cli/.env` has a valid TOKEN and BASE_URL

Verify with:
```bash
fhcli health
# ✓ API is ok  (https://manager.your_domain.com)
```

---

## Step 1 — Deploy the project (auto-create + auto-detect)

Run this from the `examples/freeholdy-help/` directory:

```bash
fhcli deploy freeholdy-help ./
```

There is no separate create step — `deploy` creates the project automatically if it's
new. freeholdy writes the files into the project's directory, scans the root, finds the
`Dockerfile`, reads its `EXPOSE` (port 80) as the container port, wires up nginx
+ SSL, **then builds the image and starts the container** — streaming the deploy log
live. A `docker-compose.yml`, if present, would win and select compose mode instead
(the deploy would `docker compose up -d` it the same way). You can also pass a git URL
instead of a path (`fhcli deploy freeholdy-help https://github.com/owner/repo.git`).

Expected output:
```
Uploading 2 file(s) → freeholdy-help…
✓ Provisioned freeholdy-help in dockerfile mode
  Dockerfile
  HelloWorld.html

Deploy mode: dockerfile

  Container    Subdomain                      Port   SSL
  freeholdy-help  freeholdy-help.your_domain.com    8100   ✓

─── deploy output (build → run) ───
... docker build output ...
✓ Project deployed and running
```
(The exact `✓` message comes from the server; the file list and table are rendered
by the CLI. Add `--no-follow` to return immediately and poll `fhcli status freeholdy-help`.)

> The `Port` (here `8100`) is a loopback port freeholdy allocates automatically from
> its configured range — yours may differ. Public traffic always goes through nginx.
>
> If SSL shows `pending` instead of `✓`, run `fhcli ssl freeholdy-help` once the domain
> resolves to your VPS. SSL issuance needs the DNS record to already point at the host.

Image name: `freeholdy_freeholdy-help:latest`; container name: `freeholdy_freeholdy-help`,
bound to `127.0.0.1:<local-port>`. **Re-run `fhcli deploy freeholdy-help ./` to redeploy**
after changing any file.

---

## Step 2 — Verify it's running

**Check container status:**
```bash
fhcli projects
```

`freeholdy-help` should show `▶ running`, along with its subdomain and local port.

**Check directly on the VPS** (bypasses nginx/SSL — useful for a quick sanity check;
use the port shown by `fhcli projects`):
```bash
curl http://localhost:8100
# returns the HTML
```

**Check via HTTPS** (the real test):
```bash
curl https://freeholdy-help.your_domain.com
```

Or open `https://freeholdy-help.your_domain.com` in a browser.

You should see: **Hello, World! 👋**

---

## Troubleshooting

**Container shows `no_image`**
→ The deploy didn't build. Re-run `fhcli deploy freeholdy-help ./` and watch the deploy log.

**Container shows `exited`**
→ Check what happened inside:
```bash
fhcli exec freeholdy-help "ls /www"
```

**curl returns `502 Bad Gateway`**
→ Container is not running. Check status and redeploy:
```bash
fhcli projects
fhcli deploy freeholdy-help ./
```

**SSL cert missing / browser shows security warning**
```bash
fhcli ssl freeholdy-help
```
This re-runs certbot for `freeholdy-help.your_domain.com`. The domain's DNS must
point to your VPS for this to succeed.

**See container logs on the VPS directly:**
```bash
docker logs freeholdy_freeholdy-help
docker logs -f freeholdy_freeholdy-help   # follow
```

---

## Lifecycle commands

```bash
fhcli stop   freeholdy-help   # stop the container
fhcli deploy freeholdy-help ./   # redeploy (rebuild + restart) after changing any file
```

There is no separate create or build/start step — `fhcli deploy` creates (if new),
provisions, builds, and runs in one command, and re-running it is how you redeploy after a
change to `HelloWorld.html`, the `Dockerfile`, or anything else in the build context.
