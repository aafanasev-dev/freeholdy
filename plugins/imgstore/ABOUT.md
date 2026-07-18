# imgstore

A tiny, token-gated online image store. Upload images through a web page and share
them with **public, unguessable links**.

## Two subdomains

| Service   | URL                                | What it is                                   |
|-----------|------------------------------------|----------------------------------------------|
| `web`     | `web.<project>.<domain>`           | The upload / manage page (static, nginx)     |
| `storage` | `storage.<project>.<domain>`       | The API + public image serving (Node)        |

Uploaded images are served at:

```
https://storage.<project>.<domain>/images/<sha256>.<ext>
```

By default the filename is the SHA-256 of the image bytes (so re-uploading the same
file is a no-op), and `<ext>` is one of `jpeg`, `png`, `gif`, `webp`. These links need
**no token** — the long random path is the capability, so they're safe to paste
anywhere.

## Naming

Instead of the sha256, you can give an image a **custom name** that appears in its URL:

- **On upload** — type a name in the box next to the drop zone (leave it blank to keep
  the auto sha256 name). The image is then served at `/images/<name>.<ext>`.
- **After upload** — click **Rename** on any image card to change its name.

Names may contain letters, digits, `.`, `_`, and `-` (they must start with a letter or
digit). The extension is fixed by the image's content type and can't be changed by
renaming. Two images can't share a name — a clash is rejected. A **custom-named** image
is stored under that name (so re-uploading identical bytes under a *new* name keeps both
copies; the sha256 dedup only applies to blank-name uploads). Note that a **friendly
name is more guessable** than a sha256, so it's a weaker capability. Renaming or deleting
an image invalidates its old URL (a subsequent upload could even reuse a freed name for
different bytes), so treat a changed URL as a new link.

## Access tokens

The web page asks for a **token** before it will upload, list, or delete. Mint one on
the server by running the bundled script inside the storage container:

```bash
fhcli exec <project> --service storage /app/gen-token.sh
```

It prints a fresh token **once** and stores only its SHA-256 hash in the data volume
(`/data/tokens.txt`). You can mint as many as you like — every one stays valid until
you remove its line. A lost token can't be recovered; just mint another. Revoke a
token by deleting its hash line:

```bash
fhcli exec <project> --service storage 'cat /data/tokens.txt'   # inspect
```

Managing images (upload / list / delete) requires a token; **viewing** an image via
its link does not.

## Data & persistence

Images and `tokens.txt` live in the named docker volume **`<project>_data`** (mounted
at `/data`). Like other freeholdy compose volumes, it **survives redeploys and even
project deletion** — remove the volume by hand (`docker volume rm <project>_data`) if
you really want the images gone.

## Limits

Uploads are capped at **50 MB** (enforced both in nginx via `nginx-extra.conf` and in
the storage service). Only `jpeg`, `png`, `gif`, and `webp` are accepted.
