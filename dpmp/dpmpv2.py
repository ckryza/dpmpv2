#!/usr/bin/env python3
"""
DPMP - Dual-Pool Mining Proxy (Stratum v1)
Dual upstream + weighted scheduling, with correct miner handshake forwarding.

Key fix:
- Forward ALL upstream responses (messages with "id") to the miner (subscribe/auth/submit responses).
- Forward upstream "setup" methods from Pool A to miner (e.g., mining.set_extranonce, mining.set_version_mask, client.reconnect),
  because miners often require these to complete the session.
- Do NOT forward upstream mining.notify or mining.set_difficulty directly.
  Those are sent downstream only by DPMP scheduling + downstream diff policy.

Scheduling:
- Store latest mining.notify from each pool.
- When a NEW notify arrives, pick A/B by weight and forward that pool’s notify downstream.
- Record job_id -> pool ownership when forwarded.
- Route mining.submit to the owning pool by job_id.
- Downstream difficulty policy = min(diffA, diffB), forwarded only when changed.
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
SWITCH_SUBMIT_GRACE_S = 0.75  # seconds to tolerate stale submits right after a pool switch
MAX_CACHED_NOTIFY_AGE_S = 20.0  # don't switch into pool if cached notify older than this

def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

LOG_LEVEL = os.environ.get("DPMP_LOG_LEVEL", "info").strip().lower()
LOG_ALLOW = set(x.strip() for x in os.environ.get("DPMP_LOG_ALLOW", "").split(",") if x.strip())
LOG_DENY  = set(x.strip() for x in os.environ.get("DPMP_LOG_DENY", "").split(",") if x.strip())

_DEBUG_EVENTS = {
    "downstream_tx", "upstream_tx", "miner_method",
    "submit_snapshot", "submit_local_sanity",
    "job_forwarded_diff_state",
    "downstream_send_notify", "downstream_send_raw",
    "downstream_send_diff",
}


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

def loads_json(b: bytes) -> Dict[str, Any]:
    if orjson is not None:
        return orjson.loads(b)
    return json.loads(b.decode("utf-8", errors="replace"))

def dumps_json(obj: Dict[str, Any]) -> bytes:
    # Ensure Stratum responses include "error": null when "id" is non-null.
    # Some miners disconnect if "error" is missing from {"id":..., "result":...} responses.
    if isinstance(obj, dict) and obj.get("id") is not None and "result" in obj and "error" not in obj:
        obj = dict(obj)
        obj["error"] = None
    if orjson is not None:
        return orjson.dumps(obj) + b"\n"
    return (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")

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

@dataclass
class SchedulerCfg:
    wA: int
    wB: int
    min_switch_seconds: int
    slice_seconds: int

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
        )

    wA = int(sched.get("poolA_weight", 50))
    wB = int(sched.get("poolB_weight", 50))
    if wA < 0 or wB < 0 or (wA == 0 and wB == 0):
        wA, wB = 50, 50

    return AppCfg(
        listen_host=str(listen_host),
        listen_port=int(listen_port),
        metrics_enabled=bool(metrics_enabled),
        metrics_host=str(metrics_host),
        metrics_port=int(metrics_port),
        poolA=pool("A"),
        poolB=pool("B"),
        sched=SchedulerCfg(wA=wA, wB=wB, min_switch_seconds=int(sched.get("min_switch_seconds", 15)), slice_seconds=int(sched.get("slice_seconds", 15))),
        downstream_diff=dict(cfg.get("downstream_diff", {})),
    )

async def iter_lines(reader: asyncio.StreamReader, side: str):
    while True:
        line = await reader.readline()
        if not line:
            return
        if not line.strip():
            continue
        MSG_RX.labels(side=side).inc()
        yield line

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
def jobid_from_notify(msg: Dict[str, Any]) -> Optional[str]:
    try:
        p = msg.get("params") or []
        return str(p[0]) if len(p) >= 1 else None
    except Exception:
        return None

def jobid_from_submit(msg: Dict[str, Any]) -> Optional[str]:
    try:
        p = msg.get("params") or []
        return str(p[1]) if len(p) >= 2 else None
    except Exception:
        return None

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
        self.active_pool: str = "A"  # pool whose job we last forwarded

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

    def next_internal_id(self) -> int:
        self._internal_next_id += 1
        return self._internal_next_id

    async def bootstrap_pool(self, pcfg: PoolCfg) -> None:
        """Internal subscribe/auth to ensure pool emits notify and we can cache jobs."""
        # TEMP TEST: internal bootstrap was disabled globally.
        # We need Pool B to receive mining.subscribe so it emits mining.notify.
        # Keep Pool A disabled (it already has miner-driven subscribe).
        if pcfg.key != "B":
            return
        try:
            sid_sub = self.next_internal_id()
            self._internal_ids.add(sid_sub)
            self._internal_subscribe_id[pcfg.key] = sid_sub
            sub = {"id": sid_sub, "method": "mining.subscribe", "params": ["dpmpv2/1.0"]}
            await self.send_upstream(pcfg.key, sub)
            log("pool_bootstrap_subscribe_sent", sid=self.sid, pool=pcfg.key, id=sid_sub)

            aid = self.next_internal_id()
            self._internal_ids.add(aid)
            self._internal_authorize_id[pcfg.key] = aid
            user = f"{pcfg.wallet}.dpmp_bootstrap" if pcfg.wallet else "dpmp_bootstrap"
            auth = {"id": aid, "method": "mining.authorize", "params": [user, "x"]}
            await self.send_upstream(pcfg.key, auth)
            log("pool_bootstrap_authorize_sent", sid=self.sid, pool=pcfg.key, id=aid, user=user)
        except Exception as e:
            log("pool_bootstrap_error", sid=self.sid, pool=pcfg.key, err=str(e))

    async def connect_pool(self, pcfg: PoolCfg) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
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

        await self.bootstrap_pool(pcfg)
        return r, w

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

    async def maybe_send_downstream_extranonce(self, pool_key: str):
        # Never send extranonce for a non-active pool; it poisons miner context and causes mass rejects.
        ap = getattr(self, "active_pool", None)
        if ap not in ("A", "B"):
            ap = getattr(self, "last_forwarded_pool", None) or getattr(self, "handshake_pool", None)
        if ap in ("A", "B") and pool_key != ap:
            log("downstream_extranonce_suppressed_nonactive", sid=self.sid, pool=pool_key, active=ap)
            return
        # If we already raw-forwarded the pool's subscribe result,
        # do NOT re-send extranonce (pool-agnostic safety).
        if getattr(self, "raw_subscribe_forwarded_pool", None) == pool_key and getattr(self, "last_downstream_extranonce_pool", None) == pool_key:
            log("downstream_extranonce_skip_raw_subscribe", pool=pool_key,
                raw_subscribe_forwarded_pool=getattr(self, "raw_subscribe_forwarded_pool", None),
                last_downstream_extranonce_pool=getattr(self, "last_downstream_extranonce_pool", None))
            return
        en1 = self.extranonce1.get(pool_key)
        en2s = self.extranonce2_size.get(pool_key)
        if not en1 or en2s is None:
            return

        async with self.downstream_setup_lock:
            # Only send extranonce when it actually changes.
            new_en1 = str(en1)
            new_en2s = int(en2s)
            if self.last_downstream_en1 == new_en1 and self.last_downstream_en2s == new_en2s:
                log("downstream_extranonce_skip_nochange", sid=self.sid, pool=pool_key,
                    en1=new_en1, en2s=new_en2s,
                    last_en1=str(self.last_downstream_en1), last_en2s=int(self.last_downstream_en2s))
                return

            # IMPORTANT: only "commit" the downstream extranonce state if the write succeeds.
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
            log("downstream_extranonce_set", sid=self.sid, pool=pool_key, extranonce1=new_en1, extranonce2_size=new_en2s)

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
                self.last_notify_mono = time.monotonic()
                log("resend_notify_clean", sid=self.sid, pool=pool_key, jobid=jid, reason=reason)
                return
        except Exception as e:
            log("resend_notify_error", sid=self.sid, pool=pool_key, jobid=jid, reason=reason, err=str(e))
        log("downstream_send_raw", payload=raw.decode("utf-8", errors="replace"))
        await write_line(self.miner_w, raw, "downstream")
        log("resend_notify_raw", sid=self.sid, pool=pool_key, jobid=jid, reason=reason)

    async def miner_to_pools(self):
        assert self.wA and self.wB
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
                    # Reuse the existing internal-id suppression mechanism used for bootstrap.
                    iid = self.next_internal_id()
                    self._internal_ids.add(iid)
                    msg2 = dict(msg)
                    msg2["id"] = iid
                    await self.send_upstream(other, msg2)

                    log("configure_forwarded_both_pools", sid=self.sid, handshake=hp, other=other, id=cfg_id, internal_id=iid)
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
                try:
                    other = "B" if self.handshake_pool == "A" else "A"
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

                self.miner_ready.set()
                log("miner_ready_for_jobs", sid=self.sid, worker=self.worker, handshake_pool=self.handshake_pool)

                # Immediately push extranonce+diff so the miner has correct targets before submitting.
                try:
                    await self.maybe_send_downstream_extranonce(self.handshake_pool)
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
                            # Very rough heuristic: random hash → expected diff ~ 1
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
                        params[0] = f"{self.cfg.poolA.wallet}.{self.worker}" if self.cfg.poolA.wallet else str(params[0])
                        out["params"] = params
                    await write_line(self.wA, dumps_json(out), "upstreamA")
                continue

            await write_line(self.wA, raw, "upstreamA")
            await write_line(self.wB, raw, "upstreamB")

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
                            try:
                                active = self.last_forwarded_pool or self.handshake_pool
                                if active == pool_key and self.extranonce1.get(pool_key) is not None and self.extranonce2_size.get(pool_key) is not None:
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
                    if ok_auth and (self.handshake_pool is not None) and (pool_key == self.handshake_pool):
                        try:
                            # Extranonce (if known)
                            en1 = self.extranonce1.get(pool_key)
                            en2s = self.extranonce2_size.get(pool_key)
                            if en1 is not None and en2s is not None:
                                en_msg = {"method": "mining.set_extranonce", "params": [str(en1), int(en2s)]}
                                log("post_auth_push_extranonce", sid=self.sid, pool=pool_key, extranonce1=str(en1), extranonce2_size=int(en2s))
                                await write_line(self.miner_w, dumps_json(en_msg), "downstream")

                            # Difficulty (prefer latest pool diff if we have it)
                            diff = None
                            try:
                                diff = float(self.pool_latest_diff.get(pool_key))
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
                                        self.last_notify_mono = time.monotonic()
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
                        self.accepted_diff_sum[p] = self.accepted_diff_sum.get(p, 0.0) + d
                        log("share_result", sid=self.sid, pool=p, accepted=True)
                    else:
                        SHARES_REJECTED.labels(pool=p).inc()
                        log("share_result", sid=self.sid, pool=p, accepted=False, error=msg.get("error"))
                await write_line(self.miner_w, dumps_json(msg), "downstream")
                continue

            # Forward setup methods from the handshake pool only (but never notify/diff)
            primary = self.handshake_pool or "A"
            if pool_key == primary and method is not None:
                await write_line(self.miner_w, raw, "downstream")

    async def forward_jobs(self):
        await self.miner_ready.wait()
        last_seen = {"A": 0, "B": 0}
        current_pool = self.active_pool
        ACTIVE_POOL.labels(pool="A").set(1 if current_pool == "A" else 0)
        ACTIVE_POOL.labels(pool="B").set(1 if current_pool == "B" else 0)
        last_switch_ts = time.monotonic()
        min_switch = max(0, int(self.cfg.sched.min_switch_seconds))


        last_sent_seq = {"A": 0, "B": 0}

        while True:
            now = time.monotonic()
            slice_s = max(1, int(self.cfg.sched.slice_seconds))
            min_switch = max(0, int(self.cfg.sched.min_switch_seconds))
            switched_this_tick = False  # force-forward cached notify immediately after a switch

            # Time-slice switching (ratio by time), not by notify frequency.
            # Time-slice switching (ratio by time), not by notify frequency.
            if (now - last_switch_ts) >= slice_s and (now - last_switch_ts) >= min_switch:
                # Choose the pool that is behind in accepted difficulty share vs target.
                wA = max(0, int(getattr(self.cfg.sched, "wA", getattr(self.cfg.sched, "poolA_weight", 0))))
                wB = max(0, int(getattr(self.cfg.sched, "wB", getattr(self.cfg.sched, "poolB_weight", 0))))
                totw = wA + wB

                reason = "hold_current"
                pick = current_pool

                if totw > 0:
                    targetA = (wA / totw)
                    targetB = (wB / totw)

                    diffA = float(self.accepted_diff_sum.get("A", 0.0))
                    diffB = float(self.accepted_diff_sum.get("B", 0.0))
                    tot = diffA + diffB

                    shareA = (diffA / tot) if tot > 0 else targetA
                    shareB = (diffB / tot) if tot > 0 else targetB

                    prefer = "B" if shareB < targetB else "A"
                    reason = "behind_target"

                    # If only one pool is enabled, force it.
                    if wA == 0 and wB > 0:
                        prefer = "B"
                        reason = "force_B_only"
                    elif wB == 0 and wA > 0:
                        prefer = "A"
                        reason = "force_A_only"

                    pick = prefer

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

                        # Immediately sync extranonce+diff and resend clean notify after switch
                        await self.maybe_send_downstream_extranonce(pick)
                        await self.maybe_send_downstream_diff(pick, force=True)
                        await self.resend_active_notify_clean(pick, reason="switch")                
                    pick = current_pool
                else:
                    targetB = (wB / totw)
                    diffA = float(self.accepted_diff_sum.get("A", 0.0))
                    diffB = float(self.accepted_diff_sum.get("B", 0.0))
                    tot = diffA + diffB
                    shareB = (diffB / tot) if tot > 0 else targetB

                    prefer = "B" if shareB < targetB else "A"
                    # If only one pool is enabled, force it.
                    if wA == 0 and wB > 0:
                        prefer = "B"
                    elif wB == 0 and wA > 0:
                        prefer = "A"
                    pick = prefer

                # Don't switch into a pool until we have a cached job for it.
                if pick != current_pool and self.latest_notify_raw.get(pick) is not None:
                    self.active_pool = pick
                    ACTIVE_POOL.labels(pool="A").set(1 if pick == "A" else 0)
                    ACTIVE_POOL.labels(pool="B").set(1 if pick == "B" else 0)
                    current_pool = pick
                    last_switch_ts = now
                    log("pool_switched", sid=self.sid, to_pool=pick)
                    switched_this_tick = True
                    self.last_switch_mono = time.monotonic()

                    # Immediately sync extranonce+diff and resend clean notify after switch
                    await self.maybe_send_downstream_extranonce(pick)
                    await self.maybe_send_downstream_diff(pick, force=True)
                    await self.resend_active_notify_clean(pick, reason="switch")

            pick = current_pool
            raw = self.latest_notify_raw.get(pick)
            jid = self.latest_jobid.get(pick)
            if raw is not None:
                seq = int(self.notify_seq.get(pick, 0))

                # Forward only when there's a new notify for this pool,
                # or immediately after a switch (seq will differ).
                if (seq > last_sent_seq.get(pick, 0)) or switched_this_tick:
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

    async def run(self):
        self.rA, self.wA = await self.connect_pool(self.cfg.poolA)
        self.rB, self.wB = await self.connect_pool(self.cfg.poolB)

        tA = asyncio.create_task(self.pool_reader("A", self.rA))
        tB = asyncio.create_task(self.pool_reader("B", self.rB))
        tM = asyncio.create_task(self.miner_to_pools())
        tF = asyncio.create_task(self.forward_jobs())

        done, pending = await asyncio.wait({tA, tB, tM, tF}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in done:
            if not t.cancelled():
                exc = t.exception()
                if exc:
                    raise exc

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

async def main():
    cfg_path = os.environ.get("DPMP_CONFIG", os.path.join(os.path.dirname(__file__), "config.json"))
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

    if cfg.metrics_enabled:
        try:
            start_http_server(cfg.metrics_port, addr=cfg.metrics_host)
            log("metrics_started", host=cfg.metrics_host, port=cfg.metrics_port)
        except OSError as e:
            # Do not crash the proxy if metrics port is already in use.
            log("metrics_start_failed", host=cfg.metrics_host, port=cfg.metrics_port, err=str(e))
    server = await asyncio.start_server(lambda r, w: handle_miner(r, w, cfg), cfg.listen_host, cfg.listen_port)
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

    serve_task = asyncio.create_task(server.serve_forever())
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
    asyncio.run(main())
