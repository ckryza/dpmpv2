#!/bin/sh
set -eu
cd /home/umbrel/dpmp
echo "===== MARK gui_restart $(date -u +%FT%TZ) =====" >> dpmpv2_run.log
pgrep -af 'dpmpv2\.py' | awk '{print $1}' | xargs -r sudo kill -9
nohup env DPMP_CONFIG=dpmp/config_v2.json .venv/bin/python -u dpmp/dpmpv2.py >> dpmpv2_run.log 2>&1 &
sleep 1
pgrep -af 'dpmpv2\.py' >/dev/null
