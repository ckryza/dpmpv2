#!/bin/sh
set -eu

mkdir -p /data

# Ensure config exists (volume-mounted /data)
if [ ! -f /data/config_v2.json ]; then
  cp -a /app/dpmp/config_v2_example.json /data/config_v2.json
fi

# Ensure log files exist for GUI Log tab
: > /data/dpmpv2_run.log
: > /data/dpmpv2_gui.log

# Start DPMP (proxy + metrics) -> log to /data
python -u /app/dpmp/dpmpv2.py >> /data/dpmpv2_run.log 2>&1 &
DPMP_PID=$!

# Start NiceGUI -> log to /data
python -u /app/gui_nice/app.py >> /data/dpmpv2_gui.log 2>&1 &
GUI_PID=$!

# If either dies, exit non-zero so container restarts
while true; do
  if ! kill -0 "$DPMP_PID" 2>/dev/null; then
    echo "dpmpv2 exited" >&2
    exit 1
  fi
  if ! kill -0 "$GUI_PID" 2>/dev/null; then
    echo "nicegui exited" >&2
    exit 1
  fi
  sleep 1
done
