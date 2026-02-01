#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/ckryza/dpmpv2.git"

# Dev checkout (optional, but we keep it for upgrades / troubleshooting)
DEV_DIR="${HOME}/dpmp"

# Runtime install (minimal, clean)
RUNTIME_DIR="${HOME}/dpmp_runtime"

SYSTEMD_DIR="${HOME}/.config/systemd/user"

echo "DPMP v2 installer (non-docker)"
echo "Dev dir:     ${DEV_DIR}"
echo "Runtime dir: ${RUNTIME_DIR}"

if [ "$(id -u)" -eq 0 ]; then
  echo "ERROR: do not run as root" >&2
  exit 1
fi

mkdir -p "${SYSTEMD_DIR}"

if [ ! -d "${DEV_DIR}/.git" ]; then
  echo "Cloning repo into dev dir..."
  git clone "${REPO_URL}" "${DEV_DIR}"
else
  echo "Dev repo already present, skipping clone."
fi

cd "${DEV_DIR}"

PIN_COMMIT="aff8d5c"

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

echo "Creating clean runtime dir..."
rm -rf "${RUNTIME_DIR}"
mkdir -p "${RUNTIME_DIR}/dpmp" "${RUNTIME_DIR}/gui_nice"

cp -a "${DEV_DIR}/requirements.txt" "${RUNTIME_DIR}/"
cp -a "${DEV_DIR}/dpmp/dpmpv2.py" "${RUNTIME_DIR}/dpmp/"
cp -a "${DEV_DIR}/dpmp/config_v2_example.json" "${RUNTIME_DIR}/dpmp/"
cp -a "${DEV_DIR}/gui_nice" "${RUNTIME_DIR}/"

CONFIG_DST="${RUNTIME_DIR}/dpmp/config_v2.json"
CONFIG_SRC="${RUNTIME_DIR}/dpmp/config_v2_example.json"
echo "Creating default runtime config at ${CONFIG_DST} (edit Pool A/B in GUI)..."
cp -a "${CONFIG_SRC}" "${CONFIG_DST}"

echo "Creating venv in runtime dir..."
python3 -m venv "${RUNTIME_DIR}/.venv"

echo "Installing python deps..."
"${RUNTIME_DIR}/.venv/bin/pip" install -U pip
"${RUNTIME_DIR}/.venv/bin/pip" install -r "${RUNTIME_DIR}/requirements.txt"

echo "Installing systemd user services..."
cp -a "${DEV_DIR}/services/dpmpv2.service" "${SYSTEMD_DIR}/dpmpv2.service"
cp -a "${DEV_DIR}/services/dpmpv2-nicegui.service" "${SYSTEMD_DIR}/dpmpv2-nicegui.service"
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
