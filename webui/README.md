# freeholdy — Web UI

React-based control panel for freeholdy. Covers all `fhcli` CLI commands except SFTP upload.

## Quick start

```bash
cd webui
npm install
npm run dev        # http://localhost:5173
```

## Build for production

```bash
npm run build      # outputs to dist/
npm run preview    # preview the build locally
```

## Deploy (Docker + nginx)

The app runs as a Docker container (builds the bundle and serves it with `vite preview`),
fronted by the host's nginx for TLS at `https://your_domain.com`.

The API URL is baked in at build time (`BASE` in `src/App.jsx` → `https://api.your_domain.com`),
so rebuild the image after changing it.

**1. Build and run the container** (bound to loopback, like all freeholdy containers):

```bash
docker build -t freeholdy-webui .
docker run -d --name freeholdy_webui --restart unless-stopped \
    -p 127.0.0.1:14173:14173 freeholdy-webui
```

**2. Install the nginx config and issue the cert:**

```bash
sudo cp nginx-webui.conf /etc/nginx/sites-available/freeholdy_webui.conf
sudo ln -s /etc/nginx/sites-available/freeholdy_webui.conf /etc/nginx/sites-enabled/
sudo certbot certonly --nginx -d your_domain.com   # the HTTP block must be live first
sudo nginx -t && sudo nginx -s reload
```

`nginx-webui.conf` redirects `:80` → `:443` and proxies `https://your_domain.com` to the
container on `127.0.0.1:14173`. Add `your_domain.com` to the `DOMAINS` array in
`scripts/cert-manager.sh` so the cert is included in the nightly renewal cron job.

The container serves a single-page app; `vite preview` allows the `your_domain.com` host via
`allowedHosts` in `vite.config.js`. To add another public hostname, extend that list and rebuild.

### CORS

The UI is served from `your_domain.com` but calls the API at `api.your_domain.com` (cross-origin).
The API allows the UI origin via `CORS_ORIGINS` in `app/config.py`. If you serve the UI from a
different hostname, add it there (or override `CORS_ORIGINS` in the server's `.env`) and restart
the API.

### Local-only static deploy (alternative)

```bash
npm run build
cp -r dist/ /var/www/freeholdy-ui/        # then point an nginx `root` at it with an index.html fallback
```

## Auth

On first load you'll be prompted for an API token. Generate one on the server:

```bash
python scripts/generate_token.py generate --name web_ui
```

The token is stored in `localStorage` and persists across sessions.
