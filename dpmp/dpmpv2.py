#!/usr/bin/env python3
"""
DPMP - Dual-Pool Mining Proxy (Stratum v1)
Dual upstream + weighted scheduling, with correct miner handshake forwarding.
Copyright (c) 2025-2026 Christopher Kryza. Subject to the MIT License.

Max Miners
- For high-end Umbrel box setup, ~50 miners per DPMP instance is reasonable.
- For low-end Raspberry Pi setup, ~10 miners per DPMP instance is reasonable.
- Upstream pool connection limits may apply (e.g., 20 connections max).
- Docker container resources may limit max miners per instance.
"""
from __future__ import annotations

import asyncio
import itertools
import datetime as dt
import json
import time
import os
import signal
from dataclasses import dataclass
from typing import Any, Dict, Optional

try:
    import orjson  # type: ignore
except Exception:
    orjson = None

from prometheus_client import Counter, Gauge, start_http_server

CONN_DOWNSTREAM = Gauge("dpmp_downstream_connections", "Active downstream miner connections")
CONN_UPSTREAM = Gauge("dpmp_upstream_connections", "Active upstream pool connections", ["pool"])
MSG_RX = Counter("dpmp_messages_rx_total", "Messages received", ["side"])
MSG_TX = Counter("dpmp_messages_tx_total", "Messages sent", ["side"])
SHARES_SUBMITTED = Counter("dpmp_shares_submitted_total", "Shares submitted by miners")
SHARES_ACCEPTED = Counter("dpmp_shares_accepted_total", "Shares accepted by pools", ["pool"])
SHARES_REJECTED = Counter("dpmp_shares_rejected_total", "Shares rejected by pools", ["pool"])
JOBS_FORWARDED = Counter("dpmp_jobs_forwarded_total", "Jobs forwarded to miner", ["pool"])
ACCEPTED_DIFFICULTY_SUM = Counter("dpmp_accepted_difficulty_sum", "Sum of difficulty for accepted shares", ["pool"])
DIFF_DOWNSTREAM = Gauge("dpmp_downstream_difficulty", "Current downstream difficulty")
ACTIVE_POOL = Gauge("dpmp_active_pool", "Active pool (1=active,0=inactive)", ["pool"])

# Oracle metrics
ORACLE_HASHRATE = Gauge("dpmp_oracle_hashrate", "Network hashrate from oracle", ["chain", "window"])
ORACLE_RATIO = Gauge("dpmp_oracle_ratio", "Hashrate ratio (short/baseline)", ["chain"])
ORACLE_WEIGHT = Gauge("dpmp_oracle_weight", "Oracle-calculated pool weight", ["pool"])
ORACLE_STATUS = Gauge("dpmp_oracle_status", "Oracle status (1=healthy, 0=error)")
ORACLE_AGE = Gauge("dpmp_oracle_data_age_seconds", "Age of oracle data in seconds")

SWITCH_SUBMIT_GRACE_S = 4.0  # seconds to tolerate stale submits right after a pool switch (was 0.75)
# Path to optional weights override file (written by GUI slider, polled by scheduler)
WEIGHTS_OVERRIDE_PATH = None  # set in main() from config path
# Path to oracle mode file (written by GUI switch button, polled by oracle task)
ORACLE_MODE_PATH = None       # set in main() from config path
MAX_CACHED_NOTIFY_AGE_S = 20.0  # don't switch into pool if cached notify older than this
MAX_CONVERGE_DEVIATION = 0.05 # default max deviation (5%) to trigger urgent pool switch

# Read weight override file if it exists (written by GUI slider)
def read_weight_override() -> tuple[int, int] | None:
    """Return (wA, wB) from weights_override.json, or None if file missing/invalid."""
    if WEIGHTS_OVERRIDE_PATH is None:
        return None
    try:
        with open(WEIGHTS_OVERRIDE_PATH, "rb") as f:
            obj = json.loads(f.read())
        wA = int(obj.get("poolA_weight", -1))
        wB = int(obj.get("poolB_weight", -1))
        if wA < 0 or wB < 0 or (wA == 0 and wB == 0):
            return None
        return (wA, wB)
    except FileNotFoundError:
        return None
    except Exception:
        return None


def read_oracle_mode(config_auto_balance: bool) -> bool:
    """Check whether the oracle should write weights_override.json this cycle.

    Priority:
      1. oracle_mode.json exists  -> use its "oracle_active" value
      2. oracle_mode.json missing -> fall back to config auto_balance setting

    The file is written by the GUI switch button and deleted on DPMP restart.

    Args:
        config_auto_balance: the auto_balance value from config_v2.json (startup default)

    Returns:
        True  = oracle should write weights (oracle is in control)
        False = oracle should NOT write weights (slider is in control)
    """
    if ORACLE_MODE_PATH is None:
        return config_auto_balance
    try:
        with open(ORACLE_MODE_PATH, "rb") as f:
            obj = json.loads(f.read())
        return bool(obj.get("oracle_active", True))
    except FileNotFoundError:
        return config_auto_balance
    except Exception:
        return config_auto_balance


# Get current UTC time as ISO 8601 string
def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

LOG_LEVEL = os.environ.get("DPMP_LOG_LEVEL", "info").strip().lower()
LOG_ALLOW = set(x.strip() for x in os.environ.get("DPMP_LOG_ALLOW", "").split(",") if x.strip())
LOG_DENY  = set(x.strip() for x in os.environ.get("DPMP_LOG_DENY", "").split(",") if x.strip())

# Events considered "debug" level, also high-output events
_DEBUG_EVENTS = {
    "downstream_tx", "upstream_tx", "miner_method",
    "submit_snapshot", "submit_local_sanity",
    "job_forwarded_diff_state",
    "downstream_send_notify", "downstream_send_raw",
    "downstream_send_diff", "scheduler_tick",
}

# Structured logging function
def log(event: str, **fields: Any) -> None:
    # Allowlist/denylist first (highest priority)
    if LOG_ALLOW and event not in LOG_ALLOW:
        return
    if LOG_DENY and event in LOG_DENY:
        return

    # Level-based filtering
    if LOG_LEVEL in ("quiet", "off", "none"):
        return
    if LOG_LEVEL in ("info", "warn", "warning", "error"):
        if event in _DEBUG_EVENTS:
            return

    rec = {"ts": now_utc(), "event": event, **fields}
    print(json.dumps(rec, separators=(",", ":"), ensure_ascii=False), flush=True)

# JSON load/dump helpers with orjson if available
def loads_json(b: bytes) -> Dict[str, Any]:
    if orjson is not None:
        return orjson.loads(b)
    return json.loads(b.decode("utf-8", errors="replace"))

