#!/usr/bin/env bash
# Provision (or update) a bugsbot host. Runs as root on Ubuntu 24.04; safe to re-run.
#
# Expects the bugsbot source in $APP_DIR (the deploy workflow rsyncs it there) and reads its
# inputs from the environment, all optional:
#   BUGSBOT_DOMAIN           serve the UI over HTTPS at this domain via Caddy (else localhost only;
#                            the deploy workflow defaults it to the server's sslip.io name)
#   BUGSBOT_GITHUB_TOKEN     `gh auth login` for the bugsbot user (bugsbot uses `gh auth token`)
#   ANTHROPIC_API_KEY        bugsbot's Anthropic API key setting
#   CURSOR_API_KEY           bugsbot's Cursor API key setting (Cursor CLI runs and Cloud Agent workers)
#   BUGSBOT_OAUTH_CLIENT_ID / BUGSBOT_OAUTH_CLIENT_SECRET / BUGSBOT_ALLOWED_USERS /
#   BUGSBOT_ALLOWED_ORGS     GitHub sign-in for the web UI
#   BUGSBOT_REPOS / BUGSBOT_LABELS   comma-separated; added to what's tracked (never removed)
#   ADMIN_SSH_PUBLIC_KEYS    extra public keys (one per line) allowed to SSH in as root
#   BUGSBOT_SETTINGS_JSON    any other settings, e.g. {"model": "claude-opus-5-5"}
#   HARDEN_SSH               1 (default) turns off SSH password login; deploy/local.sh sets 0
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/bugsbot/app}"
UV_DIR=/opt/bugsbot/uv          # uv, its Python builds and cache: root-owned, world-readable
USER_NAME=bugsbot
USER_HOME="/home/$USER_NAME"
STATE_DIR="$USER_HOME/.bugsbot"
PORT=8765

log() { echo "provision: $*"; }
as_user() { sudo -u "$USER_NAME" -H env PATH="$USER_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" "$@"; }
export DEBIAN_FRONTEND=noninteractive

# -- packages --------------------------------------------------------------------

apt_repo() {  # name, key url, deb line
  local keyring="/etc/apt/keyrings/$1.gpg"
  if [[ ! -s "$keyring" ]]; then
    curl -fsSL "$2" | gpg --dearmor -o "$keyring"
    chmod 644 "$keyring"
  fi
  echo "$3" >"/etc/apt/sources.list.d/$1.list"
}

log "installing packages"
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg git jq rsync sqlite3 sudo build-essential >/dev/null
install -d -m 755 /etc/apt/keyrings
apt_repo github-cli https://cli.github.com/packages/githubcli-archive-keyring.gpg \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/github-cli.gpg] https://cli.github.com/packages stable main"
if [[ -n "${BUGSBOT_DOMAIN:-}" ]]; then
  apt_repo caddy https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
    "deb [signed-by=/etc/apt/keyrings/caddy.gpg] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main"
fi
apt-get update -qq
apt-get install -y -qq gh ${BUGSBOT_DOMAIN:+caddy} >/dev/null

# -- host basics -----------------------------------------------------------------

if [[ ! -f /swapfile && -z "$(swapon --noheadings --show 2>/dev/null)" ]]; then
  log "adding 2 GB of swap"
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null
  if swapon /swapfile; then
    echo '/swapfile none swap sw 0 0' >>/etc/fstab
  else
    log "couldn't enable swap; continuing without it"
  fi
fi

if [[ "${HARDEN_SSH:-1}" == 1 && -d /etc/ssh/sshd_config.d ]]; then
  cat >/etc/ssh/sshd_config.d/10-bugsbot.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
  systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
fi

if [[ -n "${ADMIN_SSH_PUBLIC_KEYS:-}" ]]; then
  keys=/root/.ssh/authorized_keys
  install -d -m 700 /root/.ssh && touch "$keys"
  sed -i '/# bugsbot-admin-begin/,/# bugsbot-admin-end/d' "$keys"
  { echo "# bugsbot-admin-begin"; printf '%s\n' "$ADMIN_SSH_PUBLIC_KEYS"; echo "# bugsbot-admin-end"; } >>"$keys"
  chmod 600 "$keys"
