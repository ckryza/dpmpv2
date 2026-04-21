#!/usr/bin/env python3
"""
DPMP Fleet Module - Fleet management, scheduling, and worker statistics.
Copyright (c) 2025-2026 Christopher Kryza. Subject to the MIT License.

This module contains:
  - Fleet state tracking (registration, ratio, switching coordination)
  - Health scoring (EWMA per-miner health with persistence)
  - Worker statistics (share tracking, hashrate estimation, best shares)
  - Reimplementation of exponential decay hashrate (ckpool/Bassin-type EWMA algorithm)
  - Rolling ratio window (accepted-difficulty based ratio measurement)
  - Global assigner (bin-packing + time-slicing fleet optimizer)
  - En2 compatibility helpers (extranonce strike/hint tracking)
  - Share difficulty calculation (true difficulty from nonce/header)

Dependencies are injected via init() -- this module never imports dpmpv2.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
import os
import hashlib
import threading
from collections import deque
from typing import Any


# ---------------------------------------------------------------------------
# Dependency injection -- wired up by dpmpv2.main() calling init()
# ---------------------------------------------------------------------------
_log_fn = None              # log(event, **kwargs) from dpmpv2
_read_weights_fn = None     # read_weight_override() from dpmpv2
_read_oracle_fn = None      # read_oracle_mode(config_auto_balance) from dpmpv2
_health_gauge = None        # Prometheus MINER_HEALTH Gauge from dpmpv2
_get_paused_fn = None       # get_paused_miners() -> set from dpmpv2


def init(*, log_fn, read_weights_fn, read_oracle_fn, health_gauge,
         worker_stats_path, best_shares_path, fleet_health_path,
         fleet_metrics_path, scheduler_diag_path, get_paused_fn=None,
         manual_mode_path=None, pinned_assignments_path=None):
    """Wire up external dependencies. Called once from dpmpv2.main().

    Args:
        log_fn:                  log(event, **kwargs) function
        read_weights_fn:         read_weight_override() -> tuple | None
        read_oracle_fn:          read_oracle_mode(config_auto_balance) -> bool
        health_gauge:            Prometheus Gauge for MINER_HEALTH
        worker_stats_path:       path to worker_stats.json
        best_shares_path:        path to best_shares.json
        fleet_health_path:       path to fleet_health.json
        fleet_metrics_path:      path to fleet_metrics.json
        scheduler_diag_path:     path to scheduler_diag.csv
        get_paused_fn:           get_paused_miners() -> set of paused worker names
        manual_mode_path:        path to manual_mode.json
        pinned_assignments_path: path to pinned_assignments.json
    """
    global _log_fn, _read_weights_fn, _read_oracle_fn, _health_gauge
    global WORKER_STATS_PATH, BEST_SHARES_PATH, FLEET_HEALTH_PATH
    global FLEET_METRICS_PATH, SCHEDULER_DIAG_PATH, _get_paused_fn
    global MANUAL_MODE_PATH, PINNED_ASSIGNMENTS_PATH
    _log_fn = log_fn
    _read_weights_fn = read_weights_fn
    _read_oracle_fn = read_oracle_fn
    _health_gauge = health_gauge
    _get_paused_fn = get_paused_fn
    WORKER_STATS_PATH = worker_stats_path
    BEST_SHARES_PATH = best_shares_path
    FLEET_HEALTH_PATH = fleet_health_path
    FLEET_METRICS_PATH = fleet_metrics_path
    SCHEDULER_DIAG_PATH = scheduler_diag_path
    MANUAL_MODE_PATH = manual_mode_path
    PINNED_ASSIGNMENTS_PATH = pinned_assignments_path

def init_pool_failover(seconds: float) -> None:
    """Set the pool failover timeout. Called from main() after config is loaded.

    Args:
        seconds: How long a pool must be job-silent before declaring unresponsive.
    """
    global _POOL_NO_JOB_FAILOVER_S
    _POOL_NO_JOB_FAILOVER_S = max(30.0, min(600.0, float(seconds)))
    log("pool_failover_timeout_set", seconds=_POOL_NO_JOB_FAILOVER_S)


def log(event, **kw):
    """Internal log wrapper -- delegates to injected log function."""
    if _log_fn:
        _log_fn(event, **kw)


def read_weight_override():
    """Internal wrapper -- delegates to injected read_weight_override."""
    if _read_weights_fn:
        return _read_weights_fn()
    return None


def read_oracle_mode(config_auto_balance=False):
    """Internal wrapper -- delegates to injected read_oracle_mode."""
    if _read_oracle_fn:
        return _read_oracle_fn(config_auto_balance)
    return config_auto_balance


# ---------------------------------------------------------------------------
# Manual Mode helpers
# ---------------------------------------------------------------------------
# Manual mode is a third scheduler mode alongside Slider and Oracle.
# When active, every miner is statically assigned to Pool A or B based on
# the user's choices stored in pinned_assignments.json.  The bin-packing
# algorithm in _compute_assignments() is bypassed entirely.
#
# manual_mode.json  -- {"manual_active": true/false}
# pinned_assignments.json -- {"WorkerName": "A", "WorkerName2": "B", ...}
#   Only miners assigned to Pool B are stored; Pool A is the default.
# ---------------------------------------------------------------------------

def is_manual_mode_active() -> bool:
    """Return True if Manual mode is currently active.

    Reads manual_mode.json from disk on every call so the scheduler
    picks up mode changes without a restart.  Returns False if the
    file is missing or unreadable (safe default: normal scheduling).
    """
    if not MANUAL_MODE_PATH:
        return False
    try:
        with open(MANUAL_MODE_PATH, "r") as f:
            obj = json.load(f)
        return bool(obj.get("manual_active", False))
    except Exception:
        return False


def read_pinned_assignments() -> dict:
    """Read pinned_assignments.json and return {worker_name: pool_key}.

    Pool A is the default -- only B assignments are stored in the file,
    so any worker not listed defaults to 'A'.

    Returns an empty dict if the file is missing (all miners default to A).

    Example return value:
        {"BM101": "B", "BitAxe2": "B"}
    """
    if not PINNED_ASSIGNMENTS_PATH:
        return {}
    try:
        with open(PINNED_ASSIGNMENTS_PATH, "r") as f:
            obj = json.load(f)
        # Validate: only keep entries with pool "A" or "B"
        return {k: v for k, v in obj.items() if v in ("A", "B")}
    except Exception:
        return {}


def write_pinned_assignment(worker_name: str, pool: str) -> None:
    """Update one miner's assignment in pinned_assignments.json.

    Reads the current file, updates the single entry, and writes back
    atomically (tmp + os.replace).  Pool A assignments are removed from
    the file (A is the default), keeping the file minimal.

    Args:
        worker_name: The miner's worker name (e.g. "AvalonQ")
        pool:        "A" or "B"
    """
    if not PINNED_ASSIGNMENTS_PATH:
        return
    try:
        assignments = read_pinned_assignments()
        if pool == "B":
            assignments[worker_name] = "B"
        else:
            # A is default -- remove from file so it stays minimal
            assignments.pop(worker_name, None)
        tmp = PINNED_ASSIGNMENTS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(assignments, f, indent=2)
        os.replace(tmp, PINNED_ASSIGNMENTS_PATH)
        log("pinned_assignment_written", worker=worker_name, pool=pool)
    except Exception as e:
        log("pinned_assignment_write_error", worker=worker_name, err=str(e))


SWITCH_SUBMIT_GRACE_S = 15.0   # BASE grace window for VarDiff suppression (seconds).
                              # The actual per-miner grace window is computed dynamically
                              # by miner_grace_window_s() which adds time proportional
                              # to the miner's hashrate.  This base covers network latency
                              # + pool processing for small miners (~1-5 TH/s).
GRACE_HASHRATE_SCALE = 20.0   # Each 20 TH/s of miner hashrate adds 1 second to grace.
                              # Examples:   1 TH/s -> 6.1s,   5 TH/s -> 6.3s,
                              #            80 TH/s -> 10.0s, 200 TH/s -> 16.0s,
                              #           400 TH/s -> 26.0s
# Path to optional weights override file (written by GUI slider, polled by scheduler)
# Path to oracle mode file (written by GUI switch button, polled by oracle task)
MAX_CONVERGE_DEVIATION = 0.05 # default max deviation (5%) to trigger urgent pool switch


def miner_grace_window_s(hashrate_ths: float) -> float:
    """Calculate the VarDiff suppression grace window for a miner.

    Higher hashrate miners submit shares faster at low difficulty after a
    pool switch.  The pool's VarDiff takes longer to ramp because it receives
    a flood of low-diff shares.  We scale the grace window proportionally.

    Args:
        hashrate_ths: Miner's estimated hashrate in TH/s.

    Returns:
        Grace window in seconds.

    Examples:
        miner_grace_window_s(1.0)   -> 6.1s   (BitAxe)
        miner_grace_window_s(5.0)   -> 6.3s   (Nano3S)
        miner_grace_window_s(80.0)  -> 10.0s  (AvalonQ)
        miner_grace_window_s(200.0) -> 16.0s  (large ASIC)
        miner_grace_window_s(400.0) -> 26.0s  (very large ASIC)
    """
    return SWITCH_SUBMIT_GRACE_S + (hashrate_ths / GRACE_HASHRATE_SCALE)


# Global fleet coordination: track which pool each miner session is on,
# weighted by each miner's observed hashrate (share difficulty).
# The scheduler uses the fleet-wide hashrate distribution to decide
# switching.  This prevents herding AND handles mixed-hashrate fleets:
# an 80 TH/s miner counts as ~80x more than a 1 TH/s miner.
_fleet_lock = threading.Lock()
_fleet_pool: dict[str, str] = {}      # sid_str -> current pool ("A" or "B")
_fleet_weight: dict[str, float] = {}  # sid_str -> hashrate weight (share difficulty)
_fleet_shareA: dict[str, float] = {}  # sid_str -> current per-session shareA ratio
_fleet_sid_worker: dict[str, str] = {}  # sid_str -> worker name (for fleet state builder)
_fleet_switch_count: dict[str, int] = {}   # sid_str -> cumulative pool switch count
_fleet_last_switch: dict[str, float] = {}  # sid_str -> monotonic time of last pool switch
_fleet_worker_switch_carry: dict[str, int] = {}  # worker_name -> switch count carried from previous session
_fleet_worker_en1_mismatch_carry: dict[str, int] = {}  # worker_name -> en1 mismatch count carried from previous session
_fleet_last_switch_mono: float = 0.0
_FLEET_SWITCH_COOLDOWN_S = 3.0  # seconds between consecutive miner switches
_fleet_next_pool_idx: int = 0  # round-robin counter for initial pool assignment

# ---------------------------------------------------------------------------
# Pool unresponsive tracking
# ---------------------------------------------------------------------------
# Tracks pools that are TCP-connected but not sending mining.notify jobs.
# This happens when an upstream node (e.g. Bitcoin Core) restarts and the
# pool software loses its block template but keeps accepting stratum
# connections.  Without this, miners get stuck unable to switch for hours.
#
# When a pool has had no cached job for _POOL_NO_JOB_FAILOVER_S seconds,
# it is declared unresponsive.  The assigner treats it as unavailable and
# moves all miners to the working pool.  When a job finally arrives, the
# pool is automatically recovered and normal operation resumes.
# ---------------------------------------------------------------------------
_pool_no_job_first_seen: dict[str, float] = {"A": 0.0, "B": 0.0}
_pool_healthy_since:     dict[str, float] = {"A": 0.0, "B": 0.0}
_POOL_HEALTHY_MIN_S = 60.0   # pool must deliver jobs for 60s before no-job clock clears
_pool_unresponsive: dict[str, bool] = {"A": False, "B": False}
_POOL_NO_JOB_FAILOVER_S = 90.0   # overridden by init() from config; default 90s
_pool_flap_count:        dict[str, int]   = {"A": 0, "B": 0}
_pool_flap_window_start: dict[str, float] = {"A": 0.0, "B": 0.0}
_POOL_FLAP_MAX      = 3      # declare unresponsive after 3 flaps...
_POOL_FLAP_WINDOW_S = 600.0  # ...within a 10-minute window

# ---------------------------------------------------------------------------
# Rolling ratio window (Phase 1a of Scheduler v3)
# ---------------------------------------------------------------------------
# Instead of decay-based counters, track a fixed-size rolling window of
# accepted share difficulty.  Each entry is (timestamp, pool_key, difficulty).
# get_actual_ratio() sums difficulty per pool over the last N seconds to
# produce a clean, predictable A/B ratio measurement.
#
# This runs IN PARALLEL with the existing v2 scheduler -- it does NOT change
# any scheduling decisions.  The new RATIO_WINDOW Prometheus gauge lets us
# compare the rolling-window ratio against the existing SCHEDULER_SHARE
# gauge to validate they converge to the same values.
# ---------------------------------------------------------------------------
_ratio_window_lock = threading.Lock()
_ratio_window: deque = deque(maxlen=10000)
_RATIO_WINDOW_SECONDS = 600.0  # 10-minute rolling window

# ---------------------------------------------------------------------------
# Miner health scoring (Phase 1b of Scheduler v3)
# ---------------------------------------------------------------------------
# Each miner gets a score from 0.1 (unreliable) to 1.0 (perfect).
# The v3 global assigner will use this to decide which miners are safe to
# time-slice aggressively vs. which should stay on a fixed pool.
#
# Keyed by worker_name (not session ID) so health persists across reconnects.
# Persisted to fleet_health.json every 30 seconds and loaded on startup.
#
# Like the rolling window, this is purely additive in Phase 1 -- the existing
# v2 scheduler does not read health scores.  They are exposed via Prometheus
# (dpmp_miner_health gauge) so we can observe them before they drive decisions.
# ---------------------------------------------------------------------------
_fleet_health_lock = threading.Lock()
_fleet_health: dict[str, float] = {}   # worker_name -> score (0.1 to 1.0)
FLEET_HEALTH_PATH: str | None = None   # set in main(), e.g. /data/fleet_health.json
FLEET_METRICS_PATH: str | None = None  # set in main(), e.g. /data/fleet_metrics.json
SCHEDULER_DIAG_PATH: str | None = None  # set in main(), e.g. /data/scheduler_diag.csv
MANUAL_MODE_PATH: str | None = None      # set in init(), e.g. /data/manual_mode.json
PINNED_ASSIGNMENTS_PATH: str | None = None  # set in init(), e.g. /data/pinned_assignments.json
_HEALTH_ALPHA = 0.1  # EWMA smoothing: each event shifts score by 10% of delta

# ---------------------------------------------------------------------------
# Per-worker stats tracking (for Stats tab in GUI)
# ---------------------------------------------------------------------------
# Unlike Prometheus metrics (which have label cardinality issues with dynamic
# worker names), this is a plain dict that we periodically dump to a JSON file.
# app.py reads the file directly -- no extra HTTP server needed.
#
# Structure: worker_stats[worker_name] = {
#   "accepted": int,        # total accepted shares
#   "rejected": int,        # total rejected shares
#   "difficulty": float,    # current downstream difficulty
#   "last_seen": float,     # time.time() of last accepted share
#   "share_log": [(ts, diff), ...],  # rolling buffer for hashrate calc
# }
worker_stats_lock = threading.Lock()
worker_stats: dict[str, dict] = {}

# Per-pool latency tracking: time from submit -> result (round-trip)
# _pool_submit_time[msg_id] = (pool_key, monotonic_timestamp)
_pool_submit_time_lock = threading.Lock()
_pool_submit_time: dict[Any, tuple[str, float]] = {}
_pool_latency: dict[str, float] = {"A": 0.0, "B": 0.0}  # latest latency in ms

# Path to worker stats JSON file (set in main())
WORKER_STATS_PATH: str | None = None
# Path to best shares JSON file (persists across restarts)
BEST_SHARES_PATH: str | None = None
# In-memory best share per worker (loaded from file on startup)
_best_shares: dict[str, float] = {}
_best_shares_lock = threading.Lock()

# Maximum share_log entries per worker (covers 24hr at ~1 share/sec = 86400,
# but most miners submit far less frequently; 5000 is plenty for 24hr window)
_SHARE_LOG_MAX = 5000


# -----------------------------------------------------------------------------
# Reimplementation of exponential decay hashrate (ckpool/Bassin-type algorithm)
# -----------------------------------------------------------------------------
#
# How it works:
#   - Each worker maintains "dsps" (diff shares per second) values at
#     multiple time windows: 1m, 5m, 60m, 24h.
#   - On every accepted share, we call decay_time() which:
#     1. Computes how many seconds elapsed since the last update
#     2. Decays the old dsps value by an exponential factor based on
#        the time elapsed and the window size
#     3. Adds the new share's difficulty
#   - To convert dsps to hashrate: hashrate_H_per_s = dsps * 2^32
#
# Key advantages over windowed/ring-buffer approaches:
#   - Updates instantly on every share (no 5-second polling delay)
#   - Naturally smooth (exponential decay has no window boundary artifacts)
#   - Handles gaps gracefully (idle time decays the rate down smoothly)
#   - Very little memory (4 floats + 1 timestamp per worker)
#
# The decay formula
#   dsps  = dsps * ratio + diff
#
# Where:
#   interval = window size in seconds (60, 300, 3600, 86400)
#   tdiff    = seconds since last decay update
#   diff     = share difficulty (0 for idle decay, actual diff for share)
# ---------------------------------------------------------------------------

# Window sizes in seconds
_DECAY_MIN1 = 60.0
_DECAY_MIN5 = 300.0
_DECAY_HOUR = 3600.0
_DECAY_DAY = 86400.0

# 2^32 -- multiply dsps by this to get hashes/second
_NONCES = 4294967296.0

# Per-worker decay state.
# _decay_state[worker_name] = {
#   "dsps1":   float,  -- diff shares per second, 1-minute EWMA
#   "dsps5":   float,  -- diff shares per second, 5-minute EWMA
#   "dsps60":  float,  -- diff shares per second, 60-minute EWMA
#   "dsps1440": float, -- diff shares per second, 24-hour EWMA
#   "last_decay": float, -- time.time() of last decay update
# }
_decay_state: dict[str, dict] = {}
# Per-worker, per-pool decay state for the 5-minute EWMA only.
# Used by the assigner to get a stable hashrate estimate for time-slicing
# miners that switch between pools with very different difficulty levels.
# Keyed by (worker_name, pool_key) e.g. ("AvalonQ", "A").
# Each entry: {"dsps5": float, "last_decay": float}
_decay_state_pool: dict[tuple, dict] = {}

def _decay_time(dsps: float, diff: float, tdiff: float, interval: float) -> float:
    """Apply an implementation of ckpool's exponential decay to a dsps value.
    The actual formula uses exp() for proper exponential decay:

        dexp  = tdiff / interval
        fprop = 1.0 - 1.0 / exp(dexp)
        ftotal = 1.0 + fprop
        dsps += (diff / tdiff * fprop)

        dsps /= ftotal

    This normalizes the added difficulty by the elapsed time (diff/tdiff
    gives the instantaneous rate), scales it by the decay proportion,
    then divides the total by ftotal to maintain proper averaging.

    Args:
        dsps:     Current diff-shares-per-second value
        diff:     Share difficulty to add (0.0 for idle decay)
        tdiff:    Seconds elapsed since last decay update
        interval: Window size in seconds (e.g. 60.0 for 1-minute)

    Returns:
        Updated dsps value after decay + addition.
    """
    if tdiff <= 0.0:
        return dsps

    # Bound tdiff to realistic lower limit
    expected_rate = max(dsps, 1e-9)
    #min_tdiff = diff / (expected_rate * 3.0) #dynamic approach
    min_tdiff = 0.02   # 20 ms #fixed approach
    if tdiff < min_tdiff:
        tdiff = min_tdiff

    dexp = tdiff / interval
    # Sanity bounds to prevent overflow or underflow in exp():
    if dexp > 36.0:
        dexp = 36.0
    fprop = 1.0 - 1.0 / math.exp(dexp)
    ftotal = 1.0 + fprop
    dsps += (diff / tdiff * fprop)

    dsps /= ftotal
    # Prevent meaningless tiny numbers
    if dsps < 2e-16:
        dsps = 0.0
    return dsps


def _decay_worker(worker: str, diff: float, now: float) -> None:
    """Update all decay windows for a worker.

    Called on every accepted share (with diff = share difficulty)
    and periodically for idle decay (with diff = 0.0).

    Args:
        worker: Worker name (e.g. "AvalonQ")
        diff:   Share difficulty (0.0 for idle decay)
        now:    Current time (time.time())
    """
    state = _decay_state.get(worker)
    if state is None:
        # First time seeing this worker -- try to warm-start from share_log.
        # This replays recent shares through the decay function so the EWMA
        # starts at the correct value instead of ramping up from zero.
        state = _decay_seed_from_share_log(worker, now)

    tdiff = now - state["last_decay"]

    # Sanity bounds
    # - Floor at 0.001s to prevent division spikes when shares arrive
    #   nearly simultaneously (e.g. batch TCP reads from pool).
    # - Cap at 86400s to handle clock jumps or process suspension.
    if tdiff < 0.001:
        tdiff = 0.001
    elif tdiff > 86400.0:
        tdiff = 86400.0

    state["dsps1"] = _decay_time(state["dsps1"], diff, tdiff, _DECAY_MIN1)
    state["dsps5"] = _decay_time(state["dsps5"], diff, tdiff, _DECAY_MIN5)
    state["dsps60"] = _decay_time(state["dsps60"], diff, tdiff, _DECAY_HOUR)
    state["dsps1440"] = _decay_time(state["dsps1440"], diff, tdiff, _DECAY_DAY)
    state["last_decay"] = now


def _decay_worker_pool(worker: str, pool_key: str, diff: float, now: float) -> None:
    """Update the per-pool 5-minute EWMA for a worker on a specific pool.

    Called on every accepted share alongside _decay_worker().  Maintains
    a separate EWMA for each (worker, pool) pair so that the assigner can
    sum Pool A + Pool B estimates to get a stable total hashrate for
    time-slicing miners.

    Without this, a miner switching between pools with very different
    difficulty levels (e.g. AvalonQ: Pool A diff ~97K, Pool B diff ~5K)
    causes the combined EWMA to oscillate with every switch cycle, making
    slice_frac calculations unstable even though the miner's actual
    hashrate is constant.

    Args:
        worker:   Worker name (e.g. "AvalonQ")
        pool_key: Pool this share was accepted on ("A" or "B")
        diff:     Share difficulty
        now:      Current time (time.time())
    """
    if not pool_key:
        return
    key = (worker, pool_key)
    state = _decay_state_pool.get(key)
    if state is None:
        state = {"dsps5": 0.0, "last_decay": now}
        _decay_state_pool[key] = state

    tdiff = now - state["last_decay"]
    if tdiff < 0.001:
        tdiff = 0.001
    elif tdiff > 86400.0:
        tdiff = 86400.0

    state["dsps5"] = _decay_time(state["dsps5"], diff, tdiff, _DECAY_MIN5)
    state["last_decay"] = now


def _decay_get_hashrate_pool_sum(worker: str) -> float:
    """Return stable total hashrate (H/s) by summing per-pool 5m EWMAs.

    For miners that stay on one pool, this returns that pool's EWMA
    (same as the combined EWMA).  For time-slicing miners, this sums
    the Pool A and Pool B EWMAs independently, giving a stable total
    that does not oscillate with pool switches.

    Returns 0.0 if no per-pool data exists yet (falls back to combined
    EWMA in _fleet_state_build).
    """
    total = 0.0
    for pool_key in ("A", "B"):
        key = (worker, pool_key)
        state = _decay_state_pool.get(key)
        if state:
            total += state["dsps5"] * _NONCES
    return total


def _decay_seed_from_share_log(worker: str, now: float) -> dict:
    """Warm-start decay state by replaying the worker's share_log.

    When DPMP restarts, miners reconnect and start submitting shares.
    Without seeding, the EWMA starts at zero and takes minutes to
    converge.  By replaying the stored share_log (which persists in
    memory across GUI reloads and is populated by shares arriving
    since DPMP started), we can give the decay system a head start.

    This is called once per worker on first access of their decay state.

    Args:
        worker: Worker name (e.g. "AvalonQ")
        now:    Current time (time.time())

    Returns:
        The newly created and seeded decay state dict.
    """
    state = {
        "dsps1": 0.0,
        "dsps5": 0.0,
        "dsps60": 0.0,
        "dsps1440": 0.0,
        "last_decay": now,
    }
    _decay_state[worker] = state

    # Get share_log under lock
    share_log = []
    with worker_stats_lock:
        ws = worker_stats.get(worker)
        if ws:
            share_log = list(ws.get("share_log", []))

    if len(share_log) < 2:
        # Not enough shares to seed -- will ramp up from zero normally.
        return state

    # Replay shares in chronological order through the decay function.
    # Set last_decay to just before the first share so tdiff is correct.
    state["last_decay"] = share_log[0][0]

    for ts, diff, _pool_key in share_log:
        tdiff = ts - state["last_decay"]
        if tdiff < 0.001:
            tdiff = 0.001
        elif tdiff > 86400.0:
            tdiff = 86400.0
        state["dsps1"] = _decay_time(state["dsps1"], diff, tdiff, _DECAY_MIN1)
        state["dsps5"] = _decay_time(state["dsps5"], diff, tdiff, _DECAY_MIN5)
        state["dsps60"] = _decay_time(state["dsps60"], diff, tdiff, _DECAY_HOUR)
        state["dsps1440"] = _decay_time(state["dsps1440"], diff, tdiff, _DECAY_DAY)
        state["last_decay"] = ts

        # Also seed the per-pool state for this share's pool.
        # This warm-starts the per-pool EWMAs so they don't have to
        # ramp up from zero after every restart.
        if _pool_key:
            pool_key_state = _decay_state_pool.get((worker, _pool_key))
            if pool_key_state is None:
                pool_key_state = {"dsps5": 0.0, "dsps60": 0.0, "last_decay": ts}
                _decay_state_pool[(worker, _pool_key)] = pool_key_state
            pool_tdiff = ts - pool_key_state["last_decay"]
            if pool_tdiff < 0.001:
                pool_tdiff = 0.001
            elif pool_tdiff > 86400.0:
                pool_tdiff = 86400.0
            pool_key_state["dsps5"] = _decay_time(
                pool_key_state["dsps5"], diff, pool_tdiff, _DECAY_MIN5)
            pool_key_state["last_decay"] = ts

    # Apply idle decay from last share to now (so we don't show stale
    # high values if the miner has been quiet for a while)
    tdiff = now - state["last_decay"]
    if tdiff > 0.5:  # Only bother if significant time gap
        state["dsps1"] = _decay_time(state["dsps1"], 0.0, tdiff, _DECAY_MIN1)
        state["dsps5"] = _decay_time(state["dsps5"], 0.0, tdiff, _DECAY_MIN5)
        state["dsps60"] = _decay_time(state["dsps60"], 0.0, tdiff, _DECAY_HOUR)
        state["dsps1440"] = _decay_time(state["dsps1440"], 0.0, tdiff, _DECAY_DAY)
        state["last_decay"] = now

    log("decay_seeded_from_share_log", worker=worker,
        shares_replayed=len(share_log),
        hr_1m=round(state["dsps1"] * _NONCES / 1e12, 2),
        hr_5m=round(state["dsps5"] * _NONCES / 1e12, 2))

    return state


def _decay_get_hashrates(worker: str) -> tuple[float, float, float, float]:
    """Read the current hashrate values for a worker from decay state.

    If no decay state exists yet but the worker has shares in the
    share_log, seeds the decay state by replaying those shares first.

    Returns:
        (hr_1m, hr_5m, hr_60m, hr_24h) in H/s.
        Returns (0, 0, 0, 0) if no data.
    """
    state = _decay_state.get(worker)
    if state is None:
        # Try to seed from share_log (e.g. GUI read before first
        # share arrived after restart, or fleet builder polling).
        state = _decay_seed_from_share_log(worker, time.time())
        # If still empty after seeding, return zeros.
        if state["dsps1"] == 0.0 and state["dsps5"] == 0.0:
            return (0.0, 0.0, 0.0, 0.0)

    # dsps * 2^32 = hashes per second
    return (
        state["dsps1"] * _NONCES,
        state["dsps5"] * _NONCES,
        state["dsps60"] * _NONCES,
        state["dsps1440"] * _NONCES,
    )


def _decay_idle_all(now: float) -> None:
    """Apply idle decay to all workers.

    Called periodically (every 5 seconds from the write loop) to ensure
    hashrate decays toward zero for miners that stop submitting shares.
    Without this, a miner that disconnects would show its last hashrate
    forever.  ckpool does something similar in its stats loop.

    Args:
        now: Current time (time.time())
    """
    for worker in list(_decay_state.keys()):
        _decay_worker(worker, 0.0, now)

    # Idle-decay per-pool states only when the miner is genuinely idle
    # (no shares on ANY pool recently).  If the miner is actively switching
    # between pools, we must NOT decay the inactive pool's EWMA -- it holds
    # the hashrate estimate for when the miner returns to that pool.
    # We use the combined decay state's last_decay as a proxy for "miner
    # is alive" -- if it received a share in the last 120 seconds, skip
    # idle decay on all its per-pool states.

    for key in list(_decay_state_pool.keys()):
        worker_name = key[0]
        state = _decay_state_pool[key]
        combined = _decay_state.get(worker_name)
        if combined is not None:
            combined_idle = now - combined["last_decay"]
            if combined_idle < 120.0:
                # Worker is actively submitting shares on some pool.
                # Only apply idle decay if this specific pool's EWMA is
                # also recent -- meaning the miner is currently on this
                # pool and we need to fill gaps between shares.
                # If this pool's last_decay is old, the miner has switched
                # away -- hold the EWMA steady so it remembers the hashrate
                # for when the miner returns.
                pool_idle = now - state["last_decay"]
                if pool_idle < 120.0:
                    # Miner is on this pool -- apply gap-filling idle decay
                    tdiff = pool_idle
                    if tdiff < 0.001:
                        tdiff = 0.001
                    state["dsps5"] = _decay_time(state["dsps5"], 0.0, tdiff, _DECAY_MIN5)
                    state["last_decay"] = now
                # else: miner switched away -- hold EWMA steady
                continue
        # Worker is genuinely offline -- decay toward zero
        tdiff = now - state["last_decay"]
        if tdiff < 0.001:
            tdiff = 0.001
        elif tdiff > 86400.0:
            tdiff = 86400.0
        state["dsps5"] = _decay_time(state["dsps5"], 0.0, tdiff, _DECAY_MIN5)
        state["last_decay"] = now

def load_best_shares() -> None:
    """Load best shares from JSON file on startup."""
    global _best_shares
    if not BEST_SHARES_PATH:
        return
    try:
        if os.path.isfile(BEST_SHARES_PATH):
            with open(BEST_SHARES_PATH, "r") as f:
                data = json.loads(f.read())
            if isinstance(data, dict):
                with _best_shares_lock:
                    _best_shares = {k: float(v) for k, v in data.items()}
                log("best_shares_loaded", count=len(_best_shares))
    except Exception as e:
        log("best_shares_load_error", err=str(e))


def _save_best_shares() -> None:
    """Write best shares to JSON file (called periodically, not on every share)."""
    if not BEST_SHARES_PATH:
        return
    try:
        with _best_shares_lock:
            snapshot = dict(_best_shares)
        tmp = BEST_SHARES_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write(json.dumps(snapshot, indent=2))
        os.replace(tmp, BEST_SHARES_PATH)
    except Exception as e:
        log("best_shares_save_error", err=str(e))


# ---------------------------------------------------------------------------
# True share difficulty calculation from block header hash
# ---------------------------------------------------------------------------
# Bitcoin difficulty 1 target: 0x00000000FFFF * 2^208
# This is the "pool difficulty" standard (pdiff), same as what pools display.
_DIFF1_TARGET = 0x00000000FFFF0000000000000000000000000000000000000000000000000000


def calc_share_difficulty(
    version_hex: str,
    prevhash_hex: str,
    coinb1_hex: str,
    coinb2_hex: str,
    merkle_branches: list,
    nbits_hex: str,
    ntime_hex: str,
    extranonce1_hex: str,
    extranonce2_hex: str,
    nonce_hex: str,
    versionbits_hex: str | None = None,
) -> float:
    """Calculate the true difficulty of a submitted share.

    Reconstructs the 80-byte block header from mining.notify params +
    mining.submit params, performs SHA256d, and computes the pool difficulty
    (pdiff) from the resulting hash.

    This gives the EXACT same difficulty value you'd see on a pool dashboard
    or miner display for a given share.

    Args:
        version_hex:     Block version from mining.notify (e.g. "20000000")
        prevhash_hex:    Previous block hash from mining.notify (64 hex chars)
        coinb1_hex:      Coinbase part 1 from mining.notify
        coinb2_hex:      Coinbase part 2 from mining.notify
        merkle_branches: List of merkle branch hashes from mining.notify
        nbits_hex:       Compact target from mining.notify (e.g. "170cf4e3")
        ntime_hex:       Block time from mining.submit (e.g. "65a7b3c1")
        extranonce1_hex: Extranonce1 from pool subscribe response
        extranonce2_hex: Extranonce2 from mining.submit
        nonce_hex:       Nonce from mining.submit (e.g. "1a2b3c4d")
        versionbits_hex: Optional version-rolling mask from mining.submit

    Returns:
        Share difficulty as a float (e.g., 12345.67).
        Returns 0.0 if calculation fails for any reason.

    Example:
        If the SHA256d hash has 40 leading zero bits, the difficulty is
        approximately 2^40 / 2^32 = 256.  A hash with more leading zeros
        means higher difficulty.
    """
    try:
        # Step 1: Build the coinbase transaction
        # coinbase = coinb1 + extranonce1 + extranonce2 + coinb2
        coinbase_hex = coinb1_hex + extranonce1_hex + extranonce2_hex + coinb2_hex
        coinbase_bin = bytes.fromhex(coinbase_hex)

        # Step 2: Hash the coinbase (SHA256d)
        coinbase_hash = hashlib.sha256(
            hashlib.sha256(coinbase_bin).digest()
        ).digest()

        # Step 3: Walk the merkle tree
        # Each branch hash is concatenated with the current hash and double-SHA256'd
        merkle_root = coinbase_hash
        for branch_hex in merkle_branches:
            branch_bin = bytes.fromhex(branch_hex)
            merkle_root = hashlib.sha256(
                hashlib.sha256(merkle_root + branch_bin).digest()
            ).digest()

        # Step 4: Build the 80-byte block header
        # Header format: version(4) + prevhash(32) + merkle_root(32) + ntime(4) + nbits(4) + nonce(4)

        # Apply version rolling if present
        version_int = int(version_hex, 16)
        if versionbits_hex:
            versionbits_int = int(versionbits_hex, 16)
            version_int = (version_int & ~0x1FFFE000) | (versionbits_int & 0x1FFFE000)

        version_bin = version_int.to_bytes(4, "little")

        # prevhash from pool is in "internal byte order" -- groups of 4 bytes
        # are individually little-endian but the groups are in big-endian order.
        # We need to reverse each 4-byte chunk.
        prevhash_bin = b""
        for i in range(0, len(prevhash_hex), 8):
            prevhash_bin += bytes.fromhex(prevhash_hex[i:i+8])[::-1]

        ntime_bin = bytes.fromhex(ntime_hex)[::-1]  # little-endian
        nbits_bin = bytes.fromhex(nbits_hex)[::-1]   # little-endian
        nonce_bin = bytes.fromhex(nonce_hex)[::-1]    # little-endian

        header = (
            version_bin
            + prevhash_bin
            + merkle_root
            + ntime_bin
            + nbits_bin
            + nonce_bin
        )

        if len(header) != 80:
            log("share_diff_calc_bad_header_len", length=len(header))
            return 0.0

        # Step 5: SHA256d the header
        header_hash = hashlib.sha256(
            hashlib.sha256(header).digest()
        ).digest()

        # Step 6: Convert hash to difficulty
        # The hash is in little-endian.  Convert to a big-endian integer
        # for comparison with the difficulty target.
        hash_int = int.from_bytes(header_hash, "little")

        if hash_int == 0:
            return 0.0

        # pdiff = diff1_target / hash_value
        share_diff = _DIFF1_TARGET / hash_int
        return share_diff

    except Exception as e:
        log("share_diff_calc_error", err=str(e))
        return 0.0


def worker_record_share(worker: str, difficulty: float, accepted: bool,
                         true_diff: float = 0.0, pool_key: str = "") -> None:
    """Record a share for a worker.  Called from the share_result handler.

    Args:
        worker: Worker name (e.g. "AvalonQ")
        difficulty: Pool difficulty (downstream diff, used for hashrate calc)
        accepted: True if pool accepted the share
        true_diff: True share difficulty computed from block header hash.
                   Used for best-share tracking (the "lucky share" value).
                   If 0.0, falls back to pool difficulty.
        pool_key: "A" or "B" -- which pool accepted/rejected this share.
                  Tagged in share_log so hashrate calc can filter by pool.
    """
    now = time.time()
    with worker_stats_lock:
        ws = worker_stats.get(worker)
        if ws is None:
            ws = {
                "accepted": 0,
                "rejected": 0,
                "diff_sum": 0.0,
                "difficulty": difficulty,
                "last_seen": now,
                "connected_at": now,
                "share_log": [],
            }
            worker_stats[worker] = ws

        if accepted:
            ws["accepted"] += 1
            ws["diff_sum"] += difficulty
            ws["share_log"].append((now, difficulty, pool_key))
            # Trim share_log to max size (drop oldest entries)
            if len(ws["share_log"]) > _SHARE_LOG_MAX:
                ws["share_log"] = ws["share_log"][-_SHARE_LOG_MAX:]
        else:
            ws["rejected"] += 1

        ws["difficulty"] = difficulty
        ws["last_seen"] = now

    # Update decay hashrate (on every accepted share).
    if accepted and difficulty > 0:
        _decay_worker(worker, difficulty, now)
        _decay_worker_pool(worker, pool_key, difficulty, now)

    # Update best share (accepted shares only)
    # Use the TRUE share difficulty computed from the block header hash.
    # This gives the real "lucky share" value -- same as what a pool dashboard
    # would show.  Falls back to pool difficulty if header calc failed.
    if accepted:
        _best_val = true_diff if true_diff > 0 else difficulty
        if _best_val > 0:
            with _best_shares_lock:
                prev = _best_shares.get(worker, 0.0)
                if _best_val > prev:
                    _best_shares[worker] = _best_val


def _worker_calc_hashrate(share_log: list, window_seconds: float,
                          pool_filter: str = "") -> float:
    """Estimate hashrate from a rolling window of share_log entries.

    Formula: hashrate = sum(difficulty_in_window) * 2^32 / elapsed_seconds

    Difficulty 1 represents 2^32 (4,294,967,296) hashes of work.  So a miner
    submitting 1 share/sec at difficulty 1000 is doing ~4.295 TH/s of work.

    Elapsed time uses the span from first_share to last_share in the window,
    NOT first_share to now.  This avoids inflating the rate when there's a
    gap between the last share and the current time (e.g., right after a
    pool switch), and avoids deflating it during the initial ramp-up.

    We require at least 2 shares to calculate -- a single share gives no
    rate information.

    Args:
        share_log: List of (timestamp, difficulty) or (timestamp, difficulty, pool_key) tuples.
        window_seconds: How far back to look (e.g. 300 for 5 minutes).
        pool_filter: If non-empty, only include shares from this pool ("A" or "B").
                     This prevents cross-pool difficulty contamination after switches
                     where old-pool low-diff shares skew the median and cause the
                     anti-inflation cap to squash legitimate high-diff new-pool shares.

    Example: AvalonQ at ~80 TH/s, pool diff 1024, ~18 shares/sec:
      18 * 1024 * 2^32 / 1 = ~79.2 TH/s
    """
    if not share_log:
        return 0.0
    now = time.time()
    cutoff = now - window_seconds

    # Collect shares within the window, optionally filtering by pool.
    # share_log entries can be 2-tuples (ts, diff) or 3-tuples (ts, diff, pool).
    window_shares = []
    for entry in share_log:
        ts = entry[0]
        d = entry[1]
        p = entry[2] if len(entry) > 2 else ""
        if ts < cutoff:
            continue
        if pool_filter and p and p != pool_filter:
            continue
        window_shares.append((ts, d))

    if len(window_shares) < 2:
        return 0.0

    # Anti-inflation: cap outlier difficulties to 4x the median.
    # With pool filtering active, the median is computed from same-pool
    # shares only, so the cap accurately reflects that pool's difficulty.
    diffs = sorted(d for _, d in window_shares)
    median_diff = diffs[len(diffs) // 2]
    diff_cap = median_diff * 4.0

    # cdk - eliminating diff cap for now
    #total_diff = sum(min(d, diff_cap) for _, d in window_shares)
    total_diff = sum(d for _, d in window_shares)
    first_ts = window_shares[0][0]
    last_ts = window_shares[-1][0]

    # Elapsed = span from first to last share in the window.
    # This measures the actual observation period with data at both ends.
    # cdk - using window_seconds since may be gaps between first_ts and last_ts
    #elapsed = last_ts - first_ts
    elapsed = window_seconds
    if elapsed < 1.0:
        return 0.0

    return total_diff * 4294967296.0 / elapsed

def _worker_calc_hashrate_combined(share_log: list, window_seconds: float) -> float:
    """Calculate hashrate by computing per-pool and summing.

    When a miner switches between pools with very different difficulty
    levels (e.g. AvalonQ gets diff 213K on Pool A but 15K on Pool B),
    mixing all shares together causes the anti-inflation cap to squash
    the higher-diff pool's shares.  By splitting the share_log per pool
    and computing each independently, each pool's median/cap is correct.

    Used for Worker Stats display (total miner hashrate across all pools).
    """
    if not share_log:
        return 0.0

    # Split share_log by pool_key
    pools_seen = set()
    for entry in share_log:
        p = entry[2] if len(entry) > 2 else ""
        if p:
            pools_seen.add(p)

    # If no pool tags or only one pool, just call the normal function
    if len(pools_seen) <= 1:
        return _worker_calc_hashrate(share_log, window_seconds)

    # Compute per-pool hashrate and sum them
    total_hr = 0.0
    for pool in pools_seen:
        hr = _worker_calc_hashrate(share_log, window_seconds, pool_filter=pool)
        total_hr += hr
    return total_hr


def _worker_build_stats_snapshot() -> dict:
    """Build a JSON-serializable snapshot of all worker stats for the GUI.

    Hashrate values come from the ckpool-style exponential decay system,
    which updates on every accepted share.  This function also triggers
    idle decay for all workers (so hashrate drops to zero for miners
    that stop submitting shares).

    Returns a dict like:
    {
      "workers": {
        "BitAxe01": {
          "hr_5m": 123.4,    # hashes/sec (5-minute EWMA)
          "hr_60m": 120.1,   # hashes/sec (60-minute EWMA)
          "hr_24h": 118.5,   # hashes/sec (24-hour EWMA)
          "sps": 0.15,       # shares per second (from 5-minute window)
          "diff": 1000.0,    # current downstream difficulty
          "shares": 450,     # total accepted shares
          "best": 25000.0,   # best share difficulty (from persistent file)
          "rejected": 3,     # total rejected shares
          "rej_pct": 0.66,   # rejected / (accepted + rejected) * 100
          "last_seen": 1708000000.0,  # Unix timestamp of last share
        }, ...
      },
      "pool_latency": {"A": 45.2, "B": 32.1},  # ms
      "ts": 1708000000.0  # snapshot timestamp
    }
    """
    # Apply idle decay to all workers so hashrate trends toward zero
    # for miners that have stopped submitting. 
    now = time.time()
    _decay_idle_all(now)

    workers = {}
    # Snapshot worker_stats under lock as quickly as possible, then
    # do all computation OUTSIDE the lock to minimize contention with
    # the async event loop (worker_record_share, fleet_update_weight, etc.).
    with worker_stats_lock:
        ws_snap = {wname: {
            "share_log": list(ws.get("share_log", [])),
            "accepted": ws.get("accepted", 0),
            "rejected": ws.get("rejected", 0),
            "difficulty": ws.get("difficulty", 0.0),
            "last_seen": ws.get("last_seen", 0.0),
            "connected_at": ws.get("connected_at", 0.0),
        } for wname, ws in worker_stats.items()}

    with _best_shares_lock:
        best_snap = dict(_best_shares)

    for wname, ws in ws_snap.items():
        sl = ws["share_log"]
        acc = ws["accepted"]
        rej = ws["rejected"]
        total = acc + rej

        # Shares per second from 5-minute window
        cutoff_5m = now - 300
        shares_in_5m = sum(1 for entry in sl if entry[0] >= cutoff_5m)
        elapsed_5m = min(300.0, now - sl[0][0]) if sl and sl[0][0] >= cutoff_5m else 300.0
        sps = shares_in_5m / max(1.0, elapsed_5m) if shares_in_5m > 0 else 0.0

        # Read hashrate from decay state.
        # 5m: use per-pool sum for stability (avoids oscillation on
        # time-slicing miners with mismatched pool difficulties).
        # 60m/24h: use combined EWMA (longer windows are less affected).
        hr_1m, hr_5m_combined, hr_60m, hr_24h = _decay_get_hashrates(wname)
        hr_pool_sum = _decay_get_hashrate_pool_sum(wname)
        hr_5m = hr_pool_sum if hr_pool_sum > 0 else hr_5m_combined

        best = best_snap.get(wname, 0.0)

        workers[wname] = {
            "hr_5m": round(hr_5m, 2),
            "hr_60m": round(hr_60m, 2),
            "hr_24h": round(hr_24h, 2),
            "sps": round(sps, 4),
            "diff": ws["difficulty"],
            "shares": acc,
            "best": best,
            "rejected": rej,
            "rej_pct": round(rej / total * 100, 2) if total > 0 else 0.0,
            "last_seen": ws["last_seen"],
            "connected_at": ws.get("connected_at", 0.0),
        }

    return {
        "workers": workers,
        "pool_latency": dict(_pool_latency),
        "ts": time.time(),
    }


def worker_stats_write_loop_sync() -> None:
    """Background thread that writes worker_stats.json and best_shares.json
    every 5 seconds.  Runs in a daemon thread so it dies with the process."""
    while True:
        try:
            time.sleep(5)
            if WORKER_STATS_PATH:
                snapshot = _worker_build_stats_snapshot()
                tmp = WORKER_STATS_PATH + ".tmp"
                with open(tmp, "w") as f:
                    f.write(json.dumps(snapshot, separators=(",", ":")))
                os.replace(tmp, WORKER_STATS_PATH)
            # Save best shares less frequently (every 30 seconds)
            if int(time.time()) % 30 < 5:
                _save_best_shares()
                _save_fleet_health()
            # Build fleet state snapshot and write fleet_metrics.json
            # every cycle (5 seconds) so the dashboard has fresh data.
            try:
                state = _fleet_state_build()
                _save_fleet_metrics(state)
            except Exception as _fse:
                log("fleet_state_build_error", err=str(_fse))
        except Exception as e:
            log("worker_stats_write_error", err=str(e))


def pool_record_submit_time(msg_id: Any, pool_key: str) -> None:
    """Record the monotonic time when a share was submitted to a pool.
    Called right before sending the share upstream."""
    with _pool_submit_time_lock:
        _pool_submit_time[msg_id] = (pool_key, time.monotonic())
        # Prune old entries (shouldn't happen, but safety)
        if len(_pool_submit_time) > 500:
            oldest = sorted(_pool_submit_time.items(), key=lambda x: x[1][1])
            for k, _ in oldest[:250]:
                _pool_submit_time.pop(k, None)


def pool_record_result_time(msg_id: Any) -> None:
    """Record when the pool responded to a submitted share.
    Calculates round-trip latency and updates the pool's latency gauge."""
    with _pool_submit_time_lock:
        entry = _pool_submit_time.pop(msg_id, None)
    if entry is None:
        return
    pool_key, submit_mono = entry
    latency_ms = (time.monotonic() - submit_mono) * 1000.0
    _pool_latency[pool_key] = round(latency_ms, 1)


