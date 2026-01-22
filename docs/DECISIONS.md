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
