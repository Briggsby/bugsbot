#!/usr/bin/env bash
# Deploy bugsbot on this machine (Ubuntu or Debian with systemd), served over HTTPS at its
# public IP's sslip.io name. Safe to re-run; re-run it after pulling to update.
#
#   cp deploy/local.env.example deploy/local.env   # then fill it in
#   sudo deploy/local.sh [path/to/env-file]        # default: deploy/local.env
#
# Uses the same provisioning as the Hetzner deploy (deploy/provision.sh), except that it leaves
# your SSH settings alone.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  exec sudo bash "$0" "$@"
fi

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
repo=$(dirname "$here")
env_file="${1:-$here/local.env}"
log() { echo "local: $*"; }

if [[ -f "$env_file" ]]; then
  log "reading $env_file"
  set -a
  # shellcheck disable=SC1090
  . "$env_file"
  set +a
else
  log "no $env_file; continuing with defaults (see deploy/local.env.example)"
fi
command -v apt-get >/dev/null && command -v systemctl >/dev/null \
  || { echo "local: this script needs Ubuntu or Debian with systemd" >&2; exit 1; }
command -v curl >/dev/null || apt-get install -y -qq curl >/dev/null

# -- the public URL ----------------------------------------------------------------

case "${BUGSBOT_DOMAIN:-}" in
  none) BUGSBOT_DOMAIN="" ;;
  "")
    public_ip="${PUBLIC_IP:-$(curl -4 -fsS --max-time 10 https://api.ipify.org)}"
    [[ "$public_ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] \
      || { echo "local: couldn't detect the public IP; set PUBLIC_IP or BUGSBOT_DOMAIN" >&2; exit 1; }
    BUGSBOT_DOMAIN="${public_ip//./-}.sslip.io"
    log "public IP $public_ip, so the web UI will be at https://$BUGSBOT_DOMAIN/"
    if ! ip -4 -o addr show 2>/dev/null | grep -qw "$public_ip"; then
      log "NOTE: $public_ip isn't on any of this machine's interfaces, so it's probably behind NAT."
      log "  Forward TCP ports 80 and 443 on your router to this machine, or HTTPS won't work."
    fi
    ;;
esac
export BUGSBOT_DOMAIN

if [[ -n "$BUGSBOT_DOMAIN" ]] && command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  log "opening ports 80 and 443 in ufw for Caddy"
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
fi

# -- install the app from this checkout --------------------------------------------

app=/opt/bugsbot/app
log "copying $repo to $app"
rm -rf "$app.new" && mkdir -p "$app.new"
tar -C "$repo" -c --exclude=.git --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache \
    --exclude=deploy/local.env . | tar -x -C "$app.new"
if [[ -d "$app/.venv" ]]; then mv "$app/.venv" "$app.new/"; fi
rm -rf "$app" && mv "$app.new" "$app"

HARDEN_SSH=0 APP_DIR="$app" bash "$app/deploy/provision.sh"

if [[ -n "$BUGSBOT_DOMAIN" ]]; then
  echo
  log "GitHub OAuth App callback URL: https://$BUGSBOT_DOMAIN/auth/github/callback"
fi
