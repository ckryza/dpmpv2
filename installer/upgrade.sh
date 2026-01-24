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

echo "Updating repo..."
git pull --ff-only

echo "Updating deps..."
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt

echo "Restarting services..."
systemctl --user start dpmpv2.service
systemctl --user start dpmpv2-nicegui.service

echo "Done."
