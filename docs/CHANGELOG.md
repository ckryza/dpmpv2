# Changelog

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
