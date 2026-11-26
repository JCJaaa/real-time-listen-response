#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

if [ ! -x ".venv/bin/python" ] || [ ! -x "mac/teams_audio_tap" ]; then
  echo "Environment not initialized. Running install_mac.sh..."
  bash "$REPO_DIR/install_mac.sh"
fi

ENV_FILE="$REPO_DIR/.env"
touch "$ENV_FILE"

get_env_val() {
  local key="$1"
  grep -E "^${key}=" "$ENV_FILE" | tail -n 1 | cut -d'=' -f2- | sed 's/^["'\'']//;s/["'\'']$//' || true
}

set_env_val() {
  local key="$1"
  local val="$2"
  local tmp_file
  tmp_file="$(mktemp)"
  grep -v -E "^${key}=" "$ENV_FILE" > "$tmp_file" || true
  echo "${key}=${val}" >> "$tmp_file"
  mv "$tmp_file" "$ENV_FILE"
}

DEFAULT_PROVIDER="$(get_env_val "AI_PROVIDER")"
DEFAULT_PROVIDER="${DEFAULT_PROVIDER:-gemini}"

echo ""
echo "Select AI Provider (default: $DEFAULT_PROVIDER):"
echo "  [1] Gemini (Recommended - Native Live Audio)"
echo "  [2] GPT (OpenAI)"
echo "  [3] Claude (Anthropic)"
read -r -p "Enter 1, 2, or 3 (press Enter for $DEFAULT_PROVIDER): " CHOICE

case "$(echo "$CHOICE" | tr '[:upper:]' '[:lower:]')" in
  "") PROVIDER="$DEFAULT_PROVIDER" ;;
  1|gemini) PROVIDER="gemini" ;;
  2|gpt) PROVIDER="gpt" ;;
  3|claude) PROVIDER="claude" ;;
  *) echo "Invalid choice, using $DEFAULT_PROVIDER."; PROVIDER="$DEFAULT_PROVIDER" ;;
esac

case "$PROVIDER" in
  gemini) KEY_VAR="GEMINI_API_KEY" ;;
  gpt) KEY_VAR="OPENAI_API_KEY" ;;
  claude) KEY_VAR="ANTHROPIC_API_KEY" ;;
esac

CUR_VAL="$(eval echo "\${${KEY_VAR}:-}")"
if [ -z "$CUR_VAL" ]; then
  CUR_VAL="$(get_env_val "$KEY_VAR")"
fi

if [ -z "$CUR_VAL" ]; then
  read -r -p "Enter your $KEY_VAR (saved locally to .env): " INPUT_KEY
  if [ -z "$INPUT_KEY" ]; then
    echo "$KEY_VAR is required." >&2
    exit 1
  fi
  set_env_val "$KEY_VAR" "$INPUT_KEY"
fi

set_env_val "AI_PROVIDER" "$PROVIDER"

ARGS=("teams_live_copilot.py" "--provider" "$PROVIDER")
if [ -f "gbsh0010_context.md" ]; then
  ARGS+=("--context" "gbsh0010_context.md")
fi

exec .venv/bin/python "${ARGS[@]}" "$@"
