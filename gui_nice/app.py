"""
DPMP - Dual-Pool Mining Proxy (Stratum v1) GUI Dashboard
Copyright (c) 2025-2026 Christopher Kryza. Subject to the MIT License.
Developed with NiceGUI (https://nicegui.io)
"""

import asyncio
import io
import json
import os
import subprocess
import time
import re
import zipfile

from datetime import date 
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.request import urlopen, Request

from nicegui import ui, app

CONFIG_PATH = os.environ.get("DPMP_CONFIG_PATH", os.path.expanduser("~/dpmp/dpmp/config_v2.json"))
METRICS_URL  = os.environ.get("DPMP_METRICS_URL", "http://127.0.0.1:9210/metrics")
DPMP_LOG_PATH = os.environ.get("DPMP_LOG_PATH", os.path.expanduser("~/dpmp/dpmpv2_run.log"))
GUI_LOG_PATH  = os.environ.get("GUI_LOG_PATH", os.path.expanduser("~/dpmp/dpmpv2_gui.log"))
WEIGHTS_OVERRIDE_PATH = os.path.join(os.path.dirname(os.environ.get("DPMP_CONFIG_PATH", os.path.expanduser("~/dpmp/dpmp/config_v2.json"))), "weights_override.json")
ORACLE_CHART_HISTORY_PATH = os.path.join(os.path.dirname(os.environ.get("DPMP_CONFIG_PATH", os.path.expanduser("~/dpmp/dpmp/config_v2.json"))), "oracle_chart_history.json")
ORACLE_MODE_PATH = os.path.join(os.path.dirname(os.environ.get("DPMP_CONFIG_PATH", os.path.expanduser("~/dpmp/dpmp/config_v2.json"))), "oracle_mode.json")
WORKER_STATS_PATH = os.path.join(os.path.dirname(os.environ.get("DPMP_CONFIG_PATH", os.path.expanduser("~/dpmp/dpmp/config_v2.json"))), "worker_stats.json")
FLEET_METRICS_PATH = os.path.join(os.path.dirname(os.environ.get("DPMP_CONFIG_PATH", os.path.expanduser("~/dpmp/dpmp/config_v2.json"))), "fleet_metrics.json")
MINER_PAUSED_PATH = os.path.join(os.path.dirname(os.environ.get("DPMP_CONFIG_PATH", os.path.expanduser("~/dpmp/dpmp/config_v2.json"))), "miner_paused.json")
MANUAL_MODE_PATH = os.path.join(os.path.dirname(os.environ.get("DPMP_CONFIG_PATH", os.path.expanduser("~/dpmp/dpmp/config_v2.json"))), "manual_mode.json")
PINNED_ASSIGNMENTS_PATH = os.path.join(os.path.dirname(os.environ.get("DPMP_CONFIG_PATH", os.path.expanduser("~/dpmp/dpmp/config_v2.json"))), "pinned_assignments.json")
POOLS_ADDRESS_BOOK_PATH = os.path.join(os.path.dirname(os.environ.get("DPMP_CONFIG_PATH", os.path.expanduser("~/dpmp/dpmp/config_v2.json"))), "pools.json")
HOST = os.environ.get("NICEGUI_HOST", "0.0.0.0")
PORT = int(os.environ.get("NICEGUI_PORT", "8845"))
POLL_S = float(os.environ.get("NICEGUI_POLL_S", "2.0"))

#DARK_KEY = 'dpmp_dark_mode'

ui.add_head_html("""
<style>
/* Restore basic HTML formatting inside the About page */
.about-content ul { list-style: disc; margin: 0.5rem 0 0.75rem 1.25rem; padding-left: 1.25rem; }
.about-content ol { list-style: decimal; margin: 0.5rem 0 0.75rem 1.25rem; padding-left: 1.25rem; }
.about-content li { margin: 0.15rem 0; }
.about-content p  { margin: 0.6rem 0; }
.about-content h3 { font-size: 1.25rem; font-weight: 700; margin: 0.75rem 0 0.5rem 0; }
.about-content h4 { font-size: 1.05rem; font-weight: 600; margin: 0.75rem 0 0.4rem 0; }
.about-content hr { margin: 0.9rem 0; opacity: 0.35; }
@media (max-width: 768px) {
  .hide-on-mobile { display: none !important; }   

</style>

<script>

(function () {
  const KEY = 'dpmp_dark_mode';

  function desiredIsDark() {
    const v = localStorage.getItem(KEY);
    return (v === '1' || v === 'true');
  }

  function applyThemeAndSwitch() {
    try {
      const isDark = desiredIsDark();

      // Apply theme if Quasar is ready
      if (window.Quasar && Quasar.Dark) {
        Quasar.Dark.set(isDark);
      }

      // Sync switch state (NiceGUI/Quasar may re-render, so keep forcing it)
      const input = document.querySelector('#dpmp_dark_switch input[type="checkbox"]');
      if (input && input.checked !== isDark) {
        input.checked = isDark;
      }

      // "ready" when Quasar exists AND switch input exists
      return !!(window.Quasar && Quasar.Dark) && !!input;
    } catch (e) {
      return false;
    }
  }

  // Try repeatedly for a short time to survive late Quasar init + component re-renders
  let tries = 0;
  const timer = setInterval(() => {
    tries++;
    const ok = applyThemeAndSwitch();
    if (ok || tries >= 50) clearInterval(timer); // ~5s
  }, 100);
})();
</script>
""")

# timestamp in UTC format
def now_utc() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())

import subprocess
import signal

# return True if running in a container (e.g., Docker)
def _in_container() -> bool:
    return os.path.exists("/.dockerenv") or (os.environ.get("container") is not None)

# return True if systemd unit is active
def systemd_is_active(unit: str) -> bool:
    # returns True if systemd reports "active" (bare-metal).
    if _in_container():
        return False
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        return r.returncode == 0 and (r.stdout or "").strip() == "active"
    except Exception:
        return False

# extract single gauge value from raw Prometheus text format
def _prom_gauge_value(text: str, name: str, pool: str | None = None) -> float | None:
    if pool is None:
        # e.g. dpmp_downstream_connections 1.0
        m = re.search(rf'^{re.escape(name)}\s+([0-9eE\+\-\.]+)\s*$', text, flags=re.M)
    else:
        # e.g. dpmp_active_pool{pool="A"} 1.0
        m = re.search(
            rf'^{re.escape(name)}\{{[^}}]*pool="{re.escape(pool)}"[^}}]*\}}\s+([0-9eE\+\-\.]+)\s*$',
            text,
            flags=re.M,
        )
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None

# extract first matching float value from parsed Prometheus metrics dict
def prom_first_float(metrics: dict, name: str, labels: dict | None = None) -> float | None:
    rows = metrics.get(name) or []
    if labels:
        for row in rows:
            if (row.get("labels") or {}) == labels:
                try:
                    return float(row.get("value"))
                except Exception:
                    return None
        return None
    # no label filter -- first value
    try:
        return float(rows[0].get("value"))
    except Exception:
        return None

# read text file with max size limit
def read_text_file(path: str, max_bytes: int = 200_000) -> str:
    try:
        with open(path, "rb") as f:
            data = f.read()
        if len(data) > max_bytes:
            data = data[-max_bytes:]
        return data.decode("utf-8", errors="replace")
    except FileNotFoundError:
        return f"[missing] {path}"
    except Exception as e:
        return f"[error reading {path}] {e}"


import math as _math

# build ratio gauge
def _build_gauge_svg(pct_a: float, size: int = 160, greyed: bool = False) -> str:
    """Build a half-circle gauge SVG. 0%A left, 50/50 top, 100%A right.

    Uses multiple small arc segments instead of complex large-arc flags
    to avoid browser rendering issues with semicircular arcs.

    Args:
        pct_a:  Fraction of Pool A (0.0 to 1.0).
        size:   SVG width in pixels.
        greyed: If True, render the gauge in muted grey (used in Manual mode
                when the SR gauge is inactive).
    """
    pct_a = max(0.001, min(0.999, pct_a))
    cx, cy = 100, 92
    r = 68
    sw = 12  # stroke width
    nr = 58  # needle length

    def _xy(deg, rr=r):
        rd = _math.radians(deg)
        return (cx + rr * _math.cos(rd), cy - rr * _math.sin(rd))

    # Needle angle: 0%A -> 180deg (left), 100%A -> 0deg (right)
    nd = 180.0 - (pct_a * 180.0)
    npt = _xy(nd, nr)

    # Build arcs using series of line-to points for reliability
    def _arc_path(start_deg, end_deg, steps=24):
        pts = []
        for i in range(steps + 1):
            d = start_deg + (end_deg - start_deg) * i / steps
            pts.append(_xy(d))
        path = 'M %.1f,%.1f' % pts[0]
        for p in pts[1:]:
            path += ' L %.1f,%.1f' % p
        return path

    # Background: full semicircle 180->0 (light grey)
    bg_path = _arc_path(180, 0, 48)
    bg = '<path d="%s" fill="none" stroke="#9ca3af" stroke-opacity="0.2" stroke-width="%d" stroke-linecap="round"/>' % (bg_path, sw)

    if greyed:
        # Greyed-out mode: both arcs use muted grey, needle is grey
        aa = ''
        a_deg = 180.0 - nd
        if a_deg > 0.5:
            a_path = _arc_path(180, nd, max(4, int(a_deg / 4)))
            aa = '<path d="%s" fill="none" stroke="#6b7280" stroke-opacity="0.4" stroke-width="%d" stroke-linecap="round"/>' % (a_path, sw)
        ba = ''
        if nd > 0.5:
            b_path = _arc_path(nd, 0, max(4, int(nd / 4)))
            ba = '<path d="%s" fill="none" stroke="#6b7280" stroke-opacity="0.25" stroke-width="%d" stroke-linecap="round"/>' % (b_path, sw)
        lb = '<text x="%d" y="%d" fill="#6b7280" font-size="11" font-weight="bold" text-anchor="middle">A</text>' % (cx - r - 14, cy + 4)
        lb += '<text x="%d" y="%d" fill="#6b7280" font-size="11" font-weight="bold" text-anchor="middle">B</text>' % (cx + r + 14, cy + 4)
        ne = '<line x1="%d" y1="%d" x2="%.1f" y2="%.1f" stroke="#6b7280" stroke-opacity="0.5" stroke-width="2.5" stroke-linecap="round"/>' % (cx, cy, npt[0], npt[1])
        ne += '<circle cx="%d" cy="%d" r="4" fill="#6b7280" fill-opacity="0.5"/>' % (cx, cy)
    else:
        # Normal mode: Pool A blue, Pool B grey, needle red
        aa = ''
        a_deg = 180.0 - nd
        if a_deg > 0.5:
            a_path = _arc_path(180, nd, max(4, int(a_deg / 4)))
            aa = '<path d="%s" fill="none" stroke="#3b82f6" stroke-width="%d" stroke-linecap="round"/>' % (a_path, sw)
        ba = ''
        if nd > 0.5:
            b_path = _arc_path(nd, 0, max(4, int(nd / 4)))
            ba = '<path d="%s" fill="none" stroke="#9ca3af" stroke-opacity="0.35" stroke-width="%d" stroke-linecap="round"/>' % (b_path, sw)
        lb = '<text x="%d" y="%d" fill="#22d3ee" font-size="11" font-weight="bold" text-anchor="middle">A</text>' % (cx - r - 14, cy + 4)
        lb += '<text x="%d" y="%d" fill="#f59e0b" font-size="11" font-weight="bold" text-anchor="middle">B</text>' % (cx + r + 14, cy + 4)
        ne = '<line x1="%d" y1="%d" x2="%.1f" y2="%.1f" stroke="#ef4444" stroke-width="2.5" stroke-linecap="round"/>' % (cx, cy, npt[0], npt[1])
        ne += '<circle cx="%d" cy="%d" r="4" fill="#ef4444"/>' % (cx, cy)

    # Tick marks at 0%, 25%, 50%, 75%, 100%
    tk = ''
    for fr in [0.0, 0.25, 0.5, 0.75, 1.0]:
        ta = 180.0 - fr * 180.0
        ti = _xy(ta, r - 9)
        to = _xy(ta, r + 9)
        tk += '<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#9ca3af" stroke-opacity="0.4" stroke-width="1.5"/>' % (ti[0], ti[1], to[0], to[1])

    w = size
    h = int(size * 0.62)
    return '<svg width="%d" height="%d" viewBox="-5 -2 210 100" xmlns="http://www.w3.org/2000/svg">%s%s%s%s%s%s</svg>' % (w, h, bg, aa, ba, tk, lb, ne)


# read JSON file
def read_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

# read weight defaults from config_v2.json
def get_config_weights() -> tuple[int, int]:
    """Read Pool A / Pool B weights from config_v2.json. Returns (wA, wB)."""
    try:
        cfg = read_json(CONFIG_PATH)
        sched = cfg.get("scheduler", {})
        wA = int(sched.get("poolA_weight", 50))
        wB = int(sched.get("poolB_weight", 50))
        return (wA, wB)
    except Exception:
        return (50, 50)

# get the data points needed for auto-balancer operation
def get_auto_balance_config() -> dict:
    """Read auto-balance and chain config from config_v2.json.
    
    Returns dict with keys:
      auto_balance (bool), max_deviation (int),
      oracle_url (str), oracle_poll_seconds (int),
      poolA_chain (str), poolB_chain (str)
    """
    try:
        cfg = read_json(CONFIG_PATH)
        sched = cfg.get("scheduler", {})
        pools = cfg.get("pools", {})
        return {
            "auto_balance": bool(sched.get("auto_balance", False)),
            "max_deviation": int(sched.get("auto_balance_max_deviation", 20)),
            "oracle_url": str(sched.get("oracle_url", "")),
            "oracle_poll_seconds": int(sched.get("oracle_poll_seconds", 600)),
            "poolA_chain": str(pools.get("A", {}).get("chain", "BTC")).upper(),
            "poolB_chain": str(pools.get("B", {}).get("chain", "BCH")).upper(),
        }
    except Exception:
        return {
            "auto_balance": False, "max_deviation": 20,
            "oracle_url": "", "oracle_poll_seconds": 600,
            "poolA_chain": "BTC", "poolB_chain": "BCH",
        }

# get name/chain info for both pools
def get_pool_info() -> dict:
    """Read pool names and chains from config_v2.json for the Stats tab.
    Returns dict like:
      {"A": {"name": "My Pool", "chain": "BTC"}, "B": {"name": "Other Pool", "chain": "BCH"}}
    """
    try:
        cfg = read_json(CONFIG_PATH)
        pools = cfg.get("pools", {})
        pa = pools.get("A", {})
        pb = pools.get("B", {})
        return {
            "A": {"name": pa.get("name", "Pool A"), "chain": str(pa.get("chain", "")).upper() or "--"},
            "B": {"name": pb.get("name", "Pool B"), "chain": str(pb.get("chain", "")).upper() or "--"},
        }
    except Exception:
        return {
            "A": {"name": "Pool A", "chain": "--"},
            "B": {"name": "Pool B", "chain": "--"},
        }

# worker stats for worker table
def read_worker_stats() -> dict:
    """Read worker_stats.json written by dpmpv2.  Returns {} on any error."""
    try:
        with open(WORKER_STATS_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}

# fleet stats for fleet table
def read_fleet_metrics() -> dict:
    """Read fleet_metrics.json written by dpmpv2.  Returns {} on any error."""
    try:
        with open(FLEET_METRICS_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}

# format hashrate for table
def fmt_hashrate(h: float) -> str:
    """Format a hashrate (in H/s) into a human-readable string.
    Examples: 1234 -> '1.23 KH/s', 1234567 -> '1.23 MH/s', etc.
    """
    if h <= 0:
        return "--"
    units = [("EH/s", 1e18), ("PH/s", 1e15), ("TH/s", 1e12),
             ("GH/s", 1e9), ("MH/s", 1e6), ("KH/s", 1e3), ("H/s", 1)]
    for label, threshold in units:
        if h >= threshold:
            return f"{h / threshold:.2f} {label}"
    return f"{h:.2f} H/s"

# format difficulty for table
def fmt_diff(d: float) -> str:
    """Format a difficulty value with K/M/G/T suffixes for readability."""
    if d <= 0:
        return "--"
    units = [("T", 1e12), ("G", 1e9), ("M", 1e6), ("K", 1e3)]
    for label, threshold in units:
        if d >= threshold:
            return f"{d / threshold:.2f}{label}"
    return f"{d:.1f}"

# write weight override file (or delete it to revert to config defaults)
def write_weight_override(wA: int, wB: int) -> None:
    """Write weights_override.json so DPMP picks up the new weights on its next tick."""
    obj = {"poolA_weight": int(wA), "poolB_weight": int(wB)}
    write_json_atomic(WEIGHTS_OVERRIDE_PATH, obj)

def delete_weight_override() -> None:
    """Remove weights_override.json so DPMP reverts to config_v2.json defaults."""
    try:
        os.remove(WEIGHTS_OVERRIDE_PATH)
    except FileNotFoundError:
        pass
    except Exception:
        pass

# oracle_mode.json helpers (hot-switch between oracle and slider)
def write_oracle_mode(oracle_active: bool) -> None:
    """Write oracle_mode.json so DPMP knows whether oracle should write weights."""
    write_json_atomic(ORACLE_MODE_PATH, {"oracle_active": oracle_active})

def read_oracle_mode() -> bool | None:
    """Read oracle_mode.json. Returns True/False, or None if file missing."""
    try:
        with open(ORACLE_MODE_PATH, "r", encoding="utf-8") as f:
            obj = json.load(f)
        return bool(obj.get("oracle_active", True))
    except FileNotFoundError:
        return None
    except Exception:
        return None

def delete_oracle_mode() -> None:
    """Remove oracle_mode.json so DPMP falls back to config auto_balance on restart."""
    try:
        os.remove(ORACLE_MODE_PATH)
    except FileNotFoundError:
        pass
    except Exception:
        pass

# miner_paused.json helpers (soft-pause individual miners from GUI)
def read_paused_miners() -> list:
    """Read miner_paused.json. Returns a list of paused worker names, or [] on error."""
    try:
        with open(MINER_PAUSED_PATH, "r", encoding="utf-8") as f:
            obj = json.load(f)
        return list(obj.get("paused", []))
    except FileNotFoundError:
        return []
    except Exception:
        return []

