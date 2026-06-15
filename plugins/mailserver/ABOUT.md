# Mail server

A complete e-mail server for your domain, powered by
[docker-mailserver](https://docker-mailserver.github.io/docker-mailserver/) —
Postfix (SMTP) + Dovecot (IMAP) with TLS from your existing Let's Encrypt
certificate, DKIM signing, and optional spam/antivirus filtering.

The install is **interactive**: you choose the addons (none by default) and the
first mailbox; a random password is generated if you leave it blank.

## Ports

| Port | Protocol | Purpose |
|------|----------|---------|
| 25   | SMTP     | Server-to-server mail delivery |
| 465  | SMTPS    | Mail submission (implicit TLS) |
| 587  | Submission | Mail submission (STARTTLS) |
| 993  | IMAPS    | Reading mail (IMAP over TLS) |

These bind directly on the host — mail protocols are not HTTP and bypass nginx.
`https://mail.<domain>` intentionally serves no page; that vhost exists only so
the TLS certificate for `mail.<domain>` is issued and renewed.

## After install: DNS records

The install prints the exact records; you must add them at your DNS provider:

- **MX** — `<domain> → mail.<domain>` so other servers know where to deliver.
- **SPF** — TXT `"v=spf1 mx ~all"` so your outgoing mail isn't rejected.
- **DKIM** — TXT record printed at the end of the install (signs outgoing mail).
- **DMARC** — TXT `"v=DMARC1; p=none; rua=mailto:<your mailbox>"`.
- **PTR** — set the VPS's reverse DNS to `mail.<domain>` in your hosting panel.

Many VPS providers **block outbound port 25** by default — ask support to
unblock it or outgoing mail will silently fail.

## Credentials & accounts

The first mailbox's credentials are saved on the host in
`projects/<project>/mailserver-credentials` (mode 600). Add more mailboxes any time:

```bash
docker exec -it freeholdy_<project>_mailserver setup email add user@<domain>
docker exec -it freeholdy_<project>_mailserver setup alias add postmaster@<domain> user@<domain>
```

Use any mail client with IMAP `mail.<domain>:993` (SSL) and SMTP
`mail.<domain>:587` (STARTTLS); username is the full e-mail address.

## Data

Mailboxes, server state, and DKIM keys live under
`projects/<project>/docker-data/dms/` on the host. **Deleting the project
deletes all stored mail and keys with it** — back up that directory first if
you need to keep anything.
