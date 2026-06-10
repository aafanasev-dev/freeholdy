# Personal File Server (SFTPGo)

Your own private file server built on [SFTPGo](https://github.com/drakkan/sftpgo) — upload,
download, and manage files over **SFTP**, **WebDAV**, and a browser **WebClient**, all backed
by a folder you choose on the host. Unlike the built-in system file server, this one is yours:
your credentials, your storage folder, and you can run several independent instances.

## What you get

- A browser file manager (WebClient) and admin panel, served over HTTPS at
  `sftpgo.{project}.{your-domain}`.
- Raw **SFTP** access on a public port you pick, so any SFTP client works.
- A single login (the admin account) that also browses and stores files.

## How it works

- **Deploy mode:** `compose` (single SFTPGo container).
- **Interactive install:** during install you are asked for —
  1. an **admin username** (default `admin`),
  2. an **admin password** (hidden; leave blank to auto-generate one),
  3. a **folder to store files** (an absolute host path, created if missing), and
  4. a **public SFTP port** (default `2022`).
  These are written to the project `.env`; the same username/password becomes both the
  web-admin login and a file user whose home is your chosen folder (mounted at `/srv/data`).
- **Subdomain:** the WebClient/admin/WebDAV are published at `sftpgo.{project}.{your-domain}`,
  so each instance gets its own address.
- **Storage:** files live in the host folder you chose; SFTPGo's own database is kept in a
  private per-instance volume.
- After install, a credentials summary is saved to `CREDENTIALS.txt` in the project directory.

## Good for

- A personal cloud drive you fully control.
- Giving someone SFTP/WebDAV access to a specific folder without exposing anything else.
- Running multiple isolated file servers (different folders, different SFTP ports) side by side.
