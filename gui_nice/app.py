import asyncio
import json
import os
import subprocess
import time
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


def now_utc() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())


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
    with urlopen(req, timeout=timeout_s) as r:
        return r.read().decode("utf-8", errors="replace")


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


ui.label(f"dpmpv2 NiceGUI ({now_utc()})").classes("text-xl font-bold")
ui.label("Tabs: Home, Config, Dashboard, Logs").classes("text-sm text-gray-500")

with ui.tabs().classes("w-full") as tabs:
    t_home = ui.tab("Home")
    t_cfg  = ui.tab("Config")
    t_dash = ui.tab("Dashboard")
    t_logs = ui.tab("Logs")

with ui.tab_panels(tabs, value=t_home).classes("w-full"):

    with ui.tab_panel(t_home):
        ui.markdown(
            f"""
**What this is:** dpmpv2 management UI (NiceGUI).

**Config:** `{CONFIG_PATH}`  
**Metrics:** `{METRICS_URL}`  
**DPMP log:** `{DPMP_LOG_PATH}`  
**GUI log:** `{GUI_LOG_PATH}`  
"""
        )
        with ui.row().classes("items-center gap-2"):
            btn_restart = ui.button("Restart dpmpv2", icon="restart_alt")
            lbl_restart = ui.label("").classes("text-sm")

        def do_restart():
            ok, msg = restart_dpmpv2()
            lbl_restart.text = f"[{now_utc()}] {msg}"
            if ok:
                ui.notify("dpmpv2 restarted", type="positive")
            else:
                ui.notify(f"restart failed: {msg}", type="negative")

        btn_restart.on("click", lambda: do_restart())

    with ui.tab_panel(t_cfg):
        ui.label("Config editor (raw JSON for now; typed controls come next)").classes("text-lg font-semibold")
        with ui.row().classes("items-center gap-2"):
            btn_reload = ui.button("Reload from disk", icon="refresh")
            btn_apply  = ui.button("Apply + Restart dpmpv2", icon="save")
            lbl_cfg = ui.label("").classes("text-sm")

        editor = ui.textarea(value=state.config_raw).props("rows=24 spellcheck=false").classes("w-full font-mono")

        def reload_cfg():
            global state
            state = load_state()
            editor.value = state.config_raw
            lbl_cfg.text = f"[{now_utc()}] reloaded"
            ui.notify("config reloaded", type="positive")

        def apply_cfg():
            raw = editor.value or ""
            try:
                obj = json.loads(raw)
            except Exception as e:
                ui.notify(f"invalid JSON: {e}", type="negative")
                return
            try:
                write_json_atomic(CONFIG_PATH, obj)
            except Exception as e:
                ui.notify(f"write failed: {e}", type="negative")
                return
            ok, msg = restart_dpmpv2()
            lbl_cfg.text = f"[{now_utc()}] saved; {msg}"
            ui.notify("saved + restarted" if ok else f"saved; restart failed: {msg}",
                      type=("positive" if ok else "warning"))

        btn_reload.on("click", lambda: reload_cfg())
        btn_apply.on("click", lambda: apply_cfg())

    with ui.tab_panel(t_dash):
        ui.label("Dashboard (basic stats; charts later)").classes("text-lg font-semibold")

        with ui.row().classes("gap-4"):
            card_conn = ui.card().classes("p-4")
            card_jobs = ui.card().classes("p-4")
            card_shrs = ui.card().classes("p-4")

        lbl_conn = ui.label("connections: ...").classes("text-sm").style("white-space: pre;")
        lbl_jobs = ui.label("jobs_forwarded: ...").classes("text-sm").style("white-space: pre;")
        lbl_shrs = ui.label("shares: ...").classes("text-sm").style("white-space: pre;")

        card_conn.clear()
        with card_conn:
            ui.label("Connections").classes("font-semibold")
            lbl_conn

        card_jobs.clear()
        with card_jobs:
            ui.label("Jobs Forwarded").classes("font-semibold")
            lbl_jobs

        card_shrs.clear()
        with card_shrs:
            ui.label("Shares").classes("font-semibold")
            lbl_shrs

        lbl_last = ui.label("").classes("text-xs text-gray-500")

        async def refresh_metrics_loop():
            while True:
                try:
                    txt = http_get_text(METRICS_URL, timeout_s=2.5)
                    state.last_metrics_raw = txt

                    down = prom_value(txt, "dpmp_downstream_connections") or 0.0
                    upA  = prom_value(txt, "dpmp_upstream_connections", {"pool": "A"}) or 0.0
                    upB  = prom_value(txt, "dpmp_upstream_connections", {"pool": "B"}) or 0.0

                    jA = prom_value(txt, "dpmp_jobs_forwarded_total", {"pool": "A"}) or 0.0
                    jB = prom_value(txt, "dpmp_jobs_forwarded_total", {"pool": "B"}) or 0.0

                    aA = prom_value(txt, "dpmp_shares_accepted_total", {"pool": "A"}) or 0.0
                    aB = prom_value(txt, "dpmp_shares_accepted_total", {"pool": "B"}) or 0.0
                    rA = prom_value(txt, "dpmp_shares_rejected_total", {"pool": "A"}) or 0.0
                    rB = prom_value(txt, "dpmp_shares_rejected_total", {"pool": "B"}) or 0.0

                    actA = prom_value(txt, "dpmp_active_pool", {"pool": "A"}) or 0.0
                    actB = prom_value(txt, "dpmp_active_pool", {"pool": "B"}) or 0.0

                    lbl_conn.text = f"downstream={int(down)}\nupstream A={int(upA)}\nupstream B={int(upB)}"
                    lbl_jobs.text = f"A={int(jA)}\nB={int(jB)}"
                    lbl_shrs.text = f"accepted A={int(aA)}  B={int(aB)}\nrejected A={int(rA)}  B={int(rB)}\nactive A={int(actA)}  B={int(actB)}"
                    lbl_last.text = f"last update: {now_utc()}"
                except Exception as e:
                    lbl_last.text = f"metrics error: {e}"
                await asyncio.sleep(POLL_S)

        ui.timer(0.2, lambda: asyncio.create_task(refresh_metrics_loop()), once=True)

        with ui.expansion("Raw /metrics (debug)", icon="terminal"):
            raw_box = ui.textarea(value="(waiting...)").props("rows=12 spellcheck=false").classes("w-full font-mono")
            def load_raw():
                raw_box.value = state.last_metrics_raw or "(empty)"
            ui.button("Refresh raw", on_click=load_raw)

    with ui.tab_panel(t_logs):
        ui.label("Logs (tail; filter; freeze)").classes("text-lg font-semibold")

        with ui.row().classes("items-center gap-3"):
            inp_filter = ui.input("filter contains…").classes("w-64")
            chk_freeze = ui.checkbox("freeze")
            btn_jump   = ui.button("jump to end", icon="south")
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

        btn_jump.on("click", lambda: jump_end())

        async def refresh_logs_loop():
            while True:
                try:
                    if not state.freeze_logs:
                        txt = read_text_file(DPMP_LOG_PATH, max_bytes=180_000)
                        flt = (state.log_filter or "").strip()
                        if flt:
                            lines = [ln for ln in txt.splitlines() if flt in ln]
                            txt = "\n".join(lines)
                        log_box.value = txt
                        lbl_logs.text = f"{now_utc()}  file={DPMP_LOG_PATH}"
                except Exception as e:
                    lbl_logs.text = f"log error: {e}"
                await asyncio.sleep(POLL_S)

        ui.timer(0.2, lambda: asyncio.create_task(refresh_logs_loop()), once=True)


ui.run(host=HOST, port=PORT, title="dpmpv2 NiceGUI", reload=False, show=False)