def write_paused_miners(paused_list: list) -> None:
    """Write miner_paused.json with the current list of paused worker names."""
    write_json_atomic(MINER_PAUSED_PATH, {"paused": paused_list})

# manual_mode.json helpers (hot-switch to Manual mode from GUI)
def read_manual_mode() -> bool | None:
    """Read manual_mode.json. Returns True/False, or None if file missing."""
    try:
        obj = read_json(MANUAL_MODE_PATH)
        return bool(obj.get("manual_active", False))
    except Exception:
        return None

def write_manual_mode(active: bool) -> None:
    """Write manual_mode.json so dpmp_fleet picks up the mode change instantly."""
    write_json_atomic(MANUAL_MODE_PATH, {"manual_active": active})

def delete_manual_mode() -> None:
    """Remove manual_mode.json so Manual mode is inactive on next startup."""
    try:
        os.remove(MANUAL_MODE_PATH)
    except Exception:
        pass

# pinned_assignments.json helpers (read by Fleet table dropdown in Manual mode)
def read_pinned_assignments_gui() -> dict:
    """Read pinned_assignments.json. Returns {worker_name: 'A'/'B'}, or {} on error."""
    try:
        obj = read_json(PINNED_ASSIGNMENTS_PATH)
        return {k: v for k, v in obj.items() if v in ("A", "B")}
    except Exception:
        return {}

def write_pinned_assignments_gui(assignments: dict) -> None:
    """Write pinned_assignments.json atomically. Only B assignments are stored."""
    b_only = {k: v for k, v in assignments.items() if v == "B"}
    write_json_atomic(PINNED_ASSIGNMENTS_PATH, b_only)


# write JSON file atomically
def write_json_atomic(path: str, obj: Dict[str, Any]) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)

# ---------------------------------------------------------------------------
# Pool Address Book helpers
# ---------------------------------------------------------------------------
def load_address_book() -> dict:
    """Load pools.json address book. Returns dict keyed by 'host:port'."""
    try:
        obj = read_json(POOLS_ADDRESS_BOOK_PATH)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    return {}

def save_address_book(book: dict) -> None:
    """Write pools.json address book atomically."""
    try:
        write_json_atomic(POOLS_ADDRESS_BOOK_PATH, book)
    except Exception:
        pass

def address_book_key(host: str, port: int) -> str:
    """Generate a stable key for an address book entry."""
    return f"{host.strip().lower()}:{int(port)}"

def address_book_autosave(host: str, port: int, name: str, wallet: str, chain: str) -> None:
    """Add a pool to the address book if not already present.
    Called automatically on Apply. Never overwrites an existing entry."""
    host = host.strip()
    if not host:
        return
    key = address_book_key(host, port)
    book = load_address_book()
    if key not in book:
        book[key] = {
            "host":   host,
            "port":   int(port),
            "name":   name.strip(),
            "wallet": wallet.strip(),
            "chain":  chain.strip().upper(),
        }
        save_address_book(book)

# Save oracle chart history to disk (survives browser refresh)
def save_oracle_chart_history(history: list, poll_seconds: int) -> None:
    """Write chart history + metadata so a browser refresh can restore the charts."""
    try:
        obj = {
            "poll_seconds": poll_seconds,
            "saved_at": time.time(),
            "points": history,
        }
        write_json_atomic(ORACLE_CHART_HISTORY_PATH, obj)
    except Exception:
        pass  # non-critical, don't crash the GUI

# Load oracle chart history from disk (if fresh enough)
def load_oracle_chart_history(poll_seconds: int) -> list:
    """Load saved chart history if the most recent point is within poll_seconds of now."""
    try:
        if not os.path.isfile(ORACLE_CHART_HISTORY_PATH):
            return []
        with open(ORACLE_CHART_HISTORY_PATH, "r", encoding="utf-8") as f:
            obj = json.load(f)
        points = obj.get("points", [])
        saved_at = obj.get("saved_at", 0)
        if not points:
            return []
        # If saved_at is older than 2x poll interval, data is stale -- discard
        age = time.time() - saved_at
        if age > poll_seconds * 2:
            return []
        # Return up to 8 most recent points
        return points[-8:]
    except Exception:
        return []

# Delete oracle chart history file (called on DPMP restart)
def clear_oracle_chart_history() -> None:
    """Remove the chart history file so charts start fresh after restart."""
    try:
        if os.path.isfile(ORACLE_CHART_HISTORY_PATH):
            os.remove(ORACLE_CHART_HISTORY_PATH)
    except Exception:
        pass

# HTTP GET with timeout
def http_get_text(url: str, timeout_s: float = 3.0) -> str:
    req = Request(url, headers={"User-Agent": "dpmpv2-nicegui"})
    try:
        with urlopen(req, timeout=timeout_s) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception:
        # dpmpv2 restarts will temporarily drop the metrics listener (Errno 111)
        return ""

# parse a single line of Prometheus text format
def parse_prom_line(line: str) -> Optional[tuple[str, Dict[str, str], float]]:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    # name{a="b"} value  OR  name value
    if " " not in line:
        return None
    left, val = line.split(None, 1)
    try:
        v = float(val.strip())
    except Exception:
        return None
    if "{" in left and left.endswith("}"):
        name, rest = left.split("{", 1)
        rest = rest[:-1]
        labels: Dict[str, str] = {}
        if rest.strip():
            # very small parser; safe for typical prom label syntax
            parts = []
            cur = ""
            in_q = False
            for ch in rest:
                if ch == '"':
                    in_q = not in_q
                if ch == "," and not in_q:
                    parts.append(cur)
                    cur = ""
                else:
                    cur += ch
            if cur:
                parts.append(cur)
            for p in parts:
                if "=" in p:
                    k, vv = p.split("=", 1)
                    labels[k.strip()] = vv.strip().strip('"')
        return name, labels, v
    return left, {}, v

# extract first matching Prometheus metric value from raw text
def prom_value(text: str, metric: str, match_labels: Dict[str, str] | None = None) -> Optional[float]:
    match_labels = match_labels or {}
    for line in text.splitlines():
        parsed = parse_prom_line(line)
        if not parsed:
            continue
        name, labels, v = parsed
        if name != metric:
            continue
        ok = True
        for k, vv in match_labels.items():
            if labels.get(k) != vv:
                ok = False
                break
        if ok:
            return v
    return None

