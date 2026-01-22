# dpmpv2 Project State (current known-good)

## Repo
- GitHub: https://github.com/ckryza/dpmpv2
- Host working copy: /home/umbrel/dpmp
- Branch: main

## Umbrel Runtime (MUST NOT BREAK)
### dpmpv2.service (user service + linger)
- Python: /home/umbrel/dpmp/.venv/bin/python
- Script: /home/umbrel/dpmp/dpmp/dpmpv2.py
- Config: DPMP_CONFIG=/home/umbrel/dpmp/dpmp/config_v2.json
- Stratum port: 3351
- Metrics port: 9210
- Logs: /home/umbrel/dpmp/dpmpv2_run.log

### dpmpv2-gui.service (legacy FastAPI GUI)
- Script: /home/umbrel/dpmp/gui/app.py
- Port: 8844
- Logs: /home/umbrel/dpmp/dpmpv2_gui.log

## URLs
- Metrics: http://192.168.0.24:9210/metrics
- GUI: http://192.168.0.24:8844/settings
