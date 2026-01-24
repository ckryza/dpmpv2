#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/ckryza/dpmpv2.git"
INSTALL_DIR="${HOME}/dpmp"
SYSTEMD_DIR="${HOME}/.config/systemd/user"

echo "DPMP v2 installer (non-docker)"
echo "Install dir: ${INSTALL_DIR}"

if [ "$(id -u)" -eq 0 ]; then
  echo "ERROR: do not run as root" >&2
  exit 1
fi

mkdir -p "${SYSTEMD_DIR}"

if [ ! -d "${INSTALL_DIR}/.git" ]; then
  echo "Cloning repo..."
  git clone "${REPO_URL}" "${INSTALL_DIR}"
else
  echo "Repo already present, skipping clone."
fi

cd "${INSTALL_DIR}"

echo "Creating venv (if missing)..."
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

echo "Installing python deps..."
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt

echo "Installing systemd user services..."
cp -a services/dpmpv2.service "${SYSTEMD_DIR}/dpmpv2.service"
cp -a services/dpmpv2-nicegui.service "${SYSTEMD_DIR}/dpmpv2-nicegui.service"
systemctl --user daemon-reload

echo "Enabling linger..."
loginctl enable-linger "${USER}" >/dev/null 2>&1 || true

echo "Enabling + starting services..."
systemctl --user enable --now dpmpv2.service
systemctl --user enable --now dpmpv2-nicegui.service

echo "Done."
echo "NiceGUI: http://$(hostname -I | awk '{print $1}'):8855/"
