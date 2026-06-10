#!/usr/bin/env bash
#
# test_nginx.sh — end-to-end domain + echo round-trip test.
#
# Proves freeholdy's core promise carries live traffic: an uploaded project is
# served at https://{name}.BASE_DOMAIN through nginx + Let's Encrypt, and the
# domain-change feature repoints it at another domain and re-issues the cert.
#
# It stands up a real echo project via the `fhold` CLI, then sends a random nonce
# to the project's URL and asserts the response body equals what was sent — once
# on the auto subdomain, once after changing the domain.
#
#   create → upload(echo) → build → start → curl https://test.DOMAIN  (echo)
#                         → domain change → curl https://CHANGE_HOST   (echo)
#                         → remove (teardown)
#
# REQUIREMENTS (this is a VPS/integration test, not a hermetic unit test):
#   • the freeholdy server is running and reachable (fhold health)
#   • cli/.env is configured with TOKEN and BASE_DOMAIN
#   • *.BASE_DOMAIN resolves to this host so certbot can issue real certs
#
# Usage:
#   ./tests/test_nginx.sh
#   CHANGE_DOMAIN=app.example.com ./tests/test_nginx.sh   # use a real external domain
#
# Exit code is 0 only when both echo round-trips matched.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FHOLD_PY="$ROOT/cli/fhold.py"
FHOLD_BIN="$ROOT/cli/venv/bin/python"

# ── Output helpers (style shared with test_install.sh) ──────────────────────────
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}✓${NC}  $*"; }
warn() { echo -e "  ${YELLOW}⚠${NC}  $*"; }
info() { echo -e "  ${CYAN}→${NC}  $*"; }
fail() { echo -e "  ${RED}✗${NC}  $*" >&2; }
hr()   { echo -e "${BOLD}── $* ──${NC}"; }

PASS=0; FAIL=0; FAILURES=()
record_pass() { PASS=$((PASS + 1)); }
record_fail() { FAIL=$((FAIL + 1)); FAILURES+=("$1"); }

fhold() { "$FHOLD_BIN" "$FHOLD_PY" "$@"; }

# ── Config + pre-flight ─────────────────────────────────────────────────────────
PROJECT="test"
ENV_FILE="$ROOT/cli/.env"

preflight() {
    hr "pre-flight"
    if [[ ! -x "$FHOLD_BIN" ]]; then
        fail "fhold venv python not found at $FHOLD_BIN — set up cli/ (see cli/README.md)"; exit 2
    fi
    if [[ ! -f "$ENV_FILE" ]]; then
        fail "missing $ENV_FILE — the CLI needs TOKEN + BASE_DOMAIN"; exit 2
    fi
    # Parse BASE_DOMAIN from cli/.env (same approach as migrate.sh).
    BASE_DOMAIN="$(grep -E '^BASE_DOMAIN=' "$ENV_FILE" | tail -n1 | cut -d= -f2- | tr -d '"'"'"' ' || true)"
    if [[ -z "$BASE_DOMAIN" ]]; then
        fail "BASE_DOMAIN not set in $ENV_FILE"; exit 2
    fi
    if ! grep -qE '^TOKEN=.+' "$ENV_FILE"; then
        fail "TOKEN not set in $ENV_FILE — mint one with scripts/generate_token.py"; exit 2
    fi
    ok "cli/.env ok (BASE_DOMAIN=$BASE_DOMAIN)"

    if ! fhold health >/dev/null 2>&1; then
        fail "freeholdy server not reachable (fhold health failed) — is it running?"; exit 2
    fi
    ok "server reachable"

    BASE_HOST="${PROJECT}.${BASE_DOMAIN}"
    CHANGE_HOST="${CHANGE_DOMAIN:-${PROJECT}-alt.${BASE_DOMAIN}}"
    info "base host:   https://$BASE_HOST"
    info "change host: https://$CHANGE_HOST"
}

# ── Teardown (always) ───────────────────────────────────────────────────────────
teardown() {
    hr "teardown"
    if fhold remove "$PROJECT" --yes >/dev/null 2>&1; then
        info "removed project $PROJECT"
    else
        info "nothing to remove (or already gone)"
    fi
}
trap teardown EXIT

# ── Echo assertion ──────────────────────────────────────────────────────────────
# assert_echo <label> <url> : POST a random nonce, expect it echoed back verbatim.
# Retries to absorb container/nginx/cert warmup.
assert_echo() {
    local label="$1" url="$2"
    local nonce="echo-$(date +%s)-$RANDOM"
    local tries=5 got=""
    info "$label: POST nonce to $url"
    for ((i = 1; i <= tries; i++)); do
        if got="$(curl -fsS --max-time 15 --data "$nonce" "$url" 2>/dev/null)"; then
            [[ "$got" == "$nonce" ]] && break
        fi
        [[ $i -lt $tries ]] && sleep 3
    done
    if [[ "$got" == "$nonce" ]]; then
        ok "$label: echo round-trip matched"
        record_pass
    else
        fail "$label: echo mismatch (sent '$nonce', got '${got:-<no response>}')"
        warn "  if https failed: check DNS for $url points here and the cert was issued"
        record_fail "$label: echo round-trip failed"
    fi
}

# A best-effort negative check: a URL should NOT echo (e.g. after a domain change).
refute_echo() {
    local label="$1" url="$2"
    local nonce="x-$RANDOM"
    if [[ "$(curl -fsS --max-time 10 --data "$nonce" "$url" 2>/dev/null || true)" == "$nonce" ]]; then
        warn "$label: $url still echoes after domain change (may be transient)"
    else
        ok "$label: $url no longer serves the project"
    fi
}

# ── Main ────────────────────────────────────────────────────────────────────────
main() {
    echo -e "${BOLD}freeholdy nginx domain + echo round-trip test${NC}"
    preflight

    # Clean slate so a leftover 'test' project can't wedge the run.
    fhold remove "$PROJECT" --yes >/dev/null 2>&1 || true

    hr "provision echo project"
    fhold create "$PROJECT"
    fhold upload "$PROJECT" "$ROOT/tests/fixtures/echo"   # auto nginx + certbot for BASE_HOST
    fhold build "$PROJECT"
    fhold start "$PROJECT"

    hr "echo on auto subdomain"
    assert_echo "auto subdomain" "https://$BASE_HOST"

    hr "domain change feature"
    fhold domain "$PROJECT" "$CHANGE_HOST"               # rewrites nginx + issues new cert
    assert_echo "changed domain" "https://$CHANGE_HOST"
    refute_echo "old subdomain" "https://$BASE_HOST"

    echo
    hr "summary"
    local total=$((PASS + FAIL))
    if [[ "$FAIL" -eq 0 ]]; then
        ok "all checks passed ($PASS/$total)"; exit 0
    fi
    fail "$FAIL of $total checks failed:"
    for f in "${FAILURES[@]}"; do echo -e "      ${RED}-${NC} $f"; done
    exit 1
}

main "$@"
