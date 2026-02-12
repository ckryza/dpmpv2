#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/ckryza/dpmpv2.git"
INSTALL_DIR="${HOME}/dpmp"
SYSTEMD_DIR="${HOME}/.config/systemd/user"

# -------------------------
# Dry-run support
# -------------------------
DRY_RUN=0
if [ "${1:-}" = "--check" ]; then
  DRY_RUN=1
  echo "DRY-RUN MODE (--check): no changes will be made"
fi

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[check] $*"
  else
    "$@"
  fi
}

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
  echo "Repo not found, cloning..."
  run git clone "${REPO_URL}" "${INSTALL_DIR}"
else
  echo "Repo already present, updating..."
fi

cd "${INSTALL_DIR}"

run git fetch --all --tags --prune
run git checkout -B main origin/main
run git pull --ff-only

# ----------------------------------------------------------------------
# Port sanity check (always real)
# ----------------------------------------------------------------------
echo "Checking required ports (3351/9210/8855)..."
for port in 3351 9210 8855; do
if ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":${port}$"; then
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[check] WARNING: port ${port} is already in use"
    ss -ltnp | grep ":${port}" || true
  else
    echo "ERROR: port ${port} already in use." >&2
    ss -ltnp | grep ":${port}" || true
    exit 1
  fi
fi
done

# ----------------------------------------------------------------------
# Check python venv support
# ----------------------------------------------------------------------
if ! python3 - <<'EOF' >/dev/null 2>&1
import ensurepip
EOF
then
  echo "ERROR: python3-venv is not installed."
  echo
  echo "Please install it with:"
  echo "  sudo apt update"
  echo "  sudo apt install -y python3-venv"
  echo
  exit 1
fi

# ----------------------------------------------------------------------
# Python virtual environment
# ----------------------------------------------------------------------
echo "Setting up Python virtual environment..."

if [ ! -x ".venv/bin/python" ]; then
  echo "Creating Python virtual environment..."
  run rm -rf .venv
  run python3 -m venv .venv
fi

echo "Installing Python dependencies..."
run .venv/bin/pip install -U pip
run .venv/bin/pip install -r requirements.txt


# ----------------------------------------------------------------------
# Default config (only if missing)
# ----------------------------------------------------------------------
if [ ! -f dpmp/config_v2.json ]; then
  echo "Creating default config at dpmp/config_v2.json"
  run cp dpmp/config_v2_example.json dpmp/config_v2.json
fi

#-------------------------------------------------------------------------------------
# Merge any new default fields into existing config (safe, non-destructive)
#-------------------------------------------------------------------------------------
run .venv/bin/python dpmp/merge_config.py dpmp/config_v2_example.json dpmp/config_v2.json

# ----------------------------------------------------------------------
# Install systemd user services
# ----------------------------------------------------------------------
echo "Installing systemd user services..."
run cp services/dpmpv2.service "${SYSTEMD_DIR}/dpmpv2.service"
run cp services/dpmpv2-nicegui.service "${SYSTEMD_DIR}/dpmpv2-nicegui.service"
run systemctl --user daemon-reload

# Hard-disable legacy FastAPI GUI forever
run systemctl --user disable --now dpmpv2-gui.service 2>/dev/null || true
run ln -sf /dev/null "${SYSTEMD_DIR}/dpmpv2-gui.service"

# ----------------------------------------------------------------------
# Enable linger + start services
# ----------------------------------------------------------------------
run loginctl enable-linger "${USER}" >/dev/null 2>&1 || true

run systemctl --user enable --now dpmpv2.service
run systemctl --user enable --now dpmpv2-nicegui.service

echo "Done."
echo "NiceGUI: http://$(hostname -I | awk '{print $1}'):8855/"