fi

# -- the bugsbot user and its tools -----------------------------------------------

if ! id "$USER_NAME" >/dev/null 2>&1; then
  log "creating user $USER_NAME"
  useradd --create-home --shell /bin/bash "$USER_NAME"
fi

log "installing uv"
install -d -m 755 "$UV_DIR"
if [[ ! -x "$UV_DIR/bin/uv" ]]; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$UV_DIR/bin" UV_NO_MODIFY_PATH=1 sh >/dev/null
else
  "$UV_DIR/bin/uv" self update -q || true
fi

log "installing Claude Code for $USER_NAME"
if as_user bash -c 'command -v claude' >/dev/null; then
  as_user claude update >/dev/null 2>&1 || true
else
  as_user bash -c 'curl -fsSL https://claude.ai/install.sh | bash' >/dev/null
fi

log "installing the Cursor CLI for $USER_NAME"
if as_user bash -c 'command -v cursor-agent' >/dev/null; then
  as_user cursor-agent update >/dev/null 2>&1 || true
else
  as_user bash -c 'curl -fsS https://cursor.com/install | bash' >/dev/null
fi

# The app (code and virtualenv) is root-owned, so the agents, which run as the bugsbot user,
# can't modify the harness itself.
log "installing bugsbot from $APP_DIR"
chown -R root:root "$APP_DIR"
(cd "$APP_DIR" && env UV_PYTHON_INSTALL_DIR="$UV_DIR/python" UV_CACHE_DIR="$UV_DIR/cache" \
  "$UV_DIR/bin/uv" sync --frozen --no-dev --python-preference only-managed -q)
chmod -R a+rX "$APP_DIR" "$UV_DIR"
BUGSBOT="$APP_DIR/.venv/bin/bugsbot"

# Stop bugsbot while we edit its state and Claude Code's config (bugsbot rewrites ~/.claude.json).
systemctl stop bugsbot 2>/dev/null || true

# -- GitHub CLI login ----------------------------------------------------------------

if [[ -n "${BUGSBOT_GITHUB_TOKEN:-}" ]]; then
  log "signing the GitHub CLI in for $USER_NAME"
  printf '%s' "$BUGSBOT_GITHUB_TOKEN" | as_user gh auth login --hostname github.com --git-protocol https --with-token
  as_user gh auth setup-git --hostname github.com
fi
gh_status=$(as_user gh auth status --hostname github.com 2>&1 | grep -o 'Logged in to github.com account [^ ]*' || true)

# -- Claude Code config: skip first-run prompts and pre-trust the home folder ------------
# Trust isn't inherited by subfolders; bugsbot trusts each remote worker's folder itself.

