#!/bin/sh
# =============================================================================
# redeploy.sh
# Trigger a freeholdy redeploy from a CI/CD pipeline.
#
# Copy this file into your repository and run it after a push. It calls
#
#     POST https://api.<FH_BASE_DOMAIN>/projects/<FH_PROJECT_NAME>/redeploy
#
# which tells freeholdy to re-clone the git URL and branch it already recorded
# for that project and roll out a new version. There is nothing to pass it: the
# server owns the repository URL, which is why this call is safe to give to a
# third party holding a *guest* token.
#
# Usage:
#   ./redeploy.sh                 # settings come from the environment
#   ./redeploy.sh path/to/.env    # settings come from that dotenv file instead
#   ./redeploy.sh --dry-run       # print the curl command instead of calling
#
# Options (accepted before or after the file argument):
#   --dry-run     Resolve and check the settings as usual, then print a ready-to-
#                 run curl command on stdout instead of sending the request.
#                 Everything else it has to say goes to stderr, so
#                 `./redeploy.sh --dry-run 2>/dev/null` is just the command, and
#                 `eval "$(./redeploy.sh --dry-run)"` runs it.
#                 The printed command leaves the token as $FH_TOKEN, and omits the
#                 -w flag this script uses internally to capture the status code.
#   --show-token  With --dry-run, put the real token in the printed command rather
#                 than $FH_TOKEN, so it runs as-is. It is then a live credential:
#                 do not paste the output into a ticket, a chat or a CI log.
#
# Settings:
#   FH_TOKEN          freeholdy API token (a guest token scoped to the project
#                     is enough, and is what you should use here)
#   FH_PROJECT_NAME   the project to redeploy
#   FH_BASE_DOMAIN    your freeholdy server's base domain; the API is reached at
#                     https://api.<FH_BASE_DOMAIN>
#   FH_API_URL        optional: full API base URL, overriding the line above.
#                     Set this only if your API is not at api.<domain>; when it
#                     is set, FH_BASE_DOMAIN is not needed.
#
# With a file argument, those settings are read from the file and the values are
# taken from it rather than from the environment. The file is parsed, not
# sourced, so nothing in it is executed; `export KEY=value`, quoted values,
# comments (including after a value) and blank lines are all understood, and a
# repeated key takes its last value -- the same dotenv dialect the freeholdy CLI
# reads its own .env with.
#
# Exit codes:
#   0   the server accepted the redeploy
#   1   a configuration problem (missing file, missing setting)
#   2   the API call failed (the server's error message is printed)
#
# This script returns as soon as the server accepts the request; the build then
# runs in the background. To follow it, poll
# GET /projects/<name>/status until its "status" is no longer "running".
#
# The token is never printed unless you ask for it with --show-token. It does
# still appear if you run this under `sh -x` (or with a CI debug trace turned on),
# like any other shell variable -- mark the variable as masked/protected in your
# CI settings.
#
# Requires: sh and curl. No bash, jq or python needed.
# =============================================================================

set -eu

die() { echo "redeploy: $1" >&2; exit "${2:-1}"; }

usage() {
    echo "usage: redeploy.sh [path/to/.env] [--dry-run [--show-token]]" >&2
}

# ── Arguments ─────────────────────────────────────────────────────────────────
# The dotenv path is the one positional argument; the flags may come before or
# after it. Hand-rolled rather than getopts, which does not do long options in
# POSIX sh.
ENV_FILE=""
DRY_RUN=0
SHOW_TOKEN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)    DRY_RUN=1 ;;
        --show-token) SHOW_TOKEN=1 ;;
        -*)           usage; die "unknown option: $1" ;;
        *)
            [ -z "$ENV_FILE" ] || { usage; die "unexpected argument: $1"; }
            ENV_FILE="$1"
            ;;
    esac
    shift
done

# --show-token only changes what --dry-run prints. Silently ignoring it on a real
# run would leave someone believing they had asked for something.
if [ "$SHOW_TOKEN" = 1 ] && [ "$DRY_RUN" = 0 ]; then
    usage
    die "--show-token only applies together with --dry-run"
fi

