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
    writer.write(data)
    await writer.drain()
    MSG_TX.labels(side=side).inc()

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



        self.latest_notify_raw: Dict[str, Optional[bytes]] = {"A": None, "B": None}
        self.latest_jobid: Dict[str, Optional[str]] = {"A": None, "B": None}
        self.notify_seq: Dict[str, int] = {"A": 0, "B": 0}
        self.extranonce1: Dict[str, Optional[str]] = {"A": None, "B": None}
        self.extranonce2_size: Dict[str, Optional[int]] = {"A": None, "B": None}


        self.latest_diff: Dict[str, Optional[float]] = {"A": None, "B": None}
        self.last_downstream_diff: Optional[float] = None
        self.active_pool: str = "A"  # pool whose job we last forwarded

        self.job_owner: Dict[str, str] = {}
        self.submit_owner: Dict[Any, str] = {}
        self.submit_diff: Dict[Any, float] = {}
        self.accepted_diff_sum: Dict[str, float] = {"A": 0.0, "B": 0.0}

        self.sched = RatioScheduler(cfg.sched.wA, cfg.sched.wB)

    async def connect_pool(self, pcfg: PoolCfg) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        log("pool_connecting", key=pcfg.key, pool=pcfg.name, host=pcfg.host, port=pcfg.port)
        r, w = await asyncio.open_connection(pcfg.host, pcfg.port)
        CONN_UPSTREAM.labels(pool=pcfg.key).inc()
        log("pool_connected", key=pcfg.key, pool=pcfg.name, host=pcfg.host, port=pcfg.port)
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

        return v

    async def maybe_send_downstream_extranonce(self, pool_key: str):
        en1 = self.extranonce1.get(pool_key)
        en2s = self.extranonce2_size.get(pool_key)
        if not en1 or en2s is None:
            return

        # Tell miner the extranonce context for the active pool (Stratum V1 extension; many miners support it)
        msg = {"id": None, "method": "mining.set_extranonce", "params": [en1, int(en2s)]}
        await write_line(self.miner_w, dumps_json(msg), "downstream")
        log("downstream_extranonce_set", pool=pool_key, extranonce1=en1, extranonce2_size=int(en2s))



    async def maybe_send_downstream_diff(self, pool_key: str):
        # If a pool is disabled by scheduler weights, never send its difficulty downstream.
        # Prevents diff flips from the non-active pool (poisoning).
        if pool_key == "A" and self.cfg.sched.wA <= 0:
            return
        if pool_key == "B" and self.cfg.sched.wB <= 0:
            return

        dd = self.downstream_diff_policy(pool_key)
        if dd is None:
            return
        if self.last_downstream_diff is not None and dd == self.last_downstream_diff:
            return
        self.last_downstream_diff = dd
        DIFF_DOWNSTREAM.set(dd)
        await write_line(self.miner_w, dumps_json({"id": None, "method": "mining.set_difficulty", "params": [dd]}), "downstream")
        log("downstream_diff_set", pool=pool_key, diff=dd)

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
                self.configure_id = msg.get("id")
                log("configure_req", sid=self.sid, id=msg.get("id"), params=msg.get("params"))
                # IMPORTANT: Some miners (e.g. Avalon) expect an immediate response to mining.configure.
                # ACK locally (do not wait for pool), then still forward to Pool A for compatibility.
                cfg_id = msg.get("id")
                if cfg_id is not None:
                    # Build a configure result that matches what the miner requested.
                    # Example miner params: [["version-rolling"], {"version-rolling.mask":"ffffffff"}]
                    result = {}
                    try:
                        p = msg.get("params") or []
                        exts = p[0] if len(p) >= 1 and isinstance(p[0], list) else []
                        opts = p[1] if len(p) >= 2 and isinstance(p[1], dict) else {}
                        if "version-rolling" in exts:
                            result["version-rolling"] = True
                            # Echo requested mask if present, else default to all bits.
                            result["version-rolling.mask"] = str(opts.get("version-rolling.mask", "ffffffff"))
                    except Exception:
                        # Fall back to minimal OK if anything unexpected happens.
                        result = {"version-rolling": True, "version-rolling.mask": "ffffffff"}
                    resp = {"id": cfg_id, "result": result, "error": None}
                    await write_line(self.miner_w, dumps_json(resp), "downstream")
                    log("configure_ack_sent", sid=self.sid, id=cfg_id)
                # Send configure only to Pool A (handshake pool)
                await write_line(self.wA, raw, "upstreamA")
                continue

            if m == "mining.subscribe":
                self.subscribe_id = msg.get("id")
                await write_line(self.wA, raw, "upstreamA")
                await write_line(self.wB, raw, "upstreamB")
                continue


            if m == "mining.authorize":
                self.authorize_id = msg.get("id")
                outA = self.rewrite_authorize(self.cfg.poolA, msg)
                outB = self.rewrite_authorize(self.cfg.poolB, msg)
                log("authorize_rewrite", pool="A", worker=self.worker, upstream_user=outA["params"][0])
                log("authorize_rewrite", pool="B", worker=self.worker, upstream_user=outB["params"][0])
                await write_line(self.wA, dumps_json(outA), "upstreamA")
                await write_line(self.wB, dumps_json(outB), "upstreamB")
                self.miner_ready.set()
                continue

            if m == "mining.submit":
                SHARES_SUBMITTED.inc()
                jid = jobid_from_submit(msg)
                pool = self.job_owner.get(jid or "", "A")
                self.submit_owner[msg.get("id")] = pool
                mid = msg.get("id")
                if mid is not None:
                    d = self.last_downstream_diff
                    if d is None:
                        d = self.latest_diff.get(pool)
                    self.submit_diff[mid] = float(d or 0.0)
                if pool == "B":
                    await write_line(self.wB, raw, "upstreamB")
                else:
                    await write_line(self.wA, raw, "upstreamA")
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

            if method == "mining.set_difficulty":
                try:
                    self.latest_diff[pool_key] = float((msg.get("params") or [None])[0])
                    log("pool_diff", pool=pool_key, diff=self.latest_diff[pool_key])
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
                log("pool_notify", sid=self.sid, pool=pool_key, jobid=jid, seq=self.notify_seq[pool_key])
                continue

            # Forward ALL id-based responses to miner (subscribe/auth/submit responses)
            if "id" in msg and msg.get("method") is None:
                mid = msg.get("id")

                # Capture per-pool subscribe response (extranonce context)
                if self.subscribe_id is not None and mid == self.subscribe_id:
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
                    except Exception as e:
                        log("subscribe_parse_error", pool=pool_key, err=str(e))

                if self.authorize_id is not None and mid == self.authorize_id:
                    log("auth_result", pool=pool_key, ok=bool(msg.get("result")), error=msg.get("error"))

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


                # NEW: only forward ONE subscribe/auth response (from Pool A)
                if self.subscribe_id is not None and mid == self.subscribe_id and pool_key != "A":
                    continue
                if self.authorize_id is not None and mid == self.authorize_id and pool_key != "A":
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
        ACTIVE_POOL.labels(pool="A").set(1 if current_pool == "A" else 0)
        ACTIVE_POOL.labels(pool="B").set(1 if current_pool == "B" else 0)
        last_switch_ts = time.monotonic()
        min_switch = max(0, int(self.cfg.sched.min_switch_seconds))


        last_sent_seq = {"A": 0, "B": 0}

        while True:
            now = time.monotonic()
            slice_s = max(1, int(self.cfg.sched.slice_seconds))
            min_switch = max(0, int(self.cfg.sched.min_switch_seconds))

            # Time-slice switching (ratio by time), not by notify frequency.
            if (now - last_switch_ts) >= slice_s and (now - last_switch_ts) >= min_switch:
                # Choose the pool that is behind in accepted difficulty share vs target.
                wA = max(0, int(self.cfg.sched.wA))
                wB = max(0, int(self.cfg.sched.wB))
                totw = wA + wB
                if totw <= 0:
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

            pick = current_pool
            raw = self.latest_notify_raw.get(pick)
            jid = self.latest_jobid.get(pick)
            if raw is not None:
                seq = int(self.notify_seq.get(pick, 0))

                # Forward only when there's a new notify for this pool,
                # or immediately after a switch (seq will differ).
                if seq > last_sent_seq.get(pick, 0):
                    if jid:
                        self.job_owner[jid] = pick

                    await self.maybe_send_downstream_extranonce(pick)
                    await self.maybe_send_downstream_diff(pick)
                    await write_line(self.miner_w, raw, "downstream")

                    last_sent_seq[pick] = seq
                    JOBS_FORWARDED.labels(pool=pick).inc()
                    log("job_forwarded", sid=self.sid, pool=pick, jobid=jid, seq=seq)

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
    CONN_DOWNSTREAM.inc()
    log("miner_connected", peer=str(peer))

    sess = ProxySession(cfg, reader, writer, sid=str(peer))
    try:
        await sess.run()
    except Exception as e:
        log("session_error", peer=str(peer), err=str(e))
    finally:
        CONN_DOWNSTREAM.dec()
        CONN_UPSTREAM.labels(pool="A").dec()
        CONN_UPSTREAM.labels(pool="B").dec()
        await sess.close()
        log("miner_disconnected", peer=str(peer))

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
