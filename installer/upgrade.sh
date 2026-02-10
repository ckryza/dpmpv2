#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${HOME}/dpmp"
SYSTEMD_DIR="${HOME}/.config/systemd/user"

echo "DPMP v2 upgrade (non-docker)"
echo "Install dir: ${INSTALL_DIR}"

if [ ! -d "${INSTALL_DIR}/.git" ]; then
  echo "ERROR: ${INSTALL_DIR} is not a git checkout" >&2
  exit 1
fi

cd "${INSTALL_DIR}"

echo "Stopping services..."
systemctl --user stop dpmpv2-nicegui.service || true
systemctl --user stop dpmpv2.service || true

# Permanently disable legacy FastAPI GUI
systemctl --user disable --now dpmpv2-gui.service 2>/dev/null || true
if [ -f "${SYSTEMD_DIR}/dpmpv2-gui.service" ] || [ -L "${SYSTEMD_DIR}/dpmpv2-gui.service" ]; then
  mv -f "${SYSTEMD_DIR}/dpmpv2-gui.service" \
    "${SYSTEMD_DIR}/dpmpv2-gui.service.DISABLED.$(date -u +%Y-%m-%d_%H%M%SZ)" \
    2>/dev/null || true
fi
ln -sf /dev/null "${SYSTEMD_DIR}/dpmpv2-gui.service"
systemctl --user daemon-reload

echo "Updating repository..."
git fetch --all --tags --prune

# Reattach to main if previously pinned or detached
git checkout -B main origin/main
git pull --ff-only

if [ ! -d ".venv" ]; then
  echo "ERROR: Python virtual environment (.venv) not found."
  echo "Run installer/install.sh to repair the installation."
  exit 1
fi

echo "Updating Python dependencies..."
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt

echo "Restarting services..."
systemctl --user restart dpmpv2.service
systemctl --user restart dpmpv2-nicegui.service

echo "Upgrade complete."