# restart dpmpv2 process
def restart_dpmpv2() -> tuple[bool, str]:
    # In Umbrel (container), there is no systemd. Restart DPMP by terminating dpmpv2;
    # entrypoint.sh will re-launch it.
    if _in_container():
        try:
            import pathlib, time as _time

            pids: list[int] = []
            for p in pathlib.Path("/proc").glob("[0-9]*"):
                try:
                    cmd = (p / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "ignore")
                except Exception:
                    continue
                if "/app/dpmp/dpmpv2.py" in cmd or "dpmpv2.py" in cmd:
                    try:
                        pids.append(int(p.name))
                    except Exception:
                        pass

            if not pids:
                return False, "container restart: dpmpv2 pid not found"

            for pid in pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                except Exception:
                    pass

            # Give dpmpv2 time to run its clean shutdown handler
            # (disconnect miners, flush state).  Check every 0.5s
            # for up to 5s before resorting to SIGKILL.
            for _wait in range(10):
                _time.sleep(0.5)
                still_alive = False
                for pid in pids:
                    try:
                        os.kill(pid, 0)  # signal 0 = check if alive
                        still_alive = True
                    except OSError:
                        pass  # already exited
                if not still_alive:
                    break

            # Force kill any stragglers
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass

            return True, f"restart requested (container): killed dpmpv2 pid(s) {pids}"
        except Exception as e:
            return False, f"container restart failed: {e}"
    # Bare-metal dev: systemd user service
    try:
        p = subprocess.run(
            ["systemctl", "--user", "restart", "dpmpv2"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if p.returncode == 0:
            return True, "systemctl restart dpmpv2: OK"
        return False, (p.stderr.strip() or p.stdout.strip() or f"returncode={p.returncode}")
    except Exception as e:
        return False, str(e)

############################################################################################################

@dataclass
class AppState:
    config_obj: Dict[str, Any]
    config_raw: str
    last_metrics_raw: str = ""
    freeze_logs: bool = False
    log_filter: str = ""
    last_log_len: int = 0

# load initial state
def load_state() -> AppState:
    try:
        obj = read_json(CONFIG_PATH)
        raw = json.dumps(obj, indent=2)
    except Exception as e:
        obj = {}
        raw = f"[error loading config] {e}"
    return AppState(config_obj=obj, config_raw=raw)


state = load_state()

# today = date.today()  # replaced by dynamic _update_date()

# we are storing the icon in static/ to avoid issues with relative paths
app.add_static_files('/static', 'gui_nice/static')

# hide certain elements on small screens
with ui.row().classes("gap-4 items-center h-10 w-full"):      
    ui.image("/static/icond.png").classes("hide-on-mobile w-12 h-12 mb-0").style('fit: fill') # - hide this on small screens
    ui.label(f"Dual Pool Mining Proxy (DPMP)").classes("text-xl font-bold").style('color: #6E93D6')
    ui.space().classes("hide-on-mobile") # hide this on small screens
    lbl_date = ui.label("").classes("hide-on-mobile text-xs").style('color: #6E93D6') # hide this on small screens
ui.separator().classes("hide-on-mobile") # hide this on small screens

def _update_date():
    lbl_date.text = date.today().strftime('%Y-%m-%d')

_update_date()  # set immediately on load
ui.timer(60.0, _update_date)  # refresh every 60 seconds

# Tabs definition
with ui.tabs().classes("w-full") as tabs:
    t_home = ui.tab("Home")
    t_stats = ui.tab("Stats")
    t_cfg  = ui.tab("Config") 
    t_logs = ui.tab("Logs")
    t_about = ui.tab("About")

with ui.tab_panels(tabs, value=t_home).classes("w-full"):
    
    with ui.tab_panel(t_home):   
            
        # Two-column layout: System Paths (left) + Weight Slider (right) 
        # On mobile, flex-wrap causes the slider card to stack below.
        with ui.row().classes("w-full flex-wrap gap-6 items-stretch"):

            # Left column: System Paths + Restart 
            with ui.card().classes("min-w-[280px]"):
            #with ui.column().classes("flex-1 min-w-[280px]"):
                ui.label("System Paths:").classes("text-lg font-semibold")
                ui.markdown(
                    f"""
**Config:** `{CONFIG_PATH}`  
**Metrics:** `{METRICS_URL}`  
**DPMP log:** `{DPMP_LOG_PATH}`  
**GUI log:** `{GUI_LOG_PATH}`  
"""
                )

                with ui.row().classes("items-center gap-2"):
                    btn_restart = ui.button("Restart DPMP", icon="restart_alt")
                ui.html('<span style="font-size:0.8rem; opacity:0.7;">[Please see the <b>About</b> tab for setup and operational instructions.]</span>', sanitize=False)

            # Right column: Hashrate allocation (slider OR oracle panel)
            # Both panels are ALWAYS built. Visibility is toggled by the switch button.
            # The oracle background data collection runs regardless of which panel is visible.

            cfg_wA, cfg_wB = get_config_weights()
            cfg_total = cfg_wA + cfg_wB
            cfg_slider_default = round((cfg_wA / cfg_total) * 100 / 5) * 5 if cfg_total > 0 else 50

            # Clamp slider default to the 5-95 range
            cfg_slider_default = max(5, min(95, cfg_slider_default))

            # Mutable container so nested functions can update these values
            _cfg = {"wA": cfg_wA, "wB": cfg_wB, "slider_default": cfg_slider_default}

            ab_cfg = get_auto_balance_config()
            _auto_balance_enabled = ab_cfg["auto_balance"]

            # Determine chain validity: oracle auto-balance requires one BTC + one BCH pool
            _chain_a = ab_cfg["poolA_chain"]
            _chain_b = ab_cfg["poolB_chain"]
            _chain_valid = sorted([_chain_a, _chain_b]) == ["BCH", "BTC"]

            # Determine initial mode:
            #   - If chain config is invalid -> always slider, no switch button
            #   - If manual_mode.json exists and is active -> Manual mode
            #   - If oracle_mode.json exists -> use its value
            #   - Otherwise -> use config auto_balance
            _oracle_mode_file = read_oracle_mode()  # True/False/None
            _manual_mode_file = read_manual_mode()  # True/False/None

            if not _chain_valid:
                _show_oracle = False
                _show_manual = False
            elif _manual_mode_file is True:
                _show_oracle = False
                _show_manual = True
            elif _oracle_mode_file is not None:
                _show_oracle = _oracle_mode_file
                _show_manual = False
            else:
                _show_oracle = _auto_balance_enabled
                _show_manual = False

            # Shared mutable state for mode switching
            # "oracle_active" and "manual_active" are mutually exclusive.
            # Both False = Slider mode.
            _mode = {"oracle_active": _show_oracle, "manual_active": _show_manual}
            # ---- SLIDER PANEL (always built) ----
            weight_slider_ref = None

            # Only build the slider interaction if BOTH pools have weight > 0
            _slider_usable = (cfg_wA > 0 and cfg_wB > 0)

            #with ui.card().classes("flex-1 min-w-[320px] max-w-[480px]").style("min-height:357px") as slider_card:
            with ui.card().classes("flex-1 min-w-[320px] max-w-[480px] slider-card-height") as slider_card:

                with ui.row().classes("w-full items-center justify-between"):
                    with ui.row().classes("items-center gap-1"):
                        ui.icon("balance", size="sm").style("color: #6E93D6")
                        ui.label("Hashrate Allocation").classes("text-base font-semibold").style("color: #6E93D6")

                    # Forward arrow: goes to Oracle if available, otherwise directly to Manual
                    if _chain_valid and _slider_usable:
                        btn_switch_to_oracle = ui.button(icon="arrow_forward").props("dense outline size=sm round")
                    else:
                        btn_switch_to_manual_from_slider = ui.button(icon="arrow_forward").props("dense outline size=sm round")

                if _slider_usable:
                    # If an override file exists (slider was moved), start there instead of config defaults
                    try:
                        ov = read_json(WEIGHTS_OVERRIDE_PATH)
                        ov_wA = int(ov.get("poolA_weight", -1))
                        ov_wB = int(ov.get("poolB_weight", -1))
                        ov_total = ov_wA + ov_wB
                        if ov_wA >= 0 and ov_wB >= 0 and ov_total > 0:
                            slider_initial = round((ov_wA / ov_total) * 100 / 5) * 5
                            slider_initial = max(5, min(95, slider_initial))
                        else:
                            slider_initial = cfg_slider_default
                    except Exception:
                        slider_initial = cfg_slider_default

                    with ui.row().classes("w-full items-center gap-3"):
                        ui.label("Pool A").classes("text-sm font-semibold").style("color: #22d3ee")
                        weight_slider = ui.slider(min=5, max=95, step=5, value=slider_initial).classes("flex-1")
                        ui.label("Pool B").classes("text-sm font-semibold").style("color: #f59e0b")

                    lbl_weight_pct = ui.html("", sanitize=False).classes("text-sm font-mono text-center w-full")
                    lbl_weight_status = ui.html("", sanitize=False).classes("text-xs text-center w-full")

                    with ui.row().classes("w-full justify-center"):
                        btn_weight_reset = ui.button("Reset to Config Defaults", icon="restart_alt").props("dense outline size=sm").classes("text-xs")

                    weight_slider_ref = weight_slider
                else:
                    ui.label("Slider disabled (one pool has 0 weight)").classes("text-sm").style("color: #888")

            # Set initial visibility
            slider_card.visible = not _show_oracle and not _show_manual

            # ---- ORACLE PANEL (only built when chain config is valid AND both pools active) ----
            _oracle_ui = {}  # holds references to oracle UI elements
            _oracle_charts = []

            if _chain_valid and _slider_usable:
                _chain_left = ab_cfg["poolA_chain"]   # e.g. "BCH"
                _chain_right = ab_cfg["poolB_chain"]   # e.g. "BTC"

                # was 540px
                with ui.card().classes("flex-1 min-w-[280px] max-w-[600px]") as oracle_card:

                    with ui.row().classes("w-full items-center justify-between"):
                        with ui.row().classes("items-center gap-1"):
                            ui.icon("auto_graph", size="sm").style("color: #6E93D6")
                            ui.label("Oracle Auto-Balance").classes("text-base font-semibold").style("color: #6E93D6")

                        with ui.row().classes("items-center gap-2"):
                            # Health indicator
                            with ui.row().classes("items-center gap-1"):
                                oracle_health_dot = ui.icon("circle", size="xs")
                                oracle_health_lbl = ui.label("starting...").classes("text-xs")
                            # Navigation buttons: [<-] back to Slider, [->] forward to Manual
                            with ui.row().classes("items-center gap-1"):
                                btn_switch_to_slider = ui.button(icon="arrow_back").props("dense outline size=sm round")
                                btn_switch_to_manual = ui.button(icon="arrow_forward").props("dense outline size=sm round")

                    _oracle_ui["health_dot"] = oracle_health_dot
                    _oracle_ui["health_lbl"] = oracle_health_lbl

                    # Charts row: one per chain
                    with ui.row().classes("w-full gap-x-2 gap-y-4 flex-wrap"):
                        # Left chart (Pool A's chain)
                        with ui.column().classes("flex-1 items-center min-w-[220px]"):
                            ui.label(f"Pool A ({_chain_left})").classes("text-xs font-semibold").style("color: #22d3ee")
                            oracle_chart_left = ui.echart({
                                "tooltip": {"trigger": "axis"},
                                "legend": {"data": ["Short (6 blk)", "Baseline (72 blk)"],
                                           "textStyle": {"fontSize": 10, "color": "#888888"}, "top": 0},
                                "grid": {"top": 25, "right": 5, "bottom": 25, "left": 40},
                                "xAxis": {"type": "category",
                                          "data": ["", "", "", "", "", "", "", ""],
                                          "axisLabel": {"fontSize": 9, "color": "#888888", "interval": 0},
                                          "axisTick": {"show": True, "alignWithLabel": True, "lineStyle": {"color": "#888888"}},
                                          "axisLine": {"lineStyle": {"color": "#888888"}},
                                          "splitLine": {"show": False}},
                                "yAxis": {"type": "value",
                                          "min": 0, "axisLabel": {"formatter": "{value}", "color": "#888888"},
                                          "axisLine": {"show": True, "lineStyle": {"color": "#888888"}},
                                          "axisTick": {"show": True, "lineStyle": {"color": "#888888"}}},
                                "series": [
                                    {"name": "Short (6 blk)", "type": "line", "smooth": False,
                                     "showSymbol": True, "symbolSize": 6, "data": [None]*8,
                                     "lineStyle": {"color": "#22d3ee"}, "itemStyle": {"color": "#22d3ee"}},
                                    {"name": "Baseline (72 blk)", "type": "line", "smooth": False,
                                     "showSymbol": True, "symbolSize": 6, "data": [None]*8,
                                     "lineStyle": {"color": "#e879f9"},
                                     "itemStyle": {"color": "#e879f9"}},
                                ],
                            }).classes("w-full").style("height: 160px")
                            oracle_caption_left = ui.label("--").classes("text-xs font-mono")
                        _oracle_ui["chart_left"] = oracle_chart_left
                        _oracle_ui["caption_left"] = oracle_caption_left
                        _oracle_ui["chain_left"] = _chain_left

                        # Right chart (Pool B's chain)
                        with ui.column().classes("flex-1 items-center min-w-[220px]"):
                            ui.label(f"Pool B ({_chain_right})").classes("text-xs font-semibold").style("color: #f59e0b")
                            oracle_chart_right = ui.echart({
                                "tooltip": {"trigger": "axis"},
                                "legend": {"data": ["Short (6 blk)", "Baseline (72 blk)"],
                                           "textStyle": {"fontSize": 10, "color": "#888888"}, "top": 0},
                                "grid": {"top": 25, "right": 5, "bottom": 25, "left": 40},
                                "xAxis": {"type": "category",
                                          "data": ["", "", "", "", "", "", "", ""],
                                          "axisLabel": {"fontSize": 9, "color": "#888888", "interval": 0},
                                          "axisTick": {"show": True, "alignWithLabel": True, "lineStyle": {"color": "#888888"}},
                                          "axisLine": {"lineStyle": {"color": "#888888"}},
                                          "splitLine": {"show": False}},
                                "yAxis": {"type": "value",
                                          "min": 0, "axisLabel": {"formatter": "{value}", "color": "#888888"},
                                          "axisLine": {"show": True, "lineStyle": {"color": "#888888"}},
                                          "axisTick": {"show": True, "lineStyle": {"color": "#888888"}}},
                                "series": [
                                    {"name": "Short (6 blk)", "type": "line", "smooth": False,
                                     "showSymbol": True, "symbolSize": 6, "data": [None]*8,
                                     "lineStyle": {"color": "#f59e0b"}, "itemStyle": {"color": "#f59e0b"}},
                                    {"name": "Baseline (72 blk)", "type": "line", "smooth": False,
                                     "showSymbol": True, "symbolSize": 6, "data": [None]*8,
                                     "lineStyle": {"color": "#e879f9"},
                                     "itemStyle": {"color": "#e879f9"}},
                                ],
                            }).classes("w-full").style("height: 160px")
                            oracle_caption_right = ui.label("--").classes("text-xs font-mono")
                        _oracle_ui["chart_right"] = oracle_chart_right
                        _oracle_ui["caption_right"] = oracle_caption_right
                        _oracle_ui["chain_right"] = _chain_right

                    ui.separator().classes("my-1")

                    # Ratio + countdown row
                    with ui.row().classes("w-full items-center justify-between"):
                        oracle_ratio_lbl = ui.html("", sanitize=False).classes("text-sm font-mono")
                        oracle_countdown_lbl = ui.label("waiting for data...").classes("text-xs").style("color: #888")
                    _oracle_ui["ratio_lbl"] = oracle_ratio_lbl
                    _oracle_ui["countdown_lbl"] = oracle_countdown_lbl

                # Set initial visibility        
                oracle_card.visible = _show_oracle and not _show_manual

                _oracle_charts = [_oracle_ui.get("chart_left"), _oracle_ui.get("chart_right")]
            else:
                oracle_card = None  # no oracle panel when chain config is invalid

            # ---- MANUAL PANEL (built whenever both pools are active, regardless of chain config) ----
            if _slider_usable:
                with ui.card().classes("flex-1 min-w-[280px] max-w-[480px] slider-card-height") as manual_card:

                    with ui.row().classes("w-full items-center justify-between"):
                        with ui.row().classes("items-center gap-1"):
                            ui.icon("tune", size="sm").style("color: #6E93D6")
                            ui.label("Manual Assignment").classes("text-base font-semibold").style("color: #6E93D6")
                        # Navigation button: [<-] back to Oracle (if available) or Slider
                        btn_switch_from_manual = ui.button(icon="arrow_back").props("dense outline size=sm round")

                    ui.separator().classes("my-2")

                    ui.label("Each miner is manually assigned to a pool.").classes("text-sm")
                    ui.html(
                        '<span style="opacity:0.7; font-size:0.82rem;">'
                        'Go to the <b>Stats</b> tab and use the <b>Pool</b> column dropdowns '
                        'in the Fleet table to assign each miner to Pool A or Pool B. '
                        'Changes take effect immediately without restarting miners.'
                        '</span>',
                        sanitize=False
                    ).classes("w-full")

                    ui.separator().classes("my-2")

                    ui.html(
                        '<span style="opacity:0.55; font-size:0.78rem;">'
                        'The Scheduler Ratio gauge is inactive in Manual mode. '
                        'Assignments persist across restarts. '
                        'Active workers may take up to 30 seconds to stabilize on their assigned pools.'
                        '</span>',
                        sanitize=False
                    ).classes("w-full")

                # Set initial visibility
                manual_card.visible = _show_manual
            else:
                manual_card = None  # no manual panel when chain config is invalid

            # ---- SWITCH BUTTON HANDLERS ----
            def _all_panels_hide():
                """Hide all three mode panels."""
                slider_card.visible = False
                if oracle_card is not None:
                    oracle_card.visible = False
                if manual_card is not None:
                    manual_card.visible = False

            def _do_switch_to_oracle():
                """Switch to Oracle mode (from Slider or Manual)."""
                _mode["oracle_active"] = True
                _mode["manual_active"] = False
                write_oracle_mode(True)
                delete_manual_mode()
                _all_panels_hide()
                if oracle_card is not None:
                    oracle_card.visible = True

                # Immediately write the oracle's current weights to weights_override.json
                # so the scheduler starts converging right away instead of waiting up to
                # 10 minutes for the next oracle poll cycle.
                try:
                    raw = http_get_text(METRICS_URL)
                    wA = prom_value(raw, "dpmp_oracle_weight", {"pool": "A"})
                    wB = prom_value(raw, "dpmp_oracle_weight", {"pool": "B"})
                    if wA is not None and wB is not None and (int(wA) + int(wB)) > 0:
                        write_weight_override(int(wA), int(wB))
                except Exception:
                    pass  # oracle data may not be available yet; next poll will handle it

                ui.notify("Switched to Oracle mode", type="info")
                update_stats()  # immediately update stats to reflect oracle weights and mode change

            def _do_switch_to_slider():
                """Switch to Slider mode (from Oracle or Manual)."""
                _mode["oracle_active"] = False
                _mode["manual_active"] = False
                write_oracle_mode(False)
                delete_manual_mode()
                _all_panels_hide()
                slider_card.visible = True
                # Write the current slider position to weights_override.json
                # so DPMP immediately picks up the slider's weights
                if weight_slider_ref is not None:
                    val = int(weight_slider_ref.value)
                    write_weight_override(val, 100 - val)
                ui.notify("Switched to Slider mode", type="info")
                update_stats()  # immediately update stats to reflect slider weights and mode change

            def _do_switch_to_manual():
                """Switch to Manual mode (from Slider or Oracle)."""
                _mode["oracle_active"] = False
                _mode["manual_active"] = True
                write_oracle_mode(False)
                write_manual_mode(True)
                _all_panels_hide()
                if manual_card is not None:
                    manual_card.visible = True
                # Snapshot miners that need to move to their assigned pool.
                # Any miner whose current pool doesn't match assignment gets
                # highlighted orange until the reconnect completes.
                _manual_pending.clear()
                _cur_assignments = read_pinned_assignments_gui()
                _fm = read_fleet_metrics()

                for _wname, _mdata in _fm.get("miners", {}).items():
                    _cur_pool = _mdata.get("pool", "A")
                    _assigned = _cur_assignments.get(_wname, "A")
                    if _cur_pool != _assigned:
                        _manual_pending[_wname] = _mdata.get("time_on_pool_s", 0.0)

                ui.notify("Switched to Manual mode", type="info")
                update_stats()

            def _do_switch_from_manual():
                """Back button on Manual panel: go to Oracle if available, else Slider."""
                if _chain_valid and oracle_card is not None:
                    _do_switch_to_oracle()
                else:
                    _do_switch_to_slider()

            if _chain_valid:
                btn_switch_to_oracle.on_click(_do_switch_to_oracle)
                btn_switch_to_slider.on_click(_do_switch_to_slider)
                btn_switch_to_manual.on_click(_do_switch_to_manual)
                btn_switch_from_manual.on_click(_do_switch_from_manual)
            else:
                btn_switch_to_manual_from_slider.on_click(_do_switch_to_manual)
                btn_switch_from_manual.on_click(_do_switch_from_manual)


            # ---- ORACLE CHART STATE + UPDATE TIMER (always runs when chain valid) ----
            _CHART_MAX_POINTS = 8
            _oracle_poll_interval = ab_cfg["oracle_poll_seconds"]

            # Helper: convert UTC epoch to user's local time string.
            # The server (Docker) may be in UTC, so we use a browser-detected offset
            # stored in _tz_offset (dict so closures can mutate it).
            # Default offset is 0 (UTC) until the browser reports its real offset.
            _tz_offset = {"seconds": 0}  # set by _init_tz_offset() after page load

            def _utc_epoch_to_local_hhmm(epoch_s: float = None) -> str:
                """Convert a UTC epoch timestamp to user-local HH:MM string."""
                if epoch_s is None:
                    epoch_s = time.time()
                adjusted = epoch_s + _tz_offset["seconds"]
                return time.strftime("%H:%M", time.gmtime(adjusted))

            def _utc_epoch_to_local_hhmmss(epoch_s: float = None) -> str:
                """Convert a UTC epoch timestamp to user-local HH:MM:SS string."""
                if epoch_s is None:
                    epoch_s = time.time()
                adjusted = epoch_s + _tz_offset["seconds"]
                return time.strftime("%H:%M:%S", time.gmtime(adjusted))

            if _chain_valid:
                # Try to restore chart history from disk (survives browser refresh)
                _restored_history = load_oracle_chart_history(_oracle_poll_interval)

                _oracle_state = {
                    "has_data": len(_restored_history) > 0,
                    "last_data_age": None,       # previous data_age to detect new polls
                    "last_hashrates": None,      # (ehs_short_l, ehs_long_l, ehs_short_r, ehs_long_r) to detect value changes
                    # Chart history: list of dicts, max 8 entries
                    # Each entry: {"time_label": "HH:MM", "left_short": float, "left_long": float,
                    #              "right_short": float, "right_long": float}
                    "chart_history": _restored_history,
                }

                # Seed detection state IMMEDIATELY (before any timers fire) so the
                # periodic poll timer doesn't see None and add a duplicate point
                # on the first tick after a browser refresh.
                if _restored_history:
                    _seed_pt = _restored_history[-1]
                    _oracle_state["last_hashrates"] = (
                        _seed_pt["left_short"], _seed_pt["left_long"],
                        _seed_pt["right_short"], _seed_pt["right_long"],
                    )
                    try:
                        _seed_raw = http_get_text(METRICS_URL)
                        _seed_age = _prom_gauge_value(_seed_raw, "dpmp_oracle_data_age_seconds")
                        if _seed_age is not None:
                            _oracle_state["last_data_age"] = _seed_age
                    except Exception:
                        pass

                # Chart history restoration is deferred to _init_tz_offset()
                # so that projected future timestamps use the browser's local
                # timezone instead of UTC (which is the default before the
                # browser reports its offset).

                def _restore_chart_history():
                    """Populate charts from restored history with correct TZ."""
                    _restored = _oracle_state.get("chart_history", [])
                    if not _restored:
                        return
                    n_real = len(_restored)

                    # Regenerate ALL labels from epoch_s using current TZ offset.
                    # This avoids any mismatch between stored time_label strings
                    # (which may have been created with a different TZ) and
                    # projected future timestamps.
                    labels = []
                    for h in _restored:
                        ep = h.get("epoch_s")
                        if ep:
                            labels.append(_utc_epoch_to_local_hhmm(ep))
                        else:
                            labels.append(h.get("time_label", "??:??"))

                    if n_real < _CHART_MAX_POINTS:
                        last_h = _restored[-1]
                        last_epoch = last_h.get("epoch_s", time.time())
                        poll_s = max(60, _oracle_poll_interval)
                        for i in range(1, _CHART_MAX_POINTS - n_real + 1):
                            proj_epoch = last_epoch + (i * poll_s)
                            labels.append(_utc_epoch_to_local_hhmm(proj_epoch))

                    left_short_data = [h["left_short"] for h in _restored] + [None] * (_CHART_MAX_POINTS - n_real)
                    left_long_data = [h["left_long"] for h in _restored] + [None] * (_CHART_MAX_POINTS - n_real)
                    right_short_data = [h["right_short"] for h in _restored] + [None] * (_CHART_MAX_POINTS - n_real)
                    right_long_data = [h["right_long"] for h in _restored] + [None] * (_CHART_MAX_POINTS - n_real)

                    ch_l = _oracle_ui["chart_left"]
                    ch_l.options["xAxis"]["data"] = labels
                    ch_l.options["series"][0]["data"] = left_short_data
                    ch_l.options["series"][1]["data"] = left_long_data
                    ch_l.update()

                    ch_r = _oracle_ui["chart_right"]
                    ch_r.options["xAxis"]["data"] = labels
                    ch_r.options["series"][0]["data"] = right_short_data
                    ch_r.options["series"][1]["data"] = right_long_data
                    ch_r.update()

                    # Restore "Last updated" label from the most recent point
                    # Use the saved epoch_s if available, otherwise show the time_label
                    _last_pt = _restored[-1]
                    if "epoch_s" in _last_pt:
                        _oracle_ui["countdown_lbl"].text = f"Last updated: {_utc_epoch_to_local_hhmmss(_last_pt['epoch_s'])}"
                    else:
                        _oracle_ui["countdown_lbl"].text = f"Last updated: {_last_pt['time_label']}"

                def _oracle_metric_from_raw(raw_text, name, labels_dict):
                    """Extract oracle metric with arbitrary labels from raw Prometheus text."""
                    for line in raw_text.splitlines():
                        parsed = parse_prom_line(line)
                        if not parsed:
                            continue
                        n, lbls, v = parsed
                        if n != name:
                            continue
                        match = True
                        for k, vv in labels_dict.items():
                            if lbls.get(k) != vv:
                                match = False
                                break
                        if match:
                            return v
                    return None

                def _update_oracle_panel():
                    """Called every 2 seconds to refresh oracle panel from Prometheus metrics."""
                    try:
                        raw = http_get_text(METRICS_URL)
                        if not raw or not raw.strip():
                            _oracle_ui["health_dot"].style("color: red")
                            _oracle_ui["health_lbl"].text = "offline"
                            _oracle_ui["ratio_lbl"].content = (
                                '<span style="color:#22d3ee">Pool A: 50%</span>'
                                ' <span style="color:#555">/</span> '
                                '<span style="color:#f59e0b">Pool B: 50%</span>'
                                ' <span style="color:#888">(fallback)</span>'
                            )
                            _oracle_ui["countdown_lbl"].text = "waiting for data..."
                            return

                        status = _prom_gauge_value(raw, "dpmp_oracle_status")
                        data_age = _prom_gauge_value(raw, "dpmp_oracle_data_age_seconds")
                        is_healthy = (status is not None and status >= 0.5)

                        # Distinguish three states:
                        # 1. Healthy (oracle has polled successfully)
                        # 2. Warming up (DPMP running but oracle hasn't polled yet -- all gauges zero)
                        # 3. Offline (oracle polled but returned error)
                        if is_healthy:
                            _oracle_ui["health_dot"].style("color: limegreen")
                            _oracle_ui["health_lbl"].text = "connected"
                            _oracle_state["has_data"] = True
                        elif status is not None and status == 0.0 and not _oracle_state["has_data"]:
                            # Gauges exist but are zero = oracle hasn't polled yet (60s startup delay)
                            _oracle_ui["health_dot"].style("color: orange")
                            _oracle_ui["health_lbl"].text = "warming up..."
                        else:
                            _oracle_ui["health_dot"].style("color: red")
                            _oracle_ui["health_lbl"].text = "offline"

                        chain_l = _oracle_ui["chain_left"]
                        chain_r = _oracle_ui["chain_right"]

                        hr_short_l = _oracle_metric_from_raw(raw, "dpmp_oracle_hashrate",
                                                              {"chain": chain_l, "window": "short"})
                        hr_long_l = _oracle_metric_from_raw(raw, "dpmp_oracle_hashrate",
                                                             {"chain": chain_l, "window": "long"})
                        hr_short_r = _oracle_metric_from_raw(raw, "dpmp_oracle_hashrate",
                                                              {"chain": chain_r, "window": "short"})
                        hr_long_r = _oracle_metric_from_raw(raw, "dpmp_oracle_hashrate",
                                                             {"chain": chain_r, "window": "long"})

                        def _to_ehs(v):
                            return round(v / 1e18, 2) if v is not None and v > 0 else 0.0

                        ehs_short_l = _to_ehs(hr_short_l)
                        ehs_long_l = _to_ehs(hr_long_l)
                        ehs_short_r = _to_ehs(hr_short_r)
                        ehs_long_r = _to_ehs(hr_long_r)

                        _oracle_ui["caption_left"].text = f"{ehs_short_l:.2f} EH/s / {ehs_long_l:.2f} EH/s (avg)"
                        _oracle_ui["caption_right"].text = f"{ehs_short_r:.2f} EH/s / {ehs_long_r:.2f} EH/s (avg)"

                        # --- Chart update: only add a point when oracle actually polls ---
                        # Detect new poll by EITHER:
                        #   a) data_age metric changed (Prometheus gauge updated by oracle), OR
                        #   b) any of the 4 hashrate values changed
                        # This dual approach should work even if one detection method has quirks.
                        is_new_poll = False
                        if is_healthy:
                            current_hashrates = (ehs_short_l, ehs_long_l, ehs_short_r, ehs_long_r)
                            any_nonzero = any(v > 0 for v in current_hashrates)

                            # Method A: data_age changed
                            age_changed = False
                            if data_age is not None:
                                prev_age = _oracle_state["last_data_age"]
                                if prev_age is None or abs(data_age - prev_age) > 1.0:
                                    age_changed = True

                            # Method B: hashrate values changed
                            values_changed = False
                            prev_hr = _oracle_state["last_hashrates"]
                            if prev_hr is None:
                                values_changed = any_nonzero  # first reading with data
                            elif current_hashrates != prev_hr:
                                values_changed = True

                            if (age_changed or values_changed) and any_nonzero:
                                is_new_poll = True
                                _oracle_state["last_data_age"] = data_age
                                _oracle_state["last_hashrates"] = current_hashrates
                                # Stamp "Last Updated" in local time
                                updated_ts = _utc_epoch_to_local_hhmmss()
                                _oracle_ui["countdown_lbl"].text = f"Last updated: {updated_ts}"

                        if is_new_poll:
                            # Add new data point to history (use local time for labels)
                            now_epoch = time.time()
                            now_label = _utc_epoch_to_local_hhmm(now_epoch)
                            history = _oracle_state["chart_history"]
                            history.append({
                                "time_label": now_label,
                                "epoch_s": now_epoch,
                                "left_short": ehs_short_l, "left_long": ehs_long_l,
                                "right_short": ehs_short_r, "right_long": ehs_long_r,
                            })

                            # Keep max 8 points
                            if len(history) > _CHART_MAX_POINTS:
                                _oracle_state["chart_history"] = history[-_CHART_MAX_POINTS:]
                                history = _oracle_state["chart_history"]

                            # Persist to disk so browser refresh can restore charts
                            save_oracle_chart_history(history, _oracle_poll_interval)

                            # Build x-axis labels: regenerate from epoch_s for
                            # consistent TZ handling across all labels.
                            n_real = len(history)
                            labels = []
                            for h in history:
                                ep = h.get("epoch_s")
                                if ep:
                                    labels.append(_utc_epoch_to_local_hhmm(ep))
                                else:
                                    labels.append(h.get("time_label", "??:??"))
                            # Fill remaining slots with projected timestamps (local time)
                            if n_real < _CHART_MAX_POINTS:
                                last_pt_h = history[-1]
                                last_epoch = last_pt_h.get("epoch_s", time.time())
                                poll_s = max(60, _oracle_poll_interval)
                                for i in range(1, _CHART_MAX_POINTS - n_real + 1):
                                    proj_epoch = last_epoch + (i * poll_s)
                                    labels.append(_utc_epoch_to_local_hhmm(proj_epoch))

                            # Build series data arrays (None for empty slots)
                            left_short_data = [h["left_short"] for h in history] + [None] * (_CHART_MAX_POINTS - n_real)
                            left_long_data = [h["left_long"] for h in history] + [None] * (_CHART_MAX_POINTS - n_real)
                            right_short_data = [h["right_short"] for h in history] + [None] * (_CHART_MAX_POINTS - n_real)
                            right_long_data = [h["right_long"] for h in history] + [None] * (_CHART_MAX_POINTS - n_real)

                            # Update left chart
                            ch_l = _oracle_ui["chart_left"]
                            ch_l.options["xAxis"]["data"] = labels
                            ch_l.options["series"][0]["data"] = left_short_data
                            ch_l.options["series"][1]["data"] = left_long_data
                            ch_l.update()

                            # Update right chart
                            ch_r = _oracle_ui["chart_right"]
                            ch_r.options["xAxis"]["data"] = labels
                            ch_r.options["series"][0]["data"] = right_short_data
                            ch_r.options["series"][1]["data"] = right_long_data
                            ch_r.update()

                        wA = _oracle_metric_from_raw(raw, "dpmp_oracle_weight", {"pool": "A"})
                        wB = _oracle_metric_from_raw(raw, "dpmp_oracle_weight", {"pool": "B"})

                        if wA is not None and wB is not None:
                            _oracle_ui["ratio_lbl"].content = (
                                f'<span style="color:#22d3ee">Pool A: {int(wA)}%</span>'
                                f' <span style="color:#555">/</span> '
                                f'<span style="color:#f59e0b">Pool B: {int(wB)}%</span>'
                            )
                        elif not is_healthy:
                            _oracle_ui["ratio_lbl"].content = (
                                '<span style="color:#22d3ee">Pool A: 50%</span>'
                                ' <span style="color:#555">/</span> '
                                '<span style="color:#f59e0b">Pool B: 50%</span>'
                                ' <span style="color:#888">(fallback)</span>'
                            )

                        if not is_healthy and not _oracle_state["has_data"]:
                            _oracle_ui["countdown_lbl"].text = "waiting for data..."

                    except Exception:
                        pass

                ui.timer(2.0, _update_oracle_panel)

        def _update_weight_display():
            """Update the percentage label and status badge based on current slider value."""
            if weight_slider_ref is None:
                return
            val = int(weight_slider_ref.value)
            bval = 100 - val
            lbl_weight_pct.content = (
                f'<span style="color:#22d3ee">Pool A: {val}%</span>'
                f' <span style="color:#555">/</span> '
                f'<span style="color:#f59e0b">Pool B: {bval}%</span>'
            )
            if val == _cfg["slider_default"]:
                lbl_weight_status.content = (
                    f'<span style="color:#888">Using config defaults ({cfg_wA}/{cfg_wB})</span>'
                )
            else:
                lbl_weight_status.content = (
                    f'<span style="color:#f59e0b">&#9650; Live override active ... DPMP is using these weights</span>'
                )

        def _on_slider_change(e):
            """Called when the slider value changes ... write override file immediately."""
            val = int(e.value)
            bval = 100 - val
            _update_weight_display()
            # Always write the override file, even if at config defaults.
            # Only explicit Reset or Restart DPMP should delete the override.
            # This prevents a second browser session from accidentally nuking
            # an active override when its slider initializes.
            write_weight_override(val, bval)

        def _reset_weights():
            """Reset slider to config defaults and remove override file."""
            if weight_slider_ref is None:
                return
            weight_slider_ref.value = cfg_slider_default
            delete_weight_override()
            _update_weight_display()
            ui.notify("Weights reset to config defaults", type="info")

        if weight_slider_ref is not None:
            weight_slider_ref.on_value_change(_on_slider_change)
            btn_weight_reset.on_click(_reset_weights)
            _update_weight_display()

        def _fmt_short(v: float) -> str:
            """Format a number with K/M/G/T suffix for compact display."""
            if v >= 1e12:
                return f"{v/1e12:.2f}T"
            if v >= 1e9:
                return f"{v/1e9:.2f}G"
            if v >= 1e6:
                return f"{v/1e6:.2f}M"
            if v >= 1e3:
                return f"{v/1e3:.2f}K"
            return f"{int(v)}"

        def do_restart():
            # Delete weight override so DPMP starts fresh with config defaults
            delete_weight_override()

            # Delete oracle_mode.json so DPMP falls back to config auto_balance
            delete_oracle_mode()
            delete_manual_mode()

            # Clear chart history so charts start fresh after DPMP restart
            # (also cleared on GUI startup, but clear here too for immediate effect)
            clear_oracle_chart_history()
            if _chain_valid:
                _oracle_state["chart_history"] = []
                _oracle_state["has_data"] = False
                _oracle_state["last_data_age"] = None
                _oracle_state["last_hashrates"] = None

            # Reset slider back to current config defaults (recompute in case config changed)
            if weight_slider_ref is not None:
                _cfg["wA"], _cfg["wB"] = get_config_weights()
                cfg_total = _cfg["wA"] + _cfg["wB"]
                if cfg_total > 0:
                    _cfg["slider_default"] = round((_cfg["wA"] / cfg_total) * 100 / 5) * 5
                    _cfg["slider_default"] = max(5, min(95, _cfg["slider_default"]))
                else:
                    _cfg["slider_default"] = 50
                weight_slider_ref.value = _cfg["slider_default"]
                _update_weight_display()

            # Reset panel visibility to config default
            _new_ab = get_auto_balance_config()
            _new_chain_valid = sorted([_new_ab["poolA_chain"], _new_ab["poolB_chain"]]) == ["BCH", "BTC"]
            _new_show_oracle = _new_ab["auto_balance"] and _new_chain_valid
            _mode["oracle_active"] = _new_show_oracle
            _mode["manual_active"] = False
            delete_manual_mode()
            slider_card.visible = not _new_show_oracle
            if oracle_card is not None:
                oracle_card.visible = _new_show_oracle
            if manual_card is not None:
                manual_card.visible = False


            ok, msg = restart_dpmpv2()
            if ok:
                ui.notify("DPMP restarted successfully", type="positive")
            else:
                ui.notify(f"Restart failed: {msg}", type="negative")

        btn_restart.on_click(do_restart)

        ui.separator()

        with ui.row().classes("gap-4 items-center w-full"):
            lbl_status = ui.label("Status").classes("text-lg font-semibold").style('color: blue;')
            lbl_dpmp = ui.html("<b>DPMP</b>: checking...", sanitize=False).classes("text-sm")
            lbl_pool = ui.html("Active pool: ...", sanitize=False).classes("text-sm").tooltip("Which pool is currently active")
            lbl_miner = ui.html("<b>Miner(s) connected</b>: ...", sanitize=False).classes("text-sm").tooltip("Whether any miners are currently connected downstream")
            lbl_spin = ui.spinner('rings', size='lg', color='green')

        _vc = "font-size:0.8rem; font-weight:600;"
        _hc = "opacity:0.5; text-transform:uppercase; letter-spacing:0.05em;"

        with ui.row().classes("w-full flex-wrap gap-6 items-stretch"):

            with ui.card().classes("min-w-[300px]"):
                with ui.row().classes("gap-x-6 gap-y-1"):
                    with ui.column().classes("gap-0"):
                        ui.label("ACCEPTED").classes("text-xs").style(_hc)
                        lbl_acc = ui.html('A -- / B --', sanitize=False).style(_vc)
                    with ui.column().classes("gap-0"):
                        ui.label("REJECTED").classes("text-xs").style(_hc)
                        lbl_rej = ui.html('A -- / B --', sanitize=False).style(_vc)
                    with ui.column().classes("gap-0"):
                        ui.label("JOBS").classes("text-xs").style(_hc)
                        lbl_jobs = ui.html('A -- / B --', sanitize=False).style(_vc)
                with ui.row().classes("gap-x-6 gap-y-1"):
                    with ui.column().classes("gap-0"):
                        ui.label("SUM DIFFICULTY").classes("text-xs").style(_hc)
                        lbl_dif = ui.html('A -- / B --', sanitize=False).style(_vc)
                    with ui.column().classes("gap-0"):
                        ui.label("DIFF RATIO (ALL-TIME)").classes("text-xs").style(_hc)
                        lbl_rat = ui.html('A --% / B --%', sanitize=False).style(_vc)

            with ui.card().classes("flex-1 min-w-[320px] max-w-[480px]"):
                with ui.row().classes("w-full justify-center gap-8"):
                    with ui.column().classes("items-center gap-0"):
                        ui.label("SCHEDULER RATIO").classes("text-xs").style(_hc)
                        lbl_gauge_sr = ui.html(_build_gauge_svg(0.5), sanitize=False)
                        lbl_sched_rat = ui.html('<span style="color:#22d3ee">A --%</span> / <span style="color:#f59e0b">B --%</span>', sanitize=False).style("font-size:0.8rem; font-weight:600;")
                    with ui.column().classes("items-center gap-0"):
                        ui.label("RECENT DIFF").classes("text-xs").style(_hc)
                        lbl_gauge_rd = ui.html(_build_gauge_svg(0.5), sanitize=False)
                        lbl_recent_rat = ui.html('<span style="color:#22d3ee">A --%</span> / <span style="color:#f59e0b">B --%</span>', sanitize=False).style("font-size:0.8rem; font-weight:600;")

        ui.separator()

        DARK_KEY = 'dpmp_dark_mode'

        dark = ui.dark_mode()
        sw_dark = ui.switch('Dark Mode').props('id=dpmp_dark_switch')

        def _to_bool(v) -> bool:
            if isinstance(v, bool):
                return v
            if v is None:
                return False
            s = str(v).strip().lower()
            return s in ('1', 'true', 'yes', 'y', 'on')

        # persist + apply immediately
        def _persist_dark(v: bool) -> None:
            v = bool(v)
            dark.value = v
            ui.run_javascript(
                "try { localStorage.setItem(%r, %r); } catch(e) {}" % (DARK_KEY, '1' if v else '0')
            )
            # Sync oracle chart text colors with dark mode
            _sync_chart_dark_mode(v)

        def _sync_chart_dark_mode(is_dark: bool) -> None:
            """Set explicit text colors on oracle charts for dark/light mode."""
            text_color = "#ffffff" if is_dark else "#888888"
            for ch in _oracle_charts:
                if ch is None:
                    continue
                try:
                    ch.options["legend"]["textStyle"]["color"] = text_color
                    # X-axis: labels, tick marks, axis line
                    ch.options["xAxis"]["axisLabel"]["color"] = text_color
                    ch.options["xAxis"]["axisTick"]["lineStyle"]["color"] = text_color
                    ch.options["xAxis"]["axisLine"]["lineStyle"]["color"] = text_color
                    # Y-axis: labels, tick marks, axis line
                    ch.options["yAxis"]["axisLabel"]["color"] = text_color
                    ch.options["yAxis"]["axisTick"]["lineStyle"]["color"] = text_color
                    ch.options["yAxis"]["axisLine"]["lineStyle"]["color"] = text_color
                    ch.update()
                except Exception:
                    pass

        sw_dark.on_value_change(lambda e: _persist_dark(_to_bool(e.value)))

        # AFTER connect: load localStorage and set BOTH theme + switch value server-side
        async def _init_dark_from_storage() -> None:
            js = """
        (() => {
        try {
            const v = localStorage.getItem('dpmp_dark_mode');
            return (v === '1' || v === 'true') ? 1 : 0;
        } catch (e) { return 0; }
        })()
        """
            v = await ui.run_javascript(js)  # v will be 0/1
            is_dark = bool(int(v))
            dark.value = is_dark
            sw_dark.value = is_dark
            # Sync oracle chart text colors with initial dark mode state
            _sync_chart_dark_mode(is_dark)


        ui.timer(0.0, _init_dark_from_storage, once=True)

        # Detect browser timezone offset so oracle times display in user's local time.
        # JavaScript's getTimezoneOffset() returns minutes AHEAD of UTC (negative for east),
        # e.g., EST (UTC-5) returns 300. We invert to get seconds to ADD to UTC epoch.
        async def _init_tz_offset() -> None:
            try:
                offset_min = await ui.run_javascript("new Date().getTimezoneOffset()")
                _tz_offset["seconds"] = -int(offset_min) * 60  # e.g., 300 -> -18000 -> add -300*60
            except Exception:
                _tz_offset["seconds"] = 0  # fall back to UTC
            # Now that we have the correct TZ offset, restore chart history
            # with properly localized future timestamps.
            # _restore_chart_history only exists when oracle/auto_balance is active.
            if '_restore_chart_history' in dir() or True:
                try:
                    _restore_chart_history()
                except NameError:
                    pass  # oracle not active, no chart to restore
                except Exception:
                    pass  # chart restore is best-effort

        ui.timer(0.0, _init_tz_offset, once=True)

        # Rolling window for "Recent Ratio" ... stores (timestamp, difA, difB) snapshots.
        # We keep ~2 minutes of history (at 2s poll interval, that's ~60 samples).
        _recent_dif_history: list[tuple[float, float, float]] = []
        _RECENT_WINDOW_S = 300.0  # 5-minute rolling window

        # periodic status update
        def update_home_status() -> None:

            # 1) dpmpv2 systemd state (bare-metal). In Docker this will be unavailable.
            active = False
            dc = 0
            try:
                active = systemd_is_active("dpmpv2")
            except Exception:
                active = False

            # 2) metrics-derived status (regex, minimal)
            try:
                raw = http_get_text(METRICS_URL)

                # If we can successfully fetch metrics, DPMP is effectively "running"
                # even if systemd isn't available (e.g., in Docker).
                if raw and raw.strip():
                    active = True

                a = _prom_gauge_value(raw, "dpmp_active_pool", pool="A")
                b = _prom_gauge_value(raw, "dpmp_active_pool", pool="B")
                if (a or 0.0) >= 0.5:
                    lbl_pool.content = "<b>Active pool</b>: A"
                elif (b or 0.0) >= 0.5:
                    lbl_pool.content = "<b>Active pool</b>: B"
                else:
                    lbl_pool.content = "<b>Active pool</b>: unknown"

                dc = _prom_gauge_value(raw, "dpmp_downstream_connections")
                if dc is None:
                    lbl_miner.content = "<b>Miner(s) connected</b>: unknown"
                else:
                    lbl_miner.content = f"<b>Miner(s) connected</b>: {'yes' if dc >= 1 else 'no'} (downstream={int(dc)})"

                accA = _prom_gauge_value(raw, "dpmp_shares_accepted_total", pool="A") or 0.0
                accB = _prom_gauge_value(raw, "dpmp_shares_accepted_total", pool="B") or 0.0
                rejA = _prom_gauge_value(raw, "dpmp_shares_rejected_total", pool="A") or 0.0
                rejB = _prom_gauge_value(raw, "dpmp_shares_rejected_total", pool="B") or 0.0
                jobA = _prom_gauge_value(raw, "dpmp_jobs_forwarded_total", pool="A") or 0.0
                jobB = _prom_gauge_value(raw, "dpmp_jobs_forwarded_total", pool="B") or 0.0

                difA = _prom_gauge_value(raw, "dpmp_accepted_difficulty_sum_total", pool="A") or 0.0
                difB = _prom_gauge_value(raw, "dpmp_accepted_difficulty_sum_total", pool="B") or 0.0

                total_dif = difA + difB
                pctA = 100*difA/(total_dif or 1)
                pctB = 100*difB/(total_dif or 1)

                rejpA = 100*rejA/accA if accA > 0 else 0.0
                rejpB = 100*rejB/accB if accB > 0 else 0.0

                lbl_acc.content = '<span style="color:#22d3ee">A %s</span> <span style="opacity:0.3">/</span> <span style="color:#f59e0b">B %s</span>' % (f'{int(accA):,}', f'{int(accB):,}')
                lbl_rej.content = '<span style="color:#22d3ee">A %d (%.2f%%)</span> <span style="opacity:0.3">/</span> <span style="color:#f59e0b">B %d (%.2f%%)</span>' % (int(rejA), rejpA, int(rejB), rejpB)
                lbl_jobs.content = '<span style="color:#22d3ee">A %s</span> <span style="opacity:0.3">/</span> <span style="color:#f59e0b">B %s</span>' % (f'{int(jobA):,}', f'{int(jobB):,}')
                lbl_dif.content = '<span style="color:#22d3ee">A %s</span> <span style="opacity:0.3">/</span> <span style="color:#f59e0b">B %s</span>' % (_fmt_short(difA), _fmt_short(difB))
                lbl_rat.content = '<span style="color:#22d3ee">A %.1f%%</span> <span style="opacity:0.3">/</span> <span style="color:#f59e0b">B %.1f%%</span>' % (pctA, pctB)

                # -- Gauge updates: Scheduler Ratio + Recent Diff --
                # Single-pool mode: pin both gauges to the active pool's extreme
                if not _slider_usable:
                    _pin = 1.0 if cfg_wA > 0 else 0.0
                    _ppA = _pin * 100
                    _ppB = (1 - _pin) * 100
                    lbl_gauge_sr.content = _build_gauge_svg(_pin)
                    lbl_sched_rat.content = '<span style="color:#22d3ee">A %.1f%%</span> <span style="opacity:0.3">/</span> <span style="color:#f59e0b">B %.1f%%</span>' % (_ppA, _ppB)
                    lbl_gauge_rd.content = _build_gauge_svg(_pin)
                    lbl_recent_rat.content = '<span style="color:#22d3ee">A %.1f%%</span> <span style="opacity:0.3">/</span> <span style="color:#f59e0b">B %.1f%%</span>' % (_ppA, _ppB)

                else:
                    # In Manual mode the scheduler does no dynamic switching,
                    # so the SR gauge is meaningless -- show it greyed out at 50/50.
                    if _mode.get("manual_active", False):
                        lbl_gauge_sr.content = _build_gauge_svg(0.5, greyed=True)
                        lbl_sched_rat.content = '<span style="opacity:0.35">Manual mode</span>'
                    else:
                        # Scheduler Ratio -- reads the averaged per-miner time-ratio
                        # directly from the Prometheus gauge.  This is instantaneous,
                        # stable, and reflects what the scheduler is actually doing.
                        _schedA = _prom_gauge_value(raw, "dpmp_scheduler_share", pool="A")
                        _schedB = _prom_gauge_value(raw, "dpmp_scheduler_share", pool="B")
                        if _schedA is not None and _schedB is not None:
                            _spctA = 100.0 * _schedA
                            _spctB = 100.0 * _schedB
                            lbl_gauge_sr.content = _build_gauge_svg(_schedA)
                            lbl_sched_rat.content = '<span style="color:#22d3ee">A %.1f%%</span> <span style="opacity:0.3">/</span> <span style="color:#f59e0b">B %.1f%%</span>' % (_spctA, _spctB)
                        else:
                            lbl_gauge_sr.content = _build_gauge_svg(0.5)
                            lbl_sched_rat.content = "waiting for data..."

                    # Rolling recent diff -- exponentially weighted difficulty.
                    # Recent data is weighted much more heavily than old data,
                    # so the display responds to ratio changes within 1-2 minutes
                    # while still smoothing out noise from switching phases.
                    # Half-life of ~45 seconds means data from 3 minutes ago
                    # contributes only ~6% as much as current data.
                    _rdifA = difA  # already read above from dpmp_accepted_difficulty_sum_total
                    _rdifB = difB
                    now_mono = time.monotonic()
                    _recent_dif_history.append((now_mono, _rdifA, _rdifB))

                    # Trim entries older than the window
                    cutoff = now_mono - _RECENT_WINDOW_S
                    while _recent_dif_history and _recent_dif_history[0][0] < cutoff:
                        _recent_dif_history.pop(0)

                    if len(_recent_dif_history) >= 2:
                        # Compute exponentially-weighted difficulty deltas.
                        # Each consecutive pair contributes (delta_A, delta_B),
                        # weighted by exp(-age / half_life * ln2).
                        _HL = 90.0  # half-life in seconds (covers ~1 full switching cycle)
                        _LN2 = 0.6931
                        _wsum_A = 0.0
                        _wsum_B = 0.0
                        _wsum_total = 0.0
                        for i in range(1, len(_recent_dif_history)):
                            _ts_prev, _a_prev, _b_prev = _recent_dif_history[i - 1]
                            _ts_curr, _a_curr, _b_curr = _recent_dif_history[i]
                            _da = _a_curr - _a_prev
                            _db = _b_curr - _b_prev
                            # Weight by midpoint age (average of the two timestamps)
                            _mid_age = now_mono - (_ts_prev + _ts_curr) / 2.0
                            _w = 2.0 ** (-_mid_age / _HL)
                            _wsum_A += _da * _w
                            _wsum_B += _db * _w
                            _wsum_total += (_da + _db) * _w

                        if _wsum_total > 0:
                            rpctA = 100.0 * _wsum_A / _wsum_total
                            rpctB = 100.0 * _wsum_B / _wsum_total
                            window_s = now_mono - _recent_dif_history[0][0]
                            lbl_gauge_rd.content = _build_gauge_svg(_wsum_A / _wsum_total)
                            lbl_recent_rat.content = '<span style="color:#22d3ee">A %.1f%%</span> <span style="opacity:0.3">/</span> <span style="color:#f59e0b">B %.1f%%</span> <span style="opacity:0.4;font-size:0.7rem;">(%ds)</span>' % (rpctA, rpctB, int(window_s))
                        else:
                            lbl_gauge_rd.content = _build_gauge_svg(0.5)
                            lbl_recent_rat.content = "no new shares yet..."
                    else:
                        lbl_gauge_rd.content = _build_gauge_svg(0.5)
                        lbl_recent_rat.content = "collecting data..."

            except Exception as e:
                lbl_pool.content = "<b>Active pool</b>: error"
                lbl_miner.content = "<b>Miner connected</b>: error"
                # optional but helpful:
                try:
                    ui.notify(f"Home status error: {e}", type="negative")
                except Exception:
                    pass

            # Final status display (works for both bare-metal and Docker)
            lbl_dpmp.content = f"<b>DPMP</b>: {'running' if active else 'stopped'}"
            lbl_status.style('color: green;' if active else 'color: red;')
            lbl_spin.visible = active and dc >= 1

        update_home_status()
        ui.timer(2.0, update_home_status)



    # ========================================================================
    # Stats Tab -- Fleet metrics + per-worker miner metrics + per-pool summary
    # ========================================================================
    with ui.tab_panel(t_stats):

        # tuck title 10px after icon in ui.expansion()
        ui.add_css("""
        .stats-expansion.q-expansion-item .q-item__section--avatar {
            min-width: 0 !important;
            padding-right: 10px !important;
        }
        """)

        # ---- Fleet Stats Card ----    
        fleet_expansion = ui.expansion("Fleet Stats", icon="groups", value=True).props("dense dense-toggle").classes("stats-expansion text-lg font-semibold w-full")
        with fleet_expansion:
        #with ui.expansion("Fleet Stats", icon="groups", value=True).props("dense dense-toggle").classes("stats-expansion text-lg font-semibold w-full"):
            with ui.card().classes("w-full").style("padding: 4px 8px 8px 8px"):
                ui.label("Real-time scheduler view. Click column headers to sort.").classes("text-xs opacity-60").style("padding-left: 6px")
                stats_fleet_html = ui.html("", sanitize=False).classes("w-full overflow-x-auto")
                lbl_fleet_footnote = ui.label("* A pinned worker will only operate on the assigned pool, which may impact convergence at extreme ratios.").classes("text-xs opacity-60").style("padding-left: 6px")

        # ---- Miner Stats Card ----
        with ui.expansion("Worker Stats", icon="memory", value=True).props("dense dense-toggle").classes("stats-expansion text-lg font-semibold w-full"):
            with ui.card().classes("w-full").style("padding: 4px 8px 8px 8px"):                
                ui.label("Workers not seen for 5 minutes are automatically removed. Click column headers to sort.").classes("text-xs opacity-60").style("padding-left: 6px")             
                stats_miner_html = ui.html("", sanitize=False).classes("w-full overflow-x-auto")
                ui.label("* Hashrate is best-estimate from work completed across both pools.").classes("text-xs opacity-60").style("padding-left: 6px")

        # ---- Pool Stats Card ----
        with ui.expansion("Pool Stats", icon="pool", value=True).props("dense dense-toggle").classes("stats-expansion w-full text-lg font-semibold"):
            with ui.card().classes("w-full").style("padding: 4px 8px 8px 8px"):
                ui.label("Click column headers to sort.").classes("text-xs opacity-60").style("padding-left: 6px")
                stats_pool_html = ui.html("", sanitize=False).classes("w-full overflow-x-auto")
                ui.label("* May include data for workers not currently attached.").classes("text-xs opacity-60").style("padding-left: 6px")

        # ---- Shared table styling (injected once) ----
        ui.add_head_html("""
        <style>
        .slider-card-height { min-height: 357px; }
        @media (max-width: 768px) { .slider-card-height { min-height: auto; } }
        .stats-tbl { border-collapse: collapse; font-size: 0.82rem; }
        .stats-tbl th { text-align: left; padding: 4px 8px; white-space: nowrap;
                        border-bottom: 2px solid rgba(110,147,214,0.4); color: #6E93D6;
                        font-weight: 600; cursor: pointer; user-select: none; }
        .stats-tbl th:hover { color: #93bbff; }
        .stats-tbl th .sa { font-size: 0.7em; margin-left: 2px; opacity: 0.3; }
        .stats-tbl th.sorted .sa { opacity: 1.0; }
        .stats-tbl td { padding: 4px 8px; white-space: nowrap; border-bottom: 1px solid rgba(255,255,255,0.07); }
        .stats-tbl tr:hover td { background: rgba(110,147,214,0.07); }
        .stats-tbl .num { text-align: right; font-variant-numeric: tabular-nums; }
        @media (max-width: 768px) {
            .stats-tbl { font-size: 0.72rem; }
            .stats-tbl th, .stats-tbl td { padding: 3px 4px; }
        }
        </style>
        """)

        # Sort state: stored as dict so the timer closure can read/write it.
        # key = sort column key, reverse = True for descending.
        _miner_sort = {"key": "name", "reverse": False}
        _pool_sort = {"key": "pool_name", "reverse": False}
        _fleet_sort = {"key": "worker_name", "reverse": False}
        _manual_pending = {}  # { worker_name: last_known_time_on_pool_s } -- orange highlight while reconnect pending

        # ---- Miner table column definitions ----
        # Each tuple: (key, header_label, css_class, format_fn)
        # key is used to extract the sort value from the worker data dict.
        _miner_cols = [
            ("name",     "Worker",  "",    None),
            ("hr_5m",    "5m HR*",   "num", lambda v: fmt_hashrate(v)),
            ("hr_60m",   "60m HR*",  "num", lambda v: fmt_hashrate(v)),
            ("hr_24h",   "24h HR*",  "num", lambda v: fmt_hashrate(v)),
            ("sps",      "Sh/s",    "num", lambda v: f"{v:.4f}"),
            ("diff",     "Diff",    "num", lambda v: fmt_diff(v)),
            ("shares",   "Shares",  "num", lambda v: f"{v:,}"),
            ("best",     "Best",    "num", lambda v: fmt_diff(v)),
            ("rejected", "Rej",     "num", lambda v: f"{v:,}"),
            ("rej_pct",  "Rej%",    "num", lambda v: f"{v:.1f}%"),
            ("ago",      "Seen",    "num", None),    # special: computed from last_seen
            ("uptime",   "Uptime",  "num", None),    # special: computed from connected_at
            ("toggle",   "",        "",    None),     # special: on/off toggle button
        ]

        # ---- Pool table column definitions ----
        _pool_cols = [
            ("pool_name", "Pool",      "",    None),
            ("slot",      "Slot",      "",    None),
            ("chain",     "Coin",      "",    None),
            ("en2_size",  "En2",       "num", lambda v: str(int(v)) if v else "--"),
            ("ratio",     "Ratio",     "num", lambda v: v if isinstance(v, str) else f"{v:.0f}%"),
            ("pool_hr",   "5m HR",     "num", lambda v: fmt_hashrate(v)),
            ("latency",   "Latency",   "num", None),
            ("accepted",  "Accepted*",  "num", lambda v: f"{int(v):,}"),
            ("rejected",  "Rejected*",  "num", lambda v: f"{int(v):,}"),
            ("rej_pct",   "Rej%",      "num", lambda v: f"{v:.1f}%"),
            ("jobs",      "Jobs",      "num", lambda v: f"{int(v):,}"),
            ("tdiff",     "TotalDiff", "num", lambda v: fmt_diff(v)),
        ]

        _fleet_cols = [
            ("worker_name",    "Worker",       "",    None),
            ("current_pool",   "Pool",         "",    None),
            ("pinned",         "Pinned*",       "",    None),
            ("time_on_pool_s", "Time on Pool", "num", None),  # special: formatted like _fmt_ago
            ("mode",           "Mode",         "",    lambda v: "Dynamic" if v == "time_slice" else "Static"),
            ("switch_count",   "Switches",     "num", lambda v: f"{int(v):,}"),
            ("contribution",   "Contribution", "num", lambda v: f"{v:.1%}"),
            ("health",         "Health",       "num", lambda v: f"{v:.2f}"),
        ]

        def _fmt_ago(seconds_ago: float) -> str:
            """Format seconds ago."""
            if seconds_ago < 0:
                return "--"
            if seconds_ago < 60:
                return f"{int(seconds_ago)}s"
            elif seconds_ago < 3600:
                return f"{int(seconds_ago / 60)}m {int(seconds_ago % 60)}s"
            else:
                return f"{int(seconds_ago / 3600)}h {int((seconds_ago % 3600) / 60)}m"

        # Click-to-sort via hidden NiceGUI elements.
        # Each table gets a hidden ui.input. When a column header is clicked,
        # JS uses NiceGUI's built-in getElement().emit() to send the column
        # key directly to the Python callback. This avoids native DOM events
        # (which don't propagate to Vue/Quasar) and custom ui.on() events
        # (which crashed NiceGUI).

        _sort_bridge_miner = ui.input("").style("display:none").props("dense")
        _sort_bridge_pool = ui.input("").style("display:none").props("dense")
        _sort_bridge_fleet = ui.input("").style("display:none").props("dense")

        # Store NiceGUI element IDs for JS access
        _bridge_miner_id = _sort_bridge_miner.id
        _bridge_pool_id = _sort_bridge_pool.id
        _bridge_fleet_id = _sort_bridge_fleet.id

        # Hidden bridge for miner on/off toggle clicks (same pattern as sort bridges)
        _toggle_bridge = ui.input("").style("display:none").props("dense")
        _bridge_toggle_id = _toggle_bridge.id

        # Hidden bridge for Manual mode pool assignment dropdown changes
        _assign_bridge = ui.input("").style("display:none").props("dense")
        _bridge_assign_id = _assign_bridge.id

        # In-memory set of paused worker names; seeded from miner_paused.json
        _paused_miners = set(read_paused_miners())

        def _on_toggle_click(e):
            """Toggle a miner's paused state when the on/off button is clicked."""
            worker = e.args if isinstance(e.args, str) else ""
            if not worker:
                return
            if worker in _paused_miners:
                _paused_miners.discard(worker)
            else:
                _paused_miners.add(worker)
            write_paused_miners(sorted(_paused_miners))
            update_stats()

        _toggle_bridge.on("toggle_miner", _on_toggle_click)

        def _on_assign_change(e):
            """Update a miner's pool assignment when the Manual mode dropdown changes."""
            data = e.args if isinstance(e.args, dict) else {}
            worker = data.get("worker", "")
            pool = data.get("pool", "A")
            if not worker or pool not in ("A", "B"):
                return
            # Read current assignments, update this miner, write back
            current = read_pinned_assignments_gui()
            if pool == "B":
                current[worker] = "B"
            else:
                current.pop(worker, None)
            write_pinned_assignments_gui(current)
            # Mark miner as pending -- highlight orange until reconnect completes.
            # Snapshot current time_on_pool_s so we can detect when it resets.
            _fm = read_fleet_metrics()
            _miners = _fm.get("miners", {})
            if worker in _miners:
                _manual_pending[worker] = _miners[worker].get("time_on_pool_s", 0.0)

            #ui.notify(f"DEBUG: {worker} pending={_manual_pending.get(worker, 'NOT SET')}", type="warning")
            update_stats()

        _assign_bridge.on("assign_pool", _on_assign_change)

        def _on_miner_header_click(e):
            val = e.args if isinstance(e.args, str) else (e.args or {}).get("key", "")
            if not val:
                return
            col_key = val
            if _miner_sort["key"] == col_key:
                _miner_sort["reverse"] = not _miner_sort["reverse"]
            else:
                _miner_sort["key"] = col_key
                _miner_sort["reverse"] = col_key != "name"
            update_stats()

        def _on_pool_header_click(e):
            val = e.args if isinstance(e.args, str) else (e.args or {}).get("key", "")
            if not val:
                return
            col_key = val
            if _pool_sort["key"] == col_key:
                _pool_sort["reverse"] = not _pool_sort["reverse"]
            else:
                _pool_sort["key"] = col_key
                _pool_sort["reverse"] = col_key not in ("pool_name", "slot", "chain")
            update_stats()

        _sort_bridge_miner.on("sort_click", _on_miner_header_click)
        _sort_bridge_pool.on("sort_click", _on_pool_header_click)

        def _on_fleet_header_click(e):
            val = e.args if isinstance(e.args, str) else (e.args or {}).get("key", "")
            if not val:
                return
            col_key = val
            if _fleet_sort["key"] == col_key:
                _fleet_sort["reverse"] = not _fleet_sort["reverse"]
            else:
                _fleet_sort["key"] = col_key
                _fleet_sort["reverse"] = col_key not in ("worker_name", "current_pool", "can_switch")
            update_stats()

        _sort_bridge_fleet.on("sort_click", _on_fleet_header_click)

        def _sort_arrow(sort_state: dict, col_key: str) -> str:
            """Return sort arrow indicator for a column header."""
            if sort_state["key"] == col_key:
                arrow = "&#9660;" if sort_state["reverse"] else "&#9650;"
                return f'<span class="sa">{arrow}</span>'
            return '<span class="sa">&#8693;</span>'

        def update_stats():
            """Poll worker_stats.json + Prometheus and rebuild both tables."""
            try:
                ws = read_worker_stats()
                workers = ws.get("workers", {})
                pool_lat = ws.get("pool_latency", {})
                _is_manual = _mode.get("manual_active", False)

                # --- Miner Table ---
                now = time.time()
                stale_cutoff = 300  # 5 minutes

                # Build row data with sort values
                row_data = []
                for name, data in workers.items():
                    last_seen = data.get("last_seen", 0)
                    # Paused miners stay in the table indefinitely so the
                    # toggle button remains accessible to turn them back on.
                    if name not in _paused_miners:
                        if last_seen <= 0 or (now - last_seen) >= stale_cutoff:
                            continue
                    ago = now - last_seen
                    connected_at = data.get("connected_at", 0)
                    uptime = (now - connected_at) if connected_at > 0 else 0.0
                    row = {
                        "name": name,
                        "hr_5m": data.get("hr_5m", 0),
                        "hr_60m": data.get("hr_60m", 0),
                        "hr_24h": data.get("hr_24h", 0),
                        "sps": data.get("sps", 0),
                        "diff": data.get("diff", 0),
                        "shares": data.get("shares", 0),
                        "best": data.get("best", 0),
                        "rejected": data.get("rejected", 0),
                        "rej_pct": data.get("rej_pct", 0),
                        "ago": ago,
                        "uptime": uptime,
                    }
                    row_data.append(row)

                # Sort
                sk = _miner_sort["key"]
                row_data.sort(key=lambda r: (r.get(sk, 0) if sk != "name" else r["name"].lower()),
                              reverse=_miner_sort["reverse"])

                if row_data:
                    # Build header
                    hdr = ""
                    for key, label, css, _ in _miner_cols:
                        if key == "toggle":
                            # Toggle column: no sort, no arrow, just empty header
                            hdr += '<th style="text-align:center;width:50px"></th>'
                            continue
                        sc = " sorted" if _miner_sort["key"] == key else ""
                        cls = f'class="{css}{sc}"' if (css or sc) else ""
                        arrow = _sort_arrow(_miner_sort, key)
                        hdr += f'<th {cls} data-sort="{key}" data-table="miner">{label}{arrow}</th>'

                    # Build rows
                    rows_html = ""
                    for r in row_data:
                        cells = ""
                        wname = r["name"]
                        is_paused = wname in _paused_miners
                        for key, _, css, fmt_fn in _miner_cols:
                            if key == "toggle":
                                # Render on/off toggle button
                                if is_paused:
                                    bg = "rgba(120,120,120,0.35)"
                                    clr = "#999"
                                    lbl = "OFF"
                                else:
                                    bg = "rgba(34,197,94,0.25)"
                                    clr = "#22c55e"
                                    lbl = "ON"
                                btn = (
                                    f'<span data-toggle="{wname}" '
                                    f'style="cursor:pointer;padding:2px 8px;border-radius:4px;'
                                    f'font-size:0.7rem;font-weight:600;'
                                    f'background:{bg};color:{clr};user-select:none">'
                                    f'{lbl}</span>'
                                )
                                cells += f'<td style="text-align:center">{btn}</td>'
                                continue
                            val = r[key]
                            cls = f' class="{css}"' if css else ""
                            if key == "name":
                                # Dim the worker name if paused
                                op = ' style="opacity:0.45"' if is_paused else ""
                                cells += f"<td{cls}{op}>{val}</td>"
                            elif key == "rejected":
                                hc = "#f59e0b"
                                op = ";opacity:0.45" if is_paused else ""
                                cells += f'<td{cls} style="color:{hc}{op}">{val}</td>'
                            elif key in ("ago", "uptime"):
                                op = ' style="opacity:0.45"' if is_paused else ""
                                cells += f'<td{cls}{op}>{_fmt_ago(val)}</td>'
                            elif fmt_fn:
                                op = ' style="opacity:0.45"' if is_paused else ""
                                cells += f"<td{cls}{op}>{fmt_fn(val)}</td>"
                            else:
                                op = ' style="opacity:0.45"' if is_paused else ""
                                cells += f"<td{cls}{op}>{val}</td>"
                        rows_html += f"<tr>{cells}</tr>"

                    # Build totals row (sums for most columns, max for best, blank for rej%/seen/worker)
                    if len(row_data) > 1:
                        tot_style = ' style="border-top:2px solid rgba(110,147,214,0.4);font-weight:600;opacity:0.85"'
                        tot_style_r = ' style="border-top:2px solid rgba(110,147,214,0.4);font-weight:600;opacity:0.85;color:rgba(245,158,11,0.95)"'
                        tot_cells = ""
                        for key, _, css, fmt_fn in _miner_cols:
                            cls = f' class="{css}"' if css else ""
                            if key == "toggle":
                                tot_cells += f"<td{tot_style}></td>"
                            elif key == "name":
                                tot_cells += f"<td{tot_style}>Total</td>"
                            elif key in ("rej_pct", "ago", "uptime"):
                                tot_cells += f"<td{cls}{tot_style}>--</td>"
                            elif key == "best":
                                val = max(r[key] for r in row_data)
                                tot_cells += f"<td{cls}{tot_style}>{fmt_fn(val)}</td>"
                            elif key == "rejected":
                                val = sum(r[key] for r in row_data)
                                hc = "#ef4444"
                                tot_cells += f"<td{cls}{tot_style_r}>{val}</td>"
                            else:
                                val = sum(r[key] for r in row_data)
                                if fmt_fn:
                                    tot_cells += f"<td{cls}{tot_style}>{fmt_fn(val)}</td>"
                                else:
                                    tot_cells += f"<td{cls}{tot_style}>{val}</td>"
                        rows_html += f"<tr>{tot_cells}</tr>"

                    stats_miner_html.content = f"""
                    <table class="stats-tbl">
                        <thead><tr>{hdr}</tr></thead>
                        <tbody>{rows_html}</tbody>
                    </table>"""
                else:
                    stats_miner_html.content = '<span style="opacity:0.5">No active workers detected yet.</span>'

                # --- Pool Table ---
                pool_info = get_pool_info()

                # Read Prometheus for per-pool accepted/rejected/jobs/diffsum
                try:
                    raw = http_get_text(METRICS_URL)
                except Exception:
                    raw = ""

                pool_data = []

                # Get target ratio for the Ratio column.
                # If oracle is active, read from Prometheus (oracle weights).
                # If not available yet (startup), default to 50/50.
                # If slider is active, read from weights_override.json.
                _target_pctA = 50.0
                if _mode.get("oracle_active", False):
                    try:
                        _owA = _prom_gauge_value(raw, "dpmp_oracle_weight", pool="A")
                        _owB = _prom_gauge_value(raw, "dpmp_oracle_weight", pool="B")
                        if _owA is not None and _owB is not None and (_owA + _owB) > 0:
                            _target_pctA = _owA / (_owA + _owB) * 100.0
                    except Exception:
                        pass
                else:
                    try:
                        _ov = read_json(WEIGHTS_OVERRIDE_PATH)
                        _wA = float(_ov.get("poolA_weight", 50))
                        _wB = float(_ov.get("poolB_weight", 50))
                        _target_pctA = _wA / (_wA + _wB) * 100.0
                    except Exception:
                        _wA, _wB = get_config_weights()
                        _target_pctA = _wA / (_wA + _wB) * 100.0 if (_wA + _wB) > 0 else 50.0

                # Compute combined 5m hashrate per pool by cross-referencing
                # fleet_metrics (worker -> pool mapping) with worker_stats
                # (worker -> hr_5m).
                _pool_hr = {"A": 0.0, "B": 0.0}
                try:
                    _fm_hr = read_fleet_metrics().get("miners", {})
                    for _wname, _wdata in _fm_hr.items():
                        _wpool = _wdata.get("pool", "")
                        if _wpool in _pool_hr:
                            _pool_hr[_wpool] += workers.get(_wname, {}).get("hr_5m", 0.0)
                except Exception:
                    pass

                for pk in ("A", "B"):
                    pi = pool_info.get(pk, {})
                    acc = _prom_gauge_value(raw, "dpmp_shares_accepted_total", pool=pk) or 0.0
                    rej = _prom_gauge_value(raw, "dpmp_shares_rejected_total", pool=pk) or 0.0
                    total = acc + rej
                    # In Manual mode there is no target ratio, so show "--"
                    _goal_pct = "--" if _is_manual else (_target_pctA if pk == "A" else (100.0 - _target_pctA))
                    _en2 = _prom_gauge_value(raw, "dpmp_extranonce2_size", pool=pk)
                    pool_data.append({
                        "pool_name": pi.get("name", f"Pool {pk}"),
                        "slot": pk,
                        "chain": pi.get("chain", "--"),
                        "en2_size": int(_en2) if _en2 else 0,
                        "ratio": _goal_pct,
                        "pool_hr": _pool_hr.get(pk, 0.0),
                        "latency": pool_lat.get(pk, 0.0),
                        "accepted": acc,
                        "rejected": rej,
                        "rej_pct": (rej / total * 100) if total > 0 else 0.0,
                        "jobs": _prom_gauge_value(raw, "dpmp_jobs_forwarded_total", pool=pk) or 0.0,
                        "tdiff": _prom_gauge_value(raw, "dpmp_accepted_difficulty_sum_total", pool=pk) or 0.0,
                    })

                # Sort pool table
                psk = _pool_sort["key"]
                pool_data.sort(
                    key=lambda r: (r.get(psk, "") if psk in ("pool_name", "slot", "chain") else r.get(psk, 0)),
                    reverse=_pool_sort["reverse"])

                # Build pool header
                phdr = ""
                for key, label, css, _ in _pool_cols:
                    sc = " sorted" if _pool_sort["key"] == key else ""
                    cls = f'class="{css}{sc}"' if (css or sc) else ""
                    arrow = _sort_arrow(_pool_sort, key)
                    phdr += f'<th {cls} data-sort="{key}" data-table="pool">{label}{arrow}</th>'

                # Build pool rows
                pool_rows = ""
                for r in pool_data:
                    cells = ""
                    for key, _, css, fmt_fn in _pool_cols:
                        val = r[key]
                        cls = f' class="{css}"' if css else ""
                        if key == "latency":
                            cells += f'<td{cls}>{f"{val:.0f} ms" if val > 0 else "--"}</td>'
                        elif fmt_fn:
                            cells += f"<td{cls}>{fmt_fn(val)}</td>"
                        else:
                            cells += f"<td{cls}>{val}</td>"
                    pool_rows += f"<tr>{cells}</tr>"

                stats_pool_html.content = f"""
                <table class="stats-tbl">
                    <thead><tr>{phdr}</tr></thead>
                    <tbody>{pool_rows}</tbody>
                </table>"""

                # --- Fleet Table ---
                fm = read_fleet_metrics()
                fleet_miners = fm.get("miners", {})

                # Adjust the fleet table title depending on mode
                fleet_expansion.text = "Fleet Stats (Manual Mode Active)" if _is_manual else "Fleet Stats"
                
                # Adjust the fleet table footnote depending on mode
                if _is_manual:
                    lbl_fleet_footnote.text = "* Active workers may take up to 30 seconds to stabilize on their assigned pools when switching to Manual mode."
                else:
                    lbl_fleet_footnote.text = "* A pinned worker will only operate on the assigned pool, which may impact convergence at extreme ratios."
                lbl_fleet_footnote.visible = True

                if fleet_miners:
                    # In Manual mode, read current pinned assignments for dropdown state
                    _pinned_assign = read_pinned_assignments_gui() if _is_manual else {}

                    fleet_data = []
                    for mname, mdata in fleet_miners.items():
                        _can_sw = mdata.get("can_switch", True)
                        fleet_data.append({
                            "worker_name": mname,
                            "current_pool": mdata.get("pool", "?"),
                            "pinned": mdata.get("pool", "?") if not _can_sw else "--",
                            "time_on_pool_s": mdata.get("time_on_pool_s", 0.0),
                            "mode": mdata.get("mode", "static"),
                            "switch_count": mdata.get("switch_count", 0),
                            "contribution": mdata.get("contribution", 0.0),
                            "health": mdata.get("health", 1.0),
                        })

                    # Sort
                    fsk = _fleet_sort["key"]
                    fleet_data.sort(
                        key=lambda r: (r.get(fsk, "") if fsk in ("worker_name", "current_pool", "pinned", "mode") else r.get(fsk, 0)),
                        reverse=_fleet_sort["reverse"])

                    # Determine which columns to show.
                    # In Manual mode: hide "Pinned*" column; Pool column becomes dropdown.
                    # In normal mode: show all columns as usual.
                    _skip_cols = {"pinned"} if _is_manual else set()

                    # Build header
                    fhdr = ""
                    for key, label, css, _ in _fleet_cols:
                        if key in _skip_cols:
                            continue
                        sc = " sorted" if _fleet_sort["key"] == key else ""
                        cls = f'class="{css}{sc}"' if (css or sc) else ""
                        arrow = _sort_arrow(_fleet_sort, key)
                        fhdr += f'<th {cls} data-sort="{key}" data-table="fleet">{label}{arrow}</th>'

                    # Build rows
                    fleet_rows = ""

                    for r in fleet_data:
                        cells = ""
                        wname = r["worker_name"]
                        # Check if this miner has completed its pending reconnect.
                        # Reconnect is complete when time_on_pool_s resets (new < stored).
                        if wname in _manual_pending:
                            _prev_top = _manual_pending[wname]
                            _cur_top = r.get("time_on_pool_s", 0.0)
                            if _cur_top < _prev_top:
                                del _manual_pending[wname]
                            else:
                                _manual_pending[wname] = _cur_top
                        _name_pending = _is_manual and wname in _manual_pending
                        for key, _, css, fmt_fn in _fleet_cols:
                            if key in _skip_cols:
                                continue
                            val = r[key]
                            cls = f' class="{css}"' if css else ""
                            if key == "current_pool" and _is_manual:
                                # Replace static pool text with A/B dropdown
                                _cur_assign = _pinned_assign.get(wname, "A")
                                _opt_a = 'selected' if _cur_assign == "A" else ''
                                _opt_b = 'selected' if _cur_assign == "B" else ''

                                _sel = (
                                    f'<select data-assign="{wname}" '
                                    f'style="background:#1e2330;color:#e5e7eb;border:1px solid rgba(110,147,214,0.4);'
                                    f'border-radius:4px;padding:1px 4px;font-size:0.8rem;cursor:pointer;">'
                                    f'<option value="A" {_opt_a} style="background:#1e2330;color:#e5e7eb;">A</option>'
                                    f'<option value="B" {_opt_b} style="background:#1e2330;color:#e5e7eb;">B</option>'
                                    f'</select>'
                                )

                                cells += f'<td{cls}>{_sel}</td>'

                            elif key == "worker_name":
                                _nc = ' style="color:#f97316;font-weight:600"' if _name_pending else ""
                                cells += f'<td{cls}{_nc}>{wname}</td>'

                            elif key == "health":
                                hc = "#22c55e" if val >= 0.9 else "#f59e0b" if val >= 0.7 else "#ef4444"
                                cells += f'<td{cls} style="color:{hc}">{fmt_fn(val)}</td>'
                            elif key == "time_on_pool_s":
                                cells += f'<td{cls}>{_fmt_ago(val)}</td>'
                            elif key == "switch_count" and _is_manual:
                                cells += f'<td{cls}>--</td>'
                            elif key == "mode":
                                _display_mode = "static" if _is_manual else val
                                mc = "#22d3ee" if _display_mode == "time_slice" else "#9ca3af"
                                cells += f'<td{cls} style="color:{mc}">{fmt_fn(_display_mode)}</td>'
                            elif key == "mode":
                                mc = "#22d3ee" if val == "time_slice" else "#9ca3af"
                                cells += f'<td{cls} style="color:{mc}">{fmt_fn(val)}</td>'
                            elif fmt_fn:
                                cells += f"<td{cls}>{fmt_fn(val)}</td>"
                            else:
                                cells += f"<td{cls}>{val}</td>"
                        fleet_rows += f"<tr>{cells}</tr>"

                    # Totals row
                    if len(fleet_data) > 1:
                        tot_style = ' style="border-top:2px solid rgba(110,147,214,0.4);font-weight:600;opacity:0.85"'
                        tot_cells = ""
                        for key, _, css, fmt_fn in _fleet_cols:
                            if key in _skip_cols:
                                continue
                            cls = f' class="{css}"' if css else ""
                            if key == "worker_name":
                                tot_cells += f"<td{tot_style}>Total ({len(fleet_data)})</td>"
                            elif key == "switch_count":
                                if _is_manual:
                                    tot_cells += f'<td{cls}{tot_style}>--</td>'
                                else:
                                    val = sum(r[key] for r in fleet_data)
                                    tot_cells += f'<td{cls}{tot_style}>{int(val):,}</td>'
                            elif key == "contribution":
                                val = sum(r[key] for r in fleet_data)
                                tot_cells += f'<td{cls}{tot_style}>{val:.1%}</td>'
                            else:
                                tot_cells += f"<td{cls}{tot_style}>--</td>"
                        fleet_rows += f"<tr>{tot_cells}</tr>"

                    stats_fleet_html.content = f"""
                    <table class="stats-tbl">
                        <thead><tr>{fhdr}</tr></thead>
                        <tbody>{fleet_rows}</tbody>
                    </table>"""
                else:
                    stats_fleet_html.content = '<span style="opacity:0.5">No fleet data available yet.</span>'

            except Exception as e:
                stats_miner_html.content = f'<span style="color:#f87171">Error: {e}</span>'

        update_stats()
        ui.timer(5.0, update_stats)

        # Inject JS click handler for sortable column headers.
        # Uses NiceGUI's built-in getElement().emit() to send click events
        # directly to the Python-side element handlers.
        _js_sort_handler = f"""
        <script>
        document.addEventListener('click', function(e) {{
            var th = e.target.closest('th[data-sort]');
            if (th) {{
                var key = th.getAttribute('data-sort');
                var table = th.getAttribute('data-table');
                if (!key || !table) return;
                var eid;
                if (table === 'miner') eid = {_bridge_miner_id};
                else if (table === 'pool') eid = {_bridge_pool_id};
                else if (table === 'fleet') eid = {_bridge_fleet_id};
                else return;
                getElement(eid).$emit('sort_click', key);
                return;
            }}
            var tog = e.target.closest('[data-toggle]');
            if (tog) {{
                var worker = tog.getAttribute('data-toggle');
                if (worker) {{
                    getElement({_bridge_toggle_id}).$emit('toggle_miner', worker);
                }}
            }}
        }});

        document.addEventListener('change', function(e) {{
            var sel = e.target.closest('select[data-assign]');
            if (sel) {{
                var worker = sel.getAttribute('data-assign');
                var pool = sel.value;
                if (worker && pool) {{
                    getElement({_bridge_assign_id}).$emit('assign_pool', {{worker: worker, pool: pool}});
                }}
            }}
        }});
        </script>
        """
        ui.add_body_html(_js_sort_handler)

    with ui.tab_panel(t_cfg):
        ui.label("DPMP Configuration").classes("text-lg font-semibold")

        # list of events that we generally do NOT want to log (default deny list)
        # Logging level options:
        #   Normal = important events only (errors, connects, switches, pins, health)
        #   Full   = all events including per-share routing, scheduler ticks, etc.
        #   None   = no logging at all
        LOGGING_LEVELS = ["Normal", "Full", "None"]

        # --- controls (created first; populated by reload_cfg) ---

        # Pool Difficulty
        with ui.expansion("Pool Difficulty Settings:", icon="settings").classes("w-full").tooltip("Preferred pool difficulty settings for downstream miners"):
            dd_default_min = ui.number("Default Min", precision=0).props("step=1 min=0").classes("w-64")
            dd_poolA_min   = ui.number("Pool A Min",  precision=0).props("step=1 min=0").classes("w-64")
            dd_poolB_min   = ui.number("Pool B Min",  precision=0).props("step=1 min=0").classes("w-64")

        # Listen
        with ui.expansion("Listen Settings:", icon="settings").classes("w-full").tooltip("DPMP Port and Host settings"):
            listen_host = ui.input("Host").classes("w-64")
            listen_port = ui.number("Port", precision=0).props("step=1 min=1 max=65535").classes("w-64")

        # Logging
        with ui.expansion("Logging Settings:", icon="settings").classes("w-full"):
            ui.label("Controls how much detail is written to the log file.").classes("text-sm")
            with ui.column().classes("gap-1"):
                ui.label("Normal: Important events only (connects, switches, pins, errors, health).").classes("text-xs text-gray-600")
                ui.label("Full: All events including per-share routing, scheduler ticks, etc. Warning: creates large log files quickly.").classes("text-xs text-gray-600")
                ui.label("None: No logging at all.").classes("text-xs text-gray-600")
            logging_level_select = ui.select(
                LOGGING_LEVELS, value="Normal", label="Log Level"
            ).classes("w-64")


        # Metrics
        with ui.expansion("Metrics Settings:", icon="settings").classes("w-full").tooltip("Prometheus metrics listener settings"):
            metrics_host    = ui.input("Host").classes("w-64")
            metrics_port    = ui.number("Port", precision=0).props("step=1 min=1 max=65535").classes("w-64")
            metrics_enabled = ui.checkbox("Enabled")

        # Swap A/B button -- swaps Pool A and Pool B field values in the GUI.
        # Does NOT write to config or restart; user must click Apply+Restart.
        def _swap_pools():
            """Swap all Pool A and Pool B field values in the Config tab."""
            # Read current values
            a_host, b_host = poolA_host.value, poolB_host.value
            a_name, b_name = poolA_name.value, poolB_name.value
            a_port, b_port = poolA_port.value, poolB_port.value
            a_wallet, b_wallet = poolA_wallet.value, poolB_wallet.value
            a_chain, b_chain = poolA_chain.value, poolB_chain.value
            a_dmin, b_dmin = dd_poolA_min.value, dd_poolB_min.value
            a_idle, b_idle = poolA_idle_disconnect.value, poolB_idle_disconnect.value
            # Swap
            poolA_host.set_value(b_host)
            poolB_host.set_value(a_host)
            poolA_name.set_value(b_name)
            poolB_name.set_value(a_name)
            poolA_port.set_value(b_port)
            poolB_port.set_value(a_port)
            poolA_wallet.set_value(b_wallet)
            poolB_wallet.set_value(a_wallet)
            poolA_chain.set_value(b_chain)
            poolB_chain.set_value(a_chain)
            dd_poolA_min.set_value(b_dmin)
            dd_poolB_min.set_value(a_dmin)
            poolA_idle_disconnect.set_value(b_idle)
            poolB_idle_disconnect.set_value(a_idle)
            ui.notify("Pool A and Pool B settings swapped. Review and click Apply + Restart when ready.", type="info")

        with ui.row().classes("items-center gap-4"):
            ui.button("Swap Pool A / Pool B", icon="swap_horiz", on_click=_swap_pools).props(
                "flat dense no-caps"
            ).classes("text-sm").style("color: #6E93D6").tooltip(
                "Swap all Pool A and Pool B settings. Does not apply changes -- you must click Apply + Restart.")

            def _open_edit_address_book():
                """Open modal to rename or delete address book entries."""
                book = load_address_book()
                if not book:
                    ui.notify("Address book is empty.", type="info")
                    return

                with ui.dialog() as dlg, ui.card().classes("w-full max-w-lg"):
                    ui.label("Edit Address Book").classes("text-base font-semibold").style("color: #6E93D6")
                    ui.separator().classes("my-2")

                    rows_container = ui.column().classes("w-full gap-2")

                    def _build_rows():
                        rows_container.clear()
                        current_book = load_address_book()
                        if not current_book:
                            with rows_container:
                                ui.label("Address book is empty.").classes("text-sm").style("color: #888")
                            return
                        for key, entry in sorted(current_book.items(),
                                                  key=lambda x: x[1].get("name", x[0]).lower()):
                            name   = entry.get("name", "") or key
                            host   = entry.get("host", "")
                            port   = entry.get("port", 3333)
                            chain  = entry.get("chain", "BTC")

                            def _make_delete(k=key):
                                def _delete():
                                    b = load_address_book()
                                    if k in b:
                                        del b[k]
                                        save_address_book(b)
                                        ui.notify("Entry deleted.", type="positive")
                                        _build_rows()
                                return _delete

                            def _make_rename(k=key, current_name=name):
                                def _rename():
                                    with ui.dialog() as rename_dlg, ui.card().classes("min-w-[300px]"):
                                        ui.label("Rename Pool").classes("text-sm font-semibold").style("color: #6E93D6")
                                        new_name_input = ui.input("Name", value=current_name).classes("w-full")
                                        with ui.row().classes("gap-2 justify-end"):
                                            def _save_rename(nd=rename_dlg, ni=new_name_input, ky=k):
                                                b = load_address_book()
                                                if ky in b:
                                                    b[ky]["name"] = ni.value.strip()
                                                    save_address_book(b)
                                                    ui.notify("Name updated.", type="positive")
                                                    nd.close()
                                                    _build_rows()
                                            ui.button("Save", on_click=_save_rename).props("dense outline size=sm")
                                            ui.button("Cancel", on_click=rename_dlg.close).props("flat dense size=sm")
                                    rename_dlg.open()
                                return _rename

                            _delete_handler = _make_delete(k=key)
                            _rename_handler = _make_rename(k=key, current_name=name)
                            with rows_container:
                                with ui.row().classes("w-full items-center justify-between gap-2"):
                                    with ui.column().classes("flex-1 gap-0"):
                                        ui.label(f"{name}").classes("text-sm font-semibold")
                                        ui.label(f"{host}:{port}  |  {chain}").classes("text-xs").style("color: #888")
                                    with ui.row().classes("gap-1"):
                                        ui.button("Rename", on_click=_rename_handler).props("dense outline size=sm")
                                        ui.button("Delete", on_click=_delete_handler).props("dense outline size=sm color=negative")
                                ui.separator().classes("my-1")

                    _build_rows()
                    ui.button("Close", on_click=dlg.close).props("flat dense no-caps").classes("text-sm")

                dlg.open()

            ui.button("Edit Address Book", icon="edit", on_click=_open_edit_address_book).props(
                "flat dense no-caps"
            ).classes("text-sm").style("color: #6E93D6").tooltip(
                "Rename or delete saved pools in the address book.")

        # Pool A
        with ui.expansion("Pool A Settings:", icon="settings").classes("w-full").tooltip("Settings for Pool A"):
            poolA_host   = ui.input("Host").classes("w-full")
            poolA_name   = ui.input("Name").classes("w-64")
            poolA_port   = ui.number("Port", precision=0).props("step=1 min=1 max=65535").classes("w-64")
            poolA_wallet = ui.input("Wallet").classes("w-full")
            poolA_chain  = ui.select(["BTC", "BCH", "BSV", "DGB", "XEC", "PPC", "None"], value="BTC", label="Chain").classes("w-64").tooltip(
                "Which SHA-256 blockchain this pool mines. Set to 'None' if not applicable. "
                "Oracle auto-balance requires one BTC and one BCH pool.")
            poolA_idle_disconnect = ui.checkbox("Pool disconnects idle connections").tooltip(
                "Enable if this pool automatically disconnects miners that have been idle "
                "(not actively mining on this pool) for an extended period. "
                "Example: MiningCore disconnects idle connections after ~10 minutes. "
                "When enabled, DPMP will wait and reconnect on-demand rather than "
                "immediately retrying. Leave unchecked for most pools (Bassin, PublicPool).")
            ui.separator().classes("my-2")
            btn_poolA_select = ui.button("Select from Address Book", icon="menu_book").props(
                "flat dense no-caps").classes("text-sm").style("color: #6E93D6").tooltip(
                "Fill Pool A fields from a previously saved pool.")

        # Pool B
        with ui.expansion("Pool B Settings:", icon="settings").classes("w-full").tooltip("Settings for Pool B"):
            poolB_host   = ui.input("Host").classes("w-full")
            poolB_name   = ui.input("Name").classes("w-64")
            poolB_port   = ui.number("Port", precision=0).props("step=1 min=1 max=65535").classes("w-64")
            poolB_wallet = ui.input("Wallet").classes("w-full")
            poolB_chain  = ui.select(["BTC", "BCH", "BSV", "DGB", "XEC", "PPC", "None"], value="BCH", label="Chain").classes("w-64").tooltip(
                "Which SHA-256 blockchain this pool mines. Set to 'None' if not applicable. "
                "Oracle auto-balance requires one BTC and one BCH pool.")
            poolB_idle_disconnect = ui.checkbox("Pool disconnects idle connections").tooltip(
                "Enable if this pool automatically disconnects miners that have been idle "
                "(not actively mining on this pool) for an extended period. "
                "Example: MiningCore disconnects idle connections after ~10 minutes. "
                "When enabled, DPMP will wait and reconnect on-demand rather than "
                "immediately retrying. Leave unchecked for most pools (Bassin, PublicPool).")
            ui.separator().classes("my-2")
            btn_poolB_select = ui.button("Select from Address Book", icon="menu_book").props(
                "flat dense no-caps").classes("text-sm").style("color: #6E93D6").tooltip(
                "Fill Pool B fields from a previously saved pool.")

        # Scheduler
        with ui.expansion("Scheduler Settings:", icon="settings").classes("w-full").tooltip("Settings for the dual-pool scheduler"):
            #sch_min_switch = ui.number("Min Switch Seconds", precision=0).props("step=1 min=25 max=300").classes("w-64").tooltip("Minimum time before switching pools. Recommend between 30 seconds and 60 seconds.")
            sch_slice      = ui.number("Slice Seconds",      precision=0).props("step=1 min=1 max=120").classes("w-64").tooltip("Mininum duration of each dynamic mining slice before switching. Recommend you use a value between 10 and 30 seconds.")

            # Visual separator to avoid accidentally editing weights when changing timing fields
            ui.separator().classes("my-2")
            ui.label("Pool Weights").classes("text-sm font-semibold").style("color: #6E93D6")
            sch_weightA    = ui.number("Pool A Weight",      precision=0).props("step=5 min=0 max=100").classes("w-64").tooltip("Weighting for Pool A in the scheduler. Values are relative (e.g. 50/50 = same as 1/1).")
            sch_weightB    = ui.number("Pool B Weight",      precision=0).props("step=5 min=0 max=100").classes("w-64").tooltip("Weighting for Pool B in the scheduler. Values are relative (e.g. 50/50 = same as 1/1).")
            ui.separator().classes("my-2")

            # Oracle Auto-Balance settings
            ui.label("Oracle Auto-Balance").classes("text-sm font-semibold").style("color: #6E93D6")

            sch_max_deviation = ui.number("Max Deviation (%)", value=20, precision=0).props("step=1 min=5 max=45").classes("w-64").tooltip(
                "Maximum percentage points the oracle can deviate from 50/50. "
                "Example: 20 means weights can range from 30/70 to 70/30. "
                "45 means weights can range from 5/95 to 95/5. "
                "Range: 5-45. Lower = more conservative, higher = more aggressive.")
            sch_oracle_url = ui.input("Oracle URL").classes("w-full").tooltip(
                "URL of the oracle data endpoint (oracle.php). "
                "Default: https://www.sr-analyst.com/dpmp/oracle.php")

            sch_oracle_poll = ui.number("Oracle Poll Seconds", value=600, precision=0).props("step=60 min=600 max=3600").classes("w-64").tooltip(
                "How often the oracle fetches fresh hashrate data, in seconds. "
                "Default: 600 (10 minutes). Minimum: 600. "
                "The data collector updates every 10 minutes, so polling faster has no benefit.")

            ui.separator().classes("my-2")

            # Pool Compatibility
            ui.label("Pool Compatibility").classes("text-sm font-semibold").style("color: #6E93D6")
            sch_force_reconnect_en2 = ui.checkbox("Force reconnect on en2 size mismatch").tooltip(
                "When enabled, miners will fully disconnect and reconnect when switching between pools "
                "that have different extranonce2 sizes (e.g. Bassin en2=8 paired with PublicPool en2=4). "
                "This ensures each pool receives correctly-sized shares. "
                "Not needed for MiningCore/Bassin which handles oversized en2 gracefully. "
                "Enable only if one pool shows 0 accepted shares.")
            sch_sr_exclusions = ui.input("SR Recruitment Exclusions").classes("w-full").tooltip(
                "Comma-separated list of worker names that should never be recruited as dynamic "
                "time-slicers for SR correction. These miners will still switch pools normally "
                "as static miners when the oracle changes the target. "
                "Use this for miners that generate reject storms when switching pools frequently "
                "(e.g. firmware-sensitive devices). Example: GekkoA2Z, BM101A")

        ui.separator()

        # bottom buttons (same behavior, now wired to controls)
        with ui.row().classes("items-center gap-2"):
            btn_reload = ui.button("Reload from Server", icon="refresh").tooltip("Reload current config from DPMP")
            btn_apply  = ui.button("Apply + Restart dpmp", icon="save").tooltip("Apply changes and restart DPMP")
            lbl_cfg = ui.label("").classes("text-sm")

        def _safe_get(d: dict, path: list, default=None):
            cur = d
            for k in path:
                if not isinstance(cur, dict) or k not in cur:
                    return default
                cur = cur[k]
            return cur

        def _to_int(x, default=0):
            try:
                if x is None or x == "":
                    return int(default)
                return int(float(x))
            except Exception:
                return int(default)

        def _ensure_logging_defaults(cfg: dict) -> None:
            cfg.setdefault("logging", {})
            cfg["logging"].setdefault("allow", [])
            cfg["logging"].setdefault("deny", [])
            cfg["logging"].setdefault("json", True)
            cfg["logging"].setdefault("level", "normal")

        def _apply_logging_level(cfg: dict) -> None:
            _ensure_logging_defaults(cfg)
            # Map UI label to config value
            _level_map = {"Normal": "normal", "Full": "full", "None": "none"}
            selected = str(logging_level_select.value or "Normal")
            cfg["logging"]["level"] = _level_map.get(selected, "normal")
            # Clear legacy deny/allow lists since level-based filtering
            # is now handled entirely in dpmpv2.py
            cfg["logging"]["allow"] = []
            cfg["logging"]["deny"] = []

        def _set_logging_from_cfg(cfg: dict) -> None:
            _level = str(_safe_get(cfg, ["logging", "level"], "normal") or "normal").strip().lower()
            # Map config value to UI label
            _ui_map = {"normal": "Normal", "info": "Normal",
                       "full": "Full", "debug": "Full",
                       "none": "None", "off": "None", "quiet": "None"}
            logging_level_select.value = _ui_map.get(_level, "Normal")

        def reload_cfg():
            global state
            state = load_state()
            try:
                cfg = json.loads(state.config_raw or "{}")
            except Exception:
                cfg = {}

            # downstream_diff
            dd_default_min.value = _to_int(_safe_get(cfg, ["downstream_diff", "default_min"], 1), 1)
            dd_poolA_min.value   = _to_int(_safe_get(cfg, ["downstream_diff", "poolA_min"], 1), 1)
            dd_poolB_min.value   = _to_int(_safe_get(cfg, ["downstream_diff", "poolB_min"], 1), 1)

            # listen
            listen_host.value = str(_safe_get(cfg, ["listen", "host"], "0.0.0.0") or "")
            listen_port.value = _to_int(_safe_get(cfg, ["listen", "port"], 3351), 3351)

            # logging (level selector)
            _ensure_logging_defaults(cfg)
            _set_logging_from_cfg(cfg)

            # metrics
            metrics_host.value    = str(_safe_get(cfg, ["metrics", "host"], "0.0.0.0") or "")
            metrics_port.value    = _to_int(_safe_get(cfg, ["metrics", "port"], 9210), 9210)
            metrics_enabled.value = bool(_safe_get(cfg, ["metrics", "enabled"], True))

            # pools A
            poolA_host.value   = str(_safe_get(cfg, ["pools", "A", "host"], "") or "")
            poolA_name.value   = str(_safe_get(cfg, ["pools", "A", "name"], "") or "")
            poolA_port.value   = _to_int(_safe_get(cfg, ["pools", "A", "port"], 3333), 3333)
            poolA_wallet.value = str(_safe_get(cfg, ["pools", "A", "wallet"], "") or "")
            _raw_chainA = str(_safe_get(cfg, ["pools", "A", "chain"], "") or "").strip().upper()
            _chain_map = {
                "BTC": "BTC", "BCH": "BCH", "BSV": "BSV",
                "DGB": "DGB", "XEC": "XEC", "PPC": "PPC",
                "NONE": "None",
            }
            poolA_chain.value = _chain_map.get(_raw_chainA, "BTC")
            poolA_idle_disconnect.value = bool(_safe_get(cfg, ["pools", "A", "idle_disconnect"], False))

            # pools B
            poolB_host.value   = str(_safe_get(cfg, ["pools", "B", "host"], "") or "")
            poolB_name.value   = str(_safe_get(cfg, ["pools", "B", "name"], "") or "")
            poolB_port.value   = _to_int(_safe_get(cfg, ["pools", "B", "port"], 3333), 3333)
            poolB_wallet.value = str(_safe_get(cfg, ["pools", "B", "wallet"], "") or "")
            _raw_chainB = str(_safe_get(cfg, ["pools", "B", "chain"], "") or "").strip().upper()
            poolB_chain.value = _chain_map.get(_raw_chainB, "BCH")
            poolB_idle_disconnect.value = bool(_safe_get(cfg, ["pools", "B", "idle_disconnect"], False))

            # scheduler
            #sch_min_switch.value = _to_int(_safe_get(cfg, ["scheduler", "min_switch_seconds"], 30), 30)
            sch_slice.value      = _to_int(_safe_get(cfg, ["scheduler", "slice_seconds"], 30), 30)
            sch_weightA.value    = _to_int(_safe_get(cfg, ["scheduler", "poolA_weight"], 50), 50)
            sch_weightB.value    = _to_int(_safe_get(cfg, ["scheduler", "poolB_weight"], 50), 50)

            # oracle auto-balance            
            sch_max_deviation.value = _to_int(_safe_get(cfg, ["scheduler", "auto_balance_max_deviation"], 20), 20)
            sch_oracle_url.value    = str(_safe_get(cfg, ["scheduler", "oracle_url"], "https://www.sr-analyst.com/dpmp/oracle.php") or "")
            sch_oracle_poll.value   = _to_int(_safe_get(cfg, ["scheduler", "oracle_poll_seconds"], 600), 600)
            sch_force_reconnect_en2.value = bool(_safe_get(cfg, ["scheduler", "force_reconnect_on_en2_mismatch"], False))
            _raw_exclusions = _safe_get(cfg, ["scheduler", "sr_recruit_exclusions"], [])
            if isinstance(_raw_exclusions, list):
                sch_sr_exclusions.value = ", ".join(str(x).strip() for x in _raw_exclusions if str(x).strip())
            else:
                sch_sr_exclusions.value = ""

            lbl_cfg.text = f"[{now_utc()}] reloaded"
            ui.notify("config reloaded", type="positive")

        def _open_address_book_dialog(target_pool: str):
            """Open address book selection dialog for the given pool (A or B)."""
            book = load_address_book()
            if not book:
                ui.notify("Address book is empty. Pools are saved automatically when you click Apply.", type="info")
                return

            with ui.dialog() as dlg, ui.card().classes("w-full max-w-lg"):
                ui.label("Select a Pool").classes("text-base font-semibold").style("color: #6E93D6")
                ui.separator().classes("my-2")

                for key, entry in sorted(book.items(), key=lambda x: x[1].get("name", x[0]).lower()):
                    name    = entry.get("name", "") or key
                    host    = entry.get("host", "")
                    port    = entry.get("port", 3333)
                    wallet  = entry.get("wallet", "")
                    chain   = entry.get("chain", "BTC")

                    def _make_handler(e=entry, d=dlg, tp=target_pool):
                        def _handler():
                            if tp == "A":
                                poolA_host.set_value(e.get("host", ""))
                                poolA_name.set_value(e.get("name", ""))
                                poolA_port.set_value(e.get("port", 3333))
                                poolA_wallet.set_value(e.get("wallet", ""))
                                _chain_val = e.get("chain", "BTC")
                                if _chain_val in ["BTC", "BCH", "BSV", "DGB", "XEC", "PPC", "None"]:
                                    poolA_chain.set_value(_chain_val)
                            else:
                                poolB_host.set_value(e.get("host", ""))
                                poolB_name.set_value(e.get("name", ""))
                                poolB_port.set_value(e.get("port", 3333))
                                poolB_wallet.set_value(e.get("wallet", ""))
                                _chain_val = e.get("chain", "BTC")
                                if _chain_val in ["BTC", "BCH", "BSV", "DGB", "XEC", "PPC", "None"]:
                                    poolB_chain.set_value(_chain_val)
                            d.close()
                            ui.notify(f"Pool fields filled from address book.", type="positive")
                        return _handler

                    with ui.row().classes("w-full items-center justify-between gap-2"):
                        with ui.column().classes("flex-1 gap-0"):
                            ui.label(f"{name}").classes("text-sm font-semibold")
                            ui.label(f"{host}:{port}  |  {chain}").classes("text-xs").style("color: #888")
                        ui.button("Select", on_click=_make_handler()).props("dense outline size=sm")
                    ui.separator().classes("my-1")

                ui.button("Cancel", on_click=dlg.close).props("flat dense no-caps").classes("text-sm")

            dlg.open()

        btn_poolA_select.on_click(lambda: _open_address_book_dialog("A"))
        btn_poolB_select.on_click(lambda: _open_address_book_dialog("B"))

        def apply_cfg():
            # start from current on-disk config so we preserve unknown fields

            try:
                raw = read_text_file(CONFIG_PATH, max_bytes=500_000)
                cfg = json.loads(raw or "{}")
            except Exception:
                cfg = {}

            # downstream_diff
            cfg.setdefault("downstream_diff", {})
            cfg["downstream_diff"]["default_min"] = _to_int(dd_default_min.value, 1)
            cfg["downstream_diff"]["poolA_min"]   = _to_int(dd_poolA_min.value,   1)
            cfg["downstream_diff"]["poolB_min"]   = _to_int(dd_poolB_min.value,   1)

            # listen
            cfg.setdefault("listen", {})
            cfg["listen"]["host"] = str(listen_host.value or "").strip()
            cfg["listen"]["port"] = _to_int(listen_port.value, 3351)

            # logging (level selector)
            _apply_logging_level(cfg)

            # metrics
            cfg.setdefault("metrics", {})
            cfg["metrics"]["host"]    = str(metrics_host.value or "").strip()
            cfg["metrics"]["port"]    = _to_int(metrics_port.value, 9210)
            cfg["metrics"]["enabled"] = bool(metrics_enabled.value)

            # pools
            cfg.setdefault("pools", {})
            cfg["pools"].setdefault("A", {})
            cfg["pools"]["A"]["host"]   = str(poolA_host.value or "").strip()
            cfg["pools"]["A"]["name"]   = str(poolA_name.value or "").strip()
            cfg["pools"]["A"]["port"]   = _to_int(poolA_port.value, 3333)
            cfg["pools"]["A"]["wallet"] = str(poolA_wallet.value or "").strip()
            cfg["pools"]["A"]["chain"]  = str(poolA_chain.value or "BTC").strip().upper()
            cfg["pools"]["A"]["idle_disconnect"] = bool(poolA_idle_disconnect.value)

            cfg["pools"].setdefault("B", {})
            cfg["pools"]["B"]["host"]   = str(poolB_host.value or "").strip()
            cfg["pools"]["B"]["name"]   = str(poolB_name.value or "").strip()
            cfg["pools"]["B"]["port"]   = _to_int(poolB_port.value, 2018)
            cfg["pools"]["B"]["wallet"] = str(poolB_wallet.value or "").strip()
            cfg["pools"]["B"]["chain"]  = str(poolB_chain.value or "BCH").strip().upper()
            cfg["pools"]["B"]["idle_disconnect"] = bool(poolB_idle_disconnect.value)

            # Auto-save both pools to address book (only if not already present)
            address_book_autosave(
                cfg["pools"]["A"]["host"], cfg["pools"]["A"]["port"],
                cfg["pools"]["A"]["name"], cfg["pools"]["A"]["wallet"],
                cfg["pools"]["A"]["chain"])
            address_book_autosave(
                cfg["pools"]["B"]["host"], cfg["pools"]["B"]["port"],
                cfg["pools"]["B"]["name"], cfg["pools"]["B"]["wallet"],
                cfg["pools"]["B"]["chain"])

            # scheduler
            cfg.setdefault("scheduler", {})
            #cfg["scheduler"]["min_switch_seconds"] = _to_int(sch_min_switch.value, 30)
            cfg["scheduler"]["slice_seconds"]      = _to_int(sch_slice.value, 30)
            cfg["scheduler"]["poolA_weight"]       = _to_int(sch_weightA.value, 50)
            cfg["scheduler"]["poolB_weight"]       = _to_int(sch_weightB.value, 50)

            # oracle auto-balance            
            cfg["scheduler"]["auto_balance_max_deviation"]  = max(5, min(45, _to_int(sch_max_deviation.value, 20)))
            cfg["scheduler"]["oracle_url"]                 = str(sch_oracle_url.value or "").strip()
            cfg["scheduler"]["oracle_poll_seconds"]        = max(600, min(3600, _to_int(sch_oracle_poll.value, 600)))
            cfg["scheduler"]["force_reconnect_on_en2_mismatch"] = bool(sch_force_reconnect_en2.value)
            _excl_raw = str(sch_sr_exclusions.value or "").strip()
            cfg["scheduler"]["sr_recruit_exclusions"] = [
                x.strip() for x in _excl_raw.split(",") if x.strip()
            ]

            cfg.setdefault("scheduler", {}).setdefault("mode", "ratio")  # preserve/ensure

            try:
                write_json_atomic(CONFIG_PATH, cfg)
            except Exception as e:
                ui.notify(f"write failed: {e}", type="negative")
                return

            # Delete weight override so DPMP starts fresh with config defaults
            delete_weight_override()
            # Delete oracle_mode.json so DPMP falls back to config auto_balance
            delete_oracle_mode()
            delete_manual_mode()
            # Reset slider back to NEW config defaults (recompute from saved config)
            if weight_slider_ref is not None:
                new_wA = _to_int(sch_weightA.value, 50)
                new_wB = _to_int(sch_weightB.value, 50)
                new_total = new_wA + new_wB
                if new_total > 0:
                    _cfg["slider_default"] = round((new_wA / new_total) * 100 / 5) * 5
                    _cfg["slider_default"] = max(5, min(95, _cfg["slider_default"]))
                else:
                    _cfg["slider_default"] = 50
                weight_slider_ref.value = _cfg["slider_default"]
                _update_weight_display()

            ok, msg = restart_dpmpv2()


            lbl_cfg.text = f"[{now_utc()}] saved; {msg}"
            ui.notify("saved + restarted" if ok else f"saved; restart failed: {msg}",
                    type=("positive" if ok else "warning"))

        btn_reload.on("click", lambda: reload_cfg())
        btn_apply.on("click", lambda: apply_cfg())

        # initial populate
        reload_cfg()


    with ui.tab_panel(t_logs):
        ui.label("Logs").classes("text-lg font-semibold")

        with ui.row().classes("items-center gap-3"):
            inp_filter = ui.input("filter contains...").classes("w-64").tooltip("Show only log lines containing this text")
            chk_freeze = ui.checkbox("freeze").tooltip("Stop auto-refreshing logs")
            #btn_jump   = ui.button("jump to end", icon="south")
            lbl_logs   = ui.label("").classes("text-xs text-gray-500")

        with ui.row().classes("items-center gap-3"):
            chk_redact = ui.checkbox("Redact Wallet Addresses").tooltip(
                "Replace BTC/BCH/BSV/DGB/XEC/PPC wallet addresses with [REDACTED] before downloading")
            btn_download = ui.button("Download Log (.zip)", icon="download").props("outline dense")

        def _redact_wallets(text: str) -> str:
            """Replace cryptocurrency wallet addresses with [REDACTED].

            Patterns matched:
              - BTC bech32:     bc1q... / bc1p...  (42-62 chars)
              - BCH cashaddr:   bitcoincash:q... / bitcoincash:p...
              - BCH short:      q + 41 hex chars  (common in logs)
              - XEC cashaddr:   ecash:q... / ecash:p...
              - DGB bech32:     dgb1q... (42-62 chars)
              - DGB legacy:     D + 25-34 base58 chars
              - PPC legacy:     P + 25-34 base58 chars
              - Legacy P2PKH:   1 + 25-34 base58 chars (BTC/BSV)
              - Legacy P2SH:    3 + 25-34 base58 chars (BTC/BSV)
            """
            # BTC bech32 (mainnet)
            text = re.sub(r'\bbc1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{38,58}\b', '[REDACTED]', text)
            # BCH cashaddr (with prefix)
            text = re.sub(r'\bbitcoincash:[qp][a-z0-9]{41,}\b', '[REDACTED]', text)
            # BCH short cashaddr (no prefix ... starts with q or p + 41 alnum)
            text = re.sub(r'\b[qp][a-z0-9]{41,55}\b', '[REDACTED]', text)
            # XEC cashaddr (with prefix)
            text = re.sub(r'\becash:[qp][a-z0-9]{41,}\b', '[REDACTED]', text)
            # DGB bech32
            text = re.sub(r'\bdgb1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{38,58}\b', '[REDACTED]', text)
            # DGB legacy (D...)
            text = re.sub(r'\bD[a-km-zA-HJ-NP-Z1-9]{25,34}\b', '[REDACTED]', text)
            # PPC legacy (P...)
            text = re.sub(r'\bP[a-km-zA-HJ-NP-Z1-9]{25,34}\b', '[REDACTED]', text)
            # Legacy addresses (1... or 3...) -- covers BTC, BSV
            text = re.sub(r'\b[13][a-km-zA-HJ-NP-Z1-9]{25,34}\b', '[REDACTED]', text)
            return text

        def _prepare_log_zip(redact: bool) -> tuple:
            """Heavy work: read log, optionally redact, zip. Runs in background thread."""
            log_text = read_text_file(DPMP_LOG_PATH, max_bytes=100_000_000)
            if redact:
                log_text = _redact_wallets(log_text)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("dpmpv2_run.log", log_text)
            buf.seek(0)
            return buf.getvalue()

        async def _do_download():
            """Read the full log, optionally redact wallets, zip it, trigger browser download."""
            try:
                btn_download.disable()
                ui.notify("Preparing log file...", type="info")
                redact = chk_redact.value
                zip_bytes = await asyncio.to_thread(_prepare_log_zip, redact)

                ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
                filename = f"dpmpv2_log_{ts}.zip"

                ui.download(zip_bytes, filename=filename, media_type="application/zip")
                ui.notify(f"Downloading {filename} ({len(zip_bytes)//1024} KB)", type="positive")
            except Exception as e:
                ui.notify(f"Download failed: {e}", type="negative")
            finally:
                btn_download.enable()

        btn_download.on_click(_do_download)

        log_box = ui.textarea(value="").props("rows=24 spellcheck=false wrap=off").classes("w-full font-mono")

        def apply_ui_state():
            state.log_filter = inp_filter.value or ""
            state.freeze_logs = bool(chk_freeze.value)

        inp_filter.on("change", lambda: apply_ui_state())
        chk_freeze.on("change", lambda: apply_ui_state())

        def jump_end():
            # just forces a refresh next tick
            state.last_log_len = 0

        #btn_jump.on("click", lambda: jump_end())

        async def refresh_logs_loop():
            while True:
                try:                    
                    state.freeze_logs = bool(chk_freeze.value)
                    if not state.freeze_logs:
                        txt = read_text_file(DPMP_LOG_PATH, max_bytes=180_000)

                        # newest-first
                        lines = txt.splitlines()
                        flt = (state.log_filter or "").strip()
                        if flt:
                            lines = [ln for ln in reversed(lines) if flt in ln]
                        else:
                            lines = list(reversed(lines))

                        log_box.value = "\n".join(lines)
                        lbl_logs.text = f"{now_utc()}  file={DPMP_LOG_PATH}"
                except Exception as e:
                    lbl_logs.text = f"log error: {e}"
                await asyncio.sleep(POLL_S)

        ui.timer(0.2, lambda: asyncio.create_task(refresh_logs_loop()), once=True)

    with ui.tab_panel(t_about):
        ABOUT_PATH = os.path.join(os.path.dirname(__file__), "about.html")

        try:
            html = read_text_file(ABOUT_PATH, max_bytes=400_000)
            if not (html or "").strip():
                html = "<p><i>(about.html is empty)</i></p>"
        except Exception as e:
            html = f"<p><b>Failed to load:</b> {ABOUT_PATH}</p><p><code>{e}</code></p>"

        ui.html(f'<div class="about-content">{html}</div>', sanitize=False).classes("w-full")

ui.run(host=HOST, port=PORT, title="DPMP Dashboard", reload=False, show=False)
