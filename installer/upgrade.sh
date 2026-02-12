#!/usr/bin/env bash
set -euo pipefail

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

echo "DPMP v2 upgrade (non-docker)"
echo "Install dir: ${INSTALL_DIR}"

if [ ! -d "${INSTALL_DIR}/.git" ]; then
  echo "ERROR: ${INSTALL_DIR} is not a git checkout" >&2
  exit 1
fi

cd "${INSTALL_DIR}"

echo "Stopping services..."
run systemctl --user stop dpmpv2-nicegui.service || true
run systemctl --user stop dpmpv2.service || true

# Permanently disable legacy FastAPI GUI
run systemctl --user disable --now dpmpv2-gui.service 2>/dev/null || true
if [ -f "${SYSTEMD_DIR}/dpmpv2-gui.service" ] || [ -L "${SYSTEMD_DIR}/dpmpv2-gui.service" ]; then
  run mv -f "${SYSTEMD_DIR}/dpmpv2-gui.service" \
    "${SYSTEMD_DIR}/dpmpv2-gui.service.DISABLED.$(date -u +%Y-%m-%d_%H%M%SZ)" || true
fi
run ln -sf /dev/null "${SYSTEMD_DIR}/dpmpv2-gui.service"
run systemctl --user daemon-reload

echo "Updating repository..."
run git fetch --all --tags --prune
run git checkout -B main origin/main
run git pull --ff-only

if [ ! -d ".venv" ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "[check] WARNING: .venv not found (upgrade would fail)"
  else
    echo "ERROR: Python virtual environment (.venv) not found."
    echo "Run installer/install.sh to repair the installation."
    exit 1
  fi
fi

echo "Updating Python dependencies..."
run .venv/bin/pip install -U pip
run .venv/bin/pip install -r requirements.txt

#-------------------------------------------------------------------------------------
# Merge any new default fields into existing config (safe, non-destructive)
#-------------------------------------------------------------------------------------
run .venv/bin/python dpmp/merge_config.py dpmp/config_v2_example.json dpmp/config_v2.json

echo "Restarting services..."
run systemctl --user restart dpmpv2.service
run systemctl --user restart dpmpv2-nicegui.service

echo "Upgrade complete."
