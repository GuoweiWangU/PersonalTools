#!/usr/bin/env bash
set -euo pipefail

: "${CHAT_ID:?Set CHAT_ID to the allowed Feishu group ID}"
: "${SENDER_OPEN_ID:?Set SENDER_OPEN_ID to the allowed controller open_id}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TEMPLATE="$ROOT/model_orchestration/deploy/personal-tools-relay.service.example"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE="$SERVICE_DIR/personal-tools-relay.service"
mkdir -p "$SERVICE_DIR"

escape_sed() {
  printf '%s' "$1" | sed 's/[&|]/\\&/g'
}

AGENT_ARGS=(--agent-root "$ROOT")
if [[ -n "${AGENT_ROOTS:-}" ]]; then
  IFS=':' read -r -a EXTRA_ROOTS <<< "$AGENT_ROOTS"
  for path in "${EXTRA_ROOTS[@]}"; do
    [[ -n "$path" ]] && AGENT_ARGS+=(--agent-root "$path")
  done
fi
if [[ -n "${SSH_HOSTS:-}" ]]; then
  IFS=',' read -r -a HOSTS <<< "$SSH_HOSTS"
  for host in "${HOSTS[@]}"; do
    [[ -n "$host" ]] && AGENT_ARGS+=(--ssh-host "$host")
  done
fi
AGENT_ARGS_TEXT="$(printf ' %q' "${AGENT_ARGS[@]}")"
ROOT_ESCAPED="$(escape_sed "$ROOT")"
CHAT_ESCAPED="$(escape_sed "$CHAT_ID")"
SENDER_ESCAPED="$(escape_sed "$SENDER_OPEN_ID")"
AGENT_ARGS_ESCAPED="$(escape_sed "$AGENT_ARGS_TEXT")"
sed \
  -e "s|__REPO_ROOT__|$ROOT_ESCAPED|g" \
  -e "s|__CHAT_ID__|$CHAT_ESCAPED|g" \
  -e "s|__SENDER_OPEN_ID__|$SENDER_ESCAPED|g" \
  -e "s|__AGENT_ARGS__|$AGENT_ARGS_ESCAPED|g" \
  "$TEMPLATE" > "$SERVICE"

systemctl --user daemon-reload
systemctl --user enable --now personal-tools-relay.service
systemctl --user status personal-tools-relay.service --no-pager