def fleet_register(sid_str: str, pool: str, weight: float = 1.0,
                    worker_name: str = "",
                    switch_count: int | None = None,
                    last_switch_mono: float | None = None) -> int:
    """Register or update a miner's current pool assignment and weight.

    Returns:
        The switch_count actually stored (may be higher than the input
        if a carried count from a previous session was restored).
    """
    _stored_sc = 0
    with _fleet_lock:
        _fleet_pool[sid_str] = pool
        if weight > 0:
            _fleet_weight[sid_str] = weight
        if worker_name:
            _fleet_sid_worker[sid_str] = worker_name
        if switch_count is not None:
            # If registering with 0, check for a carried count from a
            # previous session (miner reconnected).
            if switch_count == 0 and worker_name:
                _carried = _fleet_worker_switch_carry.pop(worker_name, 0)
                _fleet_switch_count[sid_str] = _carried
            else:
                _fleet_switch_count[sid_str] = switch_count
            _stored_sc = _fleet_switch_count[sid_str]
        if last_switch_mono is not None:
            _fleet_last_switch[sid_str] = last_switch_mono
    return _stored_sc

def fleet_update_weight(sid_str: str, weight: float) -> None:
    """Update a miner's hashrate weight (called on accepted shares)."""
    with _fleet_lock:
        if weight > 0:
            _fleet_weight[sid_str] = weight

