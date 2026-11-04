#!/usr/bin/env bash
# Ensure the Hetzner Cloud server for bugsbot exists, then print its IPv4 address.
#
# Idempotent: creates the SSH key, firewall and server only if they're missing, and never
# deletes or rebuilds an existing server (bugsbot's state lives on its disk).
#
# Env: HCLOUD_TOKEN (required), DEPLOY_SSH_PUBLIC_KEY (required),
#      SERVER_NAME (default bugsbot), LOCATION (default nbg1),
#      SERVER_TYPE (default: the cheapest x86 type with >= 2 vCPU and >= 4 GB RAM in LOCATION),
#      IMAGE (default ubuntu-24.04)
set -euo pipefail

: "${HCLOUD_TOKEN:?HCLOUD_TOKEN is required}"
: "${DEPLOY_SSH_PUBLIC_KEY:?DEPLOY_SSH_PUBLIC_KEY is required}"
NAME="${SERVER_NAME:-bugsbot}"
LOCATION="${LOCATION:-nbg1}"
IMAGE="${IMAGE:-ubuntu-24.04}"
KEY_NAME="$NAME-deploy"
FIREWALL="$NAME"

log() { echo "hetzner: $*" >&2; }

firewall_rules() {
  # Inbound SSH (the deploy job connects from GitHub's runners, whose IPs vary) and HTTP(S) for
  # Caddy. bugsbot itself listens on 127.0.0.1 only.
  jq -n '[22, 80, 443] | map({direction: "in", protocol: "tcp", port: tostring,
                              source_ips: ["0.0.0.0/0", "::/0"]})'
}

ensure_ssh_key() {
  local wanted existing
  wanted=$(ssh-keygen -lf <(echo "$DEPLOY_SSH_PUBLIC_KEY") -E md5 | awk '{print $2}' | sed 's/^MD5://')
  existing=$(hcloud ssh-key list -o json | jq -r --arg fp "$wanted" '.[] | select(.fingerprint == $fp) | .name')
  if [[ -n "$existing" ]]; then
    KEY_NAME="$existing"
  elif hcloud ssh-key describe "$KEY_NAME" >/dev/null 2>&1; then
    log "an SSH key named $KEY_NAME exists in the project but isn't DEPLOY_SSH_KEY; delete or rename it"
    exit 1
  else
    log "uploading deploy SSH key as $KEY_NAME"
    hcloud ssh-key create --name "$KEY_NAME" --public-key "$DEPLOY_SSH_PUBLIC_KEY" >/dev/null
  fi
}

ensure_firewall() {
  local rules
  rules=$(mktemp)
  firewall_rules >"$rules"
  if ! hcloud firewall describe "$FIREWALL" >/dev/null 2>&1; then
    log "creating firewall $FIREWALL"
    hcloud firewall create --name "$FIREWALL" --rules-file "$rules" --label app=bugsbot >/dev/null
  else
    hcloud firewall replace-rules "$FIREWALL" --rules-file "$rules" >/dev/null
  fi
  rm -f "$rules"
}

candidate_types() {
  if [[ -n "${SERVER_TYPE:-}" ]]; then
    echo "$SERVER_TYPE"
    return
  fi
  # Cheapest first. Skips deprecated types and ones not sold in LOCATION.
  hcloud server-type list -o json | jq -r --arg loc "$LOCATION" '
    map(select(.architecture == "x86" and .cores >= 2 and .memory >= 4
               and (.deprecated // false | not) and (.deprecation == null))
        | . as $t
        | (.prices[] | select(.location == $loc) | .price_monthly.gross | tonumber) as $price
        | {name: $t.name, price: $price})
    | sort_by(.price) | .[].name'
}

create_server() {
  local type
  for type in $(candidate_types); do
    log "creating server $NAME ($type, $IMAGE, $LOCATION)"
    if hcloud server create --name "$NAME" --type "$type" --image "$IMAGE" --location "$LOCATION" \
        --ssh-key "$KEY_NAME" --firewall "$FIREWALL" --label app=bugsbot >/dev/null; then
      return
    fi
    log "couldn't create a $type server; trying the next type"
  done
  log "no server type could be created in $LOCATION; set the HETZNER_SERVER_TYPE variable"
  exit 1
}

ensure_ssh_key
ensure_firewall
if hcloud server describe "$NAME" >/dev/null 2>&1; then
  log "server $NAME already exists"
else
  create_server
fi
hcloud server ip "$NAME"
