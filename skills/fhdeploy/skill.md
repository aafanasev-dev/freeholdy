---
description: Deploy a local folder or a git repo to the freeholdy server — upload/clone, provision nginx/SSL, build and run Docker or docker-compose
argument-hint: <project-name> [folder-path-or-git-url]
---

Deploy a project to the freeholdy API from a **local folder** or a **git URL**. freeholdy auto-detects a `Dockerfile` or `docker-compose.yml` (compose wins if both are present), provisions nginx + SSL, then builds and starts the container (or compose stack) as a new blue/green version. There is **no separate create step** — a deploy auto-creates the project if it doesn't exist, and re-deploying an existing name safely redeploys it (the old version keeps serving until the new one is verified running).

## Credentials

Read freeholdy API credentials from memory (`freeholdy-api-credentials.md`):
- `TOKEN` — bearer token
- `BASE_DOMAIN` — e.g. `cloudopen.space`
- `BASE_URL` — `https://api.{BASE_DOMAIN}`

If the memory file is absent or credentials are missing, ask the user for `TOKEN` and `BASE_DOMAIN`, save them to memory as a reference entry, then continue.

## Arguments

- `$1` — **project name** (required). DNS slug: lowercase letters, digits, hyphens; must start and end with alphanumeric.
- `$2` — **source** (optional, default `.`). Either a local directory to deploy, or a git clone URL (`https://…`, `git@host:owner/repo.git`, or `ssh://…`; an optional branch can be given by the user in plain English).

## Steps

### 1 — Validate inputs

If `$1` is empty, print usage and stop:
```
Usage: /fhdeploy <project-name> [folder-path-or-git-url]
```

Set `PROJECT = $1`, `SOURCE = $2` (default `.`).

Decide the transport:
- `SOURCE` starts with `https://`, `http://`, `ssh://`, or matches `git@host:path` → **git deploy** (step 2b).
- Otherwise → **folder deploy** (step 2a). Resolve `SOURCE` to an absolute path and verify it exists and is a directory; abort with a clear error if not.

### 2a — Deploy a local folder (chunked upload)

The API sits behind nginx with an ~8 MB request-body limit, so folders are shipped as a zip in 1 MiB chunks (this is the same flow `fhcli` and the web UI use). The `complete` call creates the project row if needed, extracts the files, auto-detects the manifest, provisions nginx + SSL, and **auto-launches build + run**.

Run this Python snippet (substituting real values for `TOKEN`, `BASE_URL`, `PROJECT`, `SOURCE`):

```python
import io, os, secrets, sys, zipfile, requests
from pathlib import Path

H = {"Authorization": f"Bearer {TOKEN}"}
folder = Path(SOURCE).resolve()

# Zip the folder tree in memory (relative paths, forward slashes).
buf = io.BytesIO()
count = 0
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
    for p in sorted(folder.rglob("*")):
        if p.is_file():
            zf.write(p, str(p.relative_to(folder)).replace(os.sep, "/"))
            count += 1
if count == 0:
    sys.exit("No files found in folder")
data = buf.getvalue()

# Stream the zip in 1 MiB pieces, then finalize.
upload_id = secrets.token_hex(16)   # must be lowercase hex, ≥8 chars
CHUNK = 1024 * 1024
print(f"Uploading {count} file(s) ({len(data)} bytes zipped) → project '{PROJECT}'…")
for offset in range(0, len(data), CHUNK):
    r = requests.post(
        f"{BASE_URL}/projects/{PROJECT}/upload/chunk",
        params={"upload_id": upload_id, "offset": offset},
        headers={**H, "Content-Type": "application/octet-stream"},
        data=data[offset:offset + CHUNK], timeout=60)
    r.raise_for_status()

r = requests.post(f"{BASE_URL}/projects/{PROJECT}/upload/complete",
                  headers=H, json={"upload_id": upload_id, "total_size": len(data)},
                  timeout=300)
r.raise_for_status()
deploy_data = r.json()
```

Capture the response as `deploy_data` (an `UploadResponse`).

If `deploy_data["provisioned"] == False` (no `Dockerfile` / `docker-compose.yml` in the root):
- Print: "Files uploaded (no manifest detected — nothing to build)."
- Stop here. Success.