def _fleet_update_share(sid_str: str, shareA: float) -> None:
    """Update a miner's current scheduler shareA ratio."""
    with _fleet_lock:
        _fleet_shareA[sid_str] = shareA

def _fleet_avg_share() -> tuple[float, float]:
    """Return the average (shareA, shareB) across all active miners.
    Simple average -- each miner counts equally regardless of hashrate,
    since each independently targets the same ratio."""
    with _fleet_lock:
        if not _fleet_shareA:
            return 0.5, 0.5
        avg_a = sum(_fleet_shareA.values()) / len(_fleet_shareA)
        return avg_a, 1.0 - avg_a

def fleet_unregister(sid_str: str) -> None:
    """Remove a miner from fleet tracking (on disconnect)."""
    with _fleet_lock:
        # Carry forward switch count by worker name so it survives reconnects
        _wname = _fleet_sid_worker.get(sid_str, "")
        _sc = _fleet_switch_count.get(sid_str, 0)
        if _wname and _sc > 0:
            _fleet_worker_switch_carry[_wname] = _sc
        _fleet_pool.pop(sid_str, None)
        _fleet_weight.pop(sid_str, None)
        _fleet_shareA.pop(sid_str, None)
        _fleet_sid_worker.pop(sid_str, None)
        _fleet_switch_count.pop(sid_str, None)
        _fleet_last_switch.pop(sid_str, None)


def en1_mismatch_carry_save(worker_name: str, count: int) -> None:
    """Save en1 mismatch count for a worker so it survives reconnects."""
    if worker_name and count > 0:
        with _fleet_lock:
            _fleet_worker_en1_mismatch_carry[worker_name] = count


def en1_mismatch_carry_restore(worker_name: str) -> int:
    """Restore and consume the carried en1 mismatch count for a worker.

    Returns:
        The carried count (0 if none).
    """
    if not worker_name:
        return 0
    with _fleet_lock:
        return _fleet_worker_en1_mismatch_carry.pop(worker_name, 0)


def en1_mismatch_carry_clear(worker_name: str) -> None:
    """Clear the carried en1 mismatch count (called after a successful switch)."""
    if worker_name:
        with _fleet_lock:
            _fleet_worker_en1_mismatch_carry.pop(worker_name, None)

def _fleet_ratio() -> tuple[float, float]:
    """Return (hashrate_on_A, hashrate_on_B) across all active miners.
    Each miner's contribution is weighted by its observed share difficulty."""
    with _fleet_lock:
        a = sum(_fleet_weight.get(sid, 1.0)
                for sid, p in _fleet_pool.items() if p == "A")
        b = sum(_fleet_weight.get(sid, 1.0)
                for sid, p in _fleet_pool.items() if p == "B")
        return a, b

def fleet_try_switch() -> bool:
    """Try to claim a fleet-wide switch slot (cooldown gate)."""
    global _fleet_last_switch_mono
    with _fleet_lock:
        now = time.monotonic()
        if now - _fleet_last_switch_mono >= _FLEET_SWITCH_COOLDOWN_S:
            _fleet_last_switch_mono = now
            return True
        return False


# ---------------------------------------------------------------------------
# Pool unresponsive tracking functions
# ---------------------------------------------------------------------------


def pool_record_no_job(pool_key: str, now: float) -> None:
    """Record that a pool has no cached job (mining.notify not yet received).

    Called each time switch_skipped_no_cached_job fires for a pool that is
    TCP-connected but not sending work.  After _POOL_NO_JOB_FAILOVER_S
    seconds of continuous no-job state, the pool is declared unresponsive
    and the assigner will move all miners to the working pool.

    Args:
        pool_key: "A" or "B"
        now:      Current time (time.monotonic())
    """
    global _pool_unresponsive
    if _pool_no_job_first_seen[pool_key] == 0.0:
        _pool_no_job_first_seen[pool_key] = now
        log("pool_no_job_first_seen", pool=pool_key,
            failover_in_s=_POOL_NO_JOB_FAILOVER_S)

    elapsed = now - _pool_no_job_first_seen[pool_key]
    if not _pool_unresponsive[pool_key] and elapsed >= _POOL_NO_JOB_FAILOVER_S:
        _pool_unresponsive[pool_key] = True
        log("pool_declared_unresponsive", pool=pool_key,
            elapsed_s=round(elapsed, 1),
            reason="no mining.notify received after TCP connect")


