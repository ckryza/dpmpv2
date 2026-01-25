# Known Issues / Footguns

## Rejects During Pool Switch
- Short bursts of stale / duplicate shares can occur at switch boundaries.
- Acceptable at low frequency.

## Miner Behavior Variance
- Some ASICs are sensitive to message ordering.
- Avoid unnecessary resend of notify / set_difficulty.

## Legacy GUI
- FastAPI GUI is temporary.
- Will be replaced by NiceGUI.

## Logging Growth
- dpmpv2_run.log can grow large.
- Log rotation required for long runtimes.

## Do NOT Edit Live Config Blindly
- config_v2.json changes can break running service.
- Always restart dpmpv2 after config edits.

## Queue Flush vs Pool Switch (Race Window)
### Symptom
- Messages queued for a pool can be flushed after active_pool/handshake_pool changes, sending stale-context traffic to an unintended upstream.

### Minimal Fix Strategy (Invariant-based)
- Tag queued items with intended pool (or a generation counter captured at enqueue time).
- On connect_pool() flush, only flush items whose tag matches the pool being connected; leave others queued.

### Invariant
- A queued message must only ever be sent to the pool intended at enqueue-time.

## Extranonce mismatch submit drops after pool switches (mitigated)
- Symptom: `submit_dropped_extranonce_mismatch` events right after `pool_switched`, rejects spike.
- Fix: add short grace window `SWITCH_SUBMIT_GRACE_S` and log `submit_dropped_extranonce_mismatch_grace` for submits arriving immediately after switch; still reject them locally to avoid upstream churn.
- Result: non-grace mismatch drops eliminated in test window; Nano3S reject % dropped materially.
- Commit: grace-window mitigation added in dpmpv2.py.

## Config Leakage / Secrets
- `dpmp/config_v2.json` must never be committed (contains pool URLs/wallets). It is now git-ignored.
- Keep any real configs only as local backups (e.g., `dpmp/config_backups/`) and do not ship them in a public distro repo.
- Installer uses only `dpmp/config_v2_example.json` to seed a new install.
