FROM node:20-alpine
WORKDIR /app

# Install deps first for layer caching
COPY package.json package-lock.json* ./
RUN npm install

# NOTE: at provision time the webui plugin's install.sh copies webui/ over this build
# context, so webui/Dockerfile is what actually builds — keep the two in sync.
COPY . .

# Build the static bundle → /app/dist. The API URL is baked in at build time, set
# either way:
#   1. --build-arg VITE_API_URL=https://api.example.com   (the standalone curl installer)
#   2. a .env file in the build context with VITE_API_URL=…  (the webui plugin writes one)
# A build-arg, when given, wins and is written into .env. We deliberately do NOT use
# `ENV VITE_API_URL=$VITE_API_URL`: an empty ENV would shadow the .env file (Vite lets
# real env vars override .env), silently baking the App.jsx fallback instead.
ARG VITE_API_URL=""
# A build-arg (standalone installer) wins → write it into .env. Then `unset` it:
# Docker exposes a declared ARG as an env var to RUN, and an empty VITE_API_URL would
# otherwise shadow the .env file (Vite lets real env vars override .env), silently
# baking the App.jsx fallback. Unsetting makes .env the single source of truth.
RUN if [ -n "$VITE_API_URL" ]; then echo "VITE_API_URL=$VITE_API_URL" > .env; fi; \
    unset VITE_API_URL; \
    npm run build

# Serve the built bundle. vite preview serves dist/ as static files.
EXPOSE 14173
CMD ["npm", "run", "preview", "--", "--host", "0.0.0.0", "--port", "14173"]
