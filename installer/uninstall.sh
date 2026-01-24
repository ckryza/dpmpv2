#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="${HOME}/dpmp"
SYSTEMD_DIR="${HOME}/.config/systemd/user"

echo "Stopping + disabling services..."
systemctl --user disable --now dpmpv2-nicegui.service || true
systemctl --user disable --now dpmpv2.service || true

echo "Removing service files..."
rm -f "${SYSTEMD_DIR}/dpmpv2.service" "${SYSTEMD_DIR}/dpmpv2-nicegui.service"
systemctl --user daemon-reload

echo "NOTE: install dir kept: ${INSTALL_DIR}"
echo "If you want to delete EVERYTHING (including config/logs), run:"
echo "  rm -rf ${INSTALL_DIR}"