# JSON dump helper with orjson if available
def dumps_json(obj: Dict[str, Any]) -> bytes:
    # Ensure Stratum responses include "error": null when "id" is non-null.
    # Some miners disconnect if "error" is missing from {"id":..., "result":...} responses.
    if isinstance(obj, dict) and obj.get("id") is not None and "result" in obj and "error" not in obj:
        obj = dict(obj)
        obj["error"] = None
    if orjson is not None:
        return orjson.dumps(obj) + b"\n"
    return (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")

# Sanitize downstream notification (remove JSON-RPC 2.0 fields)
def sanitize_downstream_notification(msg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Stratum v1 pool->miner notifications should NOT include JSON-RPC 2.0 fields.
    Many miners are picky: omit "jsonrpc" and omit "id" (no id:null).
    """
    if not isinstance(msg, dict):
        return msg
    if msg.get("method") is None:
        return msg
    m = dict(msg)
    m.pop("jsonrpc", None)
    m.pop("id", None)
    return m

# Extract worker name from miner 'user' string
def extract_worker_name(user: str) -> str:
    """
    Accepts miner 'user' strings like:
      - wallet.worker
      - wallet.worker.suffix
      - worker
    Returns a worker name used internally for metrics and upstream worker tagging.
    """
    if not user:
        return "unknown"
    u = user.strip()
    # take last token after '.' if present
    if "." in u:
        last = u.rsplit(".", 1)[-1].strip()
        if last:
            return last
    return u


@dataclass
class PoolCfg:
    key: str
    name: str
    host: str
    port: int
    wallet: str
    chain: str = ""


@dataclass
class SchedulerCfg:
    wA: int
    wB: int
    min_switch_seconds: int
    slice_seconds: int
    auto_balance: bool = False
    auto_balance_max_deviation: int = 20
    oracle_url: str = "https://www.sr-analyst.com/dpmp/oracle.php"
    oracle_poll_seconds: int = 600


@dataclass
class AppCfg:
    listen_host: str
    listen_port: int
    metrics_enabled: bool
    metrics_host: str
    metrics_port: int
    poolA: PoolCfg
    poolB: PoolCfg
    sched: SchedulerCfg
    downstream_diff: dict

# Load configuration from JSON file
def load_config(path: str) -> AppCfg:
    global LOG_LEVEL, LOG_ALLOW, LOG_DENY
    with open(path, "rb") as f:
        cfg = loads_json(f.read())

    # Config-driven logging defaults (env vars still override if set)
    logcfg = cfg.get("logging", {})
    if isinstance(logcfg, dict):
        if "DPMP_LOG_LEVEL" not in os.environ:
            lvl = str(logcfg.get("level", "")).strip().lower()
            if lvl:
                LOG_LEVEL = lvl
        if "DPMP_LOG_ALLOW" not in os.environ:
            allow = logcfg.get("allow", None)
            if isinstance(allow, str):
                LOG_ALLOW = set(x.strip() for x in allow.split(",") if x.strip())
            elif isinstance(allow, list):
                LOG_ALLOW = set(str(x).strip() for x in allow if str(x).strip())
        if "DPMP_LOG_DENY" not in os.environ:
            deny = logcfg.get("deny", None)
            if isinstance(deny, str):
                LOG_DENY = set(x.strip() for x in deny.split(",") if x.strip())
            elif isinstance(deny, list):
                LOG_DENY = set(str(x).strip() for x in deny if str(x).strip())

    listen = cfg.get("listen", {})
    if not isinstance(listen, dict):
        listen = {}
    metrics = cfg.get("metrics", {})
    if not isinstance(metrics, dict):
        metrics = {}

    # Backward/forward compatible config parsing:
    # - Prefer nested listen/metrics dicts
    # - Fall back to legacy top-level fields if present
    listen_host = listen.get("host") or cfg.get("listen_host") or "0.0.0.0"
    listen_port = listen.get("port") if listen.get("port") is not None else cfg.get("listen_port", 3350)
    try:
        listen_port = int(listen_port)
    except Exception:
        listen_port = 3350

    if "enabled" in metrics:
        metrics_enabled = bool(metrics.get("enabled"))
    else:
        metrics_enabled = bool(cfg.get("metrics_enabled", True))
    metrics_host = metrics.get("host") or cfg.get("metrics_host") or "0.0.0.0"
    metrics_port = metrics.get("port") if metrics.get("port") is not None else cfg.get("metrics_port", 9109)
    try:
        metrics_port = int(metrics_port)
    except Exception:
        metrics_port = 9109
    pools = cfg.get("pools", {})
    sched = cfg.get("scheduler", {})

    def pool(key: str) -> PoolCfg:
        p = pools.get(key, {})
        return PoolCfg(
            key=key,
            name=str(p.get("name", key)),
            host=str(p.get("host", "127.0.0.1")),
            port=int(p.get("port", 3333)),
            wallet=str(p.get("wallet", "")).strip(),
            chain=str(p.get("chain", "")).strip().upper(),
        )

    wA = int(sched.get("poolA_weight", 50))
    wB = int(sched.get("poolB_weight", 50))
    if wA < 0 or wB < 0 or (wA == 0 and wB == 0):
        wA, wB = 50, 50

    # Oracle auto-balance config
    auto_balance = bool(sched.get("auto_balance", False))
    auto_balance_max_deviation = int(sched.get("auto_balance_max_deviation", 20))
    if auto_balance_max_deviation < 5 or auto_balance_max_deviation > 45:
        safe_val = max(5, min(45, auto_balance_max_deviation))
        log("config_safety_max_deviation_clamped",
            raw=auto_balance_max_deviation, corrected=safe_val,
            reason="auto_balance_max_deviation must be between 5 and 45")
        auto_balance_max_deviation = safe_val

    oracle_url = str(sched.get("oracle_url", "https://www.sr-analyst.com/dpmp/oracle.php")).strip()
    oracle_poll_seconds = int(sched.get("oracle_poll_seconds", 600))
    if oracle_poll_seconds < 600:
        log("config_safety_oracle_poll_clamped",
            raw=oracle_poll_seconds, corrected=600,
            reason="oracle_poll_seconds must be >= 600 to stay within rate limits")
        oracle_poll_seconds = 600
    if auto_balance:
        log("oracle_config", auto_balance=True,
            max_deviation=auto_balance_max_deviation,
            oracle_url=oracle_url,
            poll_seconds=oracle_poll_seconds)

    # --- Scheduler timing validation ---
    # Parse raw values from config (defaults: 30s each)
    raw_min_switch = int(sched.get("min_switch_seconds", 30))
    raw_slice = int(sched.get("slice_seconds", 30))

    # Safety 1: min_switch_seconds must be at least 25 seconds.
    # Switching pools faster than this risks reject storms from context mismatches.
    MIN_SWITCH_FLOOR = 25
    if raw_min_switch < MIN_SWITCH_FLOOR:
        log("config_safety_min_switch_clamped",
            raw=raw_min_switch, corrected=MIN_SWITCH_FLOOR,
            reason=f"min_switch_seconds must be >= {MIN_SWITCH_FLOOR}s to avoid reject storms")
        raw_min_switch = MIN_SWITCH_FLOOR

    # Safety 2: slice_seconds must be less than min_switch_seconds.
    # If slice >= min_switch, the urgent-correction feature is effectively disabled
    # and the safety floor adds no value. Clamp slice to min_switch - 5 (at least 1).
    if raw_slice >= raw_min_switch:
        corrected_slice = max(1, raw_min_switch - 5)
        log("config_safety_slice_clamped",
            raw_slice=raw_slice, raw_min_switch=raw_min_switch,
            corrected=corrected_slice,
            reason="slice_seconds must be < min_switch_seconds")
        raw_slice = corrected_slice

    log("scheduler_config_validated",
        min_switch_seconds=raw_min_switch, slice_seconds=raw_slice,
        wA=wA, wB=wB)

    return AppCfg(
        listen_host=str(listen_host),
        listen_port=int(listen_port),
        metrics_enabled=bool(metrics_enabled),
        metrics_host=str(metrics_host),
        metrics_port=int(metrics_port),
        poolA=pool("A"),
        poolB=pool("B"),
        sched=SchedulerCfg(wA=wA, wB=wB, min_switch_seconds=raw_min_switch, slice_seconds=raw_slice,
                           auto_balance=auto_balance, auto_balance_max_deviation=auto_balance_max_deviation,
                           oracle_url=oracle_url, oracle_poll_seconds=oracle_poll_seconds),
        downstream_diff=dict(cfg.get("downstream_diff", {})),
    )

# Async read/write helpers with Prometheus metrics
async def iter_lines(reader: asyncio.StreamReader, side: str):
    while True:
        line = await reader.readline()
        if not line:
            return
        if not line.strip():
            continue
        MSG_RX.labels(side=side).inc()
        yield line

# 
async def write_line(writer: asyncio.StreamWriter, data: bytes, side: str):
    try:
        # Downstream miners expect Stratum v1 notifications WITHOUT JSON-RPC 2.0 fields.
        # Ensure we never send {'jsonrpc':'2.0', ...} or id:null on mining.notify.
        if side == "downstream":
            try:
                msg = loads_json(data)
                if isinstance(msg, dict) and msg.get("method") == "mining.notify":
                    msg2 = sanitize_downstream_notification(msg)
                    data = dumps_json(msg2)
            except Exception:
                pass
        writer.write(data)
        await writer.drain()
        MSG_TX.labels(side=side).inc()

        # Lightweight visibility into what we actually send.
        # Log bytes + a small safe preview (helps confirm miner is receiving what we expect).
        peer = writer.get_extra_info("peername")
        preview = ""
        try:
            s = data.decode("utf-8", errors="replace").strip()
            if len(s) > 1200:
                preview = s[:1200] + "...(trunc)"
            else:
                preview = s
        except Exception:
            preview = "<decode_error>"

        if side == "downstream":
            log("downstream_tx", peer=str(peer), bytes=len(data), preview=preview)
        else:
            log("upstream_tx", peer=str(peer), side=side, bytes=len(data), preview=preview)

    except Exception as e:
        peer = writer.get_extra_info("peername")
        log("write_failed", peer=str(peer), side=side, err=str(e))
        raise

# Extract jobid from mining.notify params
def jobid_from_notify(msg: Dict[str, Any]) -> Optional[str]:
    try:
        p = msg.get("params") or []
        return str(p[0]) if len(p) >= 1 else None
    except Exception:
        return None

# Extract jobid from mining.submit params
def jobid_from_submit(msg: Dict[str, Any]) -> Optional[str]:
    try:
        p = msg.get("params") or []
        return str(p[1]) if len(p) >= 2 else None
    except Exception:
        return None

# Simple weighted round-robin scheduler
class RatioScheduler:
    def __init__(self, wA: int, wB: int):
        self.wA = max(0, int(wA))
        self.wB = max(0, int(wB))
        self.total = self.wA + self.wB
        self.acc = 0

    def pick(self) -> str:
        if self.wA == 0 and self.wB > 0:
            return "B"
        if self.wB == 0 and self.wA > 0:
            return "A"
        self.acc += self.wA
        if self.acc >= self.total:
            self.acc -= self.total
            return "A"
        return "B"

# Hashrate Oracle 
# Async background task that polls the oracle endpoint and writes
# weights_override.json based on real-time BTC/BCH hashrate measurements.
# Only runs when auto_balance=true in config.

import base64
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

async def oracle_poll_loop(cfg: AppCfg):
    """
    Background task that runs whenever chain config is valid (one BTC + one BCH pool).

    Always collects data and updates Prometheus gauges regardless of mode.
    Only writes weights_override.json when oracle_mode.json says oracle is active
    (or when no oracle_mode.json exists and config auto_balance is true).

    Every oracle_poll_seconds (default 600 = 10 min):
      1. GET oracle endpoint -> JSON with BTC/BCH block timestamps + difficulty
      2. Calculate hashrate for short window (6 blocks) and long window (72 blocks)
      3. Compute weights using inverse-ratio model
      4. Clamp to max_deviation (default 30/70)
      5. If oracle mode is active: write weights_override.json

    Safety rules:
      - 60-second startup delay (avoids hammering if user restarts repeatedly)
      - On error: hold current weights, try again next cycle
      - After 3 consecutive failures: revert to 50/50 (only if oracle mode active)
      - Stale data (>20 min old): treat as error
    """
    poll_s = max(60, int(cfg.sched.oracle_poll_seconds))
    url = cfg.sched.oracle_url
    max_dev = int(cfg.sched.auto_balance_max_deviation)
    min_pct = 50 - max_dev   # e.g., 30
    max_pct = 50 + max_dev   # e.g., 70

    # Figure out which pool is BTC and which is BCH from config.
    # The oracle needs this to apply the correct weights to the correct pool.
    pool_chain = {}   # "A" -> "BTC" or "BCH"
    pool_chain["A"] = getattr(cfg.poolA, "chain", "").upper()
    pool_chain["B"] = getattr(cfg.poolB, "chain", "").upper()

    if sorted([pool_chain["A"], pool_chain["B"]]) != ["BCH", "BTC"]:
        log("oracle_disabled_bad_chain_config",
            poolA_chain=pool_chain["A"], poolB_chain=pool_chain["B"],
            reason="auto_balance requires one BTC pool and one BCH pool")
        return  # exit task -- oracle cannot run without proper chain labels

    btc_pool = "A" if pool_chain["A"] == "BTC" else "B"
    bch_pool = "A" if pool_chain["A"] == "BCH" else "B"

    log("oracle_starting",
        url=url, poll_s=poll_s, max_deviation=max_dev,
        btc_pool=btc_pool, bch_pool=bch_pool)

    # Safety: wait 60 seconds before first poll
    log("oracle_startup_delay", delay_s=60)
    await asyncio.sleep(60)

    consecutive_failures = 0

    while True:
        try:
            # Step 1: Fetch data from oracle endpoint 
            log("oracle_poll_start")

            # Run the blocking HTTP call in a thread so we don't stall
            # the asyncio event loop (which is running the proxy).
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, _oracle_fetch, url)

            if data is None:
                raise Exception("fetch returned None")

            if not data.get("ok"):
                raise Exception(f"oracle response not ok: {data.get('error', 'unknown')}")

            # Step 2: Check data freshness 
            # The "ts" field is the MySQL timestamp when the collector pushed data.
            # If it's more than 20 minutes old, the collector might be down.
            ts_str = data.get("ts", "")
            age_s = None  # will be set if ts parsing succeeds
            if ts_str:
                try:
                    # Parse MySQL datetime format: "2026-02-10 19:01:02"
                    from datetime import datetime, timezone
                    db_time = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    now_time = datetime.now(timezone.utc)
                    age_s = (now_time - db_time).total_seconds()
                    log("oracle_data_age", ts=ts_str, age_s=round(age_s, 1))
                    if age_s > 1200:  # 20 minutes
                        raise Exception(f"oracle data is stale ({int(age_s)}s old)")
                except ValueError as e:
                    log("oracle_ts_parse_warning", ts=ts_str, err=str(e))
                    # Don't fail on parse error -- the data might still be fine

            # Step 3: Calculate hashrates 
            short_n = int(data.get("short_window", 6))
            long_n = int(data.get("long_window", 72))

            btc_hr_short, btc_hr_long = _calc_hashrate_pair(
                data["btc_difficulty"], data["btc_ts_latest"],
                data["btc_ts_short"], data["btc_ts_long"],
                short_n, long_n, "BTC")

            bch_hr_short, bch_hr_long = _calc_hashrate_pair(
                data["bch_difficulty"], data["bch_ts_latest"],
                data["bch_ts_short"], data["bch_ts_long"],
                short_n, long_n, "BCH")

            # Step 4: Compute weights (inverse ratio model) 
            btc_ratio = btc_hr_short / btc_hr_long if btc_hr_long > 0 else 1.0
            bch_ratio = bch_hr_short / bch_hr_long if bch_hr_long > 0 else 1.0

            # Inverse: mine MORE where hashrate DROPPED (your shares worth more)
            w_btc = 1.0 / btc_ratio if btc_ratio > 0 else 1.0
            w_bch = 1.0 / bch_ratio if bch_ratio > 0 else 1.0

            total_w = w_btc + w_bch
            pct_btc = (w_btc / total_w) * 100.0 if total_w > 0 else 50.0
            pct_bch = 100.0 - pct_btc

            # Clamp to max deviation
            pct_btc = max(min_pct, min(max_pct, pct_btc))
            pct_bch = 100.0 - pct_btc

            # Round to nearest integer for clean weights
            wt_btc = round(pct_btc)
            wt_bch = 100 - wt_btc

            # Update Prometheus gauges
            ORACLE_HASHRATE.labels(chain="BTC", window="short").set(btc_hr_short)
            ORACLE_HASHRATE.labels(chain="BTC", window="long").set(btc_hr_long)
            ORACLE_HASHRATE.labels(chain="BCH", window="short").set(bch_hr_short)
            ORACLE_HASHRATE.labels(chain="BCH", window="long").set(bch_hr_long)
            ORACLE_RATIO.labels(chain="BTC").set(round(btc_ratio, 4))
            ORACLE_RATIO.labels(chain="BCH").set(round(bch_ratio, 4))
            ORACLE_WEIGHT.labels(pool="A").set(wt_bch if bch_pool == "A" else wt_btc)
            ORACLE_WEIGHT.labels(pool="B").set(wt_bch if bch_pool == "B" else wt_btc)
            ORACLE_STATUS.set(1)
            if age_s is not None:
                ORACLE_AGE.set(round(age_s, 1))

            log("oracle_calc_result",
                btc_hr_short=f"{btc_hr_short:.3e}", btc_hr_long=f"{btc_hr_long:.3e}",
                bch_hr_short=f"{bch_hr_short:.3e}", bch_hr_long=f"{bch_hr_long:.3e}",
                btc_ratio=round(btc_ratio, 4), bch_ratio=round(bch_ratio, 4),
                raw_btc_pct=round(pct_btc, 1), raw_bch_pct=round(pct_bch, 1),
                clamped_btc=wt_btc, clamped_bch=wt_bch)

            # Step 5: Map to pools and write override 
            # btc_pool / bch_pool tell us which config pool is which chain.
            wA = wt_btc if btc_pool == "A" else wt_bch
            wB = wt_btc if btc_pool == "B" else wt_bch

            log("oracle_weights_applied",
                poolA_weight=wA, poolA_chain=pool_chain["A"],
                poolB_weight=wB, poolB_chain=pool_chain["B"])

            # Only write weights_override.json if oracle mode is active.
            # When the user switches to slider mode via the GUI, oracle_mode.json
            # is set to false and the slider controls weights_override.json instead.
            # The oracle still runs (collecting data + updating Prometheus gauges)
            # but does not interfere with the slider's weight file.
            oracle_active = read_oracle_mode(cfg.sched.auto_balance)
            if oracle_active and WEIGHTS_OVERRIDE_PATH is not None:
                try:
                    tmp = f"{WEIGHTS_OVERRIDE_PATH}.tmp"
                    obj = {"poolA_weight": int(wA), "poolB_weight": int(wB),
                           "source": "oracle", "ts": ts_str}
                    with open(tmp, "w") as f:
                        f.write(json.dumps(obj))
                        f.write("\n")
                    os.replace(tmp, WEIGHTS_OVERRIDE_PATH)
                    log("oracle_override_written", path=WEIGHTS_OVERRIDE_PATH,
                        wA=wA, wB=wB)
                except Exception as e:
                    log("oracle_override_write_error", err=str(e))
            elif not oracle_active:
                log("oracle_mode_slider", reason="oracle_mode.json says slider is active, skipping weight write")

            consecutive_failures = 0

        except asyncio.CancelledError:
            log("oracle_cancelled")
            raise
        except Exception as e:
            ORACLE_STATUS.set(0)
            consecutive_failures += 1

            log("oracle_poll_error", err=str(e),
                consecutive_failures=consecutive_failures)

            # After 3 consecutive failures, revert to 50/50
            if consecutive_failures >= 3:
                log("oracle_fallback_50_50",
                    reason=f"{consecutive_failures} consecutive failures")
                # Only write fallback weights if oracle is actually in control
                oracle_active_fb = read_oracle_mode(cfg.sched.auto_balance)
                if oracle_active_fb and WEIGHTS_OVERRIDE_PATH is not None:
                    try:
                        tmp = f"{WEIGHTS_OVERRIDE_PATH}.tmp"
                        obj = {"poolA_weight": 50, "poolB_weight": 50,
                               "source": "oracle_fallback"}
                        with open(tmp, "w") as f:
                            f.write(json.dumps(obj))
                            f.write("\n")
                        os.replace(tmp, WEIGHTS_OVERRIDE_PATH)
                    except Exception:
                        pass

        # Wait for next poll cycle
        log("oracle_next_poll", sleep_s=poll_s)
        await asyncio.sleep(poll_s)


def _oracle_fetch(url: str) -> dict:
    """Blocking HTTP GET to the oracle endpoint. Called via run_in_executor."""
    headers = {"User-Agent": "dpmpv2-oracle/1.0"}
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=15.0) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw)
    except HTTPError as e:
        raise Exception(f"HTTP {e.code}: {e.reason}")
    except URLError as e:
        raise Exception(f"Connection error: {e.reason}")


def _calc_hashrate_pair(difficulty: float, ts_latest: int,
                         ts_short: int, ts_long: int,
                         short_n: int, long_n: int,
                         chain: str) -> tuple:
    """
    Calculate short-window and long-window hashrate for one chain.
    
    Formula: hashrate = difficulty 2^32 / average_block_time
    
    Returns (hashrate_short, hashrate_long) in H/s.
    """
    elapsed_short = ts_latest - ts_short
    elapsed_long = ts_latest - ts_long

    if elapsed_short <= 0 or elapsed_long <= 0:
        log("oracle_bad_timestamps", chain=chain,
            elapsed_short=elapsed_short, elapsed_long=elapsed_long)
        return (0.0, 0.0)

    avg_short = elapsed_short / short_n
    avg_long = elapsed_long / long_n

    hr_short = difficulty * (2**32) / avg_short
    hr_long = difficulty * (2**32) / avg_long

    return (hr_short, hr_long)

# End Hashrate Oracle 

# Proxy session handling a single miner connection and two upstream pools
class ProxySession:
    def __init__(self, cfg: AppCfg, miner_r: asyncio.StreamReader, miner_w: asyncio.StreamWriter, sid: str):
        self.cfg = cfg
        self.sid = sid  # downstream session id (peer)
        self.last_switch_mono: float | None = None
        self.pool_w: Dict[str, asyncio.StreamWriter] = {}
        self.up_q: Dict[str, list[tuple[str, str]]] = {"A": [], "B": []}  # (raw, tag) queued until writer exists
        self.miner_r = miner_r
        self.miner_w = miner_w

        self.rA: Optional[asyncio.StreamReader] = None
        self.wA: Optional[asyncio.StreamWriter] = None
        self.rB: Optional[asyncio.StreamReader] = None
        self.wB: Optional[asyncio.StreamWriter] = None

        self.worker: str = ""
        self.miner_ready = asyncio.Event()
        self.authorize_id: Any = None
        self.subscribe_id: Any = None
        self.configure_id: Any = None
        self.id_gen = itertools.count(1)

        self.latest_notify_raw: Dict[str, Optional[bytes]] = {"A": None, "B": None}
        self.latest_jobid: Dict[str, Optional[str]] = {"A": None, "B": None}
        self.notify_seq: Dict[str, int] = {"A": 0, "B": 0}
        self.last_notify_mono: Dict[str, float | None] = {"A": None, "B": None}
        self.extranonce1: Dict[str, Optional[str]] = {"A": None, "B": None}
        self.extranonce2_size: Dict[str, Optional[int]] = {"A": None, "B": None}

        self.latest_diff: Dict[str, Optional[float]] = {"A": None, "B": None}
        self.last_downstream_diff_by_pool: Dict[str, Optional[float]] = {"A": None, "B": None}
        self.last_downstream_extranonce: Optional[tuple[str, int]] = None
        self.downstream_setup_lock = asyncio.Lock()
        self.last_downstream_en1: Optional[str] = None
        self.last_downstream_en2s: Optional[int] = None

        # Start active on the pool with higher weight (avoids early misroutes at 0/100 or 100/0).
        if cfg.sched.wA <= 0 and cfg.sched.wB > 0:
            self.active_pool: str = "B"
        elif cfg.sched.wB <= 0 and cfg.sched.wA > 0:
            self.active_pool: str = "A"
        elif cfg.sched.wB > cfg.sched.wA:
            self.active_pool: str = "B"
        else:
            self.active_pool: str = "A"

        self.job_owner: Dict[tuple, str] = {}  # key=(pool_key, jobid)

        self.last_forwarded_jobid: str | None = None

        self.last_forwarded_pool: str | None = None
        self.submit_owner: Dict[Any, str] = {}
        # per-pool (pool_key,id) de-dupe to prevent collisions between pools
        self.seen_upstream_response_ids = set()
        self.handshake_pool: str | None = None  # selected pool for subscribe/authorize handshake responses
        # de-dupe upstream responses (subscribe/authorize collisions)
        self.submit_diff: Dict[Any, float] = {}
        self.accepted_diff_sum: Dict[str, float] = {"A": 0.0, "B": 0.0}
        # Deduplicate submits to avoid upstream "Duplicate share" when miners retry submits.
        # key: pool -> {fingerprint: last_seen_monotonic}
        self.submit_fp_last: Dict[str, Dict[tuple, float]] = {"A": {}, "B": {}}
        self.submit_fp_max: int = 512
        self.submit_fp_ttl_s: float = 45.0

        self.sched = RatioScheduler(cfg.sched.wA, cfg.sched.wB)
        # Internal upstream bootstrap (subscribe/auth) so both pools produce notify,
        # even if the miner never sends subscribe/authorize.
        self._internal_next_id: int = 9000000
        self._internal_ids: set[int] = set()
        self._internal_subscribe_id: Dict[str, int] = {}   # pool_key -> id
        self._internal_authorize_id: Dict[str, int] = {}   # pool_key -> id

        # Failover state 
        # pool_alive: True when pool TCP connection is healthy and reading.
        #             Set to False when pool_reader detects EOF or error.
        #             Set back to True when reconnect succeeds.
        self.pool_alive: Dict[str, bool] = {"A": True, "B": True}

        # pool_fail_count: consecutive reconnect failures (drives exponential backoff).
        #                  Reset to 0 on successful reconnect.
        self.pool_fail_count: Dict[str, int] = {"A": 0, "B": 0}

        # pool_last_fail_mono: monotonic timestamp of the most recent failure.
        #                      Used to calculate "how long has this pool been down?"
        self.pool_last_fail_mono: Dict[str, Optional[float]] = {"A": None, "B": None}

        # pool_reconnect_task: handle to the background asyncio task that is
        #                      currently trying to reconnect this pool (or None).
        #                      Prevents launching duplicate reconnect attempts.
        self.pool_reconnect_task: Dict[str, Optional[asyncio.Task]] = {"A": None, "B": None}

        # original_weights: snapshot of the configured weights from config_v2.json.
        #                   When a pool goes down, the scheduler temporarily acts as
        #                   if the dead pool has weight 0. When the pool recovers,
        #                   we restore these original values.
        self.original_weights: tuple[int, int] = (cfg.sched.wA, cfg.sched.wB)
        # End failover state 

    # Send JSON stratum message upstream to pool A or B
    async def send_upstream(self, pool_key: str, msg: dict) -> None:
        """Send a JSON stratum message upstream to pool A or B."""
        raw = dumps_json(msg)
        w = self.pool_w.get(pool_key)
        if w is None:
            # pool writer not ready yet -> queue the raw line and flush when pool connects
            q = self.up_q.setdefault(pool_key, [])
            q.append((raw, pool_key))
            log("send_upstream_queued", sid=self.sid, pool=pool_key, qlen=len(q))
            return
        await write_line(w, raw, f"upstream{pool_key}")

    # Get next internal id for subscribe/authorize bootstrap
    def next_internal_id(self) -> int:
        self._internal_next_id += 1
        return self._internal_next_id

    # Bootstrap pool connection with subscribe/authorize
    async def bootstrap_pool(self, pcfg: PoolCfg, is_reconnect: bool = False) -> None:
        """Internal subscribe/auth to ensure pool emits notify and we can cache jobs.
        
        Only bootstrap the NON-handshake pool at initial startup. The handshake
        pool gets its subscribe/authorize from the miner directly during the
        initial handshake.
        
        On RECONNECT (is_reconnect=True), always bootstrap -- the miner won't
        re-send subscribe/authorize, so we must do it ourselves.
        """
        if not is_reconnect:
            # Determine which pool will be the handshake pool (same logic as in miner_to_pools)
            try:
                wA = float(getattr(self.cfg.sched, "wA", 0))
                wB = float(getattr(self.cfg.sched, "wB", 0))
            except Exception:
                wA, wB = 1.0, 0.0
            
            if wA <= 0 and wB > 0:
                handshake = "B"
            elif wB <= 0 and wA > 0:
                handshake = "A"
            elif wB > wA:
                handshake = "B"
            else:
                handshake = "A"
            
            # Only bootstrap the NON-handshake pool at initial startup
            if pcfg.key == handshake:
                log("bootstrap_skipped_handshake_pool", sid=self.sid, pool=pcfg.key, handshake=handshake)
                return
        else:
            log("bootstrap_reconnect_forced", sid=self.sid, pool=pcfg.key,
                reason="reconnect always bootstraps")

        try:
            sid_sub = self.next_internal_id()
            self._internal_ids.add(sid_sub)
            self._internal_subscribe_id[pcfg.key] = sid_sub
            sub = {"id": sid_sub, "method": "mining.subscribe", "params": ["dpmpv2/1.0"]}
            await self.send_upstream(pcfg.key, sub)
            log("pool_bootstrap_subscribe_sent", sid=self.sid, pool=pcfg.key, id=sid_sub)

            # remove the initial bootstrap as it does not play well with ckpool (Bassin), we
            # were getting "Worker Mismatch" from Bassin.
            # so instead of bootstrap then subscribe, we just subscribe which seems to be enough 
            #aid = self.next_internal_id()
            #self._internal_ids.add(aid)
            #self._internal_authorize_id[pcfg.key] = aid
            #user = f"{pcfg.wallet}.dpmp_bootstrap" if pcfg.wallet else "dpmp_bootstrap"
            #auth = {"id": aid, "method": "mining.authorize", "params": [user, "x"]}
            #await self.send_upstream(pcfg.key, auth)
            #log("pool_bootstrap_authorize_sent", sid=self.sid, pool=pcfg.key, id=aid, user=user)
        except Exception as e:
            log("pool_bootstrap_error", sid=self.sid, pool=pcfg.key, err=str(e))

    # Connect to upstream pool    
    async def connect_pool(self, pcfg: PoolCfg, is_reconnect: bool = False) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        log("pool_connecting", key=pcfg.key, pool=pcfg.name, host=pcfg.host, port=pcfg.port)
        r, w = await asyncio.open_connection(pcfg.host, pcfg.port)
        CONN_UPSTREAM.labels(pool=pcfg.key).inc()
        log("pool_connected", key=pcfg.key, pool=pcfg.name, host=pcfg.host, port=pcfg.port)

        # register writer immediately + flush any queued upstream messages for this pool
        self.pool_w[pcfg.key] = w
        q = self.up_q.get(pcfg.key) or []
        if q:
            to_flush = [raw for (raw, tag) in q if tag == pcfg.key]
            keep = [(raw, tag) for (raw, tag) in q if tag != pcfg.key]
            if to_flush:
                log("send_upstream_flush_start", sid=self.sid, pool=pcfg.key, qlen=len(to_flush))
                for raw in to_flush:
                    await write_line(w, raw, f"upstream{pcfg.key}")
                log("send_upstream_flush_done", sid=self.sid, pool=pcfg.key)
            self.up_q[pcfg.key] = keep
        
        await self.bootstrap_pool(pcfg, is_reconnect=is_reconnect)
        return r, w

    # Rewrite authorize message to use configured wallet and extracted worker name
    def rewrite_authorize(self, pcfg: PoolCfg, msg: Dict[str, Any]) -> Dict[str, Any]:
        params = msg.get("params") or []
        miner_user = str(params[0]) if len(params) >= 1 else ""

        # Only learn/overwrite worker name when miner_user is non-empty.
        # This prevents autoauthorize (which may start with empty/unknown user) from clobbering
        # an already-known worker name.
        if miner_user.strip():
            self.worker = extract_worker_name(miner_user)

        pw = str(params[1]) if len(params) >= 2 else "x"
        worker = (self.worker or "worker").strip() or "worker"
        user = f"{pcfg.wallet}.{worker}" if pcfg.wallet else worker
        out = dict(msg)
        out["params"] = [user, pw]
        return out

    # Downstream difficulty policy
    def downstream_diff_policy(self, pool_key: str) -> Optional[float]:
        d = self.latest_diff.get(pool_key)
        if d is None:
            return None

        # Config-driven clamp to keep pools from forcing unusably-low (or high) downstream difficulty.
        # Example config:
        # "downstream_diff": {"default_min": 1, "poolA_min": 8192, "poolB_min": 1, "poolA_max": null, "poolB_max": null}
        dd = getattr(self.cfg, "downstream_diff", None)
        if isinstance(dd, dict):
            default_min = dd.get("default_min")
            pool_min = dd.get(f"pool{pool_key}_min", default_min)
            pool_max = dd.get(f"pool{pool_key}_max")
        else:
            pool_min = None
            pool_max = None

        try:
            v = float(d)
        except Exception:
            return None

        if pool_min is not None:
            try:
                vmin = float(pool_min)
                if v < vmin:
                    v = vmin
            except Exception:
                pass

        if pool_max is not None:
            try:
                vmax = float(pool_max)
                if v > vmax:
                    v = vmax
            except Exception:
                pass

        # Miner-compat: force integer difficulty downstream (avoid fractional VarDiff diffs).
        # IMPORTANT: return an int so JSON params are [756] not [756.0] (some miners ignore float diffs).

        try:
            v = int(float(v) + 0.999999)  # ceil without math import
        except Exception:
            pass

        return v

    # Send extranonce1 and extranonce2_size downstream if changed
    async def maybe_send_downstream_extranonce(self, pool_key: str):
        # If we already raw-forwarded the pool's subscribe result,
        # do NOT re-send extranonce (pool-agnostic safety).
        # The miner already received the extranonce inside the subscribe response.
        # Sending mining.set_extranonce to miners that don't support it
        # (NerdAxe, NerdMiner, etc.) causes disconnect/reboot loops.
        if getattr(self, "raw_subscribe_forwarded_pool", None) == pool_key:
            # Only safe to skip if no OTHER pool's extranonce has been sent yet.
            # Once we send mining.set_extranonce for Pool B, the miner loses
            # Pool A's subscribe extranonce.  Switching back to A requires an
            # explicit send even though A was the raw-subscribe pool.
            last_en_pool = getattr(self, "last_downstream_extranonce_pool", None)
            if last_en_pool is not None and last_en_pool != pool_key:
                # Miner currently has a DIFFERENT pool's extranonce -- must send.
                # Fall through to the normal send path below.
                pass
            else:
                # Miner still has the subscribe extranonce -- safe to skip.
                en1 = self.extranonce1.get(pool_key)
                en2s = self.extranonce2_size.get(pool_key)
                if en1 is not None and en2s is not None:
                    self.last_downstream_en1 = str(en1)
                    self.last_downstream_en2s = int(en2s)
                    self.last_downstream_extranonce_pool = pool_key
                log("downstream_extranonce_skip_raw_subscribe", pool=pool_key,
                    raw_subscribe_forwarded_pool=getattr(self, "raw_subscribe_forwarded_pool", None),
                    last_downstream_extranonce_pool=getattr(self, "last_downstream_extranonce_pool", None))
                return
        en1 = self.extranonce1.get(pool_key)
        en2s = self.extranonce2_size.get(pool_key)
        if not en1 or en2s is None:
            log("downstream_extranonce_skip_no_data", sid=self.sid, pool=pool_key,
                en1=en1, en2s=en2s)
            return

        async with self.downstream_setup_lock:
            new_en1 = str(en1)
            new_en2s = int(en2s)
            
            # Force send if the miner's current extranonce context is for a
            # DIFFERENT pool than the one we're switching to.  This is the only
            # time we truly need to send mining.set_extranonce.
            # When the same non-handshake pool sends a new notify with the same
            # extranonce, we skip (no redundant sends that would crash NerdAxe).
            handshake = getattr(self, "handshake_pool", None)
            last_en_pool = getattr(self, "last_downstream_extranonce_pool", None)
            force_send = (last_en_pool is not None and last_en_pool != pool_key)

            # Debug logging to see why force_send might be False
            log("downstream_extranonce_check", sid=self.sid, pool=pool_key,
                handshake=handshake, last_en_pool=last_en_pool, force_send=force_send,
                new_en1=new_en1, new_en2s=new_en2s,
                last_en1=self.last_downstream_en1, last_en2s=self.last_downstream_en2s)
            
            # Only skip if NOT force_send AND values unchanged
            if not force_send and self.last_downstream_en1 == new_en1 and self.last_downstream_en2s == new_en2s:
                log("downstream_extranonce_skip_nochange", sid=self.sid, pool=pool_key,
                    en1=new_en1, en2s=new_en2s,
                    last_en1=str(self.last_downstream_en1), last_en2s=str(self.last_downstream_en2s),
                    force_send=force_send, handshake=handshake, last_en_pool=last_en_pool)
                return

            # Send the extranonce
            msg = {"method": "mining.set_extranonce", "params": [new_en1, new_en2s]}
            try:
                await write_line(self.miner_w, dumps_json(msg), "downstream")
            except Exception as e:
                log("downstream_extranonce_send_error", sid=self.sid, pool=pool_key, err=str(e),
                    extranonce1=new_en1, extranonce2_size=new_en2s)
                raise

            self.last_downstream_en1 = new_en1
            self.last_downstream_en2s = new_en2s
            self.last_downstream_extranonce_pool = pool_key
            log("downstream_extranonce_set", sid=self.sid, pool=pool_key, extranonce1=new_en1, extranonce2_size=new_en2s,
                force_send=force_send, handshake=handshake)

    # Send downstream difficulty if changed
    async def maybe_send_downstream_diff(self, pool_key: str, force: bool = False) -> bool:
        # If a pool is disabled by scheduler weights, never send its difficulty downstream.
        # Prevents diff flips from the non-active pool (poisoning).
        if pool_key == "A" and self.cfg.sched.wA <= 0:
            return False
        if pool_key == "B" and self.cfg.sched.wB <= 0:
            return False
        async with self.downstream_setup_lock:
            dd = self.downstream_diff_policy(pool_key)
            if dd is None:
                return False
            last_dd = self.last_downstream_diff_by_pool.get(pool_key)
            if (not force) and last_dd is not None and dd == last_dd:
                return False
            dd_sent = int(dd) if dd is not None else dd
            self.last_downstream_diff_by_pool[pool_key] = dd_sent
            DIFF_DOWNSTREAM.set(dd_sent)
            log("downstream_send_diff", sid=self.sid, pool=pool_key, payload={"method":"mining.set_difficulty","params":[dd_sent]})
            await write_line(self.miner_w, dumps_json({"method": "mining.set_difficulty", "params": [dd_sent]}), "downstream")
            log("downstream_diff_set", sid=self.sid, pool=pool_key, diff=dd, diff_sent=dd_sent)
            return True

    # Resend latest notify as clean (isCleanJob=true)
    async def resend_active_notify_clean(self, pool_key: str, reason: str):
        """After diff/extranonce changes, immediately resend latest job as clean notify.
        Reduces mismatch windows that cause bursts of 'low difficulty share' rejects.
        """
        raw = self.latest_notify_raw.get(pool_key)
        jid = self.latest_jobid.get(pool_key)
        if raw is None:
            log("resend_notify_skipped_no_cached", sid=self.sid, pool=pool_key, reason=reason)
            return
        try:
            nm = loads_json(raw)
            if nm.get("method") == "mining.notify":
                params = nm.get("params") or []
                if len(params) >= 1:
                    if len(params) >= 9:
                        params[-1] = True
                    else:
                        while len(params) < 9:
                            params.append(None)
                        params[-1] = True
                    nm["params"] = params
                nm2 = sanitize_downstream_notification(nm)
                log("downstream_send_notify", payload=nm2)
                # Ensure diff context is re-asserted before resend clean notify (prevents low-diff bursts)
                await self.maybe_send_downstream_extranonce(pool_key)
                sent_diff = await self.maybe_send_downstream_diff(pool_key, force=True)
                if sent_diff:
                    await asyncio.sleep(0.25)
                await write_line(self.miner_w, dumps_json(nm2), "downstream")
                # Commit forwarded-job state for submit routing (resend path must mirror scheduler forward path)
                self.last_forwarded_pool = pool_key
                self.last_forwarded_jobid = jid
                if jid:
                    self.job_owner[(pool_key, jid)] = pool_key
                self.last_notify_mono[pool_key] = time.monotonic()
                log("resend_notify_clean", sid=self.sid, pool=pool_key, jobid=jid, reason=reason)
                return
        except Exception as e:
            log("resend_notify_error", sid=self.sid, pool=pool_key, jobid=jid, reason=reason, err=str(e))
        log("downstream_send_raw", payload=raw.decode("utf-8", errors="replace"))
        await write_line(self.miner_w, raw, "downstream")
        log("resend_notify_raw", sid=self.sid, pool=pool_key, jobid=jid, reason=reason)

    # Forward miner messages to upstream pools
    async def miner_to_pools(self):
        # At 100/0 or 0/100, only one pool writer exists. That's OK.
        assert self.wA or self.wB, "No upstream pool connections available"

        async for raw in iter_lines(self.miner_r, "downstream"):
            try:
                msg = loads_json(raw)
            except Exception as e:
                log("miner_bad_json", err=str(e))
                continue

            m = msg.get("method")
            if m:
                log("miner_method", sid=self.sid, method=m)
            if m == "mining.configure":
                # Forward mining.configure to the handshake pool so its response goes back to the miner,
                # BUT also send a copy to the other pool using an internal id so we can consume the reply
                # without forwarding it downstream. This prevents Pool B "low difficulty share" rejects
                # when the miner is version-rolling.
                try:
                    cfg_id = msg.get("id")
                    if self.handshake_pool is None:
                        # Choose handshake pool from config weights (avoid hard-wiring to A).
                        try:
                            wA = float(getattr(self.cfg.sched, "wA", 0))
                            wB = float(getattr(self.cfg.sched, "wB", 0))
                        except Exception:
                            wA, wB = 1.0, 0.0
                        if wA <= 0 and wB > 0:
                            self.handshake_pool = "B"
                        elif wB <= 0 and wA > 0:
                            self.handshake_pool = "A"
                        elif wB > wA:
                            self.handshake_pool = "B"
                        else:
                            self.handshake_pool = "A"
                    hp = self.handshake_pool
                    other = "B" if hp == "A" else "A"

                    # 1) forward original to handshake pool (reply goes to miner)
                    await self.send_upstream(hp, msg)

                    # 2) forward copy to other pool (reply is internal-only)
                    # Skip if the other pool has zero weight (not connected).
                    other_w = getattr(self.cfg.sched, "wB" if other == "B" else "wA", 0)
                    if other_w > 0:
                        iid = self.next_internal_id()
                        self._internal_ids.add(iid)
                        msg2 = dict(msg)
                        msg2["id"] = iid
                        await self.send_upstream(other, msg2)
                        log("configure_forwarded_both_pools", sid=self.sid, handshake=hp, other=other, id=cfg_id, internal_id=iid)
                    else:
                        log("configure_skip_zero_weight_pool", sid=self.sid, pool=other)

                except Exception as e:
                    log("configure_forward_both_error", sid=self.sid, err=str(e))
                continue

            if m == "mining.subscribe":
                self.subscribe_id = msg.get("id")
                if self.handshake_pool is None:
                    # Choose handshake pool from config weights (avoid hard-wiring to A).
                    try:
                        wA = float(getattr(self.cfg.sched, "wA", 0))
                        wB = float(getattr(self.cfg.sched, "wB", 0))
                    except Exception:
                        wA, wB = 1.0, 0.0
                    if wA <= 0 and wB > 0:
                        self.handshake_pool = "B"
                    elif wB <= 0 and wA > 0:
                        self.handshake_pool = "A"
                    elif wB > wA:
                        self.handshake_pool = "B"
                    else:
                        self.handshake_pool = "A"

                # Mark that we expect a raw subscribe result from the active pool
                self.expect_raw_subscribe = True

                await self.send_upstream(self.handshake_pool, msg)
                continue


            if m == "mining.authorize":
                self.authorize_id = msg.get("id")
                if self.handshake_pool is None:
                    # Choose handshake pool from config weights (avoid hard-wiring to A).
                    try:
                        wA = float(getattr(self.cfg.sched, "wA", 0))
                        wB = float(getattr(self.cfg.sched, "wB", 0))
                    except Exception:
                        wA, wB = 1.0, 0.0
                    if wA <= 0 and wB > 0:
                        self.handshake_pool = "B"
                    elif wB <= 0 and wA > 0:
                        self.handshake_pool = "A"
                    elif wB > wA:
                        self.handshake_pool = "B"
                    else:
                        self.handshake_pool = "A"

                # Send rewritten authorize to the handshake pool (primary) AND the other pool (secondary)
                primary = self.handshake_pool
                secondary = "B" if primary == "A" else "A"

                if primary == "A":
                    out_primary = self.rewrite_authorize(self.cfg.poolA, msg)
                    out_secondary = self.rewrite_authorize(self.cfg.poolB, msg)
                else:
                    out_primary = self.rewrite_authorize(self.cfg.poolB, msg)
                    out_secondary = self.rewrite_authorize(self.cfg.poolA, msg)

                log("authorize_rewrite", pool=primary, worker=self.worker, upstream_user=out_primary["params"][0])
                await self.send_upstream(primary, out_primary)

                # Also authorize to the other pool so its UI shows the real worker name (not dpmp_bootstrap).
                # Skip if the other pool has zero weight (not connected).
                other = "B" if self.handshake_pool == "A" else "A"
                other_w = getattr(self.cfg.sched, "wB" if other == "B" else "wA", 0)
                if other_w > 0:
                    try:
                        ocfg = self.cfg.poolB if other == "B" else self.cfg.poolA
                        out2 = self.rewrite_authorize(ocfg, msg)
                        log("authorize_rewrite_other", pool=other, worker=self.worker, upstream_user=out2["params"][0])
                        await self.send_upstream(other, out2)
                    except Exception as e:
                        log("authorize_rewrite_other_error", pool=other, worker=self.worker, err=str(e))

                    # Ensure pools that key UI on authorize see the real miner worker name.
                    try:
                        log("authorize_rewrite_secondary", pool=secondary, worker=self.worker, upstream_user=out_secondary["params"][0])
                        await self.send_upstream(secondary, out_secondary)
                    except Exception as e:
                        log("authorize_secondary_send_error", sid=self.sid, pool=secondary, err=str(e))
                else:
                    log("authorize_skip_zero_weight_pool", sid=self.sid, pool=other)

                self.miner_ready.set()
                log("miner_ready_for_jobs", sid=self.sid, worker=self.worker, handshake_pool=self.handshake_pool)

                # Immediately push extranonce+diff so the miner has correct targets before submitting.
                # Skip extranonce if the miner already received it via raw subscribe response
                # (avoids mining.set_extranonce to miners that don't support it like NerdAxe).
                try:
                    if getattr(self, "raw_subscribe_forwarded_pool", None) != self.handshake_pool:
                        await self.maybe_send_downstream_extranonce(self.handshake_pool)
                    else:
                        log("post_auth_extranonce_skip_raw_subscribe", sid=self.sid, pool=self.handshake_pool)
                    await self.maybe_send_downstream_diff(self.handshake_pool)
                    log("post_auth_downstream_sync", sid=self.sid, pool=self.handshake_pool)
                except Exception as e:
                    log("post_auth_downstream_sync_error", sid=self.sid, pool=self.handshake_pool, err=str(e))

                continue

            if m == "mining.submit":
                # Guard: drop submits until we've forwarded at least one job in this session.
                # Prevents stale submits right after reconnect causing 'job not found'.
                if self.last_forwarded_jobid is None:
                    log("submit_dropped_no_job_yet", sid=self.sid, mid=msg.get("id"), jid=jobid_from_submit(msg), last_pool=self.last_forwarded_pool)
                    await write_line(self.miner_w, dumps_json({"id": msg.get("id"), "result": False, "error": {"code": 21, "message": "job not found", "data": None}}), "downstream")
                    continue

                SHARES_SUBMITTED.inc()
                jid = jobid_from_submit(msg)
                pool = "A"
                reason = "default_A"
                if jid is None:
                    # Some miners/pools may omit/obscure jobid in submit; fall back to last forwarded pool.
                    if self.last_forwarded_pool in ("A","B"):
                        pool = self.last_forwarded_pool
                        reason = "no_jid_fallback"
                else:
                    # Prefer stable job->pool mapping first (critical during pool switching).
                    pool_map = self.job_owner.get(("A", jid)) or self.job_owner.get(("B", jid))
                    if pool_map in ("A","B"):
                        pool = pool_map
                        reason = "job_owner_map"
                    elif self.last_forwarded_jobid == jid and self.last_forwarded_pool in ("A","B"):
                        pool = self.last_forwarded_pool
                        reason = "last_forwarded_match"
                    elif self.last_forwarded_pool in ("A","B"):
                        # If miner submits a jid we never forwarded/mapped, do NOT forward upstream.
                        # Avoid upstream "job not found" churn (seen on Nano3S right after connect).
                        if self.last_forwarded_jobid is not None and jid != self.last_forwarded_jobid:
                            log("submit_dropped_unknown_jid", sid=self.sid, mid=msg.get("id"), jid=jid,
                                last_jobid=self.last_forwarded_jobid, last_pool=self.last_forwarded_pool)
                            await write_line(self.miner_w, dumps_json({"id": msg.get("id"), "result": False,
                                "error": {"code": 21, "message": "job not found", "data": None}}), "downstream")
                            continue
                        pool = self.last_forwarded_pool
                        reason = "last_forwarded_pool_fallback"
                log("submit_route", sid=self.sid, jid=jid, pool=pool, reason=reason,
                    last_jobid=self.last_forwarded_jobid, last_pool=self.last_forwarded_pool)
                # Dedupe: miners sometimes retry identical submits (timeout / reconnect).
                # Forwarding duplicates upstream produces "Duplicate share" rejects.
                try:
                    pms = msg.get("params") or []
                    # Params: [user, jobid, extranonce2, ntime, nonce, (optional) versionbits]
                    fp = (
                        str(pms[1]) if len(pms) > 1 else None,
                        str(pms[2]) if len(pms) > 2 else None,
                        str(pms[3]) if len(pms) > 3 else None,
                        str(pms[4]) if len(pms) > 4 else None,
                        str(pms[5]) if len(pms) > 5 else None,
                    )
                    now = time.monotonic()
                    mfp = self.submit_fp_last.get(pool)
                    if mfp is None:
                        mfp = {}
                        self.submit_fp_last[pool] = mfp
                    ttl = float(getattr(self, "submit_fp_ttl_s", 45.0) or 45.0)
                    if mfp:
                        old = [k for k,v in mfp.items() if (now - float(v)) > ttl]
                        for k in old:
                            mfp.pop(k, None)
                    mx = int(getattr(self, "submit_fp_max", 512) or 512)
                    if len(mfp) > mx:
                        for k,_v in sorted(mfp.items(), key=lambda kv: kv[1])[: max(1, len(mfp) - mx)]:
                            mfp.pop(k, None)
                    last = mfp.get(fp)
                    if last is not None and (now - float(last)) <= ttl:
                        log("submit_dropped_duplicate_fp", sid=self.sid, mid=msg.get("id"), jid=jid, pool=pool)
                        await write_line(self.miner_w, dumps_json({"id": msg.get("id"), "result": False,
                            "error": {"code": 22, "message": "duplicate share", "data": None}}), "downstream")
                        continue
                    mfp[fp] = now
                except Exception as e:
                    log("submit_dedupe_error", sid=self.sid, err=str(e))


                # Guard: reject submits if miner extranonce context doesn't match target pool.
                # If we recently sent mining.set_extranonce for the other pool, the miner may be
                # building shares against the wrong extranonce1, which will produce mass rejects.
                ex_pool = getattr(self, "last_downstream_extranonce_pool", None)
                if ex_pool in ("A", "B") and ex_pool != pool:
                    age = None
                    if self.last_switch_mono is not None:
                        age = time.monotonic() - float(self.last_switch_mono)

                    if age is not None and age < SWITCH_SUBMIT_GRACE_S:
                        # Grace window: allow in-flight submits for the previous pool job to be forwarded.
                        # We route by job ownership (target_pool=pool). Rejecting here creates unnecessary drops.
                        log("submit_extranonce_mismatch_grace_forward", sid=self.sid, mid=msg.get("id"), jid=jid,
                            target_pool=pool, last_extranonce_pool=ex_pool, age_s=round(age, 3))
                    else:
                        log("submit_dropped_extranonce_mismatch", sid=self.sid, mid=msg.get("id"), jid=jid,
                            target_pool=pool, last_extranonce_pool=ex_pool,
                            last_jobid=self.last_forwarded_jobid, last_pool=self.last_forwarded_pool)
                        await write_line(self.miner_w, dumps_json({"id": msg.get("id"), "result": False,
                            "error": {"code": 23, "message": "stale extranonce context", "data": None}}), "downstream")
                        continue

                self.submit_owner[msg.get("id")] = pool
                mid = msg.get("id")
                if mid is not None:
                    d = self.last_downstream_diff_by_pool.get(pool)
                    # Submit-time snapshot for debugging diff mismatches (VarDiff / miner apply lag).
                    pms = msg.get("params") or []
                    u0 = pms[0] if len(pms) > 0 else None
                    en2 = pms[2] if len(pms) > 2 else None
                    ntime = pms[3] if len(pms) > 3 else None
                    nonce = pms[4] if len(pms) > 4 else None
                    # versionbits is optional (only for version-rolling miners)
                    vb = pms[5] if len(pms) > 5 else None

                    log("submit_snapshot", sid=self.sid, jid=jid, pool=pool, mid=mid,
                        user=u0, extranonce2=en2, ntime=ntime, nonce=nonce, versionbits=vb,
                        active=(self.last_forwarded_pool or self.handshake_pool),
                        raw_subscribe_forwarded_pool=getattr(self, "raw_subscribe_forwarded_pool", None),
                        last_downstream_diff_snapshot=d, pool_latest_diff=self.latest_diff.get(pool),
                        last_jobid=self.last_forwarded_jobid, last_pool=self.last_forwarded_pool)

                    # Local quick sanity: estimate share difficulty from submit nonce.
                    try:
                        # Params: [user, jobid, extranonce2, ntime, nonce, (optional) versionbits]
                        p = msg.get("params") or []
                        nonce_hex = p[4] if len(p) > 4 else None
                        if nonce_hex is not None:
                            # Very rough heuristic: random hash expected diff ~ 1
                            # If miner were meeting diff~3000, accept rate would be ~1/3000.
                            # Log just to correlate submit frequency vs expected accepts.
                            log("submit_local_sanity",
                                sid=self.sid, mid=mid, jid=jid, pool=pool,
                                expected_accept_rate=f"~1/{int(float(d or self.latest_diff.get(pool) or 1))}")

                    except Exception as e:
                        log("submit_local_sanity_error", sid=self.sid, err=str(e))

                    if d is None:
                        d = self.latest_diff.get(pool)
                    self.submit_diff[mid] = float(d or 0.0)

                # Failover guard: reject submit if target pool is dead 
                # If the pool that owns this job just died, we can't forward
                # the share.  Send the miner a clean rejection instead of
                # crashing on a None writer.
                if not self.pool_alive.get(pool, False):
                    log("submit_dropped_pool_dead", sid=self.sid, mid=msg.get("id"),
                        jid=jid, pool=pool)
                    self.submit_owner.pop(msg.get("id"), None)
                    self.submit_diff.pop(msg.get("id"), None)
                    await write_line(self.miner_w, dumps_json({
                        "id": msg.get("id"), "result": False,
                        "error": {"code": 21, "message": "pool unavailable", "data": None}
                    }), "downstream")
                    continue

                if pool == "B":
                    out = dict(msg)
                    params = list(out.get("params") or [])
                    if params:
                        # submit user should match pool wallet + miner worker
                        params[0] = f"{self.cfg.poolB.wallet}.{self.worker}" if self.cfg.poolB.wallet else str(params[0])
                        # Keep versionbits if present (miners may be version-rolling).
                        out["params"] = params
                    await write_line(self.wB, dumps_json(out), "upstreamB")
                else:
                    out = dict(msg)
                    params = list(out.get("params") or [])
                    if params:
                        # submit user should match pool wallet + miner worker
                        params[0] = f"{self.cfg.poolA.wallet}.{self.worker}" if self.cfg.poolA.wallet else str(params[0])
                        # Keep versionbits if present (miners may be version-rolling).
                        out["params"] = params
                    await write_line(self.wA, dumps_json(out), "upstreamA")
                continue

            if self.wA is not None:
                await write_line(self.wA, raw, "upstreamA")
            if self.wB is not None:
                await write_line(self.wB, raw, "upstreamB")

    # Read from upstream pool
    async def pool_reader(self, pool_key: str, reader: asyncio.StreamReader):
        side = "upstreamA" if pool_key == "A" else "upstreamB"
        async for raw in iter_lines(reader, side):
            try:
                msg = loads_json(raw)
            except Exception:
                continue

            method = msg.get("method")

            mid = msg.get("id")

            if mid is not None and method is None:

                if (pool_key, mid) in self.seen_upstream_response_ids:

                    log("upstream_response_dup_observed", sid=self.sid, pool=pool_key, id=mid)
                    continue

                self.seen_upstream_response_ids.add((pool_key, mid))

            if method == "mining.set_difficulty":
                # Track upstream diff, but DO NOT forward directly to miner.
                # Downstream difficulty is sent only by the scheduler path to avoid race/mismatch.
                try:
                    v = float((msg.get("params") or [None])[0])
                    self.latest_diff[pool_key] = v
                    log("pool_diff", pool=pool_key, diff=self.latest_diff[pool_key])
                except Exception:
                    pass
                continue

            if method == "mining.notify":
                # Cache notify; scheduler is the ONLY code path that forwards notify downstream
                # (it forces clean_jobs=True and sends extranonce/diff first).
                self.latest_notify_raw[pool_key] = raw
                jid = jobid_from_notify(msg)
                self.latest_jobid[pool_key] = jid
                self.notify_seq[pool_key] += 1
                log("pool_notify", sid=self.sid, pool=pool_key, jobid=jid, seq=self.notify_seq[pool_key])
                continue

            if "id" in msg and msg.get("method") is None:
                mid = msg.get("id")

                # INTERNAL bootstrap traffic: process but DO NOT forward to miner.
                if isinstance(mid, int) and mid in getattr(self, "_internal_ids", set()):
                    if getattr(self, "_internal_subscribe_id", {}).get(pool_key) == mid:
                        try:
                            res = msg.get("result")
                            # Typical subscribe result: [ [..], extranonce1, extranonce2_size ]
                            if isinstance(res, list) and len(res) >= 3:
                                en1 = res[1]
                                en2s = res[2]
                                if en1 is not None:
                                    self.extranonce1[pool_key] = str(en1)
                                if en2s is not None:
                                    self.extranonce2_size[pool_key] = int(en2s)
                                log("pool_bootstrap_subscribe_result", sid=self.sid, pool=pool_key,
                                    extranonce1=self.extranonce1[pool_key], extranonce2_size=self.extranonce2_size[pool_key])
                        except Exception as e:
                            log("pool_bootstrap_subscribe_parse_error", sid=self.sid, pool=pool_key, err=str(e))
                    if getattr(self, "_internal_authorize_id", {}).get(pool_key) == mid:
                        log("pool_bootstrap_auth_result", sid=self.sid, pool=pool_key,
                            ok=bool(msg.get("result")), error=msg.get("error"))
                    continue

                # Capture per-pool subscribe response (extranonce context)
                if self.subscribe_id is not None and mid == self.subscribe_id:

                    # Raw-forward subscribe result for the active pool (pool-agnostic)
                    active = self.last_forwarded_pool or self.handshake_pool
                    if active == pool_key:
                        await write_line(self.miner_w, raw, "downstream")
                        log("downstream_subscribe_forwarded_raw", sid=self.sid, pool=pool_key)
                        self.raw_subscribe_forwarded_pool = pool_key

                        # If we had to buffer a notify waiting for subscribe, flush it now.
                        try:
                            raw_n = self.latest_notify_raw.get(pool_key)
                            if raw_n:
                                # await write_line(self.miner_w, raw_n, "downstream")
                                log("downstream_notify_flushed_after_subscribe", sid=self.sid, pool=pool_key)
                        except Exception:
                            pass

                    try:
                        res = msg.get("result")
                        # Typical subscribe result: [ [..], extranonce1, extranonce2_size ]
                        if isinstance(res, list) and len(res) >= 3:
                            en1 = res[1]
                            en2s = res[2]
                            if en1 is not None:
                                self.extranonce1[pool_key] = str(en1)
                            if en2s is not None:
                                self.extranonce2_size[pool_key] = int(en2s)
                            log("subscribe_result", pool=pool_key, extranonce1=self.extranonce1[pool_key], extranonce2_size=self.extranonce2_size[pool_key])

                            # Immediately provide extranonce context to the miner for the active pool.
                            # BUT skip if we just raw-forwarded the subscribe response -- the miner
                            # already has the extranonce from that response.  Sending mining.set_extranonce
                            # to miners that don't support it (NerdAxe, NerdMiner, etc.) causes them
                            # to disconnect or reboot in a loop.
                            try:
                                active = self.last_forwarded_pool or self.handshake_pool
                                if active == pool_key and self.extranonce1.get(pool_key) is not None and self.extranonce2_size.get(pool_key) is not None:
                                    if getattr(self, "raw_subscribe_forwarded_pool", None) == pool_key:
                                        log("downstream_extranonce_skip_already_in_subscribe", sid=self.sid, pool=pool_key,
                                            extranonce1=self.extranonce1[pool_key], extranonce2_size=int(self.extranonce2_size[pool_key]))
                                    else:
                                        en_msg = {"method": "mining.set_extranonce", "params": [self.extranonce1[pool_key], int(self.extranonce2_size[pool_key])]}
                                        log("downstream_send_extranonce", sid=self.sid, pool=pool_key, extranonce1=self.extranonce1[pool_key], extranonce2_size=int(self.extranonce2_size[pool_key]))
                                        await write_line(self.miner_w, dumps_json(en_msg), "downstream")
                            except Exception as e:
                                log("downstream_send_extranonce_error", sid=self.sid, pool=pool_key, err=str(e))

                    except Exception as e:
                        log("subscribe_parse_error", pool=pool_key, err=str(e))

                if self.authorize_id is not None and mid == self.authorize_id:
                    ok_auth = bool(msg.get("result"))
                    log("auth_result", pool=pool_key, ok=ok_auth, error=msg.get("error"))

                    # Step A: after authorize OK (handshake pool), immediately push setup context in correct order:
                    # set_extranonce -> set_difficulty -> notify(clean_jobs=true)
                    # BUT skip if scheduler already switched to the other pool (don't overwrite Pool A's context)
                    last_en_pool = getattr(self, "last_downstream_extranonce_pool", None)
                    already_switched_away = (last_en_pool is not None and last_en_pool != pool_key)
                    if ok_auth and (self.handshake_pool is not None) and (pool_key == self.handshake_pool) and (not already_switched_away):
                        try:
                            # Extranonce (if known)
                            # Skip if we already raw-forwarded the subscribe response for this pool 
                            # the miner already has the extranonce.  Sending mining.set_extranonce
                            # to miners that don't support it (NerdAxe, NerdMiner, etc.) causes
                            # disconnect/reboot loops.
                            en1 = self.extranonce1.get(pool_key)
                            en2s = self.extranonce2_size.get(pool_key)
                            if en1 is not None and en2s is not None:
                                if getattr(self, "raw_subscribe_forwarded_pool", None) == pool_key:
                                    log("post_auth_extranonce_skip_already_in_subscribe", sid=self.sid, pool=pool_key,
                                        extranonce1=str(en1), extranonce2_size=int(en2s))
                                else:
                                    en_msg = {"method": "mining.set_extranonce", "params": [str(en1), int(en2s)]}
                                    log("post_auth_push_extranonce", sid=self.sid, pool=pool_key, extranonce1=str(en1), extranonce2_size=int(en2s))
                                    await write_line(self.miner_w, dumps_json(en_msg), "downstream")
                            # Difficulty (prefer latest pool diff if we have it)
                            diff = None
                            try:
                                diff = float(self.latest_diff.get(pool_key))
                            except Exception:
                                diff = None
                            if diff is not None and diff > 0:
                                dmsg = {"method": "mining.set_difficulty", "params": [int(diff)]}
                                log("post_auth_push_diff", sid=self.sid, pool=pool_key, diff=diff, diff_sent=int(diff))
                                await write_line(self.miner_w, dumps_json(dmsg), "downstream")

                            # Notify (force clean_jobs=true)
                            raw_n = self.latest_notify_raw.get(pool_key)
                            if raw_n:
                                try:
                                    n = loads_json(raw_n)
                                    if isinstance(n, dict) and n.get("method") == "mining.notify" and isinstance(n.get("params"), list) and len(n["params"]) >= 1:
                                        n["params"][-1] = True
                                        log("post_auth_push_notify_clean", sid=self.sid, pool=pool_key)
                                        await write_line(self.miner_w, dumps_json(n), "downstream")
                                        # Commit forwarded-job state for submit routing (post-auth path must mirror resend/scheduler path)
                                        jid = None
                                        try:
                                            if isinstance(n, dict):
                                                ps = n.get("params")
                                                if isinstance(ps, list) and len(ps) >= 1:
                                                    jid = ps[0]
                                        except Exception:
                                            jid = None
                                        self.last_forwarded_pool = pool_key
                                        self.last_forwarded_jobid = jid
                                        if jid:
                                            self.job_owner[(pool_key, jid)] = pool_key
                                        self.last_notify_mono[pool_key] = time.monotonic()
                                except Exception as e:
                                    log("post_auth_push_notify_clean_error", sid=self.sid, pool=pool_key, err=str(e))
                        except Exception as e:
                            log("post_auth_push_setup_error", sid=self.sid, pool=pool_key, err=str(e))

                # Forward subscribe/auth responses ONLY from the selected handshake pool
                if mid not in self.submit_owner:
                    if (mid not in self.submit_owner) and (self.handshake_pool is not None and pool_key != self.handshake_pool):
                        log("handshake_response_dropped", sid=self.sid, pool=pool_key, id=mid, chosen=self.handshake_pool)
                        continue

                log("id_response_seen", sid=self.sid, pool=pool_key, id=mid, in_submit_owner=(mid in self.submit_owner), handshake_pool=self.handshake_pool)
                # If we already raw-forwarded the subscribe response for this pool,
                # do NOT also forward it again via the generic id-response path.
                if self.subscribe_id is not None and mid == self.subscribe_id and getattr(self, "raw_subscribe_forwarded_pool", None) == pool_key:
                    log("subscribe_id_response_skipped_duplicate", sid=self.sid, pool=pool_key, id=mid)
                    continue

                if mid in self.submit_owner:
                    p = self.submit_owner.pop(mid)
                    d = float(self.submit_diff.pop(mid, 0.0))
                    ok = bool(msg.get("result"))

                    if ok:
                        SHARES_ACCEPTED.labels(pool=p).inc()
                        ACCEPTED_DIFFICULTY_SUM.labels(pool=p).inc(d)
                        # Cap the difficulty credited to the scheduler counters.
                        # Without this, a single high-diff share on the minority
                        # pool can swing the ratio 30+ points (e.g., 80/20  50/50),
                        # causing a multi-minute recovery on the majority pool.
                        # Cap: one share can move the ratio by at most ~5%.
                        _total = self.accepted_diff_sum.get("A", 0.0) + self.accepted_diff_sum.get("B", 0.0)
                        _max_credit = _total * 0.10 if _total > 0 else d
                        _credit = min(d, _max_credit) if _max_credit > 0 else d
                        self.accepted_diff_sum[p] = self.accepted_diff_sum.get(p, 0.0) + _credit
                        log("share_result", sid=self.sid, pool=p, accepted=True,
                            diff=d, credit=round(_credit, 1), capped=(d != _credit))

                    else:
                        SHARES_REJECTED.labels(pool=p).inc()
                        log("share_result", sid=self.sid, pool=p, accepted=False, error=msg.get("error"))
                await write_line(self.miner_w, dumps_json(msg), "downstream")
                continue

            # Forward setup methods from the handshake pool only (but never notify/diff)
            primary = self.handshake_pool or "A"
            if pool_key == primary and method is not None:
                await write_line(self.miner_w, raw, "downstream")

    # Pool Failover: clear stale state for a dead pool
    def clear_pool_state(self, pool_key: str):
        """Wipe cached data for a pool that just disconnected.

        Why each field is cleared:
        - latest_notify_raw / latest_jobid: The old job belongs to a now-dead
          TCP session.  If the scheduler forwarded it, the miner would submit
          shares that the pool can never accept (session is gone).
        - latest_diff: The difficulty was for the old session; the pool will
          send a new one after reconnect.
        - extranonce1 / extranonce2_size: Same session-scoped values that
          become invalid when the TCP connection drops.
        - pool_w entry: The old writer is broken; remove it so send_upstream()
          queues messages instead of writing to a dead socket.
        - CONN_UPSTREAM gauge: Decrement so Prometheus reflects reality.
        - raw_subscribe_forwarded_pool: Must be cleared so that after reconnect,
          maybe_send_downstream_extranonce() is NOT blocked by the stale
          "already sent via raw subscribe" guard.  Without this, the miner
          never receives the new pool's extranonce and every share is rejected
          as "low difficulty".
        """
        self.latest_notify_raw[pool_key] = None
        self.latest_jobid[pool_key] = None
        self.latest_diff[pool_key] = None

        self.extranonce1[pool_key] = None
        self.extranonce2_size[pool_key] = None

        # Clear "last sent" tracking so reconnect WILL send the new extranonce 
        # Without this, the bootstrap response updates extranonce1[pool_key] and
        # maybe_send_downstream_extranonce() sees new==last and skips sending.
        if getattr(self, "last_downstream_extranonce_pool", None) == pool_key:
            self.last_downstream_en1 = None
            self.last_downstream_en2s = None
            self.last_downstream_extranonce_pool = None
            log("clear_pool_state_reset_last_downstream_extranonce", sid=self.sid, pool=pool_key)

        # NEW: clear the raw-subscribe guard so reconnect can send extranonce 
        if getattr(self, "raw_subscribe_forwarded_pool", None) == pool_key:
            self.raw_subscribe_forwarded_pool = None
            log("clear_pool_state_reset_raw_subscribe_flag", sid=self.sid, pool=pool_key)

        # Remove the dead writer so send_upstream() will queue, not crash.
        old_w = self.pool_w.pop(pool_key, None)
        if old_w is not None:
            try:
                old_w.close()
            except Exception:
                pass

        # Clear the reader/writer instance attributes too.
        if pool_key == "A":
            self.rA = None
            self.wA = None
        else:
            self.rB = None
            self.wB = None

        CONN_UPSTREAM.labels(pool=pool_key).dec()
        log("pool_state_cleared", sid=self.sid, pool=pool_key)


    # Periodic state pruning 
    # Several dicts/sets grow with every job or upstream response but
    # are never trimmed.  Over weeks of runtime this leaks memory.
    # This method is called every ~60 seconds from forward_jobs().
    def prune_stale_state(self):
        """Remove entries older than 5 minutes from structures that grow unbounded."""
        now = time.monotonic()
        max_age = 300.0  # 5 minutes -- no job/response older than this is useful

        # 1) job_owner: keyed by (pool_key, jobid).
        #    Keep only the last ~200 entries.  Jobs older than that are
        #    long expired; no miner will submit against them.
        max_jobs = 200
        if len(self.job_owner) > max_jobs:
            # We can't age these (no timestamp), so just keep the most recent N.
            # Since Python 3.7+ dicts preserve insertion order, drop from the front.
            excess = len(self.job_owner) - max_jobs
            keys_to_drop = list(self.job_owner.keys())[:excess]
            for k in keys_to_drop:
                del self.job_owner[k]
            log("prune_job_owner", sid=self.sid, dropped=excess,
                remaining=len(self.job_owner))

        # 2) seen_upstream_response_ids: grows with every upstream response.
        #    These are (pool_key, msg_id) tuples used to de-dupe.
        #    After a few minutes, no duplicate will arrive.  Cap at 500.
        max_seen = 500
        if len(self.seen_upstream_response_ids) > max_seen:
            # set() has no insertion order, so just clear and start fresh.
            # The worst that can happen is a duplicate response gets forwarded
            # once -- harmless, the miner ignores unexpected responses.
            excess = len(self.seen_upstream_response_ids)
            self.seen_upstream_response_ids.clear()
            log("prune_seen_upstream_ids", sid=self.sid, cleared=excess)

        # 3) _internal_ids: bootstrap request IDs.  Grows on each reconnect.
        #    Only a handful are "active" at any time.  Keep only the last 50.
        max_internal = 50
        if len(self._internal_ids) > max_internal:
            # These are ints; keep the highest (most recent) N.
            sorted_ids = sorted(self._internal_ids)
            to_remove = sorted_ids[:len(sorted_ids) - max_internal]
            for i in to_remove:
                self._internal_ids.discard(i)
            log("prune_internal_ids", sid=self.sid, dropped=len(to_remove),
                remaining=len(self._internal_ids))

        # 4) submit_owner / submit_diff: keyed by message id.
        #    Normally pop'd when the pool responds, but orphaned entries
        #    can accumulate if a pool never responds.  Cap at 200.
        max_submit = 200
        if len(self.submit_owner) > max_submit:
            excess = len(self.submit_owner) - max_submit
            keys_to_drop = list(self.submit_owner.keys())[:excess]
            for k in keys_to_drop:
                self.submit_owner.pop(k, None)
                self.submit_diff.pop(k, None)
            log("prune_submit_owner", sid=self.sid, dropped=excess,
                remaining=len(self.submit_owner))

    # End periodic state pruning 


    # Pool Failover: reconnecting wrapper around pool_reader 
    async def pool_reader_with_reconnect(self, pool_key: str, reader: asyncio.StreamReader):
        """Wrap pool_reader in a reconnect loop.

        Normal flow:
          1. pool_reader() runs, processing messages until EOF or error.
          2. When pool_reader() returns (pool disconnected), we land here.
          3. Mark the pool dead, clear stale state.
          4. Sleep with exponential backoff (5s, 10s, 20s, 40s -- capped at 60s).
          5. Try to reconnect (connect_pool).
          6. If reconnect succeeds -- reset fail counter, loop back to step 1.
          7. If reconnect fails -- increment fail counter, loop back to step 4.

        This method runs forever (until the miner session itself ends),
        so the asyncio.wait(FIRST_COMPLETED) in run() is no longer
        triggered by a pool going down.
        """
        pcfg = self.cfg.poolA if pool_key == "A" else self.cfg.poolB

        while True:
            # Phase 1: read from pool until it disconnects 
            try:
                await self.pool_reader(pool_key, reader)
            except asyncio.CancelledError:
                # Session is shutting down -- don't reconnect, just exit.
                raise
            except Exception as e:
                log("pool_reader_error", sid=self.sid, pool=pool_key, err=str(e))

            # If we get here, pool_reader returned (EOF) or raised.
            # That means the pool's TCP connection is dead.

            # Phase 2: mark dead + clear stale state 
            self.pool_alive[pool_key] = False
            self.pool_last_fail_mono[pool_key] = time.monotonic()
            self.clear_pool_state(pool_key)
            log("pool_down", sid=self.sid, pool=pool_key,
                fail_count=self.pool_fail_count[pool_key],
                other_alive=self.pool_alive["B" if pool_key == "A" else "A"])

            # Phase 3: reconnect loop with backoff 
            while True:
                # Exponential backoff: 5, 10, 20, 40, 60, 60, 60 
                base_delay = 5.0
                max_delay = 60.0
                delay = min(base_delay * (2 ** self.pool_fail_count[pool_key]), max_delay)
                log("pool_reconnect_wait", sid=self.sid, pool=pool_key,
                    delay_s=round(delay, 1),
                    fail_count=self.pool_fail_count[pool_key])

                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    raise

                # Attempt reconnect
                try:
                    r, w = await asyncio.wait_for(
                        self.connect_pool(pcfg, is_reconnect=True),
                        timeout=15.0  # don't hang forever on a dead host 
                   )

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.pool_fail_count[pool_key] += 1
                    self.pool_last_fail_mono[pool_key] = time.monotonic()
                    log("pool_reconnect_failed", sid=self.sid, pool=pool_key,
                        err=str(e), fail_count=self.pool_fail_count[pool_key])
                    continue  # back to top of reconnect loop (sleep again)

                # Phase 4: reconnect succeeded! 
                self.pool_alive[pool_key] = True
                self.pool_fail_count[pool_key] = 0
                self.pool_last_fail_mono[pool_key] = None

                # Update reader/writer instance attributes.
                if pool_key == "A":
                    self.rA = r
                    self.wA = w
                else:
                    self.rB = r
                    self.wB = w

                reader = r  # use the new reader for the next pool_reader() call
                log("pool_reconnected", sid=self.sid, pool=pool_key,
                    other_alive=self.pool_alive["B" if pool_key == "A" else "A"])

                # Force miner to re-handshake after pool reconnect 
                # When a pool reconnects, it issues a NEW extranonce1.
                # The miner must pick up this new extranonce or every share
                # will be rejected ("low difficulty share").
                #
                # We can't rely on mining.set_extranonce (NerdAxe, Nano3S,
                # AvalonQ don't support it) or client.reconnect (NerdAxe/
                # NerdMiner don't support it either).
                #
                # The most universally compatible approach: close the miner's
                # TCP connection.  Every miner handles a dropped connection
                # by reconnecting and doing a fresh subscribe handshake,
                # which picks up the new extranonce naturally.
                try:
                    log("miner_disconnect_for_reconnect", sid=self.sid, pool=pool_key,
                        reason="pool_reconnected_new_extranonce")
                    self.miner_w.close()
                    await self.miner_w.wait_closed()
                except Exception as e:
                    log("miner_disconnect_for_reconnect_failed", sid=self.sid, pool=pool_key, err=str(e))

                break  # exit reconnect loop -- back to Phase 1 (pool_reader)

    # Scheduler: forward jobs to miner based on configured weights
    async def forward_jobs(self):
        await self.miner_ready.wait()
        last_seen = {"A": 0, "B": 0}
        current_pool = self.active_pool
        ACTIVE_POOL.labels(pool="A").set(1 if current_pool == "A" else 0)
        ACTIVE_POOL.labels(pool="B").set(1 if current_pool == "B" else 0)
        last_switch_ts = time.monotonic()
        min_switch = max(0, int(self.cfg.sched.min_switch_seconds))

        last_sent_seq = {"A": 0, "B": 0}
        last_prune_mono = time.monotonic()

        while True:
            # Periodic cleanup (every 60s) 
            if time.monotonic() - last_prune_mono >= 60.0:
                self.prune_stale_state()
                last_prune_mono = time.monotonic()
            now = time.monotonic()
            slice_s = max(1, int(self.cfg.sched.slice_seconds))
            min_switch = max(0, int(self.cfg.sched.min_switch_seconds))
            switched_this_tick = False  # force-forward cached notify immediately after a switch

            # Failover: emergency switch if current pool is dead 
            # If the pool we're currently forwarding from just died, don't
            # wait for the normal min_switch_seconds timer -- switch immediately
            # to the other pool if it's alive.  Without this, the miner would
            # sit idle (no new jobs) until the next scheduler tick.
            if not self.pool_alive.get(current_pool, False):
                other = "B" if current_pool == "A" else "A"
                if self.pool_alive.get(other, False) and self.latest_notify_raw.get(other) is not None:
                    log("failover_emergency_switch", sid=self.sid,
                        dead_pool=current_pool, switching_to=other)
                    self.active_pool = other
                    ACTIVE_POOL.labels(pool="A").set(1 if other == "A" else 0)
                    ACTIVE_POOL.labels(pool="B").set(1 if other == "B" else 0)
                    current_pool = other
                    last_switch_ts = now
                    self.last_switch_mono = now
                    switched_this_tick = True
                    await self.resend_active_notify_clean(other, reason="failover_emergency")
                elif not self.pool_alive.get(other, False):
                    # Both pools dead -- nothing to do, just wait.
                    await asyncio.sleep(0.10)
                    continue

            # Normal scheduling logic 
            # Read weights early so we can scale the min-switch time.
            _override = read_weight_override()
            if _override is not None:
                wA, wB = _override
            else:
                wA = max(0, int(getattr(self.cfg.sched, "wA", getattr(self.cfg.sched, "poolA_weight", 0))))
                wB = max(0, int(getattr(self.cfg.sched, "wB", getattr(self.cfg.sched, "poolB_weight", 0))))
            totw = wA + wB

            # Scale min_switch by the active pool's target weight.
            # At 15/85, a full 50s slice on Pool A massively overshoots
            # (the miner should only spend ~15% of time on A).
            # Floor of 10s prevents sub-second thrashing.
            if totw > 0:
                _active_frac = (wA / totw) if current_pool == "A" else (wB / totw)
            else:
                _active_frac = 0.5
            _effective_min_switch = max(float(slice_s), min(float(min_switch), float(min_switch) * _active_frac * 2.0))

            if (now - last_switch_ts) >= _effective_min_switch:
                # Choose the pool that is behind in accepted difficulty share vs target.
                # BUT if one or more pool has failed....
                # Read configured weights, then override dead pools to 0.
                # This is the core failover mechanism: when a pool is down,
                # the scheduler acts as if it has zero weight (all hashrate
                # goes to the surviving pool).  When the pool recovers,
                # pool_alive flips back to True and the original weight applies.


                # Log when weights change (slider moved or override removed)
                _prev = getattr(self, "_last_effective_weights", None)
                _curr = (wA, wB)
                if _prev != _curr:
                    # Reseed accepted difficulty counters to match the NEW target ratio.
                    # Why not reset to 0/0? Because after a reset, the very first share
                    # creates a massive deviation (e.g., 100%/0%) and triggers urgent
                    # oscillation. Instead, keep the total difficulty but redistribute
                    # it to match the new targets, so the scheduler starts balanced.
                    old_diffA = self.accepted_diff_sum.get("A", 0.0)
                    old_diffB = self.accepted_diff_sum.get("B", 0.0)
                    old_total = old_diffA + old_diffB
                    new_total = wA + wB
                    if old_total > 0 and new_total > 0:
                        self.accepted_diff_sum["A"] = old_total * (wA / new_total)
                        self.accepted_diff_sum["B"] = old_total * (wB / new_total)
                    else:
                        self.accepted_diff_sum["A"] = 0.0
                        self.accepted_diff_sum["B"] = 0.0
                    log("weights_override_changed", sid=self.sid,
                        wA=wA, wB=wB, prev=_prev,
                        source="slider" if _override is not None else "config",
                        old_diffA=round(old_diffA, 1), old_diffB=round(old_diffB, 1),
                        new_diffA=round(self.accepted_diff_sum["A"], 1),
                        new_diffB=round(self.accepted_diff_sum["B"], 1))
                    self._last_effective_weights = _curr

                if not self.pool_alive.get("A", False):
                    if wA > 0:
                        log("failover_weight_override", sid=self.sid, pool="A",
                            configured_weight=wA, effective_weight=0, reason="pool_dead")
                    wA = 0
                if not self.pool_alive.get("B", False):
                    if wB > 0:
                        log("failover_weight_override", sid=self.sid, pool="B",
                            configured_weight=wB, effective_weight=0, reason="pool_dead")
                    wB = 0

                totw = wA + wB 

                reason = "hold_current"
                pick = current_pool

                if totw > 0:
                    targetA = (wA / totw)
                    targetB = (wB / totw)

                    # Decay accepted difficulty so old history doesn't dominate.
                    # But use a VERY gentle decay (0.9995) to avoid making the
                    # counters volatile -- especially for the minority pool at
                    # lopsided ratios (e.g., 80/20) where a single share can
                    # swing the ratio by 20+ points if the baseline is too small.
                    #
                    # 0.9995 per tick means effective memory of ~2000 ticks.
                    # At 30s ticks that's ~16 hours before old data fades to 50%.
                    # The weight-change reseed handles the "slider moved" case,
                    # so decay only needs to handle very long-term drift.

                    _decay = 0.9995
                    self.accepted_diff_sum["A"] = self.accepted_diff_sum.get("A", 0.0) * _decay
                    self.accepted_diff_sum["B"] = self.accepted_diff_sum.get("B", 0.0) * _decay


                    diffA = float(self.accepted_diff_sum.get("A", 0.0))
                    diffB = float(self.accepted_diff_sum.get("B", 0.0))
                    tot = diffA + diffB

                    shareA = (diffA / tot) if tot > 0 else targetA
                    shareB = (diffB / tot) if tot > 0 else targetB

                    # How far off-target is the current pool? (positive = over-target)
                    if current_pool == "A":
                        current_deviation = shareA - targetA
                    else:
                        current_deviation = shareB - targetB

                    # Only consider switching if:
                    #   1) We've been on this pool at least slice_seconds (normal cadence), OR
                    #   2) The current pool is MORE than MAX_CONVERGE_DEVIATION over its target (urgent correction)
                    #   3) MAX_CONVERGE_DEVIATION can be configured to adjust the urgency threshold (default 2%).
                    time_on_pool = now - last_switch_ts

                    # Compute minority_frac early -- needed by both urgency and hysteresis checks.
                    minority_frac = min(targetA, targetB)

                    # Scale urgency threshold by minority pool fraction.
                    # At 50/50: threshold = max(0.05, 0.50) = 0.50 -- urgent almost never fires
                    # At 80/20: threshold = max(0.05, 0.20) = 0.20 -- only truly large deviations
                    # At 95/5:  threshold = max(0.05, 0.05) = 0.05 -- tighter at extreme ratios
                    _urgency_threshold = max(MAX_CONVERGE_DEVIATION, minority_frac)
                    urgent = current_deviation > _urgency_threshold

                    if time_on_pool < slice_s and not urgent:
                        # Not enough time elapsed and not urgently over-target; skip this tick
                        pick = current_pool
                        reason = "hold_current_not_due"
                    else:
                        prefer = "B" if shareB < targetB else "A"
                        reason = "behind_target"

                        # If only one pool is enabled, force it.
                        if wA == 0 and wB > 0:
                            prefer = "B"
                            reason = "force_B_only"
                        elif wB == 0 and wA > 0:
                            prefer = "A"
                            reason = "force_A_only"

                        # Hysteresis: don't switch to the minority pool for tiny
                        # deviations. A 30s slice on the minority pool creates a
                        # large overshoot; only switch when the deficit is big
                        # enough to justify that slice.
                        # At 15/85 on B: minority_frac=0.15, threshold=0.0375
                        #   -- only switch B/A when shareA < 0.1125 (meaningfully behind)
                        # At 50/50: minority_frac=0.50, threshold=0.125
                        #   -- rarely triggers (deviation seldom that large at 50/50)

                        if prefer != current_pool and not urgent:
                            hysteresis = minority_frac / 4.0
                            if abs(current_deviation) < hysteresis:
                                prefer = current_pool
                                reason = "hold_current_hysteresis"

                        pick = prefer

                    # Throttle scheduler_tick: only log on switch decisions or every 60s as heartbeat
                    _now_mono = time.monotonic()
                    _last_tick_log = getattr(self, "_last_scheduler_tick_log", 0.0)
                    if pick != current_pool or urgent or (_now_mono - _last_tick_log) >= 60.0:
                        log("scheduler_tick", sid=self.sid, current=current_pool, pick=pick,
                            reason=reason, shareA=round(shareA, 4), shareB=round(shareB, 4),
                            targetA=round(targetA, 4), targetB=round(targetB, 4),
                            deviation=round(current_deviation, 4), time_on_pool=round(time_on_pool, 1),
                            urgent=urgent)
                        self._last_scheduler_tick_log = _now_mono

                # Don't switch into a pool until we have a cached job for it.
                if pick != current_pool:
                    if self.latest_notify_raw.get(pick) is None:
                        log("switch_skipped_no_cached_job", sid=self.sid, from_pool=current_pool, to_pool=pick)
                    else:
                        self.active_pool = pick
                        ACTIVE_POOL.labels(pool="A").set(1 if pick == "A" else 0)
                        ACTIVE_POOL.labels(pool="B").set(1 if pick == "B" else 0)
                        current_pool = pick
                        last_switch_ts = now
                        log("pool_switched", sid=self.sid, to_pool=pick)
                        switched_this_tick = True
                        self.last_switch_mono = time.monotonic()                      

                        # Immediately sync extranonce+diff and resend clean notify after switch.
                        # resend_active_notify_clean() handles extranonce+diff+notify internally,
                        # so we don't need separate calls here (avoids duplicate sends).
                        await self.resend_active_notify_clean(pick, reason="switch")

                    pick = current_pool

            pick = current_pool
            raw = self.latest_notify_raw.get(pick)
            jid = self.latest_jobid.get(pick)
            if raw is not None:
                seq = int(self.notify_seq.get(pick, 0))

                # Skip if we already sent everything during the switch block above.
                # resend_active_notify_clean() already sent extranonce+diff+notify.
                if switched_this_tick:
                    last_sent_seq[pick] = seq
                    JOBS_FORWARDED.labels(pool=pick).inc()
                    self.last_forwarded_jobid = jid
                    self.last_forwarded_pool = pick
                    if jid:
                        self.job_owner[(pick, jid)] = pick
                    log("job_forwarded", sid=self.sid, pool=pick, jobid=jid, seq=seq)
                    log("job_forwarded_diff_state", sid=self.sid, pool=pick, jobid=jid, latest_diff=self.latest_diff.get(pick), last_dd=self.last_downstream_diff_by_pool.get(pick))
                elif seq > last_sent_seq.get(pick, 0):
                    # Metrics truth: whichever pool we actually forward is 'active'.
                    self.active_pool = pick
                    ACTIVE_POOL.labels(pool="A").set(1 if pick == "A" else 0)
                    ACTIVE_POOL.labels(pool="B").set(1 if pick == "B" else 0)
                    # Ensure miner has correct extranonce/diff for this pool before notify
                    await self.maybe_send_downstream_extranonce(pick)
                    await self.maybe_send_downstream_diff(pick)
                    if jid:
                        self.job_owner[(pick, jid)] = pick

                    # Force clean_jobs=True on downstream notify to avoid miners hashing stale jobs.
                    try:
                        nm = loads_json(raw)
                        if nm.get("method") == "mining.notify":
                            params = nm.get("params") or []
                            if len(params) >= 1:
                                if len(params) >= 9:
                                    params[-1] = True
                                else:
                                    while len(params) < 9:
                                        params.append(None)
                                    params[-1] = True
                                nm["params"] = params
                                nm2 = sanitize_downstream_notification(nm)
                                raw2 = dumps_json(nm2)
                                await self.maybe_send_downstream_extranonce(pick)
                                sent_diff = await self.maybe_send_downstream_diff(pick, force=(pick != self.last_forwarded_pool))
                                if sent_diff:
                                    await asyncio.sleep(0.25)
                                await write_line(self.miner_w, raw2, "downstream")
                                log("notify_clean_forced", sid=self.sid, pool=pick, jobid=jid)
                            else:
                                await self.maybe_send_downstream_extranonce(pick)
                                sent_diff = await self.maybe_send_downstream_diff(pick, force=(pick != self.last_forwarded_pool))
                                if sent_diff:
                                    await asyncio.sleep(0.25)
                                await write_line(self.miner_w, raw, "downstream")
                        else:
                            await self.maybe_send_downstream_extranonce(pick)
                            sent_diff = await self.maybe_send_downstream_diff(pick, force=(pick != self.last_forwarded_pool))
                            if sent_diff:
                                await asyncio.sleep(0.25)
                            await write_line(self.miner_w, raw, "downstream")
                    except Exception as e:
                        log("notify_clean_force_error", sid=self.sid, pool=pick, err=str(e))
                        await self.maybe_send_downstream_extranonce(pick)
                        sent_diff = await self.maybe_send_downstream_diff(pick, force=(pick != self.last_forwarded_pool))
                        if sent_diff:
                            await asyncio.sleep(0.25)
                        await write_line(self.miner_w, raw, "downstream")
                    last_sent_seq[pick] = seq
                    JOBS_FORWARDED.labels(pool=pick).inc()
                    self.last_forwarded_jobid = jid
                    self.last_forwarded_pool = pick
                    log("job_forwarded", sid=self.sid, pool=pick, jobid=jid, seq=seq)
                    log("job_forwarded_diff_state", sid=self.sid, pool=pick, jobid=jid, latest_diff=self.latest_diff.get(pick), last_dd=self.last_downstream_diff_by_pool.get(pick))

            await asyncio.sleep(0.10)

    # Main session runner
    async def run(self):
        tasks = set()

        # Only connect to pools that have weight > 0.
        # At 100/0, skip Pool B entirely (avoids crash if Pool B is unreachable).
        # At 0/100, skip Pool A entirely.
        if self.cfg.sched.wA > 0:
            try:
                self.rA, self.wA = await asyncio.wait_for(
                    self.connect_pool(self.cfg.poolA), timeout=15.0)
                self.pool_alive["A"] = True
            except Exception as e:
                # Pool A unreachable at startup -- not fatal.
                # Mark dead and let the reconnect wrapper handle recovery.
                log("pool_initial_connect_failed", sid=self.sid, pool="A", err=str(e))
                self.pool_alive["A"] = False
                self.pool_fail_count["A"] = 1
                self.pool_last_fail_mono["A"] = time.monotonic()
                # Create a dummy reader so pool_reader_with_reconnect
                # skips straight to its reconnect loop.
                self.rA = asyncio.StreamReader()
                self.rA.feed_eof()  # immediately signals "disconnected"
            tasks.add(asyncio.create_task(
                self.pool_reader_with_reconnect("A", self.rA)))
        else:
            self.pool_alive["A"] = False
            log("pool_skipped_zero_weight", pool="A", wA=self.cfg.sched.wA)

        if self.cfg.sched.wB > 0:
            try:
                self.rB, self.wB = await asyncio.wait_for(
                    self.connect_pool(self.cfg.poolB), timeout=15.0)
                self.pool_alive["B"] = True
            except Exception as e:
                log("pool_initial_connect_failed", sid=self.sid, pool="B", err=str(e))
                self.pool_alive["B"] = False
                self.pool_fail_count["B"] = 1
                self.pool_last_fail_mono["B"] = time.monotonic()
                self.rB = asyncio.StreamReader()
                self.rB.feed_eof()
            tasks.add(asyncio.create_task(
                self.pool_reader_with_reconnect("B", self.rB)))
        else:
            self.pool_alive["B"] = False
            log("pool_skipped_zero_weight", pool="B", wB=self.cfg.sched.wB)

        tasks.add(asyncio.create_task(self.miner_to_pools()))
        tasks.add(asyncio.create_task(self.forward_jobs()))

        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        for t in pending:
            t.cancel()
        for t in done:
            if not t.cancelled():
                exc = t.exception()
                if exc:
                    raise exc

    # Close all connections
    async def close(self):
        try:
            self.miner_w.close()
            await self.miner_w.wait_closed()
        except Exception:
            pass
        for w in (self.wA, self.wB):
            if w is not None:
                try:
                    w.close()
                    await w.wait_closed()
                except Exception:
                    pass

# Handle incoming miner connection
async def handle_miner(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, cfg: AppCfg):
    peer = writer.get_extra_info("peername")

    # Allow multiple miners to connect concurrently.

    CONN_DOWNSTREAM.inc()
    log("miner_connected", peer=str(peer))

    sess = ProxySession(cfg, reader, writer, sid=str(peer))
    try:
        await sess.run()
    except Exception as e:
        log("session_error", peer=str(peer), err=str(e))
    finally:

        # Clear per-session downstream state so reconnects start clean.
        try:
            sess.last_downstream_diff_by_pool = {"A": None, "B": None}
            sess.last_downstream_en1 = None
            sess.last_downstream_en2s = None
            sess.last_downstream_extranonce = None
        except Exception:
            pass

        CONN_DOWNSTREAM.dec()
        CONN_UPSTREAM.labels(pool="A").dec()
        CONN_UPSTREAM.labels(pool="B").dec()
        await sess.close()
        log("miner_disconnected", peer=str(peer))

# Main entry point
async def main():
    global WEIGHTS_OVERRIDE_PATH, ORACLE_MODE_PATH
    cfg_path = os.environ.get("DPMP_CONFIG", os.path.join(os.path.dirname(__file__), "config_v2.json"))

    WEIGHTS_OVERRIDE_PATH = os.path.join(os.path.dirname(cfg_path), "weights_override.json")
    ORACLE_MODE_PATH = os.path.join(os.path.dirname(cfg_path), "oracle_mode.json")

    # Delete oracle_mode.json on startup so config auto_balance is the default.
    # The file is only created when the GUI switch button is clicked at runtime.
    try:
        if os.path.isfile(ORACLE_MODE_PATH):
            os.remove(ORACLE_MODE_PATH)
            log("oracle_mode_file_deleted_on_startup")
    except Exception:
        pass

    # Clear oracle chart history on startup so GUI charts begin fresh
    _chart_hist = os.path.join(os.path.dirname(cfg_path), "oracle_chart_history.json")
    try:
        if os.path.isfile(_chart_hist):
            os.remove(_chart_hist)
    except Exception:
        pass

    cfg = load_config(cfg_path)

    # Log normalized scheduler targets (weights need not sum to 100; they are relative ratios).
    try:
        wA = max(0, int(getattr(cfg.sched, "wA", 0)))
    except Exception:
        wA = 0
    try:
        wB = max(0, int(getattr(cfg.sched, "wB", 0)))
    except Exception:
        wB = 0
    totw = wA + wB
    if totw > 0:
        targetA = wA / totw
        targetB = wB / totw
    else:
        targetA = None
        targetB = None
    log("weights_normalized", wA=wA, wB=wB, weights_raw=f"{getattr(cfg.sched, 'wA', None)}:{getattr(cfg.sched, 'wB', None)}", targetA=targetA, targetB=targetB)
    log("config_loaded", config=cfg_path, listen_host=cfg.listen_host, listen_port=cfg.listen_port, metrics_enabled=cfg.metrics_enabled, metrics_host=cfg.metrics_host, metrics_port=cfg.metrics_port)

    # Start metrics server
    if cfg.metrics_enabled:
        try:
            start_http_server(cfg.metrics_port, addr=cfg.metrics_host)
            log("metrics_started", host=cfg.metrics_host, port=cfg.metrics_port)
        except OSError as e:
            # Do not crash the proxy if metrics port is already in use.
            log("metrics_start_failed", host=cfg.metrics_host, port=cfg.metrics_port, err=str(e))

    # Start listening for miners
    server = await asyncio.start_server(lambda r, w: handle_miner(r, w, cfg), cfg.listen_host, cfg.listen_port)
    # Log listening addresses
    addrs = ", ".join(str(sock.getsockname()) for sock in (server.sockets or []))
    log(
        "dpmp_listening",
        addrs=addrs,
        config=cfg_path,
        upstreamA=f"{cfg.poolA.host}:{cfg.poolA.port}",
        upstreamB=f"{cfg.poolB.host}:{cfg.poolB.port}",
        mode="dual_pool_scheduling_handshake_forward",
        weights=f"{cfg.sched.wA}:{cfg.sched.wB}",
    )

    stop = asyncio.Event()
    def _stop(*_args):
        log("shutdown_signal")
        stop.set()

    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, _stop)
        except NotImplementedError:
            pass

    # Keep running until stopped
    serve_task = asyncio.create_task(server.serve_forever())

    # Start oracle task if chain config is valid (one BTC + one BCH pool).
    # The oracle ALWAYS runs to collect data and update Prometheus gauges.
    # Whether it actually writes weights_override.json depends on oracle_mode.json
    # (checked inside oracle_poll_loop each cycle).
    oracle_task = None
    chain_a = getattr(cfg.poolA, "chain", "").upper()
    chain_b = getattr(cfg.poolB, "chain", "").upper()
    chain_valid = sorted([chain_a, chain_b]) == ["BCH", "BTC"]
    if chain_valid:
        oracle_task = asyncio.create_task(oracle_poll_loop(cfg))
        log("oracle_task_started", auto_balance=cfg.sched.auto_balance,
            reason="chain config valid, oracle always collects data")
    else:
        log("oracle_disabled_invalid_chains", chain_a=chain_a, chain_b=chain_b,
            reason="need exactly one BTC and one BCH pool for oracle")

    try:
        await stop.wait()
    finally:
        log("shutdown_begin")

        try:
            log("shutdown_server_close_begin")
            server.close()
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=2.0)
                log("shutdown_server_close_done")
            except asyncio.TimeoutError:
                log("shutdown_server_close_timeout")
        except Exception as e:
            log("shutdown_server_close_error", err=str(e))

        # Stop the serve_forever loop        
        log("shutdown_serve_task_cancel_begin")
        try:
            serve_task.cancel()
            await asyncio.wait_for(asyncio.gather(serve_task, return_exceptions=True), timeout=2.0)
            log("shutdown_serve_task_cancel_done")
        except asyncio.TimeoutError:
            log("shutdown_serve_task_cancel_timeout")
        except Exception as e:
            log("shutdown_serve_task_error", err=str(e))

        log("shutdown_cancel_tasks")
        if oracle_task is not None and not oracle_task.done():
            oracle_task.cancel()
            log("oracle_task_cancelled")
        current_task = asyncio.current_task()
        tasks = [t for t in asyncio.all_tasks() if t is not current_task and t is not serve_task and not t.done()]

        for t in tasks:
            t.cancel()

        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5.0)
            except asyncio.TimeoutError:
                log("shutdown_timeout", n=len(tasks))

        log("shutdown_done")



if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("shutdown_keyboard_interrupt")
    except Exception as e:
        log("fatal_crash", err=str(e), err_type=type(e).__name__)
        import traceback
        traceback.print_exc()
        raise
    finally:
        log("process_exiting")
