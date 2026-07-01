---
description: Deploy a local folder to the freeholdy server — upload files, provision nginx/SSL, build and run Docker or docker-compose
argument-hint: <project-name> [folder-path]
---

Upload a local folder to the freeholdy API, auto-detect a `Dockerfile` or `docker-compose.yml`, provision nginx + SSL, then build and start the container (or compose stack). If the project doesn't exist yet it is created automatically.

## Credentials

Read freeholdy API credentials from memory (`freeholdy-api-credentials.md`):
- `TOKEN` — bearer token
- `BASE_DOMAIN` — e.g. `cloudopen.space`
- `BASE_URL` — `https://api.{BASE_DOMAIN}`

If the memory file is absent or credentials are missing, ask the user for `TOKEN` and `BASE_DOMAIN`, save them to memory as a reference entry, then continue.

## Arguments

- `$1` — **project name** (required). DNS slug: lowercase letters, digits, hyphens; must start and end with alphanumeric.
- `$2` — **folder path** (optional, default `.`). Local directory to deploy.

## Steps

### 1 — Validate inputs

If `$1` is empty, print usage and stop:
```
Usage: /fhdeploy <project-name> [folder-path]
```

Set `PROJECT = $1`, `FOLDER = $2` (default `.`).  
Resolve `FOLDER` to an absolute path and verify it exists. Abort with a clear error if it does not.

### 2 — Ensure project exists

Run this Python snippet (substituting real values for `TOKEN`, `BASE_URL`, `PROJECT`):

```python
import requests, sys

r = requests.get(f"{BASE_URL}/projects/{PROJECT}",
                 headers={"Authorization": f"Bearer {TOKEN}"}, timeout=15)
if r.status_code == 404:
    r2 = requests.post(f"{BASE_URL}/projects",
                       headers={"Authorization": f"Bearer {TOKEN}"},
                       json={"name": PROJECT}, timeout=15)
    if r2.status_code not in (200, 201):
        sys.exit(f"Failed to create project: {r2.status_code} — {r2.text}")
    print(f"Created project '{PROJECT}'")
elif r.status_code == 200:
    print(f"Project '{PROJECT}' already exists")
else:
    sys.exit(f"Error checking project: {r.status_code} — {r.text}")
```

### 3 — Upload files

Upload every file in `FOLDER` recursively, preserving relative paths as multipart filenames:

```python
import os, sys, requests
from pathlib import Path

folder = Path("FOLDER").resolve()
files_list, handles = [], []
try:
    for p in sorted(folder.rglob("*")):
        if p.is_file():
            rel = str(p.relative_to(folder)).replace(os.sep, "/")
            fh = open(p, "rb")
            handles.append(fh)
            files_list.append(("files", (rel, fh, "application/octet-stream")))

    if not files_list:
        sys.exit("No files found in folder")

    print(f"Uploading {len(files_list)} file(s) → project '{PROJECT}'…")
    r = requests.post(
        f"{BASE_URL}/projects/{PROJECT}/upload",
        headers={"Authorization": f"Bearer {TOKEN}"},
        files=files_list,
        timeout=120,
    )
    r.raise_for_status()
    import json; print(json.dumps(r.json(), indent=2))
finally:
    for fh in handles:
        fh.close()
```

Capture the response as `upload_data`.

If `upload_data["provisioned"] == False` (no `Dockerfile` / `docker-compose.yml` found):
- Print: "Files uploaded (no manifest detected — nothing to build)."
- Stop here. Success.

### 4 — Stream the deploy

A provisioned upload **already launched build + run** (dockerfile: build + `docker run`;
compose: `docker compose up -d`). There is nothing extra to POST — just stream the running
job's log. The status endpoint depends on `upload_data["deploy_mode"]`:

- `"dockerfile"` → `GET /projects/{PROJECT}/status`
- `"compose"`    → `GET /projects/{PROJECT}/compose/status`

Poll it every second until the job leaves `running`:
```python
import time, requests, sys

deploy_mode = upload_data["deploy_mode"]
status_path = (f"{BASE_URL}/projects/{PROJECT}/compose/status"
               if deploy_mode == "compose"
               else f"{BASE_URL}/projects/{PROJECT}/status")

printed = 0
print("─── deploy output (build → run) ─────────────────")
while True:
    data = requests.get(status_path,
                        headers={"Authorization": f"Bearer {TOKEN}"}, timeout=10).json()
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

(To redeploy after a code change, re-run the upload — it rebuilds and restarts.)

### 5 — Show summary

Fetch the project: `GET {BASE_URL}/projects/{PROJECT}`

Print a summary table with:
- Project name, deploy mode
- For each endpoint (dockerfile → `container`; compose → `services[]`): subdomain, SSL status (`✓`/`✗`), container status
