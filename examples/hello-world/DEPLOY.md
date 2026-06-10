# Deploy hello-world with freeholdy

This example runs the simplest possible HTTP server — a single static HTML page
served by Python's built-in `http.server`. No dependencies, no build step.

**What you'll get:** `https://hello-world.your_domain.com`

The folder contains everything freeholdy needs:

```
examples/hello-world/
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

## Step 1 — Create the project

```bash
fhcli create hello-world
```

Expected output:
```
Creating project hello-world…

✓ Project 'hello-world' created  (deploy mode: pending)

  Next: fhcli upload hello-world ./path-to-your-project
  The folder should contain a Dockerfile or a docker-compose.yml.
```

The project starts in `pending` mode — no port, subdomain, or nginx config yet.
All of that is wired up on the **first upload**, once freeholdy can see what kind
of project it is.

---

## Step 2 — Upload the project (auto-detect)

Run this from the `examples/hello-world/` directory:

```bash
fhcli upload hello-world ./
```

freeholdy writes the files into the project's directory, scans the root, finds the
`Dockerfile`, reads its `EXPOSE` (port 80) as the container port, and wires up nginx
+ SSL. A `docker-compose.yml`, if present, would win and select compose mode instead.

Expected output:
```
Uploading 2 file(s) → hello-world…
✓ Provisioned hello-world in dockerfile mode
  Dockerfile
  HelloWorld.html

Deploy mode: dockerfile

  Container    Subdomain                      Port   SSL
  hello-world  hello-world.your_domain.com    8100   ✓

  Next: fhcli build hello-world
```
(The exact `✓` message comes from the server; the file list and table are rendered
by the CLI.)

> The `Port` (here `8100`) is a loopback port freeholdy allocates automatically from
> its configured range — yours may differ. Public traffic always goes through nginx.
>
> If SSL shows `pending` instead of `✓`, run `fhcli ssl hello-world` once the domain
> resolves to your VPS. SSL issuance needs the DNS record to already point at the host.

---

## Step 3 — Build the Docker image

```bash
fhcli build hello-world
```

The build streams live and finishes with:
```
✓ Image built successfully
```
Image name: `freeholdy_hello-world:latest`. Add `--no-follow` to return immediately
and poll with `fhcli status hello-world`.

---

## Step 4 — Start the container

```bash
fhcli start hello-world
```

Expected output:
```
✓ Container started
```
Container name: `freeholdy_hello-world`, bound to `127.0.0.1:<local-port>`.

---

## Step 5 — Verify it's running

**Check container status:**
```bash
fhcli projects
```

`hello-world` should show `▶ running`, along with its subdomain and local port.

**Check directly on the VPS** (bypasses nginx/SSL — useful for a quick sanity check;
use the port shown by `fhcli projects`):
```bash
curl http://localhost:8100
# returns the HTML
```

**Check via HTTPS** (the real test):
```bash
curl https://hello-world.your_domain.com
```

Or open `https://hello-world.your_domain.com` in a browser.

You should see: **Hello, World! 👋**

---

## Troubleshooting

**Container shows `no_image`**
→ Run `fhcli build hello-world` first.

**Container shows `exited`**
→ Check what happened inside:
```bash
fhcli exec hello-world "ls /www"
```

**curl returns `502 Bad Gateway`**
→ Container is not running. Check status and start it:
```bash
fhcli projects
fhcli start hello-world
```

**SSL cert missing / browser shows security warning**
```bash
fhcli ssl hello-world
```
This re-runs certbot for `hello-world.your_domain.com`. The domain's DNS must
point to your VPS for this to succeed.

**See container logs on the VPS directly:**
```bash
docker logs freeholdy_hello-world
docker logs -f freeholdy_hello-world   # follow
```

---

## Lifecycle commands

```bash
fhcli stop  hello-world   # stop the container
fhcli start hello-world   # start it again
fhcli build hello-world   # rebuild after changing the Dockerfile
```

If you change `HelloWorld.html` (or any file in the build context), re-upload before
rebuilding so the server has the new files:
```bash
fhcli upload hello-world ./ && \
fhcli build  hello-world && \
fhcli start  hello-world
```
