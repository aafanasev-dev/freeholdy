# AmneziaWG VPN

A personal VPN server running **AmneziaWG** — WireGuard with traffic
obfuscation (junk packets, randomized handshake headers) that makes the tunnel
look like noise to deep-packet inspection. Where stock WireGuard is detected
and throttled or blocked, AmneziaWG keeps working.

**Clients:** use the [AmneziaVPN apps](https://amnezia.org) (Windows, macOS,
Linux, iOS, Android) or `amneziawg-go` tooling. **Stock WireGuard clients can
NOT connect** — the obfuscation params are deliberate protocol changes.

## What you get

- One container publishing a single **UDP** port (you pick it at install,
  default 51820). No subdomain, no SSL, nginx untouched — this is a raw UDP
  endpoint, not a website.
- A first client config + QR code, saved into the project directory.
- Per-install randomized obfuscation parameters, so your server doesn't share
  a traffic fingerprint with other deployments.

## Firewall: UDP port (required)

Your cloud provider's security group **must allow the chosen UDP port in** —
this is the step that actually blocks most setups. Docker-published ports
bypass ufw on stock installs, but if you filter the DOCKER-USER chain add:
`ufw allow <port>/udp`.

## Getting client configs

After install, in the project directory (`PROJECTS_DIR/<project>/`):

- `clients/<name>.vpn.txt` — Amnezia `vpn://` code; paste into the app's
  "Add by code/text". This is what the apps import.
- `clients/<name>.qr.txt` — `cat` it in a terminal, scan with the mobile app
  (it encodes the `vpn://` code above)
- `clients/<name>.conf` — the raw AmneziaWG config (for `amneziawg-go` tooling)
- `amneziavpn-info` — endpoint, file locations, reminders

The AmneziaVPN apps import the `vpn://` code (QR or pasted text), **not** the raw
`.conf` — scanning a QR of the plain `.conf` will fail.

Fetch them over SFTP, or from a shell on the host:
`./add-client.sh --show <name>` prints the config and QR.

## Adding and removing clients

On the host, in the project directory (needs docker access):

```
./add-client.sh laptop          # add a client, print conf + QR
./add-client.sh --show laptop   # print an existing client's conf + QR
./add-client.sh --remove laptop # revoke a client
```

Without host shell access, use the freeholdy exec bridge:
`fhcli exec <project> --service awg`, then run `awguser` (numbered menu) and
`awgstart` (apply). Applying the config briefly drops live tunnels.

## Security notes

- **Client `.conf` files ARE credentials** — anyone holding one gets full VPN
  access and egresses with your server's IP. They are saved mode 600 in a
  mode 700 directory; treat copies accordingly.
- Server keys live in `awg-data/` inside the project directory.
- A public UDP VPN endpoint is a wider attack surface than an HTTP service
  behind nginx — install this deliberately, remove it when unused.

## Data & teardown

Everything lives in the project directory. **Deleting the project deletes the
server keys — every distributed client config stops working permanently.**
A re-install generates fresh keys and params; old client configs will not
reconnect. After a delete, verify `awg-data/` is gone from the projects
directory (leftovers would be silently skipped if file ownership was changed
outside the plugin's control).
