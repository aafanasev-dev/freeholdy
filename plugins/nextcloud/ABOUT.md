# Nextcloud

Self-hosted file sync & share at **`https://nextcloud.<your-domain>`** — the
official [nextcloud](https://hub.docker.com/_/nextcloud) image (apache variant,
pinned to a major release), wired for freeholdy's reverse proxy.

## What you get

- Four containers: **app** (the web app), **db** (PostgreSQL 16), **cache**
  (Redis — required for file locking), **cron** (Nextcloud background jobs).
- One subdomain with automatic SSL, proxy-aware out of the box (no redirect
  loops, no "untrusted domain" wall, correct https:// links).
- The admin account is chosen during the interactive install; credentials are
  saved to `nextcloud-credentials` in the project directory.
- Desktop and mobile clients just point at the URL; CalDAV/CardDAV discovery
  (`/.well-known/carddav`, `/.well-known/caldav`) works out of the box.

## Uploads

Files up to **10 GB** per upload — both nginx (`client_max_body_size`) and PHP
(`PHP_UPLOAD_LIMIT`) are raised, and large uploads stream through the proxy
unbuffered.

## First boot

The first start copies the Nextcloud source tree into its volume and runs the
installer — expect **several minutes** before the site answers. The install log
streams live; the post-install step waits for Nextcloud to report healthy
before declaring success.

## Data & teardown (read this)

Unlike most plugins, your files and database live in **named docker volumes**
(`<project>_app`, `<project>_db`) — and freeholdy's project deletion does *not*
remove volumes. That means:

- **Deleting the project keeps your data.** Containers, images, and the nginx
  config go away; the volumes stay.
- **Re-adding a project with the same name reattaches the old data.** The admin
  account you type at the new install prompt is then **ignored** — Nextcloud
  only installs into an empty volume, so the old admin account (and old
  password) wins.
- To wipe everything for real:
  `docker volume rm <project>_app <project>_db`

## Limits

- **One instance per domain.** The `nextcloud.` subdomain is fixed by the
  plugin; a second install would collide on the same hostname.
- PHP memory is capped at 1G — fine for a small user count on a VPS.