def pool_record_job_received(pool_key: str) -> None:
    """Record that a pool has sent a valid mining.notify job.

    Called when latest_notify_raw[pool_key] is populated.  Clears the
    unresponsive state so the assigner can resume normal operation.

    Uses hysteresis: when a pool is in the no-job detection window, a
    single job arrival does NOT immediately clear the no-job clock.
    Instead, _pool_healthy_since tracks when continuous job delivery
    resumed.  The clock only clears after _POOL_HEALTHY_MIN_S seconds
    of sustained delivery.  This prevents pool flickering (brief job
    then silence) from repeatedly restarting the 300s failover timer.

    Args:
        pool_key: "A" or "B"
    """
    global _pool_unresponsive
    now = time.monotonic()

    if _pool_unresponsive[pool_key]:
        # Full recovery from declared-unresponsive -- clear everything.
        # Note: only clear the unresponsive flag here, not at the top of
        # the function -- this prevents a flap-triggered unresponsive state
        # from being immediately undone by the next job arrival in the same
        # call (race condition fix).
        _pool_unresponsive[pool_key] = False
        _pool_no_job_first_seen[pool_key] = 0.0
        _pool_healthy_since[pool_key] = 0.0
        _pool_flap_count[pool_key] = 0
        _pool_flap_window_start[pool_key] = 0.0
        log("pool_recovered", pool=pool_key,
            reason="mining.notify received after unresponsive period")

    elif _pool_no_job_first_seen[pool_key] > 0.0:
        # In no-job detection window -- start or continue healthy streak
        if _pool_healthy_since[pool_key] == 0.0:
            _pool_healthy_since[pool_key] = now
        elif now - _pool_healthy_since[pool_key] >= _POOL_HEALTHY_MIN_S:
            # Sustained delivery for long enough -- clear no-job clock.
            # Also count this as a flap: pool went silent, recovered briefly,
            # and is now healthy again.  Too many flaps in a short window
            # means the pool is unreliable -- declare it unresponsive even
            # though no single outage reached _POOL_NO_JOB_FAILOVER_S.
            _healthy_duration = round(now - _pool_healthy_since[pool_key], 1)
            _pool_no_job_first_seen[pool_key] = 0.0
            _pool_healthy_since[pool_key] = 0.0
            if now - _pool_flap_window_start[pool_key] > _POOL_FLAP_WINDOW_S:
                _pool_flap_count[pool_key] = 0
                _pool_flap_window_start[pool_key] = now
            _pool_flap_count[pool_key] += 1
            log("pool_no_job_clock_cleared", pool=pool_key,
                healthy_s=_healthy_duration,
                flap_count=_pool_flap_count[pool_key],
                flap_max=_POOL_FLAP_MAX)
            if _pool_flap_count[pool_key] >= _POOL_FLAP_MAX:
                _pool_flap_count[pool_key] = 0
                _pool_flap_window_start[pool_key] = 0.0
                _pool_unresponsive[pool_key] = True
                log("pool_declared_unresponsive", pool=pool_key,
                    elapsed_s=0.0,
                    reason=f"pool flapping: {_POOL_FLAP_MAX} recoveries "
                           f"within {_POOL_FLAP_WINDOW_S}s window")
    else:
        # Normal operation -- reset healthy streak tracker
        _pool_healthy_since[pool_key] = 0.0


def pool_is_unresponsive(pool_key: str) -> bool:
    """Return True if the pool is TCP-connected but not sending jobs.

    Used by the assigner to skip assignments to unresponsive pools.

    Args:
        pool_key: "A" or "B"
    """
    return _pool_unresponsive.get(pool_key, False)


# ---------------------------------------------------------------------------
# Rolling ratio window functions
# ---------------------------------------------------------------------------


def ratio_window_record(pool_key: str, difficulty: float) -> None:
    """Append an accepted share to the rolling ratio window.

    Called from the share-result handler every time a pool accepts a share.
    Suppressed/fake-accepted shares never reach the handler, so they are
    automatically excluded (which is what we want).

    Args:
        pool_key: "A" or "B"
        difficulty: the share difficulty (same value sent to ACCEPTED_DIFFICULTY_SUM)
    """
    with _ratio_window_lock:
        _ratio_window.append((time.time(), pool_key, difficulty))


# Time-weighted ratio tracking -- records hashrate-seconds per pool per tick.
# Unlike the share-difficulty approach, this doesn't swing when the AvalonQ
# produces bursts of high-diff shares.  Time flows steadily regardless of
# share arrival patterns.
_time_ratio_lock = threading.Lock()
_time_ratio_window: deque = deque(maxlen=60000)  # ~100 min at 10 ticks/sec
_TIME_RATIO_WINDOW_S = 120.0  # 2-minute window for responsive display

def time_ratio_record(pool_key: str, hashrate_ths: float, dt: float,
                       sid_str: str = "") -> None:
    """Record a time slice for the ratio window.

    Called from each miner's forward_jobs loop every tick (0.1s).
    Records: (timestamp, pool, hashrate_ths * dt) -- hashrate-seconds.

    Args:
        pool_key: "A" or "B" -- which pool this miner is currently on
        hashrate_ths: this miner's estimated hashrate
        dt: seconds since last tick (typically ~0.1s)
        sid_str: session identifier (reserved for future use)
    """
    hs = hashrate_ths * dt
    if hs <= 0:
        return
    with _time_ratio_lock:
        _time_ratio_window.append((time.time(), pool_key, hs))


def get_actual_ratio() -> tuple:
    """Calculate the actual A/B ratio from time-weighted hashrate allocation.

    Uses hashrate-seconds (hashrate * time on pool) rather than share
    difficulty.  This gives a steady measurement because time flows
    continuously, unlike share submissions which arrive in bursts.

    Returns:
        (ratio_a, ratio_b) as floats that sum to 1.0.
        Returns (0.5, 0.5) if no data is in the window yet.

    Example:
        If over the last 2 minutes, 80 TH/s was on A for 60% of the time
        and 10 TH/s was on B the rest:
        get_actual_ratio() -> approximately (0.89, 0.11)
    """
    cutoff = time.time() - _TIME_RATIO_WINDOW_S
    sum_a = 0.0
    sum_b = 0.0
    with _time_ratio_lock:
        for ts, p, hs in _time_ratio_window:
            if ts >= cutoff:
                if p == "A":
                    sum_a += hs
                else:
                    sum_b += hs
    total = sum_a + sum_b
    if total == 0:
        return (0.5, 0.5)
    return (sum_a / total, sum_b / total)


def _ratio_window_flush() -> None:
    """Clear both ratio windows (time-weighted and share-based).

    Called when the target ratio changes so the displayed actual ratio
    reflects the new mining allocation immediately.
    """
    with _time_ratio_lock:
        _time_ratio_window.clear()
    with _ratio_window_lock:
        _ratio_window.clear()
    log("ratio_window_flushed", reason="target_ratio_changed")


# ---------------------------------------------------------------------------
# Health scoring functions
# ---------------------------------------------------------------------------


def _health_get(worker: str) -> float:
    """Return the current health score for a worker (default 1.0 for new miners).

    Example:
        score = _health_get("AvalonQ")  # -> 1.0 if never seen, or last known score
    """
    with _fleet_health_lock:
        return _fleet_health.get(worker, 1.0)


def health_event(worker: str, event_score: float, event_name: str = "") -> None:
    """Apply a health event using exponential weighted moving average (EWMA).

    The EWMA formula blends the current score toward the event_score:
        new = old * (1 - ALPHA) + event_score * ALPHA

    With ALPHA=0.1, each event shifts the score by 10% of the gap between
    the current score and the event_score.  Repeated bad events accumulate;
    a miner that consistently has problems will trend toward 0.5-0.6.
    A miner that had a rough start but settled will trend back toward 0.95+.

    Args:
        worker:      Worker name (e.g. "AvalonQ", "BitAxe01")
        event_score: Target score for this event type:
                     1.0 = perfect (clean switch, continuous mining)
                     0.0 = worst (reject storm, crash after switch)
        event_name:  Optional label for logging (e.g. "clean_switch")

    Examples:
        health_event("AvalonQ", 1.0, "clean_switch")    # nudge toward 1.0
        health_event("BitAxe01", 0.0, "reject_storm")   # nudge toward 0.0
    """
    with _fleet_health_lock:
        old = _fleet_health.get(worker, 1.0)
        new = old * (1.0 - _HEALTH_ALPHA) + event_score * _HEALTH_ALPHA
        # Floor at 0.1 (never fully exclude), cap at 1.0
        new = max(0.1, min(1.0, new))
        _fleet_health[worker] = new

    # Update Prometheus gauge
    try:
        if _health_gauge:
            _health_gauge.labels(worker=worker).set(round(new, 4))
    except Exception:
        pass

    if event_name:
        log("health_event", worker=worker, reason=event_name,
            old_score=round(old, 4), new_score=round(new, 4),
            event_target=event_score)


def load_fleet_health() -> None:
    """Load health scores from fleet_health.json on startup.

    Same pattern as load_best_shares(): read JSON file into _fleet_health dict.
    Missing or corrupt file is not an error -- all miners start at 1.0.
    """
    global _fleet_health
    if not FLEET_HEALTH_PATH:
        return
    try:
        if os.path.isfile(FLEET_HEALTH_PATH):
            with open(FLEET_HEALTH_PATH, "r") as f:
                data = json.loads(f.read())
            if isinstance(data, dict):
                with _fleet_health_lock:
                    _fleet_health = {
                        k: max(0.1, min(1.0, float(v)))
                        for k, v in data.items()
                    }
                log("fleet_health_loaded", count=len(_fleet_health),
                    scores={k: round(v, 3) for k, v in _fleet_health.items()})
                # Seed Prometheus gauges with loaded values
                for wname, score in _fleet_health.items():
                    try:
                        if _health_gauge:
                            _health_gauge.labels(worker=wname).set(round(score, 4))
                    except Exception:
                        pass
    except Exception as e:
        log("fleet_health_load_error", err=str(e))


def _save_fleet_health() -> None:
    """Write health scores to fleet_health.json (called periodically).

    Same pattern as _save_best_shares(): atomic write via tmp + os.replace.
    """
    if not FLEET_HEALTH_PATH:
        return
    try:
        with _fleet_health_lock:
            snapshot = dict(_fleet_health)
        if not snapshot:
            return
        tmp = FLEET_HEALTH_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write(json.dumps(
                {k: round(v, 4) for k, v in snapshot.items()},
                indent=2
            ))
        os.replace(tmp, FLEET_HEALTH_PATH)
    except Exception as e:
        log("fleet_health_save_error", err=str(e))


# ---------------------------------------------------------------------------
# Fleet state model (Phase 1c of Scheduler v3)
# ---------------------------------------------------------------------------
# Unified snapshot of the entire fleet that the global assigner (Phase 2)
# will consume.  Built periodically by _fleet_state_build() which aggregates
# data from _fleet_pool, _fleet_weight, worker_stats, _fleet_health, and
# per-session fields.
#
# This is purely additive in Phase 1 -- the existing v2 scheduler still runs
# unchanged.  The fleet state is written to fleet_metrics.json so the
# dashboard can display fleet-level data.
# ---------------------------------------------------------------------------
_fleet_state_lock = threading.Lock()
fleet_state: dict = {
    "miners": {},
    "target_ratio": {"A": 0.5, "B": 0.5},
    "actual_ratio": {"A": 0.5, "B": 0.5},
    "last_build_mono": 0.0,
}

# ---------------------------------------------------------------------------
# Global assignment table (Phase 2 of Scheduler v3)
# ---------------------------------------------------------------------------
# Written by the global assigner task, read by per-session executors (Phase 3).
# In Phase 2 this is advisory only -- miners don't read it yet.
# Each entry describes what the assigner thinks a miner should be doing.
# ---------------------------------------------------------------------------
assignments_lock = threading.Lock()
assignments: dict[str, dict] = {}
# Example entry:
# assignments["('192.168.0.55', 60544)"] = {
#     "mode": "time_slice",       # "static" or "time_slice"
#     "pool": "B",                # assigned pool (static miners)
#     "home_pool": "B",           # where time-slicer spends most time
#     "slice_pool": "A",          # where time-slicer visits periodically
#     "home_duration_s": 19.9,    # seconds on home pool per cycle
#     "slice_duration_s": 10.1,   # seconds on slice pool per cycle
#     "cycle_length_s": 30.0,     # total cycle time
#     "stagger_offset_s": 0.0,    # offset for multi-slicer coordination
# }


def _snap_assignments() -> dict:
    """Return a shallow copy of the assignments dict, holding the lock briefly.

    This is the ONLY way assigner_loop should read assignments.
    Uses a timeout to prevent blocking the asyncio event loop indefinitely
    if the stats-writer thread holds assignments_lock during a slow
    _fleet_state_build() cycle.  Returns an empty dict on timeout rather
    than freezing the event loop.
    """
    if assignments_lock.acquire(timeout=0.5):
        try:
            return dict(assignments)
        finally:
            assignments_lock.release()
    else:
        log("assignments_lock_timeout", caller="_snap_assignments",
            reason="could not acquire lock within 0.5s, returning empty snapshot")
        return {}


def _put_assignments(new: dict, clear_first: bool = True) -> None:
    """Write to the global assignments dict, holding the lock briefly.

    This is the ONLY way assigner_loop should write assignments.
    Uses a timeout to prevent blocking the asyncio event loop.

    Args:
        new:          The new assignments dict to write.
        clear_first:  If True, clear existing entries before writing.
                      If False, merge (update) into existing entries.
    """
    if assignments_lock.acquire(timeout=0.5):
        try:
            if clear_first:
                assignments.clear()
            assignments.update(new)
        finally:
            assignments_lock.release()
    else:
        log("assignments_lock_timeout", caller="_put_assignments",
            reason="could not acquire lock within 0.5s, skipping write")



