# dpmpv2 Runbook (Authoritative)

## Golden Rules
- Never break the running Umbrel services.
- Edit only via VS Code + SSH.
- Prefer smallest possible diffs.
- One change at a time.

## Service Control (user services)
Status:
systemctl --user status dpmpv2
systemctl --user status dpmpv2-gui

Restart dpmpv2:
systemctl --user restart dpmpv2

Restart GUI:
systemctl --user restart dpmpv2-gui

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
