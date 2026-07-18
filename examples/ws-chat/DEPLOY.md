# Deploy ws-chat with freeholdy

This example runs a realtime WebSocket chat: a static React frontend plus a Node.js
WebSocket backend, wired together as a two-service `docker compose` stack. No auth —
just pick a username in the browser.

**What you'll get:**
- `https://web.ws-chat.your_domain.com` — the chat UI
- `https://websocket.ws-chat.your_domain.com` — the WebSocket server the UI connects to

The folder contains everything freeholdy needs:

```
examples/ws-chat/
├── docker-compose.yml    # two services: web (nginx) + websocket (Node)
├── frontend/             # React/Vite app → static bundle served by nginx on :80
│   └── Dockerfile
└── backend/              # Node.js ws server on :8080
    └── Dockerfile
```

Two things make the wiring work automatically:
- **Service names become subdomains.** freeholdy exposes each TCP service at
  `{service}.{project}.{domain}`, so the services named `web` and `websocket` land at
  `web.ws-chat.…` and `websocket.ws-chat.…`. The frontend derives the backend URL from
  its own hostname (swaps `web.` → `websocket.`), so nothing is hard-coded.
- **The `websocket` service name triggers WebSocket support.** freeholdy scans the
  compose file and emits nginx upgrade headers for any service whose block/name looks
  like a WebSocket endpoint — here, `websocket`.

---

## Prerequisites

- freeholdy API is running at `https://manager.your_domain.com`
- CLI is set up and `fhcli` is in your PATH (see `cli/README.md`)
- `cli/.env` has a valid TOKEN and BASE_URL
- `docker compose` (Compose v2) is available on the VPS — compose-mode projects need it

Verify with:
```bash
fhcli health
# ✓ API is ok  (https://manager.your_domain.com)
```

---

## (Optional) Step 0 — name the chat room

The chat room name is baked into the frontend at build time via the `VITE_CHAT_NAME`
build arg, which defaults to `ws-chat`. To change it, drop a `.env` next to
`docker-compose.yml` before deploying (freeholdy uploads it with the project):

```bash
echo 'CHAT_NAME=Team Lounge' > examples/ws-chat/.env
```

Skip this and the room is just called **ws-chat**.

---

## Step 1 — Deploy the project (auto-create + auto-detect)

Run this from the `examples/ws-chat/` directory:

```bash
fhcli deploy ws-chat ./
```

There is no separate create step — `deploy` creates the project automatically if it's
new. freeholdy writes the files into the project's directory, scans the root, finds the
`docker-compose.yml` (which **wins over** any Dockerfile), allocates a loopback port per
exposed service, wires up nginx + SSL for each, writes a `docker-compose.override.yml`
pinning the container names and port bindings, **then runs `docker compose up -d`** —
streaming the deploy log live. You can also pass a git URL instead of a path
(`fhcli deploy ws-chat https://github.com/owner/repo.git`).

Expected output:
```
Uploading N file(s) → ws-chat…
✓ Provisioned ws-chat in compose mode

Deploy mode: compose

  Service    Subdomain                          Port   SSL
  web        web.ws-chat.your_domain.com        8100   ✓
  websocket  websocket.ws-chat.your_domain.com  8101   ✓

─── deploy output (docker compose up) ───
... docker compose build + up output ...
✓ Project deployed and running
```

> The `Port` values are loopback ports freeholdy allocates automatically from its
> configured range — yours may differ. Public traffic always goes through nginx.
>
> If SSL shows `pending` instead of `✓`, run `fhcli ssl ws-chat` once the two
> subdomains resolve to your VPS. SSL issuance needs the DNS records to already point
> at the host.

Container names: `freeholdy_ws-chat_web` and `freeholdy_ws-chat_websocket`, each bound
to `127.0.0.1:<local-port>`. **Re-run `fhcli deploy ws-chat ./` to redeploy** after
changing any file.

---

## Step 2 — Verify it's running

**Check status:**
```bash
fhcli projects
```

`ws-chat` should list both services, their subdomains, and local ports.

**Check directly on the VPS** (bypasses nginx/SSL — use the port shown by
`fhcli projects`):
```bash
curl http://localhost:8100          # web service returns the HTML
curl http://localhost:8101          # websocket service returns "ws-chat backend ok"
```

**Check via HTTPS** (the real test) — open `https://web.ws-chat.your_domain.com` in a
browser, pick a username, and start typing. Open a second tab to see messages relay in
realtime through the WebSocket backend.

---

## Troubleshooting

**A service shows `exited`**
→ Inspect it (per-service exec for compose projects):
```bash
fhcli exec ws-chat --service websocket "ls -la"
docker logs freeholdy_ws-chat_websocket
```

**The page loads but messages don't send**
→ The browser can't reach the WebSocket subdomain. Confirm
`websocket.ws-chat.your_domain.com` resolves and has a cert:
```bash
fhcli ssl ws-chat
```

**`502 Bad Gateway`**
→ A container isn't running. Check status and redeploy:
```bash
fhcli projects
fhcli deploy ws-chat ./
```

**See container logs on the VPS directly:**
```bash
docker logs -f freeholdy_ws-chat_web
docker logs -f freeholdy_ws-chat_websocket
```

---

## Lifecycle commands

```bash
fhcli stop   ws-chat      # docker compose down the stack
fhcli deploy ws-chat ./   # redeploy (rebuild + up) after changing any file
```

There is no separate create or build/up step — `fhcli deploy` creates (if new),
provisions, builds, and runs in one command, and re-running it is how you redeploy after
a change to the frontend, the backend, or the compose file.