def _fleet_state_build() -> dict:
    """Build a snapshot of the entire fleet state.

    Aggregates data from multiple sources into one dict:
    - _fleet_pool / _fleet_weight:  current pool and share difficulty per miner
    - worker_stats:  share_log for hashrate calculation, share counts
    - _fleet_health:  per-worker health scores
    - _en2_force_disconnect:  which miners are pinned (can't switch)
    - get_actual_ratio():  rolling-window actual ratio
    - read_weight_override():  current target ratio from slider/oracle/config

    Returns:
        The fleet state dict.  Also updates the global fleet_state.

    Example output:
        {
            "miners": {
                "('192.168.0.55', 56208)": {
                    "worker_name": "AvalonQ",
                    "hashrate_ths": 80.1,
                    "current_pool": "B",
                    "last_switch_mono": 1234.5,
                    "shares_since_switch": 12,
                    "pool_diff": {"A": 42.0, "B": 2768.0},
                    "can_switch": True,
                    "health": 0.97,
                    "switch_count": 47,
                }, ...
            },
            "target_ratio": {"A": 0.40, "B": 0.60},
            "actual_ratio": {"A": 0.387, "B": 0.613},
            "last_build_mono": 123456.789,
        }
    """
    global fleet_state

    miners = {}
    now_mono = time.monotonic()

    with _fleet_lock:
        fleet_snapshot = dict(_fleet_pool)  # sid_str -> pool
        weight_snapshot = dict(_fleet_weight)

    # Build per-miner entries from the session registry.
    # We look up worker name and hashrate from worker_stats (keyed by worker
    # name), cross-referencing with the fleet dicts (keyed by session ID).
    # This requires iterating worker_stats to find each sid's worker name.
    #
    # Build a sid -> worker_name map from _fleet_sid_worker (populated in
    # forward_jobs and on share acceptance).
    with _fleet_lock:
        sid_worker_snap = dict(_fleet_sid_worker)
        switch_count_snap = dict(_fleet_switch_count)
        last_switch_snap = dict(_fleet_last_switch)

    # Snapshot assignments for mode info
    with assignments_lock:
        assignments_snap = dict(assignments)

    # Snapshot worker_stats ONCE before the loop to minimize lock
    # contention with the async event loop.
    with worker_stats_lock:
        ws_snap = {}
        for wn, ws in worker_stats.items():
            ws_snap[wn] = {
                "accepted": ws.get("accepted", 0),
                "diff_sum": ws.get("diff_sum", 0.0),
                "last_seen": ws.get("last_seen", 0.0),
                "share_log": list(ws.get("share_log", [])),
            }

    for sid_str, pool in fleet_snapshot.items():
        worker_name = sid_worker_snap.get(sid_str, "unknown")

        # Paused miners are completely excluded from fleet state --
        # they vanish from the Fleet table as if they don't exist.
        if _get_paused_fn and worker_name:
            if worker_name in _get_paused_fn():
                continue

        # Hashrate from ckpool-style exponential decay (5-minute EWMA).
        # Falls back to raw share_log calculation if decay state not yet
        # populated (e.g., worker just connected and no shares yet).
        hashrate_ths = 0.0
        shares_total = 0
        diff_sum = 0.0
        _last_seen = 0.0
        ws = ws_snap.get(worker_name)
        if ws:
            shares_total = ws.get("accepted", 0)
            diff_sum = ws.get("diff_sum", 0.0)
            _last_seen = ws.get("last_seen", 0.0)

        # Read smoothed hashrate from per-pool decay state (summed).
        # This gives a stable estimate for time-slicing miners by summing
        # independent Pool A and Pool B EWMAs, avoiding the oscillation
        # that occurs when a single combined EWMA sees alternating share
        # difficulties from two pools (e.g. AvalonQ: Pool A diff ~97K,
        # Pool B diff ~5K -- the combined EWMA swings with every switch).
        hr_pool_sum = _decay_get_hashrate_pool_sum(worker_name)
        _, hr_5m, hr_60m, hr_24h = _decay_get_hashrates(worker_name)

        if hr_pool_sum > 0:
            hashrate_ths = hr_pool_sum / 1e12
        else:
            # Fallback: per-pool state not yet populated (first shares
            # after connect). Use combined EWMA until pool data arrives.
            if hr_5m > 0:
                hashrate_ths = hr_5m / 1e12
            elif ws:
                # Last resort: decay state empty, use raw share_log
                sl = ws.get("share_log", [])
                hashrate_ths = _worker_calc_hashrate_combined(sl, 60) / 1e12

        # Long-window floor: if the 60m or 24h combined EWMA is significantly
        # higher than the per-pool sum, the per-pool sum is likely underestimating
        # due to sparse shares on the minority pool (e.g. AvalonQ spending only
        # 10% of time on Pool A with high difficulty = very few Pool A shares).
        # Use the best long-window estimate as a floor to prevent under-assignment.
        hr_long = max(hr_60m, hr_24h)
        if hr_long > 0 and hr_long / 1e12 > hashrate_ths * 1.2:
            hashrate_ths = hr_long / 1e12

        # Stale miner detection: if this miner hasn't submitted a share
        # in over 5 minutes, skip it in this snapshot but DON'T unregister.
        # Small miners (BitAxes) at high difficulty may go 2-4 minutes
        # between shares -- 60s was too aggressive and caused them to
        # vanish from the Fleet table.  They'll re-appear on next share.
        if _last_seen > 0 and (time.time() - _last_seen) > 300.0:
            continue

        # Health from _fleet_health
        health = _health_get(worker_name)

        # Can this miner switch pools?  False if en2-pinned or soft-paused.
        can_switch = True
        try:
            # Extract IP from sid_str like "('192.168.0.55', 56208)"
            ip = sid_str.split("'")[1] if "'" in sid_str else ""
            if ip and ip in _en2_force_disconnect:
                can_switch = False
        except Exception:
            pass
        # Soft-paused miners (toggled off in GUI) should not be switched
        if can_switch and _get_paused_fn and worker_name:
            if worker_name in _get_paused_fn():
                can_switch = False

        # Mode from global assigner (static or time_slice)
        mode = assignments_snap.get(sid_str, {}).get("mode", "static")

        # Time on current pool (seconds since last switch)
        _lsm = last_switch_snap.get(sid_str, now_mono)
        time_on_pool_s = round(now_mono - _lsm, 1)

        miners[sid_str] = {
            "worker_name": worker_name,
            "hashrate_ths": round(hashrate_ths, 3),
            "current_pool": pool,
            "health": round(health, 4),
            "can_switch": can_switch,
            "weight": weight_snapshot.get(sid_str, 1.0),
            "shares_total": shares_total,
            "diff_sum": round(diff_sum, 2),
            "switch_count": switch_count_snap.get(sid_str, 0),
            "time_on_pool_s": time_on_pool_s,
            "mode": mode,
        }

    # Target ratio from slider/oracle/config
    _override = read_weight_override()
    if _override is not None:
        _wA, _wB = _override
    else:
        _wA, _wB = 50, 50  # will be overwritten with real config in main
    _totw = _wA + _wB
    if _totw > 0:
        target = {"A": round(_wA / _totw, 6), "B": round(_wB / _totw, 6)}
    else:
        target = {"A": 0.5, "B": 0.5}

    # Actual ratio from rolling window
    rw_a, rw_b = get_actual_ratio()
    actual = {"A": round(rw_a, 6), "B": round(rw_b, 6)}

    state = {
        "miners": miners,
        "target_ratio": target,
        "actual_ratio": actual,
        "last_build_mono": now_mono,
    }

    with _fleet_state_lock:
        fleet_state = state

    return state


def _save_fleet_metrics(state: dict) -> None:
    """Write fleet_metrics.json for the dashboard to read.

    Same atomic-write pattern as worker_stats.json.
    The dashboard (app.py) will read this file to display the Fleet Metrics
    table and Fleet Summary panel added in Phase 4.
    """
    if not FLEET_METRICS_PATH:
        return
    try:
        out = {
            "ts": time.time(),
            "target": state.get("target_ratio", {}),
            "actual": state.get("actual_ratio", {}),
            "deviation": round(
                abs(state.get("actual_ratio", {}).get("A", 0.5)
                    - state.get("target_ratio", {}).get("A", 0.5)), 4),
            "miners": {},
        }

        # Compute total diff_sum across all miners for contribution %
        _all_miners = state.get("miners", {})
        _total_diff = sum(m.get("diff_sum", 0.0) for m in _all_miners.values())

        for sid_str, m in _all_miners.items():
            _ds = m.get("diff_sum", 0.0)
            _contrib = round(_ds / _total_diff, 6) if _total_diff > 0 else 0.0
            out["miners"][m.get("worker_name", sid_str)] = {
                "pool": m.get("current_pool", "?"),
                "hashrate_ths": m.get("hashrate_ths", 0.0),
                "health": m.get("health", 1.0),
                "can_switch": m.get("can_switch", True),
                "shares_total": m.get("shares_total", 0),
                "mode": m.get("mode", "static"),
                "switch_count": m.get("switch_count", 0),
                "time_on_pool_s": m.get("time_on_pool_s", 0.0),
                "contribution": _contrib,
            }
        tmp = FLEET_METRICS_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write(json.dumps(out, indent=2))
        os.replace(tmp, FLEET_METRICS_PATH)
    except Exception as e:
        log("fleet_metrics_save_error", err=str(e))


# ---------------------------------------------------------------------------
# Scheduler diagnostic log (temporary, for analysis)
# ---------------------------------------------------------------------------
# Writes a CSV snapshot every ~10 seconds with key scheduler metrics.
# File: /data/scheduler_diag.csv
# Enable by setting SCHEDULER_DIAG_PATH in main().
# Delete the path assignment to disable.
# ---------------------------------------------------------------------------
_diag_last_write: float = 0.0
_DIAG_INTERVAL_S = 10.0
_diag_header_written: bool = False

def _write_scheduler_diag(state: dict, assignments: dict) -> None:
    """Append one CSV row to scheduler_diag.csv with current scheduler state."""
    global _diag_last_write, _diag_header_written
    if not SCHEDULER_DIAG_PATH:
        return
    now = time.time()
    if now - _diag_last_write < _DIAG_INTERVAL_S:
        return
    _diag_last_write = now

    try:
        target = state.get("target_ratio", {})
        actual = state.get("actual_ratio", {})
        deviation = abs(actual.get("A", 0.5) - target.get("A", 0.5))

        # Oracle mode check: read oracle_mode.json if it exists,
        # otherwise fall back to config auto_balance setting.
        try:
            _oracle_active = read_oracle_mode(
                bool(json.loads(open(
                    os.path.join(os.path.dirname(SCHEDULER_DIAG_PATH),
                                 "config_v2.json"), "rb"
                ).read()).get("scheduler", {}).get("auto_balance", False)))
        except Exception:
            _oracle_active = False

        # Build per-miner summary: name|pool|mode|hr|health|switches|time_on_pool
        miners = state.get("miners", {})
        miner_parts = []
        for sid_str, m in sorted(miners.items(),
                                  key=lambda x: x[1].get("worker_name", "")):
            wn = m.get("worker_name", "?")
            pool = m.get("current_pool", "?")
            hr = m.get("hashrate_ths", 0.0)
            health = m.get("health", 1.0)
            sw = m.get("switch_count", 0)
            top = m.get("time_on_pool_s", 0.0)
            # Get mode from assignments
            a = assignments.get(sid_str, {})
            mode = a.get("mode", "static")
            sf = a.get("slice_frac", 0.0) if mode == "time_slice" else 0.0
            cl = a.get("cycle_length_s", 0.0) if mode == "time_slice" else 0.0
            miner_parts.append(
                f"{wn}:{pool}:{mode}:{hr:.1f}TH:{health:.2f}:sw{sw}"
                f":top{top:.0f}s:sf{sf:.0%}:cy{cl:.0f}s")
        miners_str = " | ".join(miner_parts)

        # Total fleet hashrate
        total_ths = sum(m.get("hashrate_ths", 0.0) for m in miners.values())

        # Slicer count
        slicer_count = sum(1 for a in assignments.values()
                           if a.get("mode") == "time_slice")

        # Write header if first time
        write_header = not _diag_header_written
        if not os.path.isfile(SCHEDULER_DIAG_PATH):
            write_header = True

        with open(SCHEDULER_DIAG_PATH, "a") as f:
            if write_header:
                f.write("timestamp,target_A,target_B,actual_A,actual_B,"
                        "deviation,oracle_active,total_ths,slicer_count,"
                        "miners\n")
                _diag_header_written = True
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
            f.write(f"{ts},{target.get('A', 0.5):.4f},{target.get('B', 0.5):.4f},"
                    f"{actual.get('A', 0.5):.4f},{actual.get('B', 0.5):.4f},"
                    f"{deviation:.4f},{_oracle_active},{total_ths:.1f},"
                    f"{slicer_count},{miners_str}\n")
    except Exception as e:
        log("scheduler_diag_error", err=str(e))


# ---------------------------------------------------------------------------
# Global Assigner
# ---------------------------------------------------------------------------
# A single async task that runs every assigner_interval_seconds (default 3s).
# It reads the fleet state, computes optimal miner assignments, and writes
# them to assignments.
# ---------------------------------------------------------------------------

