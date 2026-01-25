import asyncio
import json
import os
import subprocess
import time
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.request import urlopen, Request

from nicegui import ui

CONFIG_PATH = os.environ.get("DPMP_CONFIG_PATH", os.path.expanduser("~/dpmp/dpmp/config_v2.json"))
METRICS_URL  = os.environ.get("DPMP_METRICS_URL", "http://127.0.0.1:9210/metrics")
DPMP_LOG_PATH = os.environ.get("DPMP_LOG_PATH", os.path.expanduser("~/dpmp/dpmpv2_run.log"))
GUI_LOG_PATH  = os.environ.get("GUI_LOG_PATH", os.path.expanduser("~/dpmp/dpmpv2_gui.log"))

HOST = os.environ.get("NICEGUI_HOST", "0.0.0.0")
PORT = int(os.environ.get("NICEGUI_PORT", "8845"))
POLL_S = float(os.environ.get("NICEGUI_POLL_S", "2.0"))

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
</style>
""")


def now_utc() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())

import subprocess

def systemd_is_active(unit: str) -> bool:
    # returns True if systemd reports "active"
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

def prom_first_float(metrics: dict, name: str, labels: dict | None = None) -> float | None:
    """
    Expect your metrics parser to return something like:
      metrics[name] = list of {"labels": {...}, "value": float}
    Adjust this if your internal representation differs.
    """
    rows = metrics.get(name) or []
    if labels:
        for row in rows:
            if (row.get("labels") or {}) == labels:
                try:
                    return float(row.get("value"))
                except Exception:
                    return None
        return None
    # no label filter → first value
    try:
        return float(rows[0].get("value"))
    except Exception:
        return None



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


def read_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json_atomic(path: str, obj: Dict[str, Any]) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)


def http_get_text(url: str, timeout_s: float = 3.0) -> str:
    req = Request(url, headers={"User-Agent": "dpmpv2-nicegui"})
    try:
        with urlopen(req, timeout=timeout_s) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception:
        # dpmpv2 restarts will temporarily drop the metrics listener (Errno 111)
        return ""



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


def restart_dpmpv2() -> tuple[bool, str]:
    # Runs as the same user as the service, so --user is fine.
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


def load_state() -> AppState:
    try:
        obj = read_json(CONFIG_PATH)
        raw = json.dumps(obj, indent=2)
    except Exception as e:
        obj = {}
        raw = f"[error loading config] {e}"
    return AppState(config_obj=obj, config_raw=raw)


state = load_state()


ui.label(f"Dual Pool Mining Proxy (DPMP)").classes("text-xl font-bold").style('color: #6E93D6')
#ui.label("Tabs: Home, Config, Logs, About").classes("text-sm text-gray-500")

with ui.tabs().classes("w-full") as tabs:
    t_home = ui.tab("Home")
    t_cfg  = ui.tab("Config") 
    t_logs = ui.tab("Logs")
    t_about = ui.tab("About")

with ui.tab_panels(tabs, value=t_home).classes("w-full"):

    
    with ui.tab_panel(t_home):
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

        def do_restart():
            ok, msg = restart_dpmpv2()
            lbl_restart.text = f"[{now_utc()}] {msg}"
            if ok:
                ui.notify("DPMP restarted", type="positive")
            else:
                ui.notify(f"restart failed: {msg}", type="negative")

            btn_restart.on("click", lambda: do_restart())

        ui.separator()
        lbl_status = ui.label("Status").classes("text-lg font-semibold").style('color: blue;')

        with ui.row().classes("gap-6 items-center"):            
            lbl_dpmp = ui.html("<b>DPMP</b>: checking…", sanitize=False).classes("text-sm")
            lbl_pool = ui.html("Active pool: …", sanitize=False).classes("text-sm").tooltip("Which pool is currently active")
            lbl_miner = ui.html("<b>Miner(s) connected</b>: …", sanitize=False).classes("text-sm").tooltip("Whether any miners are currently connected downstream")

        with ui.row().classes("gap-6 items-center"):
            lbl_acc = ui.html("<b>Accepted</b>: A … / B …", sanitize=False).classes("text-sm").tooltip("Total accepted shares per pool")
            lbl_rej = ui.html("<b>Rejected</b>: A … / B …", sanitize=False).classes("text-sm").tooltip("Total rejected shares per pool")
            lbl_jobs = ui.html("<b>Jobs</b>: A … / B …", sanitize=False).classes("text-sm").tooltip("Total jobs forwarded per pool")
            lbl_dif = ui.html("<b>SumDiff</b>: A … / B …", sanitize=False).classes("text-sm").tooltip("Sum of difficulty of accepted shares per pool")
            lbl_rat = ui.html("<b>Diff Ratio</b>: A …% / B …%", sanitize=False).classes("text-sm").tooltip("Percentage of accepted difficulty per pool")

        ui.separator()
        lbl_note = ui.html("<b>Note</b>: The <i>SumDiff</i> and <i>Diff Ratio</i> metrics above are the best indicators for measuring proxy performance...over time the <i>Diff Ratio</i> values should converge toward the configured pool ratio in Scheduler Settings.", sanitize=False).classes("text-sm")

        def update_home_status() -> None:

            # 1) dpmpv2 systemd state (bare-metal). In Docker this will be unavailable.
            active = False
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
                ratioA = (difA / total_dif * 100.0) if total_dif > 0.0 else 0.0
                ratioB = (difB / total_dif * 100.0) if total_dif > 0.0 else 0.0
                pctA = 100*difA/(total_dif or 1)
                pctB = 100*difB/(total_dif or 1)

                lbl_acc.content = f"<b>Accepted</b>: A {int(accA)} / B {int(accB)}"
                lbl_rej.content = f"<b>Rejected</b>: A {int(rejA)} / B {int(rejB)}"
                lbl_jobs.content = f"<b>Jobs</b>: A {int(jobA)} / B {int(jobB)}"
                lbl_dif.content = f"<b>SumDiff</b>: A {int(difA)} / B {int(difB)}"
                lbl_rat.content = f"<b>Diff Ratio</b>: A {pctA:.2f}% / B {pctB:.2f}%"

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


        update_home_status()
        ui.timer(2.0, update_home_status)
            
    with ui.tab_panel(t_cfg):
        ui.label("DPMP Configuration").classes("text-lg font-semibold")

        # list of events that we generally do NOT want to log (default deny list)
        DEFAULT_DENY = [
            "submit_route",
            "share_result",
            "id_response_seen",
            "downstream_extranonce_skip_raw_subscribe",
            "downstream_extranonce_skip_nochange",
            "downstream_extranonce_set",
            "downstream_diff_set",
            "job_forwarded",
            "pool_notify",
            "notify_clean_forced",
        ]

        # --- all log events (canonical list; keep in sync with dpmpv2.py log("...") calls) ---
        ALL_EVENTS = [
            "auth_result","authorize_rewrite","authorize_rewrite_other","authorize_rewrite_other_error",
            "authorize_rewrite_secondary","authorize_secondary_send_error","config_loaded","configure_req",
            "downstream_diff_set","downstream_extranonce_send_error","downstream_extranonce_set",
            "downstream_extranonce_skip_nochange","downstream_extranonce_skip_raw_subscribe",
            "downstream_extranonce_suppressed_nonactive","downstream_notify_flushed_after_subscribe",
            "downstream_send_diff","downstream_send_extranonce","downstream_send_extranonce_error",
            "downstream_send_notify","downstream_send_raw","downstream_subscribe_forwarded_raw",
            "downstream_tx","handshake_response_dropped","id_response_seen","job_forwarded",
            "job_forwarded_diff_state","metrics_start_failed","metrics_started","miner_bad_json",
            "miner_connected","miner_disconnected","miner_method","miner_ready_for_jobs",
            "miner_rejected_extra_session","notify_clean_force_error","notify_clean_forced",
            "pool_bootstrap_auth_result","pool_bootstrap_authorize_sent","pool_bootstrap_error",
            "pool_bootstrap_subscribe_parse_error","pool_bootstrap_subscribe_result",
            "pool_bootstrap_subscribe_sent","pool_connected","pool_connecting","pool_diff","pool_notify",
            "pool_switched","post_auth_downstream_sync","post_auth_downstream_sync_error",
            "post_auth_push_diff","post_auth_push_extranonce","post_auth_push_notify_clean",
            "post_auth_push_notify_clean_error","post_auth_push_setup_error","resend_notify_clean",
            "resend_notify_error","resend_notify_raw","resend_notify_skipped_no_cached",
            "send_upstream_flush_done","send_upstream_flush_start","send_upstream_queued","session_error",
            "share_result","shutdown_begin","shutdown_cancel_tasks","shutdown_done",
            "shutdown_serve_task_cancel_begin","shutdown_serve_task_cancel_done",
            "shutdown_serve_task_cancel_timeout","shutdown_serve_task_error",
            "shutdown_server_close_begin","shutdown_server_close_done","shutdown_server_close_error",
            "shutdown_server_close_timeout","shutdown_signal","shutdown_timeout","submit_dedupe_error",
            "submit_dropped_duplicate_fp","submit_dropped_extranonce_mismatch","submit_dropped_no_job_yet",
            "submit_dropped_unknown_jid","submit_extranonce_mismatch_grace_forward","submit_local_sanity",
            "submit_local_sanity_error","submit_route","submit_snapshot",
            "subscribe_id_response_skipped_duplicate","subscribe_parse_error","subscribe_result",
            "switch_skipped_no_cached_job","upstream_response_dup_observed","upstream_tx",
            "weights_normalized","write_failed",
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
            ui.label("Check the events that you want to log. Certain events (such as share_result, id_reponse_seen, job_forwarded, etc.) can generate a lot of log output. When in doubt, just click on the Reset to Defaults button to uncheck all noisy events.").classes("text-sm")
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

                with ui.grid(columns=3).classes("w-full gap-6"):
                    for c in range(cols):
                        with ui.column().classes("min-w-0"):
                            for r in range(rows):
                                idx = c * rows + r
                                if idx >= len(ALL_EVENTS):
                                    break
                                ev = ALL_EVENTS[idx]
                                logging_event_cbs[ev] = ui.checkbox(ev, value=True).classes("text-sm")


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

        # Pool B
        with ui.expansion("Pool B Settings:", icon="settings").classes("w-full").tooltip("Settings for Pool B"):
            poolB_host   = ui.input("Host").classes("w-full")
            poolB_name   = ui.input("Name").classes("w-64")
            poolB_port   = ui.number("Port", precision=0).props("step=1 min=1 max=65535").classes("w-64")
            poolB_wallet = ui.input("Wallet").classes("w-full")

        # Scheduler
        with ui.expansion("Scheduler Settings:", icon="settings").classes("w-full").tooltip("Settings for the dual-pool scheduler"):
            sch_min_switch = ui.number("Min Switch Seconds", precision=0).props("step=1 min=0").classes("w-64").tooltip("Minimum time before switching pools. Recommend between 30 seconds and 60 seconds.")
            sch_slice      = ui.number("Slice Seconds",      precision=0).props("step=1 min=0").classes("w-64").tooltip("Duration of each mining slice before switching.")
            sch_weightA    = ui.number("Pool A Weight",      precision=0).props("step=1 min=0").classes("w-64").tooltip("Weighting for Pool A in the scheduler.")
            sch_weightB    = ui.number("Pool B Weight",      precision=0).props("step=1 min=0").classes("w-64").tooltip("Weighting for Pool B in the scheduler.")

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

            # pools B
            poolB_host.value   = str(_safe_get(cfg, ["pools", "B", "host"], "") or "")
            poolB_name.value   = str(_safe_get(cfg, ["pools", "B", "name"], "") or "")
            poolB_port.value   = _to_int(_safe_get(cfg, ["pools", "B", "port"], 3333), 3333)
            poolB_wallet.value = str(_safe_get(cfg, ["pools", "B", "wallet"], "") or "")

            # scheduler
            sch_min_switch.value = _to_int(_safe_get(cfg, ["scheduler", "min_switch_seconds"], 30), 30)
            sch_slice.value      = _to_int(_safe_get(cfg, ["scheduler", "slice_seconds"], 30), 30)
            sch_weightA.value    = _to_int(_safe_get(cfg, ["scheduler", "poolA_weight"], 50), 50)
            sch_weightB.value    = _to_int(_safe_get(cfg, ["scheduler", "poolB_weight"], 50), 50)

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

            cfg["pools"].setdefault("B", {})
            cfg["pools"]["B"]["host"]   = str(poolB_host.value or "").strip()
            cfg["pools"]["B"]["name"]   = str(poolB_name.value or "").strip()
            cfg["pools"]["B"]["port"]   = _to_int(poolB_port.value, 2018)
            cfg["pools"]["B"]["wallet"] = str(poolB_wallet.value or "").strip()

            # scheduler
            cfg.setdefault("scheduler", {})
            cfg["scheduler"]["min_switch_seconds"] = _to_int(sch_min_switch.value, 30)
            cfg["scheduler"]["slice_seconds"]      = _to_int(sch_slice.value, 30)
            cfg["scheduler"]["poolA_weight"]       = _to_int(sch_weightA.value, 50)
            cfg["scheduler"]["poolB_weight"]       = _to_int(sch_weightB.value, 50)
            cfg.setdefault("scheduler", {}).setdefault("mode", "ratio")  # preserve/ensure

            try:
                write_json_atomic(CONFIG_PATH, cfg)
            except Exception as e:
                ui.notify(f"write failed: {e}", type="negative")
                return

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
            inp_filter = ui.input("filter contains…").classes("w-64").tooltip("Show only log lines containing this text")
            chk_freeze = ui.checkbox("freeze").tooltip("Stop auto-refreshing logs")
            #btn_jump   = ui.button("jump to end", icon="south")
            lbl_logs   = ui.label("").classes("text-xs text-gray-500")

        log_box = ui.textarea(value="").props("rows=24 spellcheck=false").classes("w-full font-mono")

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


ui.run(host=HOST, port=PORT, title="dpmpv2 NiceGUI", reload=False, show=False)
