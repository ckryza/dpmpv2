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

# ----------------------------------------------------------------------
# Clone or update repo
# ----------------------------------------------------------------------
if [ ! -d "${INSTALL_DIR}/.git" ]; then
  echo "Cloning repo..."
  git clone "${REPO_URL}" "${INSTALL_DIR}"
fi

cd "${INSTALL_DIR}"

git fetch --all --tags --prune
git checkout -B main origin/main
git pull --ff-only

# ----------------------------------------------------------------------
# Port sanity check BEFORE starting anything
# ----------------------------------------------------------------------
echo "Checking required ports (3351/9210/8855)..."
for port in 3351 9210 8855; do
  if ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":${port}$"; then
    echo "ERROR: port ${port} already in use." >&2
    ss -ltnp | grep ":${port}" || true
    exit 1
  fi
done

# ----------------------------------------------------------------------
# Python virtual environment
# ----------------------------------------------------------------------
echo "Setting up Python virtual environment..."
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt

# ----------------------------------------------------------------------
# Default config (only if missing)
# ----------------------------------------------------------------------
if [ ! -f dpmp/config_v2.json ]; then
  echo "Creating default config at dpmp/config_v2.json"
  cp dpmp/config_v2_example.json dpmp/config_v2.json
fi

# ----------------------------------------------------------------------
# Install systemd user services
# ----------------------------------------------------------------------
echo "Installing systemd user services..."
cp services/dpmpv2.service "${SYSTEMD_DIR}/dpmpv2.service"
cp services/dpmpv2-nicegui.service "${SYSTEMD_DIR}/dpmpv2-nicegui.service"
systemctl --user daemon-reload

# Hard-disable legacy FastAPI GUI forever
systemctl --user disable --now dpmpv2-gui.service 2>/dev/null || true
ln -sf /dev/null "${SYSTEMD_DIR}/dpmpv2-gui.service"

# ----------------------------------------------------------------------
# Enable linger + start services
# ----------------------------------------------------------------------
loginctl enable-linger "${USER}" >/dev/null 2>&1 || true

systemctl --user enable --now dpmpv2.service
systemctl --user enable --now dpmpv2-nicegui.service

echo "Done."
echo "NiceGUI: http://$(hostname -I | awk '{print $1}'):8855/"
