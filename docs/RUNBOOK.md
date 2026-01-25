# dpmpv2 Runbook (Authoritative)

## Golden Rules
- Never break the running Umbrel services.
- Edit only via VS Code + SSH.
- Prefer smallest possible diffs.
- One change at a time.

## Service Control (user services)
Status:
systemctl --user status dpmpv2.service
systemctl --user status dpmpv2-nicegui.service
# legacy (disabled by default): systemctl --user status dpmpv2-gui.service

Restart dpmpv2:
systemctl --user restart dpmpv2.service

Restart GUI:
systemctl --user restart dpmpv2-nicegui.service
# legacy: systemctl --user restart dpmpv2-gui.service

Logs:
tail -n 50 ~/dpmp/dpmpv2_run.log
tail -n 50 ~/dpmp/dpmpv2_gui.log

## Git Workflow
git status -sb
git diff
git commit -m "message"
git push

## Do NOT Track
- config_v2.json
- logs
- backups
- one-off scripts

## Memory
Project state lives in docs/.
Update docs after every meaningful change.

## Installer (non-docker)
Install:
cd ~
rm -rf ~/dpmp
git clone https://github.com/ckryza/dpmpv2.git ~/dpmp
cd ~/dpmp
./installer/install.sh

Uninstall (keeps install dir unless you rm -rf it):
cd ~/dpmp
./installer/uninstall.sh

Notes:
- Installer fails early if ports 3351/9210/8855 are already in use.
- Installer creates dpmp/config_v2.json from dpmp/config_v2_example.json if missing.
- After install, edit Pool A/B in NiceGUI before pointing miners at DPMP.