cfg="$USER_HOME/.claude.json"
[[ -s "$cfg" ]] || echo '{}' >"$cfg"
jq --arg home "$USER_HOME" '
  .hasCompletedOnboarding = true
  | .theme = (.theme // "dark")
  | .remoteDialogSeen = true
  | .projects[$home].hasTrustDialogAccepted = true
' "$cfg" >"$cfg.new"
chown "$USER_NAME:$USER_NAME" "$cfg.new" && chmod 600 "$cfg.new" && mv "$cfg.new" "$cfg"
claude_status=$(as_user claude auth status --json 2>/dev/null || echo '{}')

# -- bugsbot settings ------------------------------------------------------------------

extra="${BUGSBOT_SETTINGS_JSON:-}"
[[ -n "$extra" ]] || extra='{}'
csv_json() { jq -cn --arg s "${1:-}" '$s | split(",") | map(gsub("^\\s+|\\s+$"; "")) | map(select(. != ""))'; }
config=$(jq -n \
  --arg api_key "${ANTHROPIC_API_KEY:-}" \
  --arg cursor_key "${CURSOR_API_KEY:-}" \
  --arg client_id "${BUGSBOT_OAUTH_CLIENT_ID:-}" \
  --arg client_secret "${BUGSBOT_OAUTH_CLIENT_SECRET:-}" \
  --arg users "${BUGSBOT_ALLOWED_USERS:-}" \
  --arg orgs "${BUGSBOT_ALLOWED_ORGS:-}" \
  --argjson extra "$extra" \
  --argjson repos "$(csv_json "${BUGSBOT_REPOS:-}")" \
  --argjson labels "$(csv_json "${BUGSBOT_LABELS:-}")" '
  {settings: (({anthropic_api_key: $api_key, cursor_api_key: $cursor_key, github_oauth_client_id: $client_id,
                github_oauth_client_secret: $client_secret, oauth_allowed_users: $users,
                oauth_allowed_orgs: $orgs} | with_entries(select(.value != ""))) + $extra),
   repos: $repos, labels: $labels}')
log "applying bugsbot settings"
printf '%s' "$config" | as_user "$BUGSBOT" configure --home "$STATE_DIR" | sed 's/^/provision:   /'

# -- services ----------------------------------------------------------------------------

public_url="${BUGSBOT_DOMAIN:+https://$BUGSBOT_DOMAIN}"
cat >/etc/systemd/system/bugsbot.service <<EOF
[Unit]
Description=bugsbot agent harness
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$USER_HOME
Environment=PATH=$USER_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=BUGSBOT_HOME=$STATE_DIR
ExecStart=$BUGSBOT serve --no-browser --host 127.0.0.1 --port $PORT ${public_url:+--public-url $public_url}
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=full

[Install]
WantedBy=multi-user.target
EOF

if [[ -n "${BUGSBOT_DOMAIN:-}" ]]; then
  cat >/etc/caddy/Caddyfile <<EOF
$BUGSBOT_DOMAIN {
	encode gzip
	reverse_proxy 127.0.0.1:$PORT
}
EOF
  systemctl enable -q caddy
  systemctl reload-or-restart caddy
elif systemctl is-enabled -q caddy 2>/dev/null; then
  log "no domain set; turning off Caddy"
  systemctl disable -q --now caddy
fi

systemctl daemon-reload
systemctl enable -q bugsbot
systemctl restart bugsbot

log "waiting for bugsbot to start"
for _ in $(seq 30); do
  if curl -fsS -o /dev/null "http://127.0.0.1:$PORT/login" 2>/dev/null; then break; fi
  sleep 1
done
if ! curl -fsS -o /dev/null "http://127.0.0.1:$PORT/login"; then
  journalctl -u bugsbot -n 50 --no-pager
  echo "provision: bugsbot didn't start" >&2
  exit 1
fi

# -- summary (no secrets: Actions logs are public on public repos) -------------------------

echo
log "bugsbot is running."
if [[ -n "${BUGSBOT_DOMAIN:-}" ]]; then
  log "  web UI: https://$BUGSBOT_DOMAIN/"
  [[ "$BUGSBOT_DOMAIN" == *.sslip.io ]] || log "    (point the domain's DNS at this server)"
else
  log "  web UI: localhost only; use an SSH tunnel: ssh -L $PORT:127.0.0.1:$PORT root@<server>"
fi
log "  GitHub CLI: ${gh_status:-NOT signed in (set BUGSBOT_GITHUB_TOKEN)}"
if [[ "$(jq -r '.loggedIn // false' <<<"$claude_status")" == "true" ]]; then
  log "  Claude Code: signed in as $(jq -r '.email // "?"' <<<"$claude_status")"
else
  log "  Claude Code: NOT signed in. Once, on the host run: sudo -iu $USER_NAME claude"
  log "    (from elsewhere: ssh -t root@<server> sudo -iu $USER_NAME claude)"
  log "    then type /login and follow the link. It stays signed in after that."
fi
