# SnappyMail webmail

A fast, lightweight webmail client ([SnappyMail](https://snappymail.eu/))
served at `mailui.<domain>` — the web UI companion to the **mailserver**
plugin, which has no interface of its own.

## Logging in

Use a full e-mail address and its mailbox password. Accounts are managed by
the mailserver plugin — the first one is in the mail project's
`mailserver-credentials` file, and more can be added with:

```bash
docker exec -it freeholdy_<mail-project>_mailserver setup email add user@<domain>
```

The install pre-configures your domain to reach the mail server at
`mail.<domain>` (IMAP 993 / SMTP 465 over SSL), so no setup is needed before
the first login.

## Admin panel

`https://mailui.<domain>/?admin` — the username and password are chosen during
the interactive install (defaults: user `admin`, auto-generated password) and
saved to `projects/<project>/snappymail-credentials` (mode 600) on the host.
Use it to tweak the UI, add more mail domains, or enable plugins.

## Data

Settings, the admin password, and per-user data live in
`projects/<project>/docker-data/snappymail/` on the host — deleting the
project removes them too. If an earlier install attempt left data behind,
the installer asks whether to **keep** the old config or **recreate** it
from scratch (new admin password, fresh domain config).