# Read one key out of a dotenv file. Never sources it: `. file` would execute
# whatever the file contains, and this is the file most likely to have been
# pasted in from elsewhere. Only the keys this script needs are read, so nothing
# else in the file can reach the environment.
read_dotenv() {
    key="$1"
    file="$2"
    # tail -n1: the last assignment of a key wins, as it would in a shell.
    line=$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=" "$file" 2>/dev/null | tail -n 1 || true)
    [ -n "$line" ] || return 0

    # Strip everything up to and including the first '=', then leading whitespace,
    # then quotes. A quoted value ends at its closing quote and anything after it
    # (a trailing comment) is dropped; an unquoted value keeps a bare '#' but
    # loses a ' #' comment. That is python-dotenv's behaviour, which is what the
    # freeholdy CLI reads its own .env with, so a file that works for one works
    # for the other.
    value=${line#*=}
    value=${value#"${value%%[![:space:]]*}"}       # leading whitespace
    case "$value" in
        \"*) value=${value#\"}; value=${value%%\"*} ;;
        \'*) value=${value#\'}; value=${value%%\'*} ;;
        *)
            value=$(printf '%s' "$value" | sed 's/[[:space:]][[:space:]]*#.*$//')
            value=${value%"${value##*[![:space:]]}"}   # trailing whitespace
            ;;
    esac
    printf '%s' "$value"
}

# ── Settings ──────────────────────────────────────────────────────────────────
if [ -n "$ENV_FILE" ]; then
    [ -f "$ENV_FILE" ] || die "no such file: $ENV_FILE"
    [ -r "$ENV_FILE" ] || die "cannot read: $ENV_FILE"
    FH_TOKEN=$(read_dotenv FH_TOKEN "$ENV_FILE")
    FH_PROJECT_NAME=$(read_dotenv FH_PROJECT_NAME "$ENV_FILE")
    FH_BASE_DOMAIN=$(read_dotenv FH_BASE_DOMAIN "$ENV_FILE")
    FH_API_URL=$(read_dotenv FH_API_URL "$ENV_FILE")
    SOURCE="$ENV_FILE"
else
    FH_TOKEN="${FH_TOKEN:-}"
    FH_PROJECT_NAME="${FH_PROJECT_NAME:-}"
    FH_BASE_DOMAIN="${FH_BASE_DOMAIN:-}"
    FH_API_URL="${FH_API_URL:-}"
    SOURCE="the environment"
fi

# Report everything that is missing at once -- a pipeline that has to re-run
# three times to learn about three unset variables is nobody's idea of a good time.
missing=""
[ -n "$FH_TOKEN" ]        || missing="$missing FH_TOKEN"
[ -n "$FH_PROJECT_NAME" ] || missing="$missing FH_PROJECT_NAME"
[ -n "$FH_BASE_DOMAIN" ] || [ -n "$FH_API_URL" ] || missing="$missing FH_BASE_DOMAIN"
[ -z "$missing" ] || die "missing from ${SOURCE}:${missing}"

API_URL="${FH_API_URL:-https://api.${FH_BASE_DOMAIN}}"
API_URL=${API_URL%/}                               # tolerate a trailing slash
URL="${API_URL}/projects/${FH_PROJECT_NAME}/redeploy"

# ── Dry run ───────────────────────────────────────────────────────────────────
# Everything above has already run, so a broken configuration still fails here the
# same way it would on a real call -- checking that is most of the point. Only the
# request itself is skipped.
#
# The command goes to stdout and nothing else does, so it can be redirected to a
# file or eval'd; the commentary goes to stderr.
if [ "$DRY_RUN" = 1 ]; then
    echo "redeploy: would POST $URL" >&2
    if [ "$SHOW_TOKEN" = 1 ]; then
        # Single quotes: whatever the token contains, the shell running this must
        # not re-expand it.
        auth="-H 'Authorization: Bearer ${FH_TOKEN}'"
        echo "redeploy: the command below contains a live token — treat it as a secret" >&2
    else
        # Double quotes so $FH_TOKEN expands in the shell the user pastes into.
        auth='-H "Authorization: Bearer $FH_TOKEN"'
        echo "redeploy: \$FH_TOKEN is left unexpanded — export it first, or re-run with --show-token" >&2
    fi
    printf 'curl -sS -X POST "%s" \\\n  %s \\\n  -H "Accept: application/json"\n' \
        "$URL" "$auth"
    exit 0
fi

# ── Call ──────────────────────────────────────────────────────────────────────
echo "redeploy: POST $URL"

# Deliberately not `curl -f`: that throws the response body away, and the body is
# where the server explains itself ("this guest token is limited to: ...", "was
# not deployed from git"). The status code is appended on its own last line.
if ! response=$(curl -sS -X POST "$URL" \
        -H "Authorization: Bearer ${FH_TOKEN}" \
        -H "Accept: application/json" \
        -w '\n%{http_code}' 2>&1); then
    # curl itself failed (DNS, refused connection, TLS). Its diagnostic is in the
    # captured output; drop the placeholder status line -w still wrote and fold the
    # rest onto one line so the pipeline log stays readable.
    die "could not reach ${API_URL}: $(printf '%s' "$response" \
        | grep -v '^[0-9][0-9][0-9]$' | tr '\n' ' ' | sed 's/[[:space:]]*$//')" 2
fi

status=$(printf '%s' "$response" | tail -n 1)
body=$(printf '%s' "$response" | sed '$d')

# Pull one string field out of the JSON body, without requiring jq inside the
# runner's image. grep -o emits matches in order so head -n1 is genuinely the
# FIRST one -- a `sed 's/.*"key"...'` would match the last instead, because the
# leading .* is greedy, and this response nests a second "message" under "job".
json_field() {
    printf '%s' "$body" \
        | grep -o "\"$1\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" \
        | head -n 1 \
        | sed 's/^[^:]*:[[:space:]]*"//; s/"$//'
}

# Written as plain `if`s rather than `[ x ] && echo`: under `set -e` a failing
# test at the end of an AND-list can abort the script, and exactly when it does
# differs between dash, bash and ash.
case "$status" in
    2*)
        message=$(json_field message)
        echo "redeploy: ${message:-accepted} (HTTP $status)"
        echo "redeploy: the build runs in the background; poll ${API_URL}/projects/${FH_PROJECT_NAME}/status to follow it"
        ;;
    *)
        detail=$(json_field detail)
        echo "redeploy: failed with HTTP ${status:-000}" >&2
        if [ -n "$detail" ]; then
            echo "redeploy: ${detail}" >&2
        elif [ -n "$body" ]; then
            # Not a freeholdy error shape (a proxy, a wrong host) -- show it raw.
            echo "$body" >&2
        fi
        exit 2
        ;;
esac
