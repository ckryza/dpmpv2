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

## Publishing a new Umbrel build (GHCR + store repo)

1) Update dpmpv2 code (repo: ckryza/dpmpv2)
- commit + push to main

2) Build + push GHCR image (NOT docker.com)
- `docker login ghcr.io -u ckryza` (password = GitHub token/PAT)
- tag with git short SHA and also push :latest

3) Update Umbrel store repo (repo: ckryza/ckryza-umbrel-app-store)
- bump `ckryza-dpmpv2/docker-compose.yml` to the new GHCR SHA tag
- ensure `DPMP_METRICS_URL=http://127.0.0.1:9210/metrics`

4) Verify on clean Umbrel (BitNode3)
- confirm store cache updated
- uninstall/reinstall
- confirm UI says running, and metrics are reachable inside container on 9210