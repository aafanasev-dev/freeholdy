# WebSocket Chat

A realtime chat room built as a two-container **compose** stack: a **React** frontend and
a **Node.js** WebSocket backend. There is no account system — pick a username and start
talking. It is the reference example for multi-service projects and live WebSocket proxying.

## What you get

- A working realtime chat you can open in two browser tabs and watch messages sync.
- A clean **frontend + backend** split managed as one `docker compose` stack.
- Automatic WebSocket-aware nginx config (upgrade headers) and HTTPS per service.

## How it works

- **Deploy mode:** `compose` (multiple containers).
- **Services:** a `frontend` (React UI) and a `backend` (Node.js WebSocket server).
- **Subdomains:** each service is published at `{service}.ws-chat.{your-domain}`.
- WebSocket usage is detected automatically, so the proxy is configured for live connections.

## Good for

- Seeing how freeholdy wires up a real multi-service application.
- A template for any frontend/backend pair that needs WebSockets.
- Demonstrating realtime features without standing up auth or a database.
