#!/usr/bin/env bash
# RemoteCode installer — sets up tmux + a Python venv, and (optionally) a systemd service.
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"

say() { printf '\033[1;36m[RemoteCode]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[RemoteCode]\033[0m %s\n' "$*" >&2; }

SUDO=""
if [ "$(id -u)" -ne 0 ]; then command -v sudo >/dev/null 2>&1 && SUDO="sudo"; fi

pkg_install() {   # install one or more packages using whatever manager exists
  if   command -v apt-get >/dev/null; then $SUDO apt-get update -qq && $SUDO apt-get install -y "$@"
  elif command -v dnf     >/dev/null; then $SUDO dnf install -y "$@"
  elif command -v yum     >/dev/null; then $SUDO yum install -y "$@"
  elif command -v pacman  >/dev/null; then $SUDO pacman -Sy --noconfirm "$@"
  elif command -v zypper  >/dev/null; then $SUDO zypper install -y "$@"
  elif command -v apk     >/dev/null; then $SUDO apk add "$@"
  elif command -v brew    >/dev/null; then brew install "$@"
  else err "No supported package manager found. Please install: $*"; return 1
  fi
}

# 1. tmux ---------------------------------------------------------------
if command -v tmux >/dev/null 2>&1; then
  say "tmux present: $(tmux -V)"
else
  say "tmux not found — installing it…"
  pkg_install tmux
  say "tmux installed: $(tmux -V)"
fi

# 2. python3 + venv -----------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  say "python3 not found — installing…"; pkg_install python3
fi
if ! python3 -c 'import venv' 2>/dev/null; then
  say "python venv module missing — installing python3-venv…"
  pkg_install python3-venv || true
fi

say "creating virtualenv in $DIR/venv"
python3 -m venv venv
./venv/bin/pip -q install --upgrade pip
./venv/bin/pip -q install -r requirements.txt
say "python deps installed"

# 3. optional systemd service ------------------------------------------
if [ "${1:-}" = "--service" ]; then
  UNIT=/etc/systemd/system/remotecode.service
  say "installing systemd service at $UNIT"
  $SUDO tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=RemoteCode — web control panel for tmux agents
After=network.target

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=$DIR
Environment=REMOTECODE_HOST=${REMOTECODE_HOST:-127.0.0.1}
Environment=REMOTECODE_PORT=${REMOTECODE_PORT:-7070}
ExecStart=$DIR/venv/bin/uvicorn app:app --host ${REMOTECODE_HOST:-127.0.0.1} --port ${REMOTECODE_PORT:-7070}
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now remotecode
  say "service started. status: $(systemctl is-active remotecode)"
fi

echo
say "Done. Run it with:   ./run.sh        (or: systemctl start remotecode)"
say "Then open:           http://${REMOTECODE_HOST:-127.0.0.1}:${REMOTECODE_PORT:-7070}/"
say "Auth is OFF by default — see README (Security) before exposing beyond localhost."
