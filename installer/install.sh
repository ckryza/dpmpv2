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

PIN_COMMIT="5910ad7"

echo "Checking out pinned commit: ${PIN_COMMIT}"
git fetch --all --tags --prune
git checkout -f "${PIN_COMMIT}"


echo "Checking required ports (3351/9210/8855)..."
for port in 3351 9210 8855; do
  if ss -ltn 2>/dev/null | awk "{print \$4}" | grep -q ":${port}$"; then
    echo "ERROR: port ${port} already in use. Stop the conflicting service and retry." >&2
    ss -ltnp | grep ":${port}" || true
    exit 1
  fi
done

CONFIG_DST="${INSTALL_DIR}/dpmp/config_v2.json"
CONFIG_SRC="${INSTALL_DIR}/dpmp/config_v2_example.json"
if [ ! -f "${CONFIG_DST}" ]; then
  echo "Creating default config at ${CONFIG_DST} (edit Pool A/B in GUI)..."
  cp -a "${CONFIG_SRC}" "${CONFIG_DST}"
fi

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

# Hard-disable legacy FastAPI GUI so it can never auto-start again
systemctl --user disable --now dpmpv2-gui.service 2>/dev/null || true
if [ -f "${SYSTEMD_DIR}/dpmpv2-gui.service" ] || [ -L "${SYSTEMD_DIR}/dpmpv2-gui.service" ]; then
  mv -f "${SYSTEMD_DIR}/dpmpv2-gui.service" "${SYSTEMD_DIR}/dpmpv2-gui.service.DISABLED.$(date -u +%Y-%m-%d_%H%M%SZ)" 2>/dev/null || true
fi
ln -sf /dev/null "${SYSTEMD_DIR}/dpmpv2-gui.service"

echo "Enabling linger..."
loginctl enable-linger "${USER}" >/dev/null 2>&1 || true

echo "Enabling + starting services..."
systemctl --user enable --now dpmpv2.service
systemctl --user enable --now dpmpv2-nicegui.service

echo "Done."
echo "NiceGUI: http://$(hostname -I | awk '{print $1}'):8855/"
