# DPMP Runbook (Dual-Pool Mining Proxy)

## Goal
Build a dual-pool SHA256 solo-mining Stratum proxy for UmbrelOS 1.5:
- Miner connects to DPMP and sends only worker name as "user"
- DPMP connects to two upstream pools and rewrites auth to: wallet.worker
- DPMP schedules jobs to achieve configured split ratio (e.g., 50/50)
- Full logging + Prometheus metrics for Grafana

## Hardware/OS
- UmbrelOS 1.5 mini-PC, 16GB RAM, 1TB SSD, 12 threads

## Config Contract (planned)
- config.json holds:
  - two upstream pools.

## Milestones
1) DPMP proxy fully operational (including logging)
2) Grafana dashboard for DPMP metrics

## Commands executed
(append new commands + outcomes here)


## 2026-01-13 - Step 2: Python tooling
Installed python3-venv + python3-pip, created venv at ~/dpmp/.venv.

Commands:
- sudo apt-get update
- sudo apt-get install -y python3-venv python3-pip
- python3 -m venv .venv
- source .venv/bin/activate


## 2026-01-13 - Step 3: DPMP skeleton + config
- Created dpmp/config.json (from config.example.json)
- Installed deps: prometheus-client, orjson
- Created initial dpmp/dpmp.py skeleton (downstream accepts subscribe/authorize/submit locally)
- Metrics endpoint planned on :9109


## 2026-01-13 - Step 4: DPMP listen port
Changed DPMP downstream listen port from 3333 to 3350 to avoid confusion with Miningcore defaults.


## 2026-01-13 - Step 7: Pool A proxy milestone
Replaced dpmp/dpmp.py with Pool A proxy:
- Connects to upstream Pool A
- Forwards subscribe/configure/etc
- Rewrites mining.authorize user from worker -> wallet.worker
- Forwards notify/set_difficulty to miner
- Forwards mining.submit to upstream and counts accept/reject
Added dpmp/test_miner.py for local non-ASIC testing.


## 2026-01-13 - Step 14: Dual upstream connections (mine A-only)
Updated dpmp.py:
- Connect to Pool A + Pool B
- Rewrite mining.authorize to wallet.worker for both pools
- Forward jobs/difficulty ONLY from Pool A to miner
- Forward submits ONLY to Pool A
- Read/sink Pool B traffic and log Pool B authorize result


## 2026-01-13 - Step 16: Dual-pool scheduling + job routing
Updated dpmp.py to:
- Maintain Pool A and Pool B connections
- Capture latest notify + difficulty from each
- Forward jobs to miner using weighted scheduler (poolA_weight:poolB_weight)
- Record job_id -> pool ownership when forwarded
- Route mining.submit back to correct pool by job_id
- Downstream difficulty policy: min(diffA, diffB) (default)


## 2026-01-13 - Step 18/19: Fix CPU + miner connection (event-driven scheduler)
Bug fix:
- Removed timer-based notify spam.
- Forward mining.notify only when a NEW notify arrives from a pool.
- Keep weighted A/B selection.
- Only forward downstream difficulty when it changes (min(A,B)).

DPMP STATUS — 2026-01-13 (STABLE)

Environment

Host: UmbrelOS

Python: 3.13 (venv)

Miners: BitAxe / Avalon

Pools:

Pool A: BTC (Bitcoin Core–based)

Pool B: BCH (Miningcore SOLO)

Key Fix

Downstream mining.set_difficulty and mining.set_extranonce are forwarded only from the active pool

Prevents upstream pools from poisoning miner context

Scheduler

"scheduler": {
  "mode": "ratio",
  "poolA_weight": 50,
  "poolB_weight": 50,
  "min_switch_seconds": 15
}


Miningcore VarDiff

VarDiff restored (not forced)

Verified retargets: 256 → 756 → 1256

DPMP forwards VarDiff only when BCH pool is active

BTC Pool Protection

Downstream BTC diff is clamped (A-min) to avoid low-diff contamination

BTC shares may still reject (expected)

Metrics Verification

dpmp_jobs_forwarded_total shows both pools active

dpmp_shares_accepted_total confirms BCH acceptance

Prometheus endpoint: http://127.0.0.1:9109/metrics

Status
✅ Stable
✅ No low-diff BCH rejects
✅ Dual-pool switching working
✅ Ready for long-run testing / tuning