def _compute_assignments(fleet: dict, min_slice_s: float,
                         convergence_tolerance: float = 0.02,
                         sr_exclusions: list = None) -> dict:
    """Compute optimal fleet assignments given current state.

    This is the core bin-packing + time-slicing algorithm from spec Section 5.

    Args:
        fleet:  The fleet state dict from _fleet_state_build()
        min_slice_s:  Minimum time on any pool (from config, default 10s)

    Returns:
        Dict of {sid_str: assignment_dict} for each miner.

    Algorithm:
        1. Identify the minority pool (the one that needs LESS hashrate).
        2. Sort switchable miners smallest-first.
        3. Greedily pack small miners onto the minority pool (static assignment).
        4. Whatever deficit remains must come from time-slicing larger miners.
        5. Pick the best time-slicer (closest hashrate fit * health).
        6. Remaining miners go to the majority pool (static).
        7. Calculate time-slice durations.

    Example at 40/60 with AvalonQ(80), Nano3S(6.5), BitAxe1(1.1), BitAxe2(1.1):
        Minority pool = A (wants 40% = 35.5 TH/s)
        Pack small miners on A: 6.5 + 1.1 + 1.1 = 8.7 TH/s (static on A)
        Deficit: 35.5 - 8.7 = 26.8 TH/s -> AvalonQ time-slices
        AvalonQ: 26.8/80 = 33.5% on A, 66.5% on B
    """
    assignments = {}
    miners_data = fleet.get("miners", {})
    target = fleet.get("target_ratio", {"A": 0.5, "B": 0.5})
    target_a = target.get("A", 0.5)
    target_b = target.get("B", 0.5)

    if not miners_data:
        return assignments

    # ------------------------------------------------------------------
    # Manual Mode: bypass bin-packing entirely.
    # Each miner is assigned statically to their user-chosen pool.
    # Paused miners still get their current pool (same as normal mode).
    # ------------------------------------------------------------------
    if is_manual_mode_active():
        pinned = read_pinned_assignments()  # {worker_name: "A" or "B"}
        _paused = _get_paused_fn() if _get_paused_fn else set()
        for sid_str, m in miners_data.items():
            wname = m.get("worker_name", "")
            if wname and wname in _paused:
                # Paused miners stay on their current pool, same as normal mode
                assignments[sid_str] = {
                    "mode": "static",
                    "pool": m.get("current_pool", "A"),
                }
            else:
                # Use the user's pinned assignment; default to A if not listed
                pool = pinned.get(wname, "A")
                assignments[sid_str] = {
                    "mode": "static",
                    "pool": pool,
                }
        log("assigner_manual_mode",
            miner_count=len(miners_data),
            assignments={m.get("worker_name", s): assignments[s].get("pool")
                         for s, m in miners_data.items()})
        return assignments

    # Identify paused miners -- they get a static assignment on their
    # current pool but are excluded from all ratio/hashrate calculations.
    _paused = _get_paused_fn() if _get_paused_fn else set()
    active_miners = {}
    for sid_str, m in miners_data.items():
        wname = m.get("worker_name", "")
        if wname and wname in _paused:
            assignments[sid_str] = {
                "mode": "static",
                "pool": m.get("current_pool", "A"),
            }
        else:
            active_miners[sid_str] = m

    if not active_miners:
        return assignments

    # Pool unresponsive override: if a pool is TCP-connected but not sending
    # jobs, treat it as 0% target so all miners move to the working pool.
    # This prevents miners being stuck unable to switch for hours when the
    # upstream node (e.g. Bitcoin Core) restarts and the pool loses its
    # block template.  Normal operation resumes automatically when the pool
    # starts sending jobs again.
    _a_unresponsive = pool_is_unresponsive("A")
    _b_unresponsive = pool_is_unresponsive("B")
    if _a_unresponsive and not _b_unresponsive:
        log("assigner_pool_failover", unresponsive_pool="A",
            original_target_a=round(target_a, 3))
        target_a = 0.0
        target_b = 1.0
    elif _b_unresponsive and not _a_unresponsive:
        log("assigner_pool_failover", unresponsive_pool="B",
            original_target_b=round(target_b, 3))
        target_a = 1.0
        target_b = 0.0
    elif _a_unresponsive and _b_unresponsive:
        # Both pools unresponsive -- can't do anything useful, keep current
        log("assigner_both_pools_unresponsive")

    # Determine minority pool (the one wanting less hashrate)
    if target_a <= target_b:
        minority_pool = "A"
        majority_pool = "B"
        minority_frac = target_a
    else:
        minority_pool = "B"
        majority_pool = "A"
        minority_frac = target_b

    # Total fleet hashrate (active miners only, excludes paused)
    total_ths = sum(m.get("hashrate_ths", 0.0) for m in active_miners.values())
    if total_ths <= 0:
        # No hashrate data yet -- assign all miners static to majority pool
        for sid_str in active_miners:
            assignments[sid_str] = {"mode": "static", "pool": majority_pool}
        return assignments

    # How much hashrate the minority pool needs
    target_minority_ths = total_ths * minority_frac

    # Separate switchable miners from pinned miners
    switchable = []   # (sid_str, hashrate_ths, health)
    pinned = []       # (sid_str, current_pool)
    _excl = set(sr_exclusions) if sr_exclusions else set()

    for sid_str, m in active_miners.items():
        hr = m.get("hashrate_ths", 0.0)
        if not m.get("can_switch", True):
            pinned.append((sid_str, m.get("current_pool", majority_pool)))
        else:
            health = m.get("health", 1.0)
            switchable.append((sid_str, hr, health))

    # Assign pinned miners first -- they stay where they are
    pinned_minority_ths = 0.0
    for sid_str, pool in pinned:
        assignments[sid_str] = {"mode": "static", "pool": pool}
        if pool == minority_pool:
            hr = active_miners[sid_str].get("hashrate_ths", 0.0)
            pinned_minority_ths += hr

    # Remaining deficit after pinned miners
    remaining_deficit = target_minority_ths - pinned_minority_ths

    # Sort switchable miners by hashrate ascending (smallest first for greedy packing)
    switchable.sort(key=lambda x: x[1])

    # Step 1: Greedily pack small miners onto minority pool.
    # We consider TWO strategies and pick the one with lower deviation:
    #   Strategy A: Pack miners that fit without overshooting (may need time-slicing)
    #   Strategy B: Pack ALL small miners even if it overshoots (avoids time-slicing)
    # A small static overshoot (e.g., 12% actual vs 10% target) is preferable
    # to introducing time-slicing, which adds switch overhead and rejects.
    #
    # SLICER RESERVATION: Before packing, identify the best slicer candidate
    # (highest-hashrate switchable miner, i.e. the last entry after ascending
    # sort) and exclude it from static packing entirely.  This guarantees it
    # always has a deficit to cover via time-slicing, even at low minority
    # targets where small static miners could collectively fill the entire
    # deficit.  Without reservation, all statics get packed first, the slicer
    # finds _true_remaining <= _meaningful_threshold and goes static:majority
    # with sf=0 -- leaving no slicer to compensate when actual ratio drifts.
    # This logic is fully miner-agnostic: it reserves whichever miner has the
    # highest hashrate, regardless of model or identity.
    _slicer_candidate_sid = switchable[-1][0] if switchable else None
    _packable = [(s, h, hl) for s, h, hl in switchable
                 if s != _slicer_candidate_sid]
    _slicer_entry = [e for e in switchable if e[0] == _slicer_candidate_sid]

    # Strategy A: strict no-overshoot packing (packable miners only)
    strict_minority = []
    strict_available = []
    strict_deficit = remaining_deficit
    for sid_str, hr, health in _packable:
        if hr <= strict_deficit:
            strict_minority.append(sid_str)
            strict_deficit -= hr
        else:
            strict_available.append((sid_str, hr, health))
    # Slicer candidate always goes to available, never packed static
    strict_available.extend(_slicer_entry)

    # Strategy B: pack ALL packable miners (all except slicer candidate)
    greedy_minority = [s for s, _, _ in _packable]
    greedy_available = list(_slicer_entry)
    greedy_minority_ths = sum(hr for _, hr, _ in _packable)

    greedy_overshoot = greedy_minority_ths - target_minority_ths

    # Decision: use Strategy B (all-static) if the overshoot is within
    # tolerance AND it leaves the slicer candidate with meaningful work.
    # Use 5% overshoot tolerance since we're choosing between static
    # placement and time-slicing.
    _static_overshoot_frac = greedy_overshoot / total_ths if total_ths > 0 else 0
    _strict_needs_slicer = strict_deficit > 0.01

    # Would Strategy B leave the slicer with meaningful work?
    _greedy_remaining = target_minority_ths - greedy_minority_ths - pinned_minority_ths
    _meaningful_threshold_check = total_ths * 0.02
    _greedy_strands_slicer = _greedy_remaining <= _meaningful_threshold_check

    if (_static_overshoot_frac <= 0.05 and _static_overshoot_frac >= -0.01
            and not _greedy_strands_slicer):
        # Strategy B: all packable miners on minority, slicer candidate available
        static_minority = greedy_minority
        available = greedy_available
        remaining_deficit = _greedy_remaining
    else:
        # Strategy A: strict packing, slicer candidate reserved for time-slicing
        static_minority = strict_minority
        available = strict_available
        remaining_deficit = strict_deficit

    # Assign static minority miners
    for sid_str in static_minority:
        assignments[sid_str] = {"mode": "static", "pool": minority_pool}

    # Step 2: If there's still a deficit, we need time-slicers.
    #
    # REDESIGNED SLICE FRACTION APPROACH (scheduler redesign, April 2026):
    #
    # The old approach calculated slice_frac = remaining_deficit / slicer_hr.
    # This was problematic because hashrate estimates for high-difficulty miners
    # like AvalonQ are severely underestimated when they spend little time on
    # the high-difficulty pool (e.g. AvalonQ at ~97K Pool A diff spending only
    # 10% of time there gets very few Pool A shares, so its Pool A EWMA sits
    # near zero, causing the combined estimate to be far too low).
    #
    # NEW APPROACH:
    #   - Initial slice_frac = minority_frac (the target fraction directly).
    #     Example: target A=30% -> primary slicer starts at 30% on Pool A.
    #     This is always correct as a starting point: if the slicer is the
    #     ONLY contributor to the minority pool, it needs to spend exactly
    #     minority_frac of its time there.  Static miners already assigned
    #     to the minority pool reduce this below minority_frac proportionally.
    #   - Feasibility check: if the primary slicer's hashrate / total fleet
    #     hashrate < minority_frac, that miner cannot cover the target alone.
    #     In that case, recruit additional slicers upfront (not just via SR
    #     feedback after the fact).
    #   - SR feedback correction (in assigner_loop duration-update path) is
    #     now capped at +-0.02/cycle with a 3% threshold, preventing the
    #     lag-induced oscillation seen with the old +-0.15 cap.
    #
    # Hashrate is still used for RANKING candidates (highest hashrate = best
    # primary slicer) but NOT for computing the initial slice fraction.
    if remaining_deficit > 0.01 and available:
        # Select the primary slicer: highest-hashrate eligible miner with
        # good health.  Healthy miners (>= 0.98) get priority; fall back to
        # all available if no healthy candidate exists.
        # SR-excluded miners are deprioritized -- only considered if no
        # other switchable miner is available to cover the deficit.
        best_sid = None
        best_score = -1.0

        _non_excluded = [(s, h, hl) for s, h, hl in available
                         if active_miners.get(s, {}).get("worker_name", "") not in _excl]
        _slicer_pool = _non_excluded if _non_excluded else available
        _healthy_available = [(s, h, hl) for s, h, hl in _slicer_pool if hl >= 0.98]
        _search_pool = _healthy_available if _healthy_available else _slicer_pool

        for sid_str, hr, health in _search_pool:
            if hr < 0.001:
                continue
            # Primary selection criterion: highest hashrate (most impact).
            # Health is used as a tiebreaker multiplier.
            score = hr * health
            if score > best_score:
                best_score = score
                best_sid = sid_str

        # Track how many slicers we've assigned so far for stagger calculation
        slicer_count = 0
        primary_cycle = 30.0  # will be updated once primary is assigned

        if best_sid is not None:
            slicer_hr = active_miners[best_sid].get("hashrate_ths", 1.0)
            slicer_health = active_miners[best_sid].get("health", 1.0)

            # Initial slice_frac = remaining_deficit / slicer_hr,
            # clamped to [min_floor, minority_frac].
            #
            # WHY remaining_deficit / slicer_hr (not minority_frac directly):
            #   When static miners already cover part of the minority target,
            #   the slicer only needs to cover what remains.  Using minority_frac
            #   directly ignores static coverage and causes massive overshoot --
            #   e.g. at 33% target with ~14 TH/s of statics already on Pool A,
            #   adding AvalonQ at 33% pushes actual A to ~55-60%.
            #
            # WHY not plain remaining_deficit / slicer_hr alone:
            #   At high pool difficulty (e.g. Pool A ~97K vs Pool B ~5K), AvalonQ's
            #   hashrate EWMA is underestimated because it gets few high-difficulty
            #   shares when spending little time on Pool A.  This causes
            #   remaining_deficit / slicer_hr to be too LOW (e.g. 5% when it
            #   should be 20%).  The SR feedback correction (+-0.02/cycle) handles
            #   this -- it climbs from an underestimate in ~2-3 minutes.
            #
            # WHY minority_frac as the cap:
            #   A slicer can never meaningfully spend MORE than minority_frac of
            #   its time on the minority pool -- that would over-contribute even
            #   if statics contribute zero.  The cap prevents runaway overcorrection.
            #
            # MIN FLOOR = 1% so the slicer stays in time_slice mode rather than
            #   being silently zeroed out.  If remaining_deficit <= 0 (statics
            #   already exceed the target), we assign AvalonQ static:majority so
            #   it doesn't contribute to the minority pool at all.
            #
            # EXAMPLE (target A=33%, total=39 TH/s, statics on A = 14.3 TH/s):
            #   target_minority_ths = 39 * 0.33 = 12.9 TH/s
            #   remaining_deficit   = 12.9 - 14.3 = -1.4  -> clamped to 0
            #   -> Statics already exceed target; assign AvalonQ static:B
            #
            # EXAMPLE (target A=48%, total=70 TH/s, statics on A = 8 TH/s):
            #   target_minority_ths = 70 * 0.48 = 33.6 TH/s
            #   remaining_deficit   = 33.6 - 8.0 = 25.6 TH/s
            #   slice_frac = 25.6 / 56 = 0.457, capped at minority_frac=0.48 -> 0.457
            #   SR correction lifts this if AvalonQ EWMA is underestimated.
            _static_on_minority = sum(
                active_miners.get(s, {}).get("hashrate_ths", 0.0)
                for s, a in assignments.items()
                if a.get("mode") == "static" and a.get("pool") == minority_pool
            )
            _true_remaining = target_minority_ths - _static_on_minority

            # Meaningful work threshold: if the remaining deficit is less than
            # 2% of total fleet hashrate, there is not enough work left for the
            # slicer to justify putting it in time_slice mode.  A 1% slice_frac
            # assignment causes disruptive pool transitions (temporary SR dips
            # each cycle) for essentially zero SR benefit.  Send it static:majority
            # instead and let the SR feedback correction handle any tiny residual.
            _meaningful_threshold = total_ths * 0.02

            if _true_remaining <= _meaningful_threshold:
                # Deficit too small to justify time-slicing.
                # Assign the slicer static on the majority pool instead.
                assignments[best_sid] = {"mode": "static", "pool": majority_pool}
                log("assigner_primary_slicer",
                    worker=active_miners[best_sid].get("worker_name", best_sid),
                    slice_frac=0.0,
                    slicer_hr=round(slicer_hr, 2),
                    total_ths=round(total_ths, 2),
                    minority_frac=round(minority_frac, 4),
                    static_on_minority=round(_static_on_minority, 2),
                    remaining_deficit=round(_true_remaining, 2),
                    can_cover_alone=False,
                    note="deficit_below_threshold_slicer_goes_static")
                available = [(s, h, hl) for s, h, hl in available if s != best_sid]
            else:
                # Normal path: slicer covers the remaining deficit.
                _raw_frac = _true_remaining / slicer_hr if slicer_hr > 0 else minority_frac
                slice_frac = min(minority_frac, max(0.01, _raw_frac))

                # Feasibility: can this slicer cover the remaining deficit alone?
                _slicer_can_cover = (slicer_hr > 0
                                     and _true_remaining / slicer_hr <= 1.0)

                # Health-adjusted minimum slice floor
                effective_floor = min_slice_s + (1.0 - slicer_health) * 10.0

                # Calculate cycle length from the slice fraction
                if slice_frac > 0.001:
                    cycle_length = effective_floor / slice_frac
                else:
                    cycle_length = 60.0
                cycle_length = max(20.0, min(120.0, cycle_length))

                # Durations on each pool
                slice_duration = cycle_length * slice_frac
                home_duration = cycle_length * (1.0 - slice_frac)

                # Ensure both durations respect the floor
                slice_duration = max(effective_floor, slice_duration)
                home_duration = max(effective_floor, home_duration)

                primary_cycle = slice_duration + home_duration

                assignments[best_sid] = {
                    "mode": "time_slice",
                    "pool": majority_pool,    # current/default pool
                    "home_pool": majority_pool,
                    "slice_pool": minority_pool,
                    "home_duration_s": round(home_duration, 1),
                    "slice_duration_s": round(slice_duration, 1),
                    "cycle_length_s": round(primary_cycle, 1),
                    "stagger_offset_s": 0.0,
                    "slice_frac": round(slice_frac, 4),
                }
                slicer_count = 1
                log("assigner_primary_slicer",
                    worker=active_miners[best_sid].get("worker_name", best_sid),
                    slice_frac=round(slice_frac, 4),
                    slicer_hr=round(slicer_hr, 2),
                    total_ths=round(total_ths, 2),
                    minority_frac=round(minority_frac, 4),
                    static_on_minority=round(_static_on_minority, 2),
                    remaining_deficit=round(_true_remaining, 2),
                    can_cover_alone=_slicer_can_cover)

                # Remove the primary slicer from available list
                available = [(s, h, hl) for s, h, hl in available if s != best_sid]

        # Step 2b: Recruit additional slicers if the primary cannot cover the
        # target alone (feasibility check) or if the predicted deviation after
        # the primary assignment is still above convergence_tolerance.
        #
        # Additional slicers are recruited in DESCENDING hashrate order so
        # the most impactful miner is added first.  Each additional slicer
        # uses the same target-fraction approach: slice_frac = minority_frac
        # adjusted for what's already covered by statics and prior slicers.
        #
        # Recruitment stops when predicted deviation drops within tolerance
        # or no more eligible miners remain.

        # Sort remaining available miners by hashrate DESCENDING for recruitment
        available_desc = sorted(available, key=lambda x: x[1], reverse=True)

        for sid_str, hr, health in available_desc:
            # Skip SR-excluded miners in secondary slicer recruitment.
            # They can still be static miners but should not time-slice.
            _wn = active_miners.get(sid_str, {}).get("worker_name", "")
            if _wn in _excl:
                continue
            # Compute what's already covered: statics + slicers assigned so far
            slicer_minority_ths = sum(
                active_miners.get(s, {}).get("hashrate_ths", 0.0)
                * a.get("slice_frac", 0.0)
                for s, a in assignments.items()
                if a.get("mode") == "time_slice"
                and a.get("slice_pool") == minority_pool
            )
            static_minority_ths = sum(
                active_miners.get(s, {}).get("hashrate_ths", 0.0)
                for s, a in assignments.items()
                if a.get("mode") == "static" and a.get("pool") == minority_pool
            )
            effective_deficit = max(0.0, target_minority_ths
                                    - static_minority_ths
                                    - slicer_minority_ths)

            # Check predicted deviation with current assignments
            predicted_minority_frac = ((static_minority_ths + slicer_minority_ths)
                                        / total_ths if total_ths > 0 else 0.0)
            predicted_deviation = abs(predicted_minority_frac - minority_frac)

            # Stop recruiting if already within tolerance
            if predicted_deviation <= convergence_tolerance:
                break

            # Stop if no meaningful deficit remains
            if effective_deficit <= 0.01:
                break

            # Skip if this miner can't contribute meaningfully
            if hr < 0.001:
                continue

            # Slice fraction for this additional slicer.
            # effective_deficit already accounts for what statics and prior
            # slicers contribute, so deficit/hr gives the correct time fraction.
            # Cap at minority_frac to prevent over-contribution.
            slice_frac_n = min(minority_frac, max(0.01,
                               effective_deficit / hr if hr > 0 else minority_frac))
            eff_floor_n = min_slice_s + (1.0 - health) * 10.0

            if slice_frac_n > 0.001:
                cycle_n = eff_floor_n / slice_frac_n
            else:
                cycle_n = 60.0
            cycle_n = max(20.0, min(120.0, cycle_n))
            sd_n = max(eff_floor_n, cycle_n * slice_frac_n)
            hd_n = max(eff_floor_n, cycle_n * (1.0 - slice_frac_n))

            # Stagger offset: spread switches across time so slicers alternate
            # naturally rather than all switching simultaneously.
            stagger_n = (primary_cycle / (slicer_count + 1)) * slicer_count

            assignments[sid_str] = {
                "mode": "time_slice",
                "pool": majority_pool,
                "home_pool": majority_pool,
                "slice_pool": minority_pool,
                "home_duration_s": round(hd_n, 1),
                "slice_duration_s": round(sd_n, 1),
                "cycle_length_s": round(sd_n + hd_n, 1),
                "stagger_offset_s": round(stagger_n, 1),
                "slice_frac": round(slice_frac_n, 4),
            }
            slicer_count += 1
            log("assigner_recruited_slicer",
                worker=active_miners[sid_str].get("worker_name", sid_str),
                slicer_count=slicer_count,
                slice_frac=round(slice_frac_n, 4),
                stagger_offset_s=round(stagger_n, 1),
                predicted_deviation_before=round(predicted_deviation, 4))

    # Step 3: Remaining unassigned miners go to majority pool (static)
    for sid_str, hr, health in available:
        if sid_str not in assignments:
            assignments[sid_str] = {"mode": "static", "pool": majority_pool}

    return assignments


