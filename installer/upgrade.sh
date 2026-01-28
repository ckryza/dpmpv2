#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${HOME}/dpmp"

if [ ! -d "${INSTALL_DIR}/.git" ]; then
  echo "ERROR: ${INSTALL_DIR} is not a git checkout" >&2
  exit 1
fi

cd "${INSTALL_DIR}"

echo "Stopping services..."
systemctl --user stop dpmpv2-nicegui.service || true
systemctl --user stop dpmpv2.service || true

# Hard-disable legacy FastAPI GUI so it can never auto-start again
SYSTEMD_DIR="${HOME}/.config/systemd/user"
systemctl --user disable --now dpmpv2-gui.service 2>/dev/null || true
if [ -f "${SYSTEMD_DIR}/dpmpv2-gui.service" ] || [ -L "${SYSTEMD_DIR}/dpmpv2-gui.service" ]; then
  mv -f "${SYSTEMD_DIR}/dpmpv2-gui.service" "${SYSTEMD_DIR}/dpmpv2-gui.service.DISABLED.$(date -u +%Y-%m-%d_%H%M%SZ)" 2>/dev/null || true
fi
ln -sf /dev/null "${SYSTEMD_DIR}/dpmpv2-gui.service"
systemctl --user daemon-reload

echo "Updating repo..."
git pull --ff-only

echo "Updating deps..."
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt

echo "Restarting services..."
systemctl --user restart dpmpv2.service
systemctl --user restart dpmpv2-nicegui.service


echo "Done."