Otherwise read `DEPLOY_MODE = deploy_data["deploy_mode"]` (`"dockerfile"` or `"compose"`) and continue to step 3.

### 2b — Deploy from a git URL

`POST /git/add` clones the repo (optionally a branch), runs the same auto-provisioning as an upload, and auto-launches build + run. Idempotent: an existing name is re-cloned and redeployed. A repo with no manifest in its root is rejected with 400.

```python
import requests, sys

H = {"Authorization": f"Bearer {TOKEN}"}
body = {"name": PROJECT, "git_url": SOURCE}
# If the user asked for a branch: body["branch"] = BRANCH
r = requests.post(f"{BASE_URL}/git/add", headers=H, json=body, timeout=300)
if r.status_code not in (200, 201):
    detail = r.json().get("detail", r.text)
    # SSH-auth failures point at GET /git/key — surface those instructions verbatim.
    sys.exit(f"Git deploy failed: {r.status_code} — {detail}")
deploy_data = r.json()
DEPLOY_MODE = deploy_data["project"]["deploy_mode"]
```

If the clone fails with an SSH auth error, the 400 detail tells the user to fetch the server's deploy key from `GET {BASE_URL}/git/key` and add it to the repository's Deploy keys on GitHub — relay that to the user, then retry once they confirm.

### 3 — Stream the deploy

The deploy (build → run → nginx switchover) **is already running** — nothing extra to POST. Poll the job status until it leaves `running`. The endpoint depends on `DEPLOY_MODE`:

- `"dockerfile"` → `GET {BASE_URL}/projects/{PROJECT}/status`
- `"compose"`    → `GET {BASE_URL}/projects/{PROJECT}/compose/status`

```python
import time, requests, sys

H = {"Authorization": f"Bearer {TOKEN}"}
status_path = (f"{BASE_URL}/projects/{PROJECT}/compose/status"
               if DEPLOY_MODE == "compose"
               else f"{BASE_URL}/projects/{PROJECT}/status")

printed = 0
print("─── deploy output (build → run) ─────────────────")
while True:
    data = requests.get(status_path, headers=H, timeout=10).json()
    status = data.get("status", "no_job")
    logs   = data.get("logs", "")
    new    = logs[printed:]
    if new:
        print(new, end="", flush=True)
        printed = len(logs)
    if status != "running":
        if status != "done":
            sys.exit(f"\nDeploy failed — status={status}, exit_code={data.get('exit_code')}")
        break
    time.sleep(1)
print("\n✓ Deploy succeeded")
```

(Deploys are blue/green: a failed build never takes a previously running site down — the old version keeps serving. To redeploy after a code change, just re-run this skill.)

### 4 — Show summary

There is no single-project GET endpoint; fetch the project list and pick this project (it carries live container status):

```python
import requests, sys

H = {"Authorization": f"Bearer {TOKEN}"}
projects = requests.get(f"{BASE_URL}/projects/", headers=H, timeout=15).json()
proj = next((p for p in projects if p["name"] == PROJECT), None)
if proj is None:
    sys.exit(f"Project '{PROJECT}' not found after deploy")
```

Print a summary table with:
- Project name, deploy mode
- For each endpoint — dockerfile → the `container` object; compose → each entry of `services[]` (skip services with no subdomain; those are internal): subdomain URL (`https://{subdomain}`), SSL status (`✓`/`✗` from `ssl_enabled`), container status

## Notes for the agent

- A dockerfile project **must `EXPOSE` a port** — the upload is rejected with 400 otherwise; tell the user to add `EXPOSE` to the Dockerfile.
- `docker-compose.yml` (or `compose.yaml`/`compose.yml`) wins over a `Dockerfile`. Switching an already-provisioned project to the other mode is rejected (400) — the project must be removed and redeployed.
- Every successful redeploy creates a new version; the previous one is kept for rollback (`POST {BASE_URL}/projects/{PROJECT}/rollback` with `{"version": N}`; list via `GET {BASE_URL}/projects/{PROJECT}/versions`) — mention this if a deploy leaves the user unhappy with the new release.
