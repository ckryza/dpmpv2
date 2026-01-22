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
