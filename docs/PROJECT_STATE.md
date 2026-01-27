# dpmpv2 Project State (current known-good)

## Repo
- GitHub: https://github.com/ckryza/dpmpv2
- Host working copy: /home/umbrel/dpmp
- Branch: main

## Umbrel Runtime (MUST NOT BREAK)
### dpmpv2.service (user service + linger)
- **Runs via systemd user service (auto-start).** Prefer `systemctl --user` over `nohup` during normal operation.
- Service file: /home/umbrel/.config/systemd/user/dpmpv2.service
- Control:
  - Status: `systemctl --user --no-pager --full status dpmpv2.service`
  - Restart: `systemctl --user restart dpmpv2.service`
  - Stop: `systemctl --user stop dpmpv2.service`
  - Disable autostart: `systemctl --user disable --now dpmpv2.service`
  - Enable autostart: `systemctl --user enable --now dpmpv2.service`
- Ports owned by service when running: 3351 (stratum), 9210 (metrics)
- If you must run manually for debugging: **stop the service first** to avoid `OSError: [Errno 98] address already in use`.
- Python: /home/umbrel/dpmp/.venv/bin/python
- Script: /home/umbrel/dpmp/dpmp/dpmpv2.py
- Config: DPMP_CONFIG=/home/umbrel/dpmp/dpmp/config_v2.json
- Stratum port: 3351
- Metrics port: 9210
- Logs: /home/umbrel/dpmp/dpmpv2_run.log

### dpmpv2-nicegui.service (primary GUI)
- Runs via systemd user service (auto-start)
- Service file: /home/umbrel/.config/systemd/user/dpmpv2-nicegui.service
- Python: /home/umbrel/dpmp/.venv/bin/python
- Script: /home/umbrel/dpmp/gui_nice/app.py
- Port: 8845
- Note: 8855 is the planned standard later; currently 8845 matches dpmpv2-nicegui.service
- Control:
  - Status: `systemctl --user --no-pager --full status dpmpv2-nicegui.service`
  - Restart: `systemctl --user restart dpmpv2-nicegui.service`
  - Stop: `systemctl --user stop dpmpv2-nicegui.service`
  - Disable autostart: `systemctl --user disable --now dpmpv2-nicegui.service`
  - Enable autostart: `systemctl --user enable --now dpmpv2-nicegui.service`
- Notes: This replaces the legacy FastAPI GUI.

### dpmpv2-gui.service (legacy FastAPI GUI)

- Status: **deprecated; hard-disabled (masked)**
  - We permanently prevent it from ever starting by masking it to `/dev/null`.
  - Commands used on Umbrel:
    - `systemctl --user disable --now dpmpv2-gui.service || true`
    - `mv ~/.config/systemd/user/dpmpv2-gui.service ~/.config/systemd/user/dpmpv2-gui.service.DISABLED.<timestamp>`
    - `ln -sf /dev/null ~/.config/systemd/user/dpmpv2-gui.service`
    - `systemctl --user daemon-reload`
