# Outline Server (Shadowbox)

[Outline](https://getoutline.org) is Jigsaw's self-hosted VPN built on Shadowsocks.
This plugin runs the **Shadowbox** server and you manage access keys from the free
**Outline Manager** desktop app (macOS/Windows/Linux). Each key is a shareable link;
users connect with the **Outline client** app.

## How it runs on freeholdy

Unlike the web plugins, Outline is **not** proxied by nginx and gets **no subdomain
or SSL vhost**. It runs on **host networking** — exactly like Outline's own
`docker run --net host` installer — because it needs:

- a **management API** on a TCP port, served over a *self-signed* certificate that the
  Outline Manager pins by SHA-256 fingerprint (an nginx/Let's Encrypt proxy in front of
  it would break that pinning), and
- a **Shadowsocks access-key port** reachable publicly over **both TCP and UDP** (VPN
  traffic), which freeholdy's normal loopback-only proxy model can't carry.

Because it publishes no proxied TCP port, freeholdy leaves the stack alone: no
`ComposeService` row, no vhost, no certificate. One Outline instance per host (the ports
are real host ports).

## Installing

The install is **interactive**. You'll confirm:

1. **Public host** — the IP or hostname clients reach this VPS at (auto-detected from
   your base domain / public IP; override if needed). It goes into the management API URL.
2. **Management API port** (tcp) — where the Outline Manager connects.
3. **Access-key port** (tcp + udp) — where VPN clients connect. All keys share this one
   port.

At the end the installer prints an **Outline Manager connection string** like:

```
{"apiUrl":"https://<host>:<api-port>/<secret>","certSha256":"<fingerprint>"}
```

Copy it, open the Outline Manager app → **Add server → "I already have a server"**, and
paste it in. It's also saved to `outline-access.txt` in the project directory.

## Firewall — required

Host-bound ports bypass `ufw` on stock Docker installs, but your **cloud provider's
security group** is your responsibility. Open **both**:

- `<api-port>/tcp` — the Outline Manager
- `<keys-port>/tcp` **and** `/udp` — VPN clients

If you filter the `DOCKER-USER` iptables chain, also `ufw allow <api-port>/tcp` and
`ufw allow <keys-port>`.

## State & teardown

All server state — the self-signed cert, the API secret, `shadowbox_server_config.json`,
and every access key — lives under **`persisted-state/`** in the project directory.
`outline-access.txt` and `outline-info` hold the connection details (mode 600, private —
the apiUrl + certSha256 grant full admin).

Deleting the project removes the container **and** all of that state, so **every access
key dies with it**. Back up `persisted-state/` if you need to move the server.
