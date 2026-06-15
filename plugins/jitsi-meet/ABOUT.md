# Jitsi Meet

Self-hosted video conferencing at **`https://meet.<your-domain>`** — the official
[docker-jitsi-meet](https://github.com/jitsi/docker-jitsi-meet) stack (pinned to a
stable release), wired for freeholdy's reverse proxy.

## What you get

- Four containers: **web** (the UI + signaling proxy), **prosody** (XMPP),
  **jicofo** (conference focus), **jvb** (the videobridge that mixes media).
- One subdomain with automatic SSL. All signaling (BOSH/WebSocket) rides through
  the same `meet.<domain>` origin — no extra subdomains.
- **Authenticated hosting** (Jitsi "secure domain"): only the moderator account
  chosen during install can start a meeting ("I am the host" → log in). Guests
  need no account but wait until a moderator has joined. This stops strangers
  from using your server for their calls.
- Credentials are saved to `jitsi-credentials` in the project directory.

## Firewall: UDP 10000 (required)

WebRTC media is UDP and **cannot go through nginx** — the videobridge binds
UDP 10000 directly on the host. You must allow it in:

- **Your cloud provider's security group / firewall** (AWS, Hetzner, DO, …) —
  this is the one that actually blocks it. Allow inbound **UDP 10000**.
- **ufw**: Docker-published ports bypass ufw on a stock install, so usually no
  rule is needed — but if you filter the `DOCKER-USER` chain, add
  `ufw allow 10000/udp`.

## Test with 3+ participants

Two-person calls use direct peer-to-peer and **never touch the videobridge** —
so a 2-person test can work perfectly while UDP 10000 or the advertised IP is
broken. The real test is the **third participant**: if everyone still has audio
and video with 3+ people, the media path is good. If the 3rd participant gets
no audio/video, check the firewall and `JVB_ADVERTISE_IPS` in the project's
`.env` (it must be the server's public IP).

## Limits

- **One instance per host.** The videobridge binds the fixed host port
  UDP 10000; a second install fails at startup with "port already in use".
- **CPU-bound capacity.** The videobridge needs roughly 4+ cores for 10+
  simultaneous participants. On a small VPS expect a practical cap well below
  that; joiners from the 10th on start with video muted (`START_VIDEO_MUTED=10`)
  to soften the load.

## Data & teardown

All state (Jitsi config under `jitsi-cfg/`, the credentials file) lives inside
the project directory. Deleting the project removes the containers, images, the
internal network, the nginx config, **and all of that data**.
