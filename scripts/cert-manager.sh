#!/bin/bash
# =============================================================================
# cert-manager.sh  —  your_domain.com
# Checks and renews SSL certificates for freeholdy fixed domains.
# Pet project subdomains are handled by freeholdy itself via certbot.
#
# Run via crontab:
#   0 3 * * * /path/to/freeholdy/scripts/cert-manager.sh
# =============================================================================

set -euo pipefail

# Fixed domains that always need certs (freeholdy itself + any static sites)
DOMAINS=(
    "api.your_domain.com"
)

RENEW_BEFORE_DAYS=30
WEBSERVER="nginx"
CERTBOT_EMAIL="admin@your_domain.com"
CERTBOT_EXTRA_FLAGS=""
LOG_FILE="/var/log/freeholdy-cert-manager.log"

RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; NC='\033[0m'

log()     { echo -e "$(date '+%Y-%m-%d %H:%M:%S') [$1] ${*:2}" | tee -a "$LOG_FILE"; }
info()    { log "INFO " "${GREEN}${*}${NC}"; }
warn()    { log "WARN " "${YELLOW}${*}${NC}"; }
error()   { log "ERROR" "${RED}${*}${NC}"; }
divider() { printf '─%.0s' {1..60}; echo; }

require_root() {
    [[ $EUID -eq 0 ]] || { error "Must be run as root (sudo)."; exit 1; }
}

require_certbot() {
    command -v certbot &>/dev/null || {
        error "certbot not found. Install: sudo apt install certbot python3-certbot-nginx"
        exit 1
    }
}

cert_path()          { echo "/etc/letsencrypt/live/${1}/fullchain.pem"; }
cert_exists()        { [[ -f "$(cert_path "$1")" ]]; }

days_until_expiry() {
    local cert; cert=$(cert_path "$1")
    [[ -f "$cert" ]] || { echo -1; return; }
    local expiry; expiry=$(openssl x509 -enddate -noout -in "$cert" 2>/dev/null | sed 's/notAfter=//')
    [[ -z "$expiry" ]] && { echo -1; return; }
    local exp_epoch; exp_epoch=$(date -d "$expiry" +%s 2>/dev/null || date -j -f "%b %d %T %Y %Z" "$expiry" +%s 2>/dev/null)
    echo $(( (exp_epoch - $(date +%s)) / 86400 ))
}

issue_cert() {
    info "Issuing cert for: $1"
    certbot certonly --"$WEBSERVER" --non-interactive --agree-tos \
        --email "$CERTBOT_EMAIL" -d "$1" $CERTBOT_EXTRA_FLAGS \
        >> "$LOG_FILE" 2>&1 \
        && info "Issued: $1" || error "Failed: $1"
}

renew_cert() {
    info "Renewing cert for: $1"
    certbot renew --cert-name "$1" --non-interactive --force-renewal \
        $CERTBOT_EXTRA_FLAGS >> "$LOG_FILE" 2>&1 \
        && info "Renewed: $1" || error "Failed to renew: $1"
}

require_root
require_certbot
mkdir -p "$(dirname "$LOG_FILE")"; touch "$LOG_FILE"

divider
info "cert-manager started — ${#DOMAINS[@]} domain(s)"
divider

CHANGED=0

for domain in "${DOMAINS[@]}"; do
    divider; info "Checking: $domain"
    if ! cert_exists "$domain"; then
        warn "No cert found — issuing..."
        issue_cert "$domain"; CHANGED=1; continue
    fi
    days=$(days_until_expiry "$domain")
    if   [[ $days -lt 0 ]];                  then warn "Cannot read expiry — re-issuing..."; issue_cert "$domain"; CHANGED=1
    elif [[ $days -le 0 ]];                  then warn "EXPIRED — renewing..."; renew_cert "$domain"; CHANGED=1
    elif [[ $days -le $RENEW_BEFORE_DAYS ]]; then warn "Expires in ${days}d — renewing early..."; renew_cert "$domain"; CHANGED=1
    else info "Valid, expires in ${days} day(s). No action needed."
    fi
done

divider
[[ $CHANGED -eq 1 ]] && { info "Reloading $WEBSERVER..."; systemctl reload "$WEBSERVER"; } \
                      || info "All certs up to date."
divider
info "Done."
