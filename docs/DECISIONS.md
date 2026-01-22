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
