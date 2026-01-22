# Architectural Decisions

## Dual-Pool Scheduling
- Use weighted time-slice scheduling (e.g. 50/50).
- Avoid per-share switching to reduce rejects.

## Downstream Difficulty Handling
- Forward upstream difficulty only for the active pool.
- Never poison the inactive pool with diff updates.

## Extranonce Strategy
- Forward extranonce from upstream to miner.
- Change-detect to avoid unnecessary resends.

## Job Routing
- Track job ownership by (pool, job_id).
- Route submits back to the originating pool.

## Logging
- Structured JSON logs only.
- Prefer minimal verbosity in steady-state.

## Config
- config_v2.json is runtime-only and not tracked.
- Docs are the source of truth for project memory.

## Code Review Summary (Initial dpmpv2.py)
### Core Responsibilities
- Acts as a Stratum v1 dual-pool proxy (miners ↔ Pool A/B).
- Manages handshake/session forwarding and share routing.
- Schedules mining.notify jobs based on configured weights.
- Routes mining.submit back to the originating pool.
- Enforces downstream difficulty policy.
- Exposes Prometheus metrics and JSON logs.

### Critical State Across Pool Switches
- job_id → pool ownership mapping.
- Latest mining.notify per pool.
- Active downstream difficulty value.
- Extranonce and session setup state.

### Primary Risk Areas
1. Job ID mapping races during pool switches.
2. Missing or malformed handshake/setup forwarding.
3. Difficulty desynchronization causing invalid shares.

## Code Review Summary (dpmpv2.py: config + IO + scheduler + ProxySession)
### Responsibilities
- Loads config with compatibility handling (logging/listen/metrics/pools/weights).
- Async Stratum line IO helpers (iter_lines/write_line) with wire logging.
- Extracts jobid from notify/submit.
- RatioScheduler for weighted switching.
- ProxySession owns per-miner session state: pool connections, queues, job routing, extranonce/diff tracking, internal bootstrap IDs.

### Key Invariants
- Job ownership mapping must remain correct for submit routing.
- Extranonce + difficulty values sent downstream must match active context.
- Internal request IDs for bootstrap must never collide with normal IDs.
- active_pool / last_forwarded_jobid must reflect what miner most recently saw.

### Main Risk Windows
- Queue flush timing vs session state changes.
- Out-of-order notify/submit mapping if async tasks interleave.
- Only downstream setup is locked; other shared mappings rely on async discipline.

## Code Review Summary (Handlers: notify/diff/extranonce/submit)
### Handler Flows
- maybe_send_downstream_extranonce(): change-detect extranonce and send mining.set_extranonce for active context; updates last_downstream_extranonce_* tracking.
- maybe_send_downstream_diff(): change-detect diff and send mining.set_difficulty (per-pool tracking); often serialized with downstream_setup_lock.
- resend_active_notify_clean(): re-sends latest job with clean_jobs=true after setup changes.
- miner_to_pools():
  - mining.configure/subscribe/authorize: forwarded to handshake pool; authorize also mirrored to other pool; then sync downstream extranonce/diff.
  - mining.submit: routes shares using job mapping + last_forwarded heuristics; rejects/drops if jid unknown or extranonce context mismatch (safer than misroute).

### Ordering Assumptions
- Downstream extranonce/diff should be synced before forwarding jobs and before accepts.
- resend_active_notify_clean is used to reduce mismatch windows after setup changes.

### Primary Routing Pitfalls
- Stale job mapping or last_forwarded state near pool switches.
- Extranonce context mismatch (expected drop/reject behavior if out of sync).
- Any lag between setup change and notify forwarding can create a brief reject window.
