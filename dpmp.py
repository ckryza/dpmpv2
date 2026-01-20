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
- Downstream difficulty policy is config-driven clamp per pool.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import signal
import time
import traceback
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
SUBMITS_DROPPED = Counter("dpmp_submits_dropped_total", "Miner submits dropped locally", ["reason"])
SHARES_ACCEPTED = Counter("dpmp_shares_accepted_total", "Shares accepted by pools", ["pool"])
SHARES_REJECTED = Counter("dpmp_shares_rejected_total", "Shares rejected by pools", ["pool"])
JOBS_FORWARDED = Counter("dpmp_jobs_forwarded_total", "Jobs forwarded to miner", ["pool"])
DIFF_DOWNSTREAM = Gauge("dpmp_downstream_difficulty", "Current downstream difficulty")


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def log(event: str, **fields: Any) -> None:
    rec = {"ts": now_utc(), "event": event, **fields}
    print(json.dumps(rec, separators=(",", ":"), ensure_ascii=False), flush=True)


def loads_json(b: bytes) -> Dict[str, Any]:
    if orjson is not None:
        return orjson.loads(b)
    return json.loads(b.decode("utf-8", errors="replace"))


def dumps_json(obj: Dict[str, Any]) -> bytes:
    if orjson is not None:
        return orjson.dumps(obj) + b"\n"
    return (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


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
    with open(path, "rb") as f:
        cfg = loads_json(f.read())

    listen = cfg.get("listen", {})
    metrics = cfg.get("metrics", {})
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
        listen_host=str(listen.get("host", "0.0.0.0")),
        listen_port=int(listen.get("port", 3350)),
        metrics_enabled=bool(metrics.get("enabled", True)),
        metrics_host=str(metrics.get("host", "0.0.0.0")),
        metrics_port=int(metrics.get("port", 9109)),
        poolA=pool("A"),
        poolB=pool("B"),
        sched=SchedulerCfg(wA=wA, wB=wB, min_switch_seconds=int(sched.get("min_switch_seconds", 15))),
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
        writer.write(data)
        await asyncio.wait_for(writer.drain(), timeout=2.0)
        MSG_TX.labels(side=side).inc()
    except Exception as e:
        log("write_failed", side=side, err=str(e))
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
    def __init__(self, cfg: AppCfg, miner_r: asyncio.StreamReader, miner_w: asyncio.StreamWriter):
        self.cfg = cfg
        self.miner_r = miner_r
        self.miner_w = miner_w

        peer = miner_w.get_extra_info("peername")
        self.sid = f"{peer[0]}:{peer[1]}" if peer else "unknown"

        self.rA: Optional[asyncio.StreamReader] = None
        self.wA: Optional[asyncio.StreamWriter] = None
        self.rB: Optional[asyncio.StreamReader] = None
        self.wB: Optional[asyncio.StreamWriter] = None

        self.upstream_connected: Dict[str, bool] = {"A": False, "B": False}

        self.worker: str = ""
        self.miner_ready = asyncio.Event()
        self.authorize_id: Any = None
        self.subscribe_id: Any = None
        self.configure_id: Any = None

        self.latest_notify_raw: Dict[str, Optional[bytes]] = {"A": None, "B": None}
        self.force_clean_next_notify: bool = False
        self.latest_jobid: Dict[str, Optional[str]] = {"A": None, "B": None}
        self.notify_seq: Dict[str, int] = {"A": 0, "B": 0}
        self.extranonce1: Dict[str, Optional[str]] = {"A": None, "B": None}
        self.extranonce2_size: Dict[str, Optional[int]] = {"A": None, "B": None}

        self.latest_diff: Dict[str, Optional[float]] = {"A": None, "B": None}
        self.last_downstream_diff: Optional[float] = None

        self.active_pool: str = "A"  # pool whose job we last forwarded

        # Job-context barrier (epoch gating)
        self.ctx_pool: str = self.active_pool
        self.ctx_epoch: int = 0
        self.barrier_pending: bool = False
        self.barrier_until: float = 0.0
        self.barrier_notifies_needed: int = 0
        self.barrier_notifies_seen: int = 0
        self.job_epoch: Dict[str, int] = {}

        self.job_owner: Dict[str, str] = {}      # jobid -> pool key
        self.submit_owner: Dict[Any, str] = {}   # submit msg id -> pool key

        self.sched = RatioScheduler(cfg.sched.wA, cfg.sched.wB)

        # Choose which pool's subscribe/auth responses we forward downstream.
        # In single-pool mode, many miners require the handshake to match the active mining pool.
        if int(cfg.sched.wA) <= 0 and int(cfg.sched.wB) > 0:
            self.handshake_pool = "B"
        elif int(cfg.sched.wB) <= 0 and int(cfg.sched.wA) > 0:
            self.handshake_pool = "A"
        else:
            self.handshake_pool = "A"
    def slog(self, event: str, **fields: Any) -> None:
        log(event, sid=self.sid, **fields)

    async def connect_pool(self, pcfg: PoolCfg) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        self.slog("pool_connecting", key=pcfg.key, pool=pcfg.name, host=pcfg.host, port=pcfg.port)
        r, w = await asyncio.open_connection(pcfg.host, pcfg.port)
        CONN_UPSTREAM.labels(pool=pcfg.key).inc()
        self.upstream_connected[pcfg.key] = True
        self.slog("pool_connected", key=pcfg.key, pool=pcfg.name, host=pcfg.host, port=pcfg.port)
        return r, w

    def rewrite_authorize(self, pcfg: PoolCfg, msg: Dict[str, Any]) -> Dict[str, Any]:
        params = msg.get("params") or []
        miner_user = str(params[0]) if len(params) >= 1 else ""
        self.worker = extract_worker_name(miner_user)
        pw = str(params[1]) if len(params) >= 2 else "x"
        user = f"{pcfg.wallet}.{self.worker}" if pcfg.wallet else self.worker
        out = dict(msg)
        out["params"] = [user, pw]
        return out

    def downstream_diff_policy(self, pool_key: str) -> Optional[float]:
        d = self.latest_diff.get(pool_key)
        if d is None:
            return None

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

        return v

    async def maybe_send_downstream_extranonce(self, pool_key: str):
        en1 = self.extranonce1.get(pool_key)
        en2s = self.extranonce2_size.get(pool_key)
        if not en1 or en2s is None:
            return

        msg = {"id": None, "method": "mining.set_extranonce", "params": [en1, int(en2s)]}
        await write_line(self.miner_w, dumps_json(msg), "downstream")
        self.slog("downstream_extranonce_set", pool=pool_key, extranonce1=en1, extranonce2_size=int(en2s))

    async def maybe_replay_downstream_subscribe(self, pool_key: str):
        # Replay a subscribe RESULT with the pool's extranonce1
        if self.subscribe_id is None:
            return
        en1 = self.extranonce1.get(pool_key)
        en2s = self.extranonce2_size.get(pool_key)
        if not en1 or en2s is None:
            return

        subid = "dpmp"
        res = [[["mining.set_difficulty", subid], ["mining.notify", subid]], str(en1), int(en2s)]
        msg = {"id": self.subscribe_id, "result": res, "error": None}
        await write_line(self.miner_w, dumps_json(msg), "downstream")
        self.slog("downstream_subscribe_replay", pool=pool_key, extranonce1=str(en1), extranonce2_size=int(en2s))

    async def maybe_send_downstream_diff(self, pool_key: str):
        dd = self.latest_diff.get(pool_key)
        if dd is None:
            return

        # DEBUG HAMMER: always send diff downstream (no de-dupe) so we can prove it is on the wire
        self.last_downstream_diff = dd

        msg = {"id": None, "method": "mining.set_difficulty", "params": [float(dd)]}
        await write_line(self.miner_w, dumps_json(msg), "downstream")
        self.slog("downstream_diff_set", pool=pool_key, diff=float(dd))

    async def miner_to_pools(self):
        assert self.wA and self.wB

        async for raw in iter_lines(self.miner_r, "downstream"):
            try:
                msg = loads_json(raw)
            except Exception as e:
                self.slog("miner_bad_json", err=str(e))
                continue

            m = msg.get("method")
            if m:
                self.slog("miner_method", method=m)

            # Configure: ACK locally so miner can start hashing (some miners stall without this)
            if m == "mining.configure":
                req_id = msg.get("id")
                self.configure_id = req_id
                if req_id is not None:
                    resp = {"id": req_id, "result": {"version-rolling": True, "version-rolling.mask": "1fffe000", "minimum-difficulty": 1}, "error": None}
                    await write_line(self.miner_w, dumps_json(resp), "downstream")
                    self.slog("configure_ack", id=req_id)

                continue
            # Subscribe: send to BOTH pools so we learn extranonce for B as well.
            # We only forward the subscribe response from Pool A downstream.
            if m == "mining.subscribe":
                self.subscribe_id = msg.get("id")
                await write_line(self.wA, raw, "upstreamA")
                await write_line(self.wB, raw, "upstreamB")
                continue

            # Authorize: rewrite user per pool and send to both.
            if m == "mining.authorize":
                self.authorize_id = msg.get("id")
                outA = self.rewrite_authorize(self.cfg.poolA, msg)
                outB = self.rewrite_authorize(self.cfg.poolB, msg)
                self.slog("authorize_rewrite", pool="A", worker=self.worker, upstream_user=outA["params"][0])
                self.slog("authorize_rewrite", pool="B", worker=self.worker, upstream_user=outB["params"][0])
                await write_line(self.wA, dumps_json(outA), "upstreamA")
                await write_line(self.wB, dumps_json(outB), "upstreamB")
                self.miner_ready.set()
                continue

            # Submit routing
            if m == "mining.submit":
                SHARES_SUBMITTED.inc()
                mid = msg.get("id")
                jid = jobid_from_submit(msg) or ""

                wA = int(self.cfg.sched.wA)
                wB = int(self.cfg.sched.wB)

                # Single-pool mode: route directly.
                if wA <= 0 and wB > 0:
                    self.slog("submit_jid", jid=jid, route="B", mode="single")
                    self.submit_owner[mid] = "B"
                    await write_line(self.wB, raw, "upstreamB")
                    continue
                if wB <= 0 and wA > 0:
                    self.slog("submit_jid", jid=jid, route="A", mode="single")
                    self.submit_owner[mid] = "A"
                    await write_line(self.wA, raw, "upstreamA")
                    continue

                # Dual-pool mode barrier checks
                if time.monotonic() < self.barrier_until:
                    SUBMITS_DROPPED.labels(reason="barrier_grace").inc()
                    err = {"id": mid, "result": None, "error": [21, "Job not found", None]}
                    await write_line(self.miner_w, dumps_json(err), "downstream")
                    continue

                if self.barrier_pending:
                    SUBMITS_DROPPED.labels(reason="barrier_pending").inc()
                    err = {"id": mid, "result": None, "error": [21, "Job not found", None]}
                    await write_line(self.miner_w, dumps_json(err), "downstream")
                    continue

                owner = self.job_owner.get(jid)
                je = self.job_epoch.get(jid)
                self.slog("submit_ctx", jid=jid, ctx_pool=self.ctx_pool, ctx_epoch=self.ctx_epoch, owner=owner, job_epoch=je)

                if owner is None or owner != self.ctx_pool:
                    SUBMITS_DROPPED.labels(reason="wrong_pool").inc()
                    err = {"id": mid, "result": None, "error": [21, "Job not found", None]}
                    await write_line(self.miner_w, dumps_json(err), "downstream")
                    continue

                if je is None or je != self.ctx_epoch:
                    SUBMITS_DROPPED.labels(reason="wrong_epoch").inc()
                    err = {"id": mid, "result": None, "error": [21, "Job not found", None]}
                    await write_line(self.miner_w, dumps_json(err), "downstream")
                    continue

                self.submit_owner[mid] = owner
                if owner == "B":
                    await write_line(self.wB, raw, "upstreamB")
                else:
                    await write_line(self.wA, raw, "upstreamA")
                continue

            # Default: forward other miner methods to Pool A only.
            await write_line(self.wA, raw, "upstreamA")

    async def pool_reader(self, pool_key: str, reader: asyncio.StreamReader):
        side = "upstreamA" if pool_key == "A" else "upstreamB"
        async for raw in iter_lines(reader, side):
            try:
                msg = loads_json(raw)
            except Exception:
                continue

            method = msg.get("method")

            if method == "mining.set_difficulty":
                try:
                    self.latest_diff[pool_key] = float((msg.get("params") or [None])[0])
                    self.slog("pool_diff", pool=pool_key, diff=self.latest_diff[pool_key])
                    if pool_key == self.active_pool:
                        await self.maybe_send_downstream_diff(pool_key)
                except Exception:
                    pass
                continue

            if method == "mining.notify":
                self.latest_notify_raw[pool_key] = raw
                jid = jobid_from_notify(msg)
                self.latest_jobid[pool_key] = jid
                self.notify_seq[pool_key] += 1
                self.slog("pool_notify", pool=pool_key, jobid=jid, seq=self.notify_seq[pool_key])
                continue

            # Forward ALL id-based responses to miner (subscribe/auth/submit responses)
            if "id" in msg and msg.get("method") is None:
                mid = msg.get("id")

                # Capture per-pool subscribe response (extranonce context)
                if self.subscribe_id is not None and mid == self.subscribe_id:
                    try:
                        res = msg.get("result")
                        if isinstance(res, list) and len(res) >= 3:
                            en1 = res[1]
                            en2s = res[2]
                            if en1 is not None:
                                self.extranonce1[pool_key] = str(en1)
                            if en2s is not None:
                                self.extranonce2_size[pool_key] = int(en2s)
                            self.slog(
                                "subscribe_result",
                                pool=pool_key,
                                extranonce1=self.extranonce1[pool_key],
                                extranonce2_size=self.extranonce2_size[pool_key],
                            )
                    except Exception as e:
                        self.slog("subscribe_parse_error", pool=pool_key, err=str(e))

                if self.authorize_id is not None and mid == self.authorize_id:
                    self.slog("auth_result", pool=pool_key, ok=bool(msg.get("result")), error=msg.get("error"))

                if mid in self.submit_owner:
                    p = self.submit_owner.pop(mid)
                    ok = bool(msg.get("result"))
                    if ok:
                        SHARES_ACCEPTED.labels(pool=p).inc()
                        self.slog("share_result", pool=p, accepted=True)
                    else:
                        SHARES_REJECTED.labels(pool=p).inc()
                        self.slog("share_result", pool=p, accepted=False, error=msg.get("error"))

                # Only forward ONE subscribe/auth response (from handshake_pool)
                hp = getattr(self, "handshake_pool", "A")
                if self.subscribe_id is not None and mid == self.subscribe_id and pool_key != hp:
                    continue
                if self.authorize_id is not None and mid == self.authorize_id and pool_key != hp:
                    continue

                await write_line(self.miner_w, raw, "downstream")
                continue

            # Forward setup methods from Pool A only (but never notify/diff)
            if pool_key == "A" and method is not None:
                await write_line(self.miner_w, raw, "downstream")

    async def forward_jobs(self):
        await self.miner_ready.wait()
        last_seen = {"A": 0, "B": 0}
        current_pool = self.active_pool
        last_switch_ts = time.monotonic()
        min_switch = max(0, int(self.cfg.sched.min_switch_seconds))

        # In single-pool mode, start on the enabled pool immediately and
        # don't let min_switch_seconds block the first switch.
        wA0 = int(self.cfg.sched.wA)
        wB0 = int(self.cfg.sched.wB)
        if wA0 <= 0 and wB0 > 0:
            current_pool = "B"
            self.active_pool = "B"
            last_switch_ts = 0.0
        elif wB0 <= 0 and wA0 > 0:
            current_pool = "A"
            self.active_pool = "A"
            last_switch_ts = 0.0
        while True:
            while self.notify_seq["A"] <= last_seen["A"] and self.notify_seq["B"] <= last_seen["B"]:
                await asyncio.sleep(0.05)

            pick = self.sched.pick()

            # Honor single-pool mode when one weight is 0 (no fallback)
            wA = int(self.cfg.sched.wA)
            wB = int(self.cfg.sched.wB)
            if wA <= 0:
                pick = "B"
            elif wB <= 0:
                pick = "A"

            if wA > 0 and wB > 0:
                # Dual-pool mode fallback to other pool if chosen has no new notify
                if self.notify_seq[pick] <= last_seen[pick]:
                    other = "B" if pick == "A" else "A"
                    if self.notify_seq[other] <= last_seen[other]:
                          await asyncio.sleep(0.01)
                          continue
                    pick = other
            else:
                # Single-pool mode: do NOT fallback to disabled pool
                if self.notify_seq[pick] <= last_seen[pick]:
                      await asyncio.sleep(0.01)
                      continue

            now = time.monotonic()
            if pick != current_pool and (now - last_switch_ts) < min_switch:
                pick = current_pool

            self.active_pool = pick

            if pick != current_pool:
                current_pool = pick
                last_switch_ts = time.monotonic()

                # Arm job-context barrier
                self.ctx_pool = pick
                self.ctx_epoch += 1
                self.barrier_pending = True
                self.barrier_until = time.monotonic() + 0.0
                self.barrier_notifies_needed = 1
                self.barrier_notifies_seen = 0
                self.slog("ctx_switch", pool=pick, epoch=self.ctx_epoch)
                await self.maybe_replay_downstream_subscribe(pick)

            raw = self.latest_notify_raw[pick]
            jid = self.latest_jobid[pick]
            if raw is None:
                continue

            if jid:
                self.job_owner[jid] = pick
                self.job_epoch[jid] = self.ctx_epoch

            # Ensure miner receives correct extranonce/diff context before notify
            await self.maybe_send_downstream_diff(pick)
            await self.maybe_send_downstream_extranonce(pick)
            # On first notify after switch, force clean_jobs=True
            out_raw = raw
            if (self.barrier_pending and self.barrier_notifies_seen == 0) or self.force_clean_next_notify:
                try:
                    nm = loads_json(raw)
                    if nm.get("method") == "mining.notify":
                        params = nm.get("params") or []
                        if isinstance(params, list) and len(params) >= 1:
                            if len(params) >= 9:
                                params[-1] = True
                            else:
                                params.append(True)
                            nm["params"] = params
                            self.force_clean_next_notify = False
                            out_raw = dumps_json(nm)
                except Exception:
                    pass

            await write_line(self.miner_w, out_raw, "downstream")

            if self.barrier_pending and pick == self.ctx_pool:
                self.barrier_notifies_seen += 1
                self.slog(
                    "barrier_notify_seen",
                    pool=pick,
                    epoch=self.ctx_epoch,
                    seen=self.barrier_notifies_seen,
                    needed=self.barrier_notifies_needed,
                    jobid=jid,
                )
                if self.barrier_notifies_seen >= self.barrier_notifies_needed:
                    self.barrier_pending = False
                    self.slog("barrier_cleared_by_notifies", pool=pick, epoch=self.ctx_epoch, seen=self.barrier_notifies_seen)

            last_seen[pick] = self.notify_seq[pick]
            JOBS_FORWARDED.labels(pool=pick).inc()
            self.slog("job_forwarded", pool=pick, jobid=jid, seq=last_seen[pick])

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
        # Close downstream
        try:
            self.miner_w.close()
            await self.miner_w.wait_closed()
        except Exception:
            pass

        # Close upstream + adjust gauges safely
        for k, w in (("A", self.wA), ("B", self.wB)):
            if w is not None:
                try:
                    w.close()
                    await w.wait_closed()
                except Exception:
                    pass
            if self.upstream_connected.get(k):
                try:
                    CONN_UPSTREAM.labels(pool=k).dec()
                except Exception:
                    pass
                self.upstream_connected[k] = False


async def handle_miner(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, cfg: AppCfg):
    peer = writer.get_extra_info("peername")
    sid = f"{peer[0]}:{peer[1]}" if peer else "unknown"

    CONN_DOWNSTREAM.inc()
    log("miner_connected", sid=sid, peer=str(peer))

    sess = ProxySession(cfg, reader, writer)

    try:
        await sess.run()
    except Exception as e:
        log("session_error", sid=sid, peer=str(peer), err=str(e), tb=traceback.format_exc())
    finally:
        try:
            CONN_DOWNSTREAM.dec()
        except Exception:
            pass
        await sess.close()
        log("miner_disconnected", sid=sid, peer=str(peer))


async def main():
    cfg_path = os.environ.get("DPMP_CONFIG", os.path.join(os.path.dirname(__file__), "config.json"))
    cfg = load_config(cfg_path)

    if cfg.metrics_enabled:
        start_http_server(cfg.metrics_port, addr=cfg.metrics_host)
        log("metrics_started", host=cfg.metrics_host, port=cfg.metrics_port)

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

    async with server:
        await stop.wait()

    log("dpmp_stopped")


if __name__ == "__main__":
    asyncio.run(main())
