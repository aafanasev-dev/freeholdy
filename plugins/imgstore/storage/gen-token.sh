#!/bin/sh
# gen-token.sh — mint an imgstore access token.
#
# Generates a random token, appends its SHA-256 hash to /data/tokens.txt (which the
# storage server reads to validate incoming tokens), and prints the plaintext token
# ONCE to stdout. Only the hash is stored, so a lost token can't be recovered — just
# mint another (multiple tokens are all valid).
#
# Run it inside the storage container, e.g.:
#   fhcli exec <project> --service storage /app/gen-token.sh
#
# Uses Node (guaranteed in this image) so it needs no openssl/coreutils.
set -eu

node -e '
const c = require("crypto"), fs = require("fs");
const token = c.randomBytes(24).toString("hex");
const hash = c.createHash("sha256").update(token).digest("hex");
fs.mkdirSync("/data", { recursive: true });
fs.appendFileSync("/data/tokens.txt", hash + "\n");
process.stdout.write(token + "\n");
'
