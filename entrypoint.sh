#!/bin/sh
set -eu

mkdir -p /data

# Ensure config exists (volume-mounted /data)
if [ ! -f /data/config_v2.json ]; then
  cp -a /app/dpmp/config_v2_example.json /data/config_v2.json
fi

# Merge any new default fields into existing config (safe, non-destructive)
python3 /app/dpmp/merge_config.py /app/dpmp/config_v2_example.json /data/config_v2.json

RUN_LOG="/data/dpmpv2_run.log"
GUI_LOG="/data/dpmpv2_gui.log"

: > "$RUN_LOG"
: > "$GUI_LOG"

rotate_if_needed() {
  f="$1"
  max=$((50 * 1024 * 1024))   # 50MB
  keep=3
  sz="$(wc -c < "$f" 2>/dev/null || echo 0)"
  if [ "$sz" -gt "$max" ]; then
    i=$keep
    while [ "$i" -ge 1 ]; do
      j=$((i + 1))
      if [ -f "${f}.${i}" ]; then mv -f "${f}.${i}" "${f}.${j}"; fi
      i=$((i - 1))
    done
    cp -f "$f" "${f}.1"
    : > "$f"
  fi
}

# Start DPMP (proxy + metrics) -> log to /data
python -u /app/dpmp/dpmpv2.py >> "$RUN_LOG" 2>&1 &
DPMP_PID=$!

# Start NiceGUI -> log to /data
python -u /app/gui_nice/app.py >> "$GUI_LOG" 2>&1 &
GUI_PID=$!

# If either dies, exit non-zero so container restarts
n=0
while true; do
  if ! kill -0 "$DPMP_PID" 2>/dev/null; then
    echo "dpmpv2 exited" >&2
    exit 1
  fi
  if ! kill -0 "$GUI_PID" 2>/dev/null; then
    echo "nicegui exited" >&2
    exit 1
  fi

  n=$((n + 1))
  if [ "$n" -ge 60 ]; then
    rotate_if_needed "$RUN_LOG"
    rotate_if_needed "$GUI_LOG"
    n=0
  fi

  sleep 1
done
