# Changelog

## 3.0.6 - 2026-03-11
- startup pool correction
- implement per-submit switch timestamp
- accept-gated reject suppression
- switch failure safe pool fix
- duplicate share suppression
- grace window increase to 15 seconds
- added: manual assignment panel
- added: force_reconnect_on_en2_mismatch config option
- added: post-switch slice timer gate
- added: pool address book

## 3.0.5 - 2026-03-04
- add on/off toggle in worker table
- adjust pool switching logic
- add config A/B swap button
- fix possible blocking on startup

## 3.0.4 - 2026-02-28
- pinned-miner disconnect exemption
- fix switch-count logic for Fleet table
- disable scheduler debug log (grows fast, so update your version!)
- add 5m HR column to Pool table
- add entrypoint crash log
- modify logging options

## 3.0.3 - 2026-02-26
- finalize fleet implementation
- add Fleet table to Stats tab
- address pool/miner compatibility issues
- cosmetic updates

## 3.0.2 - 2026-02-17
- add Stats tab with Worker and Pool tables
- address pool/miner compatibility issues
- fix: no slider or auto-balancer display on 0/100 or 100/0 config ratios
- adjust hashrate allocation logic to better account for individual miner hashrate
- ratio convergence is now faster
- prep for transition to fleet management

## 3.0.1 - 2026-02-14
- can now switch between Slider and Auto-Balance with no restart required (added switch button)
- Auto-Balance times now in local time
- minor cosmetic updates

## 3.0.0 - 2026-02-11
- calculate realtime network hashrate for BTC and BCH (short-term and long-term)
- add auto-balance options to config
- add auto-balance logic to DPMP and dashboard

## 2.0.2 - 2026-02-09
- Fixes for ck-type pools and bootstrap sequence
- Add realtime hashrate allocation slider to GUI

## 2.0.1 - 2026-02-07
- Fixed mining.set_extranonce and client.reconnect issues for NerdAxe Gamma
- Fixed Braiins BM-101 initialization and handshake issues

## 2.0.0 — 2026-02-06
- Fixed reject storms during pool switches.
- Improved scheduler convergence and validation.
- Increased grace period for stale submits.
- Ensured correct single-pool behavior at 0/100 and 100/0 weights.
- Added pool failover protection.
- Added global exception handling.
- Added periodic state pruning.