async def assigner_loop(cfg):
    """Global assigner async task.

    Runs every assigner_interval_seconds, computes optimal fleet assignments,
    and writes them to assignments.

    Assignment stability: once a placement is computed, it sticks until
    the target ratio changes or a miner connects/disconnects.  Minor
    hashrate fluctuations do NOT trigger re-shuffling.  This prevents
    the thrashing problem where every 3-second tick picks a different
    slicer because hashrate estimates wobble.

    Args:
        cfg: AppCfg object from dpmpv2 (uses cfg.sched.assigner_interval_seconds,
             cfg.sched.min_slice_seconds, cfg.sched.convergence_tolerance).

    Launched from main() as an asyncio task alongside oracle_poll_loop.
    """
    interval = cfg.sched.assigner_interval_seconds
    min_slice = cfg.sched.min_slice_seconds

    log("assigner_starting", interval_s=interval, min_slice_s=min_slice,
        convergence_tolerance=cfg.sched.convergence_tolerance)

    # Short startup delay to let miners connect and produce share data
    await asyncio.sleep(10)

    _prev_assignments_summary = ""
    _prev_target = (0.0, 0.0)     # last target ratio that triggered a recompute
    _prev_miner_set: set = set()   # last set of miner sids (detect connect/disconnect)
    _prev_max_hr: float = 0.0     # last max hashrate (detect hashrate stabilization)
    _force_recompute = True        # always compute on first iteration
    _last_recompute_mono: float = 0.0  # monotonic time of last recompute

    # SR-based dynamic slicer recruitment state.
    # Rather than relying solely on hashrate estimates (which can be
    # inaccurate), we track the actual SR deviation over time and recruit
    # additional slicers when the SR persistently misses the target.
    _consec_over_tolerance: int = 0   # consecutive cycles above tolerance
    _consec_under_tolerance: int = 0  # consecutive cycles within tolerance
    _RECRUIT_AFTER_CYCLES = 5        # recruit after 5 cycles above tolerance (~15s)
    _RELEASE_AFTER_CYCLES = 5        # release after 5 cycles within tolerance (~15s)
    _extra_slicers: list = []         # sids of SR-recruited extra slicers
    _sr_recruit_grace_end = time.monotonic() + 90.0  # no recruitment for first 90s
    _sr_recruit_direction: str = ""   # "under" or "over" -- which way SR was off when recruited

    # Deviation-triggered flush cooldown: after flushing the rolling window
    # due to a deviation recompute, suppress further deviation flushes for
    # this many seconds.  Without this, the flush+recompute loop becomes
    # self-defeating -- the flush clears the window, the window shows near-zero
    # actual ratio, deviation stays high, another flush fires 30s later, repeat
    # indefinitely.  The cooldown gives the window time to rebuild from new
    # assignments before the next flush is allowed.
    # Target-change flushes bypass this cooldown (they're always correct).
    _last_deviation_flush_mono: float = 0.0
    _DEVIATION_FLUSH_COOLDOWN_S: float = 180.0  # 3 minutes between deviation flushes

    # SR recruit cooldown is tracked in the module-level _sr_recruit_cooldown
    # dict so the proxy can also call sr_recruit_record_cooldown() when it
    # detects a reject storm on an SR-recruited miner.  No local copy needed.

    # Post-convergence dead-band: track how many consecutive assigner cycles
    # the SR error has persisted before applying a slice_frac correction.
    # Small errors (2-5%) require 3 consecutive cycles to avoid reacting to
    # cycle-induced measurement noise.  Large errors (>5%) correct immediately.
    _sr_consec_error: int = 0         # consecutive cycles with SR error > threshold

    while True:
        try:
            # Yield to the event loop before doing any lock-acquiring work.
            # This ensures pending miner I/O is processed even if the
            # assigner cycle involves brief blocking lock acquisitions.
            await asyncio.sleep(0)

            # Build fresh fleet state in a thread pool executor so that
            # the threading.Lock acquisitions inside _fleet_state_build()
            # cannot block the asyncio event loop.  This is the primary
            # fix for the intermittent deadlock-on-restart issue.
            loop = asyncio.get_running_loop()
            state = await loop.run_in_executor(None, _fleet_state_build)
            miners = state.get("miners", {})

            if not miners:
                await asyncio.sleep(interval)
                continue

            # Check if we need to recompute assignments.
            # Recompute when:
            # 1. Target ratio changed (slider/oracle moved)
            # 2. Fleet composition changed (miner connected/disconnected)
            # 3. First iteration after startup
            # 4. Max hashrate changed significantly (estimates stabilizing)
            # 5. Deviation-triggered: actual ratio drifted far from target
            target = state.get("target_ratio", {"A": 0.5, "B": 0.5})

            # Pool unresponsive override: if a pool has been connected but
            # sending no jobs for _POOL_NO_JOB_FAILOVER_S seconds, treat
            # it as 0% target so the assigner moves all miners to the
            # working pool.  Both the recompute path and duration-update
            # path use this corrected target automatically.
            _a_unresp = pool_is_unresponsive("A")
            _b_unresp = pool_is_unresponsive("B")
            if _a_unresp and not _b_unresp:
                target = {"A": 0.0, "B": 1.0}
            elif _b_unresp and not _a_unresp:
                target = {"A": 1.0, "B": 0.0}

            _curr_target = (round(target.get("A", 0.5), 4),
                            round(target.get("B", 0.5), 4))
            _curr_miner_set = set(miners.keys())
            _curr_max_hr = max((m.get("hashrate_ths", 0.0) for m in miners.values()), default=0.0)

            # Detect significant hashrate change (>20% shift in max miner)
            _hr_changed = False
            if _prev_max_hr > 0 and _curr_max_hr > 0:
                _hr_ratio = _curr_max_hr / _prev_max_hr
                _hr_changed = _hr_ratio > 1.5 or _hr_ratio < 0.5
            elif _curr_max_hr > 5.0 and _prev_max_hr <= 5.0:
                # Crossed the startup threshold -- hashrate data is now reliable
                _hr_changed = True

            # Deviation-triggered recompute: only recompute periodically if
            # the current assignments aren't achieving the target ratio.
            # This prevents unnecessary reshuffling when things are working.
            _deviation_recompute = False
            _dev = 0.0
            if (time.monotonic() - _last_recompute_mono) >= 30.0:
                actual = state.get("actual_ratio", {})
                _dev = abs(actual.get("A", 0.5) - target.get("A", 0.5))
                if _dev > 0.10:
                    _deviation_recompute = True
                    log("assigner_deviation_recompute",
                        deviation=round(_dev, 4),
                        actual_A=round(actual.get("A", 0.5), 4),
                        target_A=round(target.get("A", 0.5), 4))
                # Also recompute if we don't have a slicer but need one
                # (e.g. all static when ratio requires time-slicing)
                # NOTE: use _snap_assignments() to avoid blocking the event
                # loop on the threading.Lock (see deadlock fix comments).
                _assign_snap = _snap_assignments()
                _has_slicer = any(
                    a.get("mode") == "time_slice"
                    for a in _assign_snap.values())
                if not _has_slicer and abs(_curr_target[0] - 0.5) > 0.03:
                    _deviation_recompute = True

            _needs_recompute = (
                _force_recompute
                or _curr_target != _prev_target
                or _curr_miner_set != _prev_miner_set
                or _hr_changed
                or _deviation_recompute
            )

            if _needs_recompute:
                # Flush the rolling window when the target changes OR when a
                # deviation recompute fires with large deviation (>10%). In both
                # cases the window holds stale data from the previous allocation
                # that will drag the actual ratio measurement for up to 10 min.
                # Flushing lets the window rebuild from the new assignments
                # immediately rather than slowly washing out stale entries.
                # Deviation-triggered flushes are rate-limited by a cooldown
                # to prevent the self-defeating loop where flush->low actual_A
                # ->recompute->flush fires every 30s indefinitely after a
                # disruption, never giving the window time to rebuild.
                # Target-change flushes always fire immediately.
                _now_mono_flush = time.monotonic()
                _deviation_flush_ok = (
                    _deviation_recompute
                    and _dev > 0.10
                    and (_now_mono_flush - _last_deviation_flush_mono)
                        >= _DEVIATION_FLUSH_COOLDOWN_S
                )
                if ((_curr_target != _prev_target and not _force_recompute)
                        or _deviation_flush_ok):
                    _ratio_window_flush()
                    if _deviation_flush_ok:
                        _last_deviation_flush_mono = _now_mono_flush
                        log("deviation_flush_cooldown_armed",
                            cooldown_s=_DEVIATION_FLUSH_COOLDOWN_S,
                            dev=round(_dev, 4))

                # Compute fresh assignments
                new_assignments = _compute_assignments(
                    state, min_slice, cfg.sched.convergence_tolerance,
                    sr_exclusions=cfg.sched.sr_recruit_exclusions)

                # Write to global table
                _put_assignments(new_assignments)

                # Clear SR recruitment state on STRUCTURAL recomputes only
                # (target change, miner connect/disconnect, hashrate shift).
                # A deviation_recompute just re-seats the slicer for the same
                # target -- resetting the recruitment counter here would starve
                # recruitment by restarting the grace window every 10 minutes,
                # preventing _consec_over_tolerance from ever reaching the
                # threshold needed to recruit a second slicer.
                _structural_recompute = (
                    _force_recompute
                    or _curr_target != _prev_target
                    or _curr_miner_set != _prev_miner_set
                    or _hr_changed
                )
                if _structural_recompute:
                    if _extra_slicers:
                        _curr = _snap_assignments()
                        _t_a = _target.get("A", 0.5)
                        _t_b = _target.get("B", 0.5)
                        _maj_pool = "A" if _t_a >= _t_b else "B"
                        for _sid in _extra_slicers:
                            if _sid in _curr:
                                _wn = miners.get(_sid, {}).get("worker_name", _sid)
                                _curr[_sid] = {"mode": "static", "pool": _maj_pool}
                                log("sr_released_slicer", worker=_wn,
                                    consec_under=0, direction="target_change")
                        _put_assignments(_curr, clear_first=False)
                    _extra_slicers.clear()
                    _consec_over_tolerance = 0
                    _consec_under_tolerance = 0
                    _sr_recruit_direction = ""
                    _sr_recruit_grace_end = time.monotonic() + 30.0

                _prev_target = _curr_target
                _prev_miner_set = _curr_miner_set
                _prev_max_hr = _curr_max_hr
                _last_recompute_mono = time.monotonic()
                _force_recompute = False

                # Build summary for logging
                summary_parts = []
                for sid_str, a in sorted(new_assignments.items()):
                    wn = miners.get(sid_str, {}).get("worker_name", "?")
                    mode = a.get("mode", "?")
                    if mode == "static":
                        summary_parts.append(f"{wn}:static:{a.get('pool', '?')}")
                    elif mode == "time_slice":
                        sf = a.get("slice_frac", 0)
                        cl = a.get("cycle_length_s", 0)
                        summary_parts.append(
                            f"{wn}:slice:{a.get('home_pool','?')}/{a.get('slice_pool','?')}"
                            f"({sf:.0%},{cl:.0f}s)")
                summary = " | ".join(summary_parts)

                if summary != _prev_assignments_summary:
                    actual = state.get("actual_ratio", {})
                    deviation = abs(actual.get("A", 0.5) - target.get("A", 0.5))
                    log("assigner_update",
                        summary=summary,
                        target_A=target.get("A"), target_B=target.get("B"),
                        actual_A=round(actual.get("A", 0.5), 4),
                        actual_B=round(actual.get("B", 0.5), 4),
                        deviation=round(deviation, 4),
                        miner_count=len(miners),
                        slicer_count=sum(1 for a in new_assignments.values()
                                         if a.get("mode") == "time_slice"),
                        static_count=sum(1 for a in new_assignments.values()
                                         if a.get("mode") == "static"),
                        trigger="recompute")
                    _prev_assignments_summary = summary

                # Diag snapshot after recompute
                try:
                    _write_scheduler_diag(state, new_assignments)
                except Exception:
                    pass

            else:
                # No structural recompute needed -- but update time-slicer
                # durations based on current hashrate estimates.
                # The structural assignment (who's static, who slices) stays
                # locked, but the slice fraction gets recalculated so that
                # drifting hashrate estimates don't cause the ratio to drift.
                actual = state.get("actual_ratio", {})
                total_ths = sum(
                    m.get("hashrate_ths", 0.0) for m in miners.values()
                )
                if total_ths < 0.001:
                    await asyncio.sleep(interval)
                    continue

                _current = _snap_assignments()

                _updated = False

                # Pre-compute the list of all time-slicers so each slicer's
                # deficit calculation can subtract other slicers' contributions.
                # This prevents multiple slicers from each trying to cover the
                # full deficit independently (which would cause over-contribution).
                _all_slicers = [
                    (s, a) for s, a in _current.items()
                    if a.get("mode") == "time_slice"
                ]

                for sid_str, a in _current.items():
                    if a.get("mode") != "time_slice":
                        continue

                    # Recalculate this slicer's fraction based on current hashrates
                    slicer_hr = miners.get(sid_str, {}).get("hashrate_ths", 0.0)
                    if slicer_hr < 0.001:
                        continue

                    _home = a.get("home_pool", "B")
                    _slice = a.get("slice_pool", "A")

                    # How much hashrate does the minority (slice) pool need?
                    _slice_target_frac = target.get(_slice, 0.5)
                    _target_slice_ths = total_ths * _slice_target_frac if total_ths > 0 else 0

                    # How much is already provided by static miners on the slice pool?
                    # Only count miners that are actually ON the slice pool right now.
                    # A miner assigned to pool X but currently on pool Y contributes
                    # zero real hashrate to X's deficit -- counting it would cause the
                    # slicer's fraction to be too low and the SR to drift from target.
                    _static_on_slice = sum(
                        miners.get(s, {}).get("hashrate_ths", 0.0)
                        for s, sa in _current.items()
                        if sa.get("mode") == "static"
                        and sa.get("pool") == _slice
                        and miners.get(s, {}).get("current_pool") == _slice
                    )

                    # How much do OTHER slicers (not this one) already contribute
                    # to this slice pool?  Subtract their contribution so this
                    # slicer only covers the remaining deficit.
                    _other_slicer_ths = sum(
                        miners.get(s, {}).get("hashrate_ths", 0.0)
                        * oa.get("slice_frac", 0.0)
                        for s, oa in _all_slicers
                        if s != sid_str
                        and oa.get("slice_pool") == _slice
                    )

                    # Deficit that THIS slicer must cover (after statics and other slicers)
                    _deficit = max(0.0, _target_slice_ths
                                   - _static_on_slice
                                   - _other_slicer_ths)
                    # Use the current slice_frac as the baseline rather than
                    # recalculating from hashrate every cycle.  Hashrate estimates
                    # (especially for high-difficulty miners like AvalonQ) are too
                    # inaccurate to use as a running baseline -- recalculating from
                    # them every cycle resets the SR correction and prevents
                    # convergence.  On first assignment (no prior slice_frac), fall
                    # back to the hashrate-based estimate as the starting value.
                    _prior_frac = a.get("slice_frac", None)
                    if _prior_frac is None:
                        _new_frac = min(1.0, max(0.0, _deficit / slicer_hr))
                    else:
                        _new_frac = _prior_frac

                    # SR-feedback correction: adjust slice_frac directly from
                    # the actual SR error.  This is the primary convergence
                    # mechanism -- hashrate estimates are only used for the
                    # initial assignment, not for ongoing adjustments.
                    #
                    # Cap is +-0.05/cycle: fast enough to close a 5% gap in
                    # ~3 minutes (matching Oracle's ~12-minute target hold time),
                    # slow enough to avoid overshoot on the 10-minute SR window.
                    #
                    # Dead-band: small errors (2-5%) require 3 consecutive
                    # assigner cycles before correcting, to avoid reacting to
                    # cycle-induced measurement noise in the 5-second SR samples.
                    # Large errors (>5%) correct immediately on every cycle.
                    _actual_slice = actual.get(_slice, 0.5)
                    _sr_error = _slice_target_frac - _actual_slice
                    if abs(_sr_error) > 0.05:
                        # Large error: correct immediately, reset dead-band
                        _sr_consec_error = 3  # treat as already persisted
                    elif abs(_sr_error) > 0.02:
                        _sr_consec_error += 1
                    else:
                        _sr_consec_error = 0  # within tolerance, reset
                    if (_sr_consec_error >= 3
                            and abs(_sr_error) > 0.02
                            and _new_frac > 0.001):
                        _slicer_share = min(1.0, _deficit / max(0.001,
                                            _target_slice_ths - _static_on_slice))
                        _correction = _sr_error * _slicer_share
                        _correction = max(-0.05, min(0.05, _correction))
                        _new_frac = min(0.95, max(0.01, _new_frac + _correction))
                        _sr_consec_error = 0  # reset after applying correction

                    # Health-adjusted floor
                    _health = miners.get(sid_str, {}).get("health", 1.0)
                    _eff_floor = min_slice + (1.0 - _health) * 10.0

                    # Cycle length
                    if _new_frac > 0.001:
                        _cycle = _eff_floor / _new_frac
                    else:
                        _cycle = 60.0
                    _cycle = max(20.0, min(120.0, _cycle))

                    _sd = max(_eff_floor, _cycle * _new_frac)
                    _hd = max(_eff_floor, _cycle * (1.0 - _new_frac))

                    # Only update if the fraction changed meaningfully (> 2%)
                    _old_frac = a.get("slice_frac", 0.0)
                    if abs(_new_frac - _old_frac) > 0.02:
                        a["slice_frac"] = round(_new_frac, 4)
                        a["slice_duration_s"] = round(_sd, 1)
                        a["home_duration_s"] = round(_hd, 1)
                        a["cycle_length_s"] = round(_sd + _hd, 1)
                        _updated = True

                if _updated:
                    _put_assignments(_current, clear_first=False)
                    # Log the duration update
                    _parts = []
                    for sid_str, a in sorted(_current.items()):
                        wn = miners.get(sid_str, {}).get("worker_name", "?")
                        if a.get("mode") == "time_slice":
                            sf = a.get("slice_frac", 0)
                            cl = a.get("cycle_length_s", 0)
                            _parts.append(f"{wn}:slice({sf:.0%},{cl:.0f}s)")
                    if _parts:
                        _total_ths_str = round(total_ths, 1) if total_ths else 0
                        log("assigner_duration_update",
                            slicers=" | ".join(_parts),
                            total_ths=_total_ths_str)

            # Write scheduler diagnostic snapshot (self-throttles to every 10s)
            try:
                _diag_assignments = _snap_assignments()
                _write_scheduler_diag(state, _diag_assignments)
            except Exception:
                pass

            # ------------------------------------------------------------------
            # SR-based dynamic slicer recruitment.
            # Uses actual SR deviation as the primary signal -- if the SR has
            # been outside tolerance for _RECRUIT_AFTER_CYCLES consecutive
            # assigner cycles, recruit the next eligible miner as an additional
            # time-slicer.  Releases extra slicers when SR stays within
            # tolerance for _RELEASE_AFTER_CYCLES consecutive cycles.
            # This corrects for hashrate estimation errors that cause the
            # hashrate-based deficit calculation to under-assign slicers.
            # ------------------------------------------------------------------
            try:
                if not is_manual_mode_active():
                    _actual = state.get("actual_ratio", {})
                    _target = state.get("target_ratio", {})
                    _sr_dev = abs(_actual.get("A", 0.5) - _target.get("A", 0.5))
                    _tol = cfg.sched.convergence_tolerance

                    if _sr_dev > _tol:
                        _consec_over_tolerance += 1
                        _consec_under_tolerance = 0
                    else:
                        _consec_under_tolerance += 1
                        _consec_over_tolerance = 0

                    # Recruit an additional slicer if persistently off-target.
                    # Skip during startup grace period to let rolling window fill.
                    # Max slicers comes from config (max_slicers); 0 = unlimited.
                    _MAX_TOTAL_SLICERS = cfg.sched.max_slicers
                    if (_consec_over_tolerance >= _RECRUIT_AFTER_CYCLES
                            and time.monotonic() >= _sr_recruit_grace_end):
                        _consec_over_tolerance = 0  # reset so we don't recruit every cycle
                        _sr_recruit_grace_end = time.monotonic() + 30.0  # wait 30s before next recruit
                        _actual_A = _actual.get("A", 0.5)
                        _target_A = _target.get("A", 0.5)
                        _sr_recruit_direction = "under" if _actual_A < _target_A else "over"
                        _curr = _snap_assignments()

                        # Find eligible miners not already slicing
                        _already_slicing = {s for s, a in _curr.items()
                                            if a.get("mode") == "time_slice"}

                        # Don't recruit if already at max slicers (0 = unlimited)
                        if _MAX_TOTAL_SLICERS > 0 and len(_already_slicing) >= _MAX_TOTAL_SLICERS:
                            _candidates = []
                        else:
                            _now_mono = time.monotonic()
                            _sr_exclusions = cfg.sched.sr_recruit_exclusions
                            _candidates = [
                                (s, m) for s, m in miners.items()
                                if s not in _already_slicing
                                and m.get("can_switch", True)
                                and m.get("health", 1.0) >= 0.90
                                and not is_manual_mode_active()
                                and m.get("worker_name", "") not in _sr_exclusions
                                and _now_mono >= _sr_recruit_cooldown.get(
                                    m.get("worker_name", ""), (0.0, 0))[0]
                            ]
                        # Sort by hashrate descending -- most impactful first
                        _candidates.sort(
                            key=lambda x: x[1].get("hashrate_ths", 0.0),
                            reverse=True)

                        if _candidates:
                            _recruit_sid, _recruit_m = _candidates[0]
                            _recruit_hr = _recruit_m.get("hashrate_ths", 1.0)
                            _recruit_health = _recruit_m.get("health", 1.0)

                            # Determine minority/majority from current target
                            _t_a = _target.get("A", 0.5)
                            _t_b = _target.get("B", 0.5)
                            _min_pool = "A" if _t_a <= _t_b else "B"
                            _maj_pool = "B" if _min_pool == "A" else "A"
                            _min_frac = min(_t_a, _t_b)
                            _total_ths = sum(
                                m.get("hashrate_ths", 0.0)
                                for m in miners.values())

                            # Slice fraction: compute what the recruited slicer
                            # still needs to cover after existing assignments.
                            # Capped at _min_frac to prevent over-contribution.
                            _static_on_min = sum(
                                miners.get(s, {}).get("hashrate_ths", 0.0)
                                for s, a in _curr.items()
                                if a.get("mode") == "static"
                                and a.get("pool") == _min_pool)
                            _slicer_on_min = sum(
                                miners.get(s, {}).get("hashrate_ths", 0.0)
                                * a.get("slice_frac", 0.0)
                                for s, a in _curr.items()
                                if a.get("mode") == "time_slice"
                                and a.get("slice_pool") == _min_pool)
                            _residual = max(0.0,
                                _total_ths * _min_frac
                                - _static_on_min
                                - _slicer_on_min)
                            _sf = min(_min_frac, max(0.01,
                                      _residual / _recruit_hr
                                      if _recruit_hr > 0 else _min_frac))

                            # Skip recruit if contribution is too small to matter.
                            # _recruit_hr * _sf is the effective TH/s this slicer
                            # would add toward the deficit. If it's less than 1% of
                            # total fleet hashrate, it won't meaningfully move SR
                            # and will only add noise and unnecessary pool switches.
                            _min_contribution = _total_ths * 0.01
                            if _recruit_hr * _sf < _min_contribution:
                                log("sr_recruit_skipped",
                                    worker=_recruit_m.get("worker_name", _recruit_sid),
                                    reason="contribution_below_threshold",
                                    contribution_ths=round(_recruit_hr * _sf, 3),
                                    threshold_ths=round(_min_contribution, 3))
                            else:
                                _floor = min_slice + (1.0 - _recruit_health) * 10.0
                                if _sf > 0.001:
                                    _cyc = _floor / _sf
                                else:
                                    _cyc = 60.0
                                _cyc = max(20.0, min(120.0, _cyc))
                                _sd = max(_floor, _cyc * _sf)
                                _hd = max(_floor, _cyc * (1.0 - _sf))

                                # Stagger offset relative to existing slicers
                                _n_slicers = len(_already_slicing)
                                _stagger = (_cyc / (_n_slicers + 1)) * _n_slicers

                                _new_a = {
                                    "mode": "time_slice",
                                    "pool": _maj_pool,
                                    "home_pool": _maj_pool,
                                    "slice_pool": _min_pool,
                                    "home_duration_s": round(_hd, 1),
                                    "slice_duration_s": round(_sd, 1),
                                    "cycle_length_s": round(_sd + _hd, 1),
                                    "stagger_offset_s": round(_stagger, 1),
                                    "slice_frac": round(_sf, 4),
                                }
                                _curr[_recruit_sid] = _new_a
                                _put_assignments(_curr, clear_first=False)
                                _extra_slicers.append(_recruit_sid)
                                log("sr_recruited_slicer",
                                    worker=_recruit_m.get("worker_name",
                                                           _recruit_sid),
                                    sr_deviation=round(_sr_dev, 4),
                                    slice_frac=round(_sf, 4),
                                    total_slicers=len(_already_slicing) + 1)

                    # Release extra slicers only when SR has crossed to the
                    # other side of target -- i.e., we recruited because actual
                    # was under target, and now actual has overshot above target
                    # (or vice versa).  This prevents premature release when SR
                    # is merely close to target but still drifting on the same
                    # side.  A safety fallback releases if no direction was
                    # recorded (e.g. after a restart).
                    elif (_extra_slicers
                          and _consec_under_tolerance >= _RELEASE_AFTER_CYCLES):
                        _actual_A_now = _actual.get("A", 0.5)
                        _target_A_now = _target.get("A", 0.5)
                        _now_over = _actual_A_now > _target_A_now
                        _should_release = (
                            (_sr_recruit_direction == "under" and _now_over)
                            or (_sr_recruit_direction == "over" and not _now_over)
                            or (_sr_recruit_direction == "")
                        )
                        if _should_release:
                            _consec_under_tolerance = 0
                            _curr = _snap_assignments()
                            _t_a = _target.get("A", 0.5)
                            _t_b = _target.get("B", 0.5)
                            _maj_pool = "A" if _t_a >= _t_b else "B"
                            for _release_sid in _extra_slicers:
                                if _release_sid in _curr:
                                    _release_wn = miners.get(_release_sid, {}).get(
                                        "worker_name", _release_sid)
                                    _curr[_release_sid] = {
                                        "mode": "static",
                                        "pool": _maj_pool,
                                    }
                                    log("sr_released_slicer",
                                        worker=_release_wn,
                                        consec_under=_RELEASE_AFTER_CYCLES,
                                        direction=_sr_recruit_direction)
                            _put_assignments(_curr, clear_first=False)
                            _extra_slicers.clear()

                    # Reset extra slicers on target change or fleet change
                    # Use the loop-level variables which are always defined
                    _current_target_now = (
                        round(_target.get("A", 0.5), 4),
                        round(_target.get("B", 0.5), 4))
                    _current_miners_now = set(miners.keys())
                    if (_current_target_now != _prev_target
                            or _current_miners_now != _prev_miner_set):
                        if _extra_slicers:
                            _curr = _snap_assignments()
                            _t_a = _target.get("A", 0.5)
                            _t_b = _target.get("B", 0.5)
                            _maj_pool = "A" if _t_a >= _t_b else "B"
                            for _sid in _extra_slicers:
                                if _sid in _curr:
                                    _wn = miners.get(_sid, {}).get("worker_name", _sid)
                                    _curr[_sid] = {"mode": "static", "pool": _maj_pool}
                                    log("sr_released_slicer", worker=_wn,
                                        consec_under=0, direction="target_change")
                            _put_assignments(_curr, clear_first=False)
                        _extra_slicers.clear()
                        _consec_over_tolerance = 0
                        _consec_under_tolerance = 0
                        _sr_recruit_direction = ""  # clear stale direction from previous target

            except Exception as _sr_err:
                log("sr_recruit_error", err=str(_sr_err))

        except Exception as e:
            log("assigner_error", err=str(e))

        await asyncio.sleep(interval)


