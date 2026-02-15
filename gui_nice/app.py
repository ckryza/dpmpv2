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

# write JSON file atomically
def write_json_atomic(path: str, obj: Dict[str, Any]) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)

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

            _time.sleep(0.3)

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

today = date.today()

# we are storing the icon in static/ to avoid issues with relative paths
app.add_static_files('/static', 'gui_nice/static')

# hide certain elements on small screens
with ui.row().classes("gap-4 items-center h-10 w-full"):      
    ui.image("/static/icond.png").classes("hide-on-mobile w-12 h-12 mb-0").style('fit: fill') # - hide this on small screens
    ui.label(f"Dual Pool Mining Proxy (DPMP)").classes("text-xl font-bold").style('color: #6E93D6')
    ui.space().classes("hide-on-mobile") # hide this on small screens
    ui.label(f"{today.strftime('%Y-%m-%d')}").classes("hide-on-mobile text-xs ").style('color: #6E93D6') # hide this on small screens
ui.separator().classes("hide-on-mobile") # hide this on small screens

# Tabs definition
with ui.tabs().classes("w-full") as tabs:
    t_home = ui.tab("Home")
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
                    lbl_restart = ui.label("").classes("text-sm")

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

            # Determine chain validity: oracle requires exactly one BTC + one BCH pool
            _chain_a = ab_cfg["poolA_chain"]
            _chain_b = ab_cfg["poolB_chain"]
            _chain_valid = sorted([_chain_a, _chain_b]) == ["BCH", "BTC"]

            # Determine initial mode:
            #   - If chain config is invalid -> always slider, no switch button
            #   - If oracle_mode.json exists -> use its value
            #   - Otherwise -> use config auto_balance
            _oracle_mode_file = read_oracle_mode()  # True/False/None
            if not _chain_valid:
                _show_oracle = False
            elif _oracle_mode_file is not None:
                _show_oracle = _oracle_mode_file
            else:
                _show_oracle = _auto_balance_enabled

            # Shared mutable state for mode switching
            _mode = {"oracle_active": _show_oracle}

            # ---- SLIDER PANEL (always built) ----
            weight_slider_ref = None

            # Only build the slider interaction if BOTH pools have weight > 0
            _slider_usable = (cfg_wA > 0 and cfg_wB > 0)

            with ui.card().classes("flex-1 min-w-[320px] max-w-[480px]") as slider_card:

                with ui.row().classes("w-full items-center justify-between"):
                    with ui.row().classes("items-center gap-1"):
                        ui.icon("balance", size="sm").style("color: #6E93D6")
                        ui.label("Hashrate Allocation").classes("text-base font-semibold").style("color: #6E93D6")

                    # Switch button: only shown when chain config is valid
                    if _chain_valid:
                        btn_switch_to_oracle = ui.button("Switch to Oracle", icon="swap_horiz").props("dense outline size=sm").classes("text-xs")

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
            slider_card.visible = not _show_oracle

            # ---- ORACLE PANEL (always built when chain config is valid) ----
            _oracle_ui = {}  # holds references to oracle UI elements
            _oracle_charts = []

            if _chain_valid:
                _chain_left = ab_cfg["poolA_chain"]   # e.g. "BCH"
                _chain_right = ab_cfg["poolB_chain"]   # e.g. "BTC"

                with ui.card().classes("flex-1 min-w-[280px] max-w-[540px]") as oracle_card:

                    with ui.row().classes("w-full items-center justify-between"):
                        with ui.row().classes("items-center gap-1"):
                            ui.icon("auto_graph", size="sm").style("color: #6E93D6")
                            ui.label("Oracle Auto-Balance").classes("text-base font-semibold").style("color: #6E93D6")

                        with ui.row().classes("items-center gap-2"):
                            # Health indicator
                            with ui.row().classes("items-center gap-1"):
                                oracle_health_dot = ui.icon("circle", size="xs")
                                oracle_health_lbl = ui.label("starting...").classes("text-xs")
                            # Switch button
                            btn_switch_to_slider = ui.button("Switch to Slider", icon="swap_horiz").props("dense outline size=sm").classes("text-xs")
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
                oracle_card.visible = _show_oracle

                _oracle_charts = [_oracle_ui.get("chart_left"), _oracle_ui.get("chart_right")]
            else:
                oracle_card = None  # no oracle panel when chain config is invalid

            # ---- SWITCH BUTTON HANDLERS ----
            def _do_switch_to_oracle():
                """User clicked 'Switch to Oracle' on the slider panel."""
                _mode["oracle_active"] = True
                write_oracle_mode(True)
                slider_card.visible = False
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

            def _do_switch_to_slider():
                """User clicked 'Switch to Slider' on the oracle panel."""
                _mode["oracle_active"] = False
                write_oracle_mode(False)
                if oracle_card is not None:
                    oracle_card.visible = False
                slider_card.visible = True
                # Write the current slider position to weights_override.json
                # so DPMP immediately picks up the slider's weights
                if weight_slider_ref is not None:
                    val = int(weight_slider_ref.value)
                    write_weight_override(val, 100 - val)
                ui.notify("Switched to Slider mode", type="info")

            if _chain_valid:
                btn_switch_to_oracle.on_click(_do_switch_to_oracle)
                btn_switch_to_slider.on_click(_do_switch_to_slider)

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

                # If we restored history, pre-populate the charts immediately
                if _restored_history:
                    n_real = len(_restored_history)
                    labels = [h["time_label"] for h in _restored_history]
                    if n_real < _CHART_MAX_POINTS:
                        # Project future timestamps using epoch if available
                        last_h = _restored_history[-1]
                        last_epoch = last_h.get("epoch_s", time.time())
                        poll_s = max(60, _oracle_poll_interval)
                        for i in range(1, _CHART_MAX_POINTS - n_real + 1):
                            proj_epoch = last_epoch + (i * poll_s)
                            labels.append(_utc_epoch_to_local_hhmm(proj_epoch))

                    left_short_data = [h["left_short"] for h in _restored_history] + [None] * (_CHART_MAX_POINTS - n_real)
                    left_long_data = [h["left_long"] for h in _restored_history] + [None] * (_CHART_MAX_POINTS - n_real)
                    right_short_data = [h["right_short"] for h in _restored_history] + [None] * (_CHART_MAX_POINTS - n_real)
                    right_long_data = [h["right_long"] for h in _restored_history] + [None] * (_CHART_MAX_POINTS - n_real)

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

                    # Seed detection state so the first poll cycle doesn't add a duplicate.
                    # Read current Prometheus values to properly initialize both methods.
                    last_pt = _restored_history[-1]
                    _oracle_state["last_hashrates"] = (
                        last_pt["left_short"], last_pt["left_long"],
                        last_pt["right_short"], last_pt["right_long"],
                    )
                    try:
                        _init_raw = http_get_text(METRICS_URL)
                        _init_age = _prom_gauge_value(_init_raw, "dpmp_oracle_data_age_seconds")
                        if _init_age is not None:
                            _oracle_state["last_data_age"] = _init_age
                    except Exception:
                        pass

                    # Restore "Last updated" label from the most recent point
                    # Use the saved epoch_s if available, otherwise show the time_label
                    if "epoch_s" in last_pt:
                        _oracle_ui["countdown_lbl"].text = f"Last updated: {_utc_epoch_to_local_hhmmss(last_pt['epoch_s'])}"
                    else:
                        _oracle_ui["countdown_lbl"].text = f"Last updated: {last_pt['time_label']}"

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
                        # 2. Warming up (DPMP running but oracle hasn't polled yet --” all gauges zero)
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

                            # Build x-axis labels: real times + projected future times
                            n_real = len(history)
                            labels = [h["time_label"] for h in history]
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
            slider_card.visible = not _new_show_oracle
            if oracle_card is not None:
                oracle_card.visible = _new_show_oracle


            ok, msg = restart_dpmpv2()
            lbl_restart.text = f"[{now_utc()}] {msg}"
            if ok:
                ui.notify("DPMP restarted", type="positive")
            else:
                ui.notify(f"restart failed: {msg}", type="negative")

        btn_restart.on_click(do_restart)

        ui.separator()


        lbl_status = ui.label("Status").classes("text-lg font-semibold").style('color: blue;')

        
        with ui.row().classes("gap-6 items-center"):            
            lbl_dpmp = ui.html("<b>DPMP</b>: checking...", sanitize=False).classes("text-sm")
            lbl_pool = ui.html("Active pool: ...", sanitize=False).classes("text-sm").tooltip("Which pool is currently active")
            lbl_miner = ui.html("<b>Miner(s) connected</b>: ...", sanitize=False).classes("text-sm").tooltip("Whether any miners are currently connected downstream")
            lbl_spin = ui.spinner('rings', size='lg', color='green')

        with ui.row().classes("gap-6 items-center"):
            lbl_acc = ui.html("<b>Accepted</b>: A ... / B ...", sanitize=False).classes("text-sm").tooltip("Total accepted shares per pool")
            lbl_rej = ui.html("<b>Rejected</b>: A ... / B ...", sanitize=False).classes("text-sm").tooltip("Total rejected shares per pool")
            lbl_jobs = ui.html("<b>Jobs</b>: A ... / B ...", sanitize=False).classes("text-sm").tooltip("Total jobs forwarded per pool")
            lbl_dif = ui.html("<b>SumDiff</b>: A ... / B ...", sanitize=False).classes("text-sm").tooltip("Sum of difficulty of accepted shares per pool")
            lbl_rat = ui.html("<b>Diff Ratio</b>: A ...% / B ...%", sanitize=False).classes("text-sm").tooltip("Percentage of accepted difficulty per pool (all-time since last restart)")

        with ui.row().classes("gap-6 items-center"):
            lbl_recent_rat = ui.html("<b>Recent Ratio (2min)</b>: waiting for data...", sanitize=False).classes("text-sm font-semibold").tooltip("Rolling 2-minute hashrate allocation ratio ... reacts to slider changes within minutes").style("color: #6E93D6")
            

        ui.separator()
        lbl_note = ui.html("<b>Note</b>: The <b>Recent Ratio</b> above is the best indicator for real-time hashrate allocation as it shows what DPMP is doing <i>right now</i>. The all-time <i>Diff Ratio</i> reflects cumulative history since the last restart and may take a long time to shift after a weight change. See the <b>About</b> tab for more details.", sanitize=False).classes("text-sm")

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

                lbl_acc.content = f"<b>Accepted</b>: A {int(accA)} / B {int(accB)}"
                lbl_rej.content = f"<b>Rejected</b>: A {int(rejA)} / B {int(rejB)} ({rejpA:.2f}% / {rejpB:.2f}%)"
                lbl_jobs.content = f"<b>Jobs</b>: A {int(jobA)} / B {int(jobB)}"
                #lbl_dif.content = f"<b>SumDiff</b>: A {int(difA)} / B {int(difB)}"
                lbl_dif.content = f"<b>SumDiff</b>: A {_fmt_short(difA)} / B {_fmt_short(difB)}"
                lbl_rat.content = f"<b>Diff Ratio (all-time)</b>: A {pctA:.2f}% / B {pctB:.2f}%"

                # Rolling 2-minute ratio 
                now_mono = time.monotonic()
                _recent_dif_history.append((now_mono, difA, difB))

                # Trim entries older than the window
                cutoff = now_mono - _RECENT_WINDOW_S
                while _recent_dif_history and _recent_dif_history[0][0] < cutoff:
                    _recent_dif_history.pop(0)

                if len(_recent_dif_history) >= 2:
                    oldest_ts, oldest_A, oldest_B = _recent_dif_history[0]
                    delta_A = difA - oldest_A
                    delta_B = difB - oldest_B
                    delta_total = delta_A + delta_B
                    if delta_total > 0:
                        rpctA = 100.0 * delta_A / delta_total
                        rpctB = 100.0 * delta_B / delta_total
                        window_s = now_mono - oldest_ts
                        lbl_recent_rat.content = (
                            f'<b>Recent Ratio ({int(window_s)}s)</b>: '
                            f'<span style="color:#22d3ee">A {rpctA:.1f}%</span>'
                            f' / '
                            f'<span style="color:#f59e0b">B {rpctB:.1f}%</span>'
                        )
                    else:
                        lbl_recent_rat.content = "<b>Recent Ratio (2min)</b>: no new shares yet..."
                else:
                    lbl_recent_rat.content = "<b>Recent Ratio (2min)</b>: collecting data..."

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
            
    with ui.tab_panel(t_cfg):
        ui.label("DPMP Configuration").classes("text-lg font-semibold")

        # list of events that we generally do NOT want to log (default deny list)
        DEFAULT_DENY = [
            "authorize_rewrite","authorize_rewrite_other","authorize_rewrite_secondary",
            "bootstrap_reconnect_forced","bootstrap_skipped_handshake_pool",
            "downstream_extranonce_check","downstream_extranonce_skip_already_in_subscribe",
            "downstream_extranonce_skip_no_data",
            "downstream_extranonce_skip_nochange","downstream_extranonce_skip_raw_subscribe",
            "downstream_extranonce_set","downstream_diff_set",
            "downstream_notify_flushed_after_subscribe",
            "downstream_send_diff","downstream_send_extranonce","downstream_send_notify",
            "downstream_send_raw","downstream_subscribe_forwarded_raw","downstream_tx",
            "handshake_response_dropped",
            "id_response_seen",
            "job_forwarded","job_forwarded_diff_state",
            "miner_method",
            "notify_clean_forced",
            "oracle_calc_result","oracle_data_age","oracle_mode_slider","oracle_next_poll",
            "oracle_override_written","oracle_poll_start","oracle_weights_applied",
            "pool_notify",
            "post_auth_downstream_sync","post_auth_extranonce_skip_already_in_subscribe",
            "post_auth_extranonce_skip_raw_subscribe",
            "post_auth_push_diff","post_auth_push_extranonce",
            "post_auth_push_notify_clean",
            "prune_internal_ids","prune_job_owner","prune_seen_upstream_ids","prune_submit_owner",
            "scheduler_tick",
            "send_upstream_flush_done","send_upstream_flush_start","send_upstream_queued",
            "share_result",
            "submit_local_sanity","submit_route","submit_snapshot",
            "subscribe_id_response_skipped_duplicate","subscribe_result",
            "upstream_response_dup_observed","upstream_tx",
            "weights_override_changed",
        ]

        # --- all log events (canonical list; keep in sync with dpmpv2.py log("...") calls) ---
        ALL_EVENTS = [
            "auth_result","authorize_rewrite","authorize_rewrite_other","authorize_rewrite_other_error",
            "authorize_rewrite_secondary","authorize_secondary_send_error","authorize_skip_zero_weight_pool",
            "bootstrap_reconnect_forced","bootstrap_skipped_handshake_pool",
            "clear_pool_state_reset_last_downstream_extranonce","clear_pool_state_reset_raw_subscribe_flag",
            "config_loaded","config_safety_max_deviation_clamped","config_safety_min_switch_clamped",
            "config_safety_oracle_poll_clamped","config_safety_slice_clamped",
            "configure_forward_both_error","configure_forwarded_both_pools","configure_skip_zero_weight_pool",
            "downstream_diff_set","downstream_extranonce_check","downstream_extranonce_send_error",
            "downstream_extranonce_set","downstream_extranonce_skip_already_in_subscribe",
            "downstream_extranonce_skip_no_data",
            "downstream_extranonce_skip_nochange","downstream_extranonce_skip_raw_subscribe",
            "downstream_notify_flushed_after_subscribe","downstream_send_diff","downstream_send_extranonce",
            "downstream_send_extranonce_error","downstream_send_notify","downstream_send_raw",
            "downstream_subscribe_forwarded_raw","downstream_tx",
            "dpmp_listening",
            "failover_emergency_switch","failover_weight_override","fatal_crash",
            "handshake_response_dropped",
            "id_response_seen",
            "job_forwarded","job_forwarded_diff_state",
            "metrics_start_failed","metrics_started","miner_bad_json","miner_connected",
            "miner_disconnect_for_reconnect","miner_disconnect_for_reconnect_failed",
            "miner_disconnected","miner_method","miner_ready_for_jobs",
            "notify_clean_force_error","notify_clean_forced",
            "oracle_bad_timestamps","oracle_calc_result","oracle_cancelled","oracle_config",
            "oracle_data_age","oracle_disabled","oracle_disabled_bad_chain_config",
            "oracle_disabled_invalid_chains",
            "oracle_fallback_50_50","oracle_mode_file_deleted_on_startup","oracle_mode_slider",
            "oracle_mode_switch_to_oracle","oracle_mode_switch_to_slider",
            "oracle_next_poll","oracle_override_write_error",
            "oracle_override_written","oracle_poll_error","oracle_poll_start",
            "oracle_starting","oracle_startup_delay","oracle_task_cancelled",
            "oracle_task_started","oracle_ts_parse_warning","oracle_weights_applied",
            "pool_bootstrap_auth_result","pool_bootstrap_authorize_sent","pool_bootstrap_error",
            "pool_bootstrap_subscribe_parse_error","pool_bootstrap_subscribe_result",
            "pool_bootstrap_subscribe_sent","pool_connected","pool_connecting","pool_diff","pool_down",
            "pool_initial_connect_failed","pool_notify","pool_reader_error","pool_reconnect_failed",
            "pool_reconnect_wait","pool_reconnected","pool_skipped_zero_weight","pool_state_cleared",
            "pool_switched",
            "post_auth_downstream_sync","post_auth_downstream_sync_error",
            "post_auth_extranonce_skip_already_in_subscribe","post_auth_extranonce_skip_raw_subscribe",
            "post_auth_push_diff",
            "post_auth_push_extranonce","post_auth_push_notify_clean","post_auth_push_notify_clean_error",
            "post_auth_push_setup_error","process_exiting",
            "prune_internal_ids","prune_job_owner","prune_seen_upstream_ids","prune_submit_owner",
            "resend_notify_clean","resend_notify_error","resend_notify_raw",
            "resend_notify_skipped_no_cached",
            "scheduler_config_validated","scheduler_tick",
            "send_upstream_flush_done","send_upstream_flush_start","send_upstream_queued",
            "session_error","share_result",
            "shutdown_begin","shutdown_cancel_tasks","shutdown_done","shutdown_keyboard_interrupt",
            "shutdown_serve_task_cancel_begin","shutdown_serve_task_cancel_done",
            "shutdown_serve_task_cancel_timeout","shutdown_serve_task_error",
            "shutdown_server_close_begin","shutdown_server_close_done","shutdown_server_close_error",
            "shutdown_server_close_timeout","shutdown_signal","shutdown_timeout",
            "submit_dedupe_error","submit_dropped_duplicate_fp","submit_dropped_extranonce_mismatch",
            "submit_dropped_no_job_yet","submit_dropped_pool_dead","submit_dropped_unknown_jid",
            "submit_extranonce_mismatch_grace_forward","submit_local_sanity","submit_local_sanity_error",
            "submit_route","submit_snapshot",
            "subscribe_id_response_skipped_duplicate","subscribe_parse_error","subscribe_result",
            "switch_skipped_no_cached_job",
            "upstream_response_dup_observed","upstream_tx",
            "weights_normalized","weights_override_changed","write_failed",
        ]

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

        # Logging (checkbox per event; deny[] only; allow[] left empty)
        with ui.expansion("Logging Settings:", icon="settings").classes("w-full"):
            ui.label("Check the events that you want to log. Certain events, while useful for debugging purposes, can generate a lot of log output very quickly. When in doubt, just click on the Reset to Defaults button to return to standard 'maintenance-mode' logging.").classes("text-sm")
            ui.label("Warning: Logging all events can create a very large log file quickly.").classes("text-sm text-red-600")

            logging_event_cbs = {}  # event -> checkbox

            if not ALL_EVENTS:
                ui.label("No log events list available.").classes("text-sm text-orange-700")
            else:
                with ui.row().classes("items-center gap-2"):
                    btn_all  = ui.button("Check All").props("dense outline").classes("text-xs")
                    btn_none = ui.button("Uncheck All").props("dense outline").classes("text-xs")
                    btn_reset = ui.button("Reset to Defaults").props("dense outline").classes("text-xs")

                cols = 3
                rows = (len(ALL_EVENTS) + cols - 1) // cols

                # on desktop show 3 columns; on mobile, stack into 1 column
                with ui.element('div').classes('grid grid-cols-1 sm:grid-cols-3 w-full gap-4 sm:gap-6'):
                    for c in range(cols):
                        with ui.column().classes("min-w-0"):
                            for r in range(rows):
                                idx = c * rows + r
                                if idx >= len(ALL_EVENTS):
                                    break
                                ev = ALL_EVENTS[idx]
                                logging_event_cbs[ev] = ui.checkbox(ev, value=True).classes("text-xs sm:text-sm")

                def _set_all_events(val: bool):
                    for cb in logging_event_cbs.values():
                        cb.value = bool(val)

                def _reset_defaults():
                    deny = set(DEFAULT_DENY)
                    for ev, cb in logging_event_cbs.items():
                        cb.value = (ev not in deny)                        

                btn_all.on("click", lambda: _set_all_events(True))
                btn_none.on("click", lambda: _set_all_events(False))
                btn_reset.on("click", lambda: _reset_defaults())


        # Metrics
        with ui.expansion("Metrics Settings:", icon="settings").classes("w-full").tooltip("Prometheus metrics listener settings"):
            metrics_host    = ui.input("Host").classes("w-64")
            metrics_port    = ui.number("Port", precision=0).props("step=1 min=1 max=65535").classes("w-64")
            metrics_enabled = ui.checkbox("Enabled")

        # Pool A
        with ui.expansion("Pool A Settings:", icon="settings").classes("w-full").tooltip("Settings for Pool A"):
            poolA_host   = ui.input("Host").classes("w-full")
            poolA_name   = ui.input("Name").classes("w-64")
            poolA_port   = ui.number("Port", precision=0).props("step=1 min=1 max=65535").classes("w-64")
            poolA_wallet = ui.input("Wallet").classes("w-full")
            poolA_chain  = ui.select(["BTC", "BCH", "None"], value="BTC", label="Chain").classes("w-64").tooltip(
                "Which blockchain this pool mines. Set to 'None' if not applicable. "
                "Required for Auto-Balance oracle to map weights correctly.")

        # Pool B
        with ui.expansion("Pool B Settings:", icon="settings").classes("w-full").tooltip("Settings for Pool B"):
            poolB_host   = ui.input("Host").classes("w-full")
            poolB_name   = ui.input("Name").classes("w-64")
            poolB_port   = ui.number("Port", precision=0).props("step=1 min=1 max=65535").classes("w-64")
            poolB_wallet = ui.input("Wallet").classes("w-full")
            poolB_chain  = ui.select(["BTC", "BCH", "None"], value="BCH", label="Chain").classes("w-64").tooltip(
                "Which blockchain this pool mines. Set to 'None' if not applicable. "
                "Required for Auto-Balance oracle to map weights correctly.")

        # Scheduler
        with ui.expansion("Scheduler Settings:", icon="settings").classes("w-full").tooltip("Settings for the dual-pool scheduler"):
            sch_min_switch = ui.number("Min Switch Seconds", precision=0).props("step=1 min=25 max=300").classes("w-64").tooltip("Minimum time before switching pools. Recommend between 30 seconds and 60 seconds.")
            sch_slice      = ui.number("Slice Seconds",      precision=0).props("step=1 min=1 max=120").classes("w-64").tooltip("Duration of each mining slice before switching. Recommend you use ~60% of Min Switch Seconds.")

            # Visual separator to avoid accidentally editing weights when changing timing fields
            ui.separator().classes("my-2")
            ui.label("Pool Weights").classes("text-sm font-semibold").style("color: #6E93D6")
            sch_weightA    = ui.number("Pool A Weight",      precision=0).props("step=5 min=0 max=100").classes("w-64").tooltip("Weighting for Pool A in the scheduler. Values are relative (e.g. 50/50 = same as 1/1).")
            sch_weightB    = ui.number("Pool B Weight",      precision=0).props("step=5 min=0 max=100").classes("w-64").tooltip("Weighting for Pool B in the scheduler. Values are relative (e.g. 50/50 = same as 1/1).")
            ui.separator().classes("my-2")

            # Oracle Auto-Balance settings
            ui.label("Oracle Auto-Balance").classes("text-sm font-semibold").style("color: #6E93D6")
            sch_auto_balance = ui.checkbox("Enable Auto-Balance").tooltip(
                "When enabled, the oracle automatically adjusts Pool A/B weights based on real-time "
                "BTC and BCH network hashrate. Manual weights and the slider are ignored. "
                "Requires Pool A and Pool B to have different chain assignments (one BTC, one BCH).")
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
            cfg["logging"].setdefault("allow", [])  # we keep this empty by design
            cfg["logging"].setdefault("deny", [])
            cfg["logging"].setdefault("json", True)
            cfg["logging"].setdefault("level", "INFO")

        def _apply_logging_checkboxes(cfg: dict) -> None:
            _ensure_logging_defaults(cfg)
            deny = []
            for ev, cb in logging_event_cbs.items():
                try:
                    if not bool(cb.value):
                        deny.append(ev)
                except Exception:
                    deny.append(ev)
            deny.sort()
            cfg["logging"]["allow"] = []   # explicit: leave empty
            cfg["logging"]["deny"] = deny  # unchecked => denied

        def _set_checkboxes_from_cfg(cfg: dict) -> None:
            deny = _safe_get(cfg, ["logging", "deny"], []) or []
            deny_set = set([str(x) for x in deny])
            for ev, cb in logging_event_cbs.items():
                cb.value = (ev not in deny_set)

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

            # logging (checkboxes -> from deny[])
            _ensure_logging_defaults(cfg)
            _set_checkboxes_from_cfg(cfg)

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
            _chain_map = {"BTC": "BTC", "BCH": "BCH", "NONE": "None"}
            poolA_chain.value = _chain_map.get(_raw_chainA, "BTC")

            # pools B
            poolB_host.value   = str(_safe_get(cfg, ["pools", "B", "host"], "") or "")
            poolB_name.value   = str(_safe_get(cfg, ["pools", "B", "name"], "") or "")
            poolB_port.value   = _to_int(_safe_get(cfg, ["pools", "B", "port"], 3333), 3333)
            poolB_wallet.value = str(_safe_get(cfg, ["pools", "B", "wallet"], "") or "")
            _raw_chainB = str(_safe_get(cfg, ["pools", "B", "chain"], "") or "").strip().upper()
            poolB_chain.value = _chain_map.get(_raw_chainB, "BCH")

            # scheduler
            sch_min_switch.value = _to_int(_safe_get(cfg, ["scheduler", "min_switch_seconds"], 30), 30)
            sch_slice.value      = _to_int(_safe_get(cfg, ["scheduler", "slice_seconds"], 30), 30)
            sch_weightA.value    = _to_int(_safe_get(cfg, ["scheduler", "poolA_weight"], 50), 50)
            sch_weightB.value    = _to_int(_safe_get(cfg, ["scheduler", "poolB_weight"], 50), 50)

            # oracle auto-balance
            sch_auto_balance.value  = bool(_safe_get(cfg, ["scheduler", "auto_balance"], False))
            sch_max_deviation.value = _to_int(_safe_get(cfg, ["scheduler", "auto_balance_max_deviation"], 20), 20)
            sch_oracle_url.value    = str(_safe_get(cfg, ["scheduler", "oracle_url"], "https://www.sr-analyst.com/dpmp/oracle.php") or "")
            sch_oracle_poll.value   = _to_int(_safe_get(cfg, ["scheduler", "oracle_poll_seconds"], 600), 600)

            lbl_cfg.text = f"[{now_utc()}] reloaded"
            ui.notify("config reloaded", type="positive")

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

            # logging (checkboxes -> deny[])
            _apply_logging_checkboxes(cfg)

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

            cfg["pools"].setdefault("B", {})
            cfg["pools"]["B"]["host"]   = str(poolB_host.value or "").strip()
            cfg["pools"]["B"]["name"]   = str(poolB_name.value or "").strip()
            cfg["pools"]["B"]["port"]   = _to_int(poolB_port.value, 2018)
            cfg["pools"]["B"]["wallet"] = str(poolB_wallet.value or "").strip()
            cfg["pools"]["B"]["chain"]  = str(poolB_chain.value or "BCH").strip().upper()

            # scheduler
            cfg.setdefault("scheduler", {})
            cfg["scheduler"]["min_switch_seconds"] = _to_int(sch_min_switch.value, 30)
            cfg["scheduler"]["slice_seconds"]      = _to_int(sch_slice.value, 30)
            cfg["scheduler"]["poolA_weight"]       = _to_int(sch_weightA.value, 50)
            cfg["scheduler"]["poolB_weight"]       = _to_int(sch_weightB.value, 50)

            # oracle auto-balance
            cfg["scheduler"]["auto_balance"]               = bool(sch_auto_balance.value)
            cfg["scheduler"]["auto_balance_max_deviation"]  = max(5, min(45, _to_int(sch_max_deviation.value, 20)))
            cfg["scheduler"]["oracle_url"]                 = str(sch_oracle_url.value or "").strip()
            cfg["scheduler"]["oracle_poll_seconds"]        = max(600, min(3600, _to_int(sch_oracle_poll.value, 600)))

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
                "Replace BTC/BCH wallet addresses with [REDACTED] before downloading")
            btn_download = ui.button("Download Log (.zip)", icon="download").props("outline dense")

        def _redact_wallets(text: str) -> str:
            """Replace BTC and BCH wallet addresses with [REDACTED].

            Patterns matched:
              - BTC bech32:  bc1q... / bc1p...  (42-62 chars)
              - BCH cashaddr: bitcoincash:q... / bitcoincash:p...
              - BCH short:    q + 41 hex chars  (common in logs)
              - Legacy P2PKH: 1 + 25-34 base58 chars
              - Legacy P2SH:  3 + 25-34 base58 chars
            """
            # BTC bech32 (mainnet)
            text = re.sub(r'\bbc1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{38,58}\b', '[REDACTED]', text)
            # BCH cashaddr (with prefix)
            text = re.sub(r'\bbitcoincash:[qp][a-z0-9]{41,}\b', '[REDACTED]', text)
            # BCH short cashaddr (no prefix ... starts with q or p + 41 alnum)
            text = re.sub(r'\b[qp][a-z0-9]{41,55}\b', '[REDACTED]', text)
            # Legacy addresses (1... or 3...)
            text = re.sub(r'\b[13][a-km-zA-HJ-NP-Z1-9]{25,34}\b', '[REDACTED]', text)
            return text

        def _do_download():
            """Read the full log, optionally redact wallets, zip it, trigger browser download."""
            try:
                # Read the FULL log (not truncated like the display)
                log_text = read_text_file(DPMP_LOG_PATH, max_bytes=100_000_000)  # up to ~100 MB

                if chk_redact.value:
                    log_text = _redact_wallets(log_text)

                # Build zip in memory
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    zf.writestr("dpmpv2_run.log", log_text)
                buf.seek(0)
                zip_bytes = buf.getvalue()

                # Generate filename with timestamp
                ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
                filename = f"dpmpv2_log_{ts}.zip"

                ui.download(zip_bytes, filename=filename, media_type="application/zip")
                ui.notify(f"Downloading {filename} ({len(zip_bytes)//1024} KB)", type="positive")
            except Exception as e:
                ui.notify(f"Download failed: {e}", type="negative")

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
