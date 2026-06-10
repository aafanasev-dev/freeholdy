#!/usr/bin/env bash
# ws-chat install — interactive (plugin.json "interactive": true), so the pre phase
# runs attached to a WebSocket session and can prompt the user. Standard compose
# two-phase contract: "pre" before compose up, "post" after (unused here).
#
# pre: ask for a chat room name and persist it as CHAT_NAME in the project .env,
#      where docker-compose.yml picks it up as the frontend's VITE_CHAT_NAME build arg.
set -euo pipefail

PHASE="${1:-pre}"

if [[ "$PHASE" == "pre" ]]; then
  read -r -p "Chat name [ws-chat]: " CHAT_NAME
  CHAT_NAME="${CHAT_NAME:-ws-chat}"

  cd "$PROJECT_DIR"
  # Idempotent across re-runs: drop any previous CHAT_NAME line before appending.
  grep -v '^CHAT_NAME=' .env > .env.tmp || true
  mv .env.tmp .env
  echo "CHAT_NAME=${CHAT_NAME}" >> .env

  echo "Chat name set to: ${CHAT_NAME}"
fi