# When an en2_size change is sent to a miner during a pool switch, this dict
# pre-writes which pool the miner should handshake on IF it disconnects and
# reconnects.  Miners that handle the change gracefully never use the hint.
# Keyed by miner IP address (str), value is (pool_key, monotonic_timestamp).
# Entries expire after _EN2_HINT_TTL_S seconds to avoid stale hints.
_next_handshake_pool: dict[str, tuple[str, float]] = {}
_EN2_HINT_TTL_S = 30.0  # hint expires after 30 seconds

# Auto-detection: miners that can't handle en2_size changes get pinned to
# one pool (avoids wasted hashing on rejects or disconnect loops).
# en2_strikes tracks consecutive strike count per miner IP.
# Once count >= _EN2_STRIKE_THRESHOLD, the IP is added to _en2_force_disconnect.
# Strikes reset to 0 when a miner successfully accepts a share after an
# en2_size change, so only consistently failing miners get flagged.
# _en2_struck_hint tracks the hint timestamp that was already counted as a
# strike, so multiple rejected shares from the same en2_size event only
# count as one strike.
en2_strikes: dict[str, int] = {}
_en2_struck_hint: dict[str, float] = {}  # miner_ip -> hint_timestamp already struck
_en2_force_disconnect: set[str] = set()
_EN2_STRIKE_THRESHOLD = 4      # consecutive en2_size failures -> pin to pool
_EN2_STRIKE_WINDOW_S = 10.0    # reject must occur within 10s of hint to count

def record_en2_strike(miner_ip: str) -> bool:
    """Record a strike for a miner that rejected shares after en2_size change.
    Only counts one strike per hint (per en2_size change event).
    Returns True if the miner has now crossed the threshold."""
    # Check if we already struck against this particular hint
    entry = _next_handshake_pool.get(miner_ip)
    if entry is None:
        return False
    _, hint_ts = entry
    if _en2_struck_hint.get(miner_ip) == hint_ts:
        return False  # already counted this en2_size event

    _en2_struck_hint[miner_ip] = hint_ts
    count = en2_strikes.get(miner_ip, 0) + 1
    en2_strikes[miner_ip] = count
    if count >= _EN2_STRIKE_THRESHOLD:
        _en2_force_disconnect.add(miner_ip)
        return True
    return False

def reset_en2_strikes(miner_ip: str) -> None:
    """Reset strikes for a miner that successfully handled an en2_size change.
    Called when an accepted share arrives within the strike window."""
    prev = en2_strikes.get(miner_ip, 0)
    if prev > 0:
        en2_strikes[miner_ip] = 0
        log("en2_strikes_reset", miner_ip=miner_ip, previous_strikes=prev,
            reason="miner accepted share after en2_size change")
    # Also clear the struck hint so the next en2_size event can be evaluated fresh
    _en2_struck_hint.pop(miner_ip, None)

def pop_en2_hint(miner_ip: str) -> str | None:
    """Pop and return the en2_size handshake hint for a miner IP, or None if expired/missing."""
    entry = _next_handshake_pool.pop(miner_ip, None)
    if entry is None:
        return None
    pool_key, ts = entry
    if time.monotonic() - ts > _EN2_HINT_TTL_S:
        return None  # hint expired
    return pool_key

def peek_en2_hint(miner_ip: str) -> str | None:
    """Read the en2_size handshake hint without consuming it. Returns None if expired/missing."""
    entry = _next_handshake_pool.get(miner_ip)
    if entry is None:
        return None
    pool_key, ts = entry
    if time.monotonic() - ts > _EN2_HINT_TTL_S:
        _next_handshake_pool.pop(miner_ip, None)  # clean up expired
        return None
    return pool_key

def has_recent_en2_hint(miner_ip: str) -> bool:
    """Check if there's a recent (non-expired) en2_size hint for this miner,
    WITHOUT consuming or expiring it. Used to detect post-switch rejects."""
    entry = _next_handshake_pool.get(miner_ip)
    if entry is None:
        return False
    _, ts = entry
    return (time.monotonic() - ts) <= _EN2_STRIKE_WINDOW_S


def en2_set_hint(miner_ip: str, pool_key: str) -> None:
    """Set a handshake hint for a miner IP (called from ProxySession).

    When a miner disconnects after an en2_size change, this hint tells
    the reconnect handler which pool to start on.

    Args:
        miner_ip: The miner's IP address string.
        pool_key: "A" or "B" -- which pool the miner should handshake on.
    """
    _next_handshake_pool[miner_ip] = (pool_key, time.monotonic())


def en2_force_pin(miner_ip: str) -> None:
    """Permanently pin a miner IP to its current pool (called from ProxySession).

    Called when ProxySession detects the miner cannot handle en2_size changes
    at all (e.g., immediate disconnect on extranonce update). The miner will
    show can_switch=False in fleet state and the assigner will never try to
    move it.

    Args:
        miner_ip: The miner's IP address string.
    """
    if miner_ip not in _en2_force_disconnect:
        _en2_force_disconnect.add(miner_ip)

def en2_clear_pin(miner_ip: str) -> None:
    """Clear a miner IP's pin, allowing it to switch pools again.
    Used by Manual mode force-reconnect to give the miner a clean slate."""
    _en2_force_disconnect.discard(miner_ip)
    en2_strikes[miner_ip] = 0
    _reconnect_switch_attempts[miner_ip] = 0

def en2_is_pinned(miner_ip: str) -> bool:
    """Check if a miner IP is pinned (cannot switch pools).

    Args:
        miner_ip: The miner's IP address string.

    Returns:
        True if the miner is in the en2_force_disconnect set.
    """
    return miner_ip in _en2_force_disconnect
    #return False #TEMP: disble pinning for testing


# ---------------------------------------------------------------------------
# SR recruit cooldown tracking
# ---------------------------------------------------------------------------
# When a miner produces a reject storm during an SR-recruited time-slice,
# it is placed in a cooldown period during which it cannot be SR-recruited
# again.  Cooldown uses exponential backoff: 30 min base, doubling on each
# repeat offense, up to 8 hours max.  After the cooldown expires the miner
# is automatically eligible again -- no manual action required.
#
# This is keyed by worker_name (not IP) so it persists across reconnects.
# The cooldown dict is module-level so both assigner_loop and the proxy's
# reject-storm handler can access it via sr_recruit_record_cooldown().
# ---------------------------------------------------------------------------

_sr_recruit_cooldown: dict[str, tuple[float, int]] = {}
# worker_name -> (expiry_mono, strike_count)
_SR_COOLDOWN_BASE_S = 1800.0   # 30 minutes base
_SR_COOLDOWN_MAX_S  = 28800.0  # 8 hours maximum


def sr_recruit_record_cooldown(worker_name: str) -> None:
    """Record a SR-recruit reject storm for a worker and set cooldown.

    Called by the proxy when a miner that was SR-recruited generates a
    reject storm (>5 rejects with 0 accepts after a pool switch).

    Args:
        worker_name: The worker's name string (e.g. "BM101A").
    """
    _expiry, strikes = _sr_recruit_cooldown.get(worker_name, (0.0, 0))
    strikes += 1
    cooldown_s = min(_SR_COOLDOWN_MAX_S,
                     _SR_COOLDOWN_BASE_S * (2 ** (strikes - 1)))
    expiry = time.monotonic() + cooldown_s
    _sr_recruit_cooldown[worker_name] = (expiry, strikes)
    log("sr_recruit_cooldown_set",
        worker=worker_name,
        strikes=strikes,
        cooldown_minutes=round(cooldown_s / 60, 1),
        expiry_in_minutes=round(cooldown_s / 60, 1))


def sr_recruit_cooldown_active(worker_name: str) -> bool:
    """Return True if this worker is currently in SR recruit cooldown.

    Args:
        worker_name: The worker's name string.
    """
    entry = _sr_recruit_cooldown.get(worker_name)
    if entry is None:
        return False
    expiry, _ = entry
    return time.monotonic() < expiry


# ---------------------------------------------------------------------------
# Force-reconnect switch: try a clean reconnect before resorting to pinning.
# ---------------------------------------------------------------------------
# When a miner fails set_extranonce (en1 mismatch rejects detected), we
# first try forcing a reconnect so the miner gets the new pool's en1 via
# a fresh subscribe handshake.  Only if the miner fails AGAIN after the
# reconnect do we escalate to a permanent pin.
#
# _reconnect_switch_attempts tracks how many times we have tried the
# reconnect-switch approach for each miner IP.  When the count reaches
# _RECONNECT_SWITCH_MAX_ATTEMPTS, the next failure triggers a pin.
#
# _reconnect_switch_last_pool tracks which pool the last reconnect-switch
# was targeting, so we can tell if the miner failed on the same target.

_reconnect_switch_attempts: dict[str, int] = {}
_reconnect_switch_last_pool: dict[str, str] = {}
_RECONNECT_SWITCH_MAX_ATTEMPTS = 3  # pin after 3 failed reconnect-switch attempts


def reconnect_switch_should_pin(miner_ip: str, target_pool: str) -> bool:
    """Check whether we should pin this miner or try a reconnect-switch.

    Returns True if we have already exhausted reconnect-switch attempts
    for this miner targeting this pool, meaning the next step is to pin.
    Returns False if we should try a reconnect-switch first.

    Args:
        miner_ip:    The miner's IP address string.
        target_pool: "A" or "B" -- the pool we are trying to switch to.
    """
    attempts = _reconnect_switch_attempts.get(miner_ip, 0)
    last_pool = _reconnect_switch_last_pool.get(miner_ip)
    # If target pool changed since last attempt, reset the counter --
    # the miner might work fine switching in the other direction.
    if last_pool is not None and last_pool != target_pool:
        _reconnect_switch_attempts[miner_ip] = 0
        return False
    return attempts >= _RECONNECT_SWITCH_MAX_ATTEMPTS


def reconnect_switch_record_attempt(miner_ip: str, target_pool: str) -> None:
    """Record that we are attempting a reconnect-switch for this miner.

    Called just before closing the miner connection to force a reconnect.

    Args:
        miner_ip:    The miner's IP address string.
        target_pool: "A" or "B" -- the pool we are reconnecting toward.
    """
    _reconnect_switch_attempts[miner_ip] = (
        _reconnect_switch_attempts.get(miner_ip, 0) + 1
    )
    _reconnect_switch_last_pool[miner_ip] = target_pool
    log("reconnect_switch_attempt", miner_ip=miner_ip,
        target_pool=target_pool,
        attempts=_reconnect_switch_attempts[miner_ip],
        max_attempts=_RECONNECT_SWITCH_MAX_ATTEMPTS)


def reconnect_switch_clear(miner_ip: str) -> None:
    """Clear reconnect-switch tracking for a miner (successful switch).

    Called when a miner produces accepted shares after a pool switch,
    proving it can handle pool changes (at least via reconnect).

    Args:
        miner_ip: The miner's IP address string.
    """
    if miner_ip in _reconnect_switch_attempts:
        prev = _reconnect_switch_attempts.pop(miner_ip, 0)
        _reconnect_switch_last_pool.pop(miner_ip, None)
        if prev > 0:
            log("reconnect_switch_cleared", miner_ip=miner_ip,
                previous_attempts=prev,
                reason="miner accepted shares after pool switch")


def next_pool_round_robin() -> str:
    """Return the next pool key ("A" or "B") in round-robin order.

    Called from ProxySession.__init__ to assign an initial pool to each
    new miner connection.  Alternates A, B, A, B, ... across connections.

    Returns:
        "A" or "B"
    """
    global _fleet_next_pool_idx
    pool = "A" if (_fleet_next_pool_idx % 2 == 0) else "B"
    _fleet_next_pool_idx += 1
    return pool