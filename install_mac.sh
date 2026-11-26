#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

echo "=== Teams Live Copilot - macOS Setup ==="

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 not found. Install via Homebrew: brew install python" >&2
  exit 1
fi

if ! command -v swiftc >/dev/null 2>&1; then
  echo "Error: swiftc not found. Install Xcode Command Line Tools: xcode-select --install" >&2
  exit 1
fi

echo "Compiling ScreenCaptureKit audio helper (mac/teams_audio_tap)..."
swiftc -O -parse-as-library \
  -target arm64-apple-macos13.0 \
  mac/AudioTap.swift \
  -o mac/teams_audio_tap 2>/dev/null || \
swiftc -O -parse-as-library \
  mac/AudioTap.swift \
  -o mac/teams_audio_tap

if [ ! -x ".venv/bin/python" ]; then
  echo "Creating virtual environment (.venv)..."
  python3 -m venv .venv
fi

echo "Installing Python dependencies..."
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

echo ""
echo "Setup complete! Run ./start_mac.sh to launch."
echo "Note: On first run, macOS will ask for 'Screen & System Audio Recording' permission."
