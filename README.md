# DPMP v2 — Dual-Pool Mining Proxy

**DPMP v2** is a lightweight, high-reliability **Stratum v1 mining proxy** designed to sit between one or more miners and multiple upstream mining pools. 

It enables **deterministic dual-pool scheduling**, advanced difficulty/extranonce handling, and deep observability via Prometheus metrics — while remaining simple to deploy and operate.

This repository contains the **v2 architecture**, which is a ground-up redesign focused on correctness, robustness, and long-running stability.

---



## Quick Install (Non-Docker)

### Prerequisites (Non-Docker)
- Python 3.10+
- `python3-venv` package installed

```bash
sudo apt install -y python3-venv
```
Install:

```bash
git clone https://github.com/ckryza/dpmpv2.git ~/dpmp
cd ~/dpmp
./installer/install.sh
```

After install:
- **Stratum**: `:3351`
- **Metrics**: `:9210/metrics`
- **Dashboard**: `http://<dpmp-proxy-ip>:8855/`

### Dry-run (no changes)

You can preview what the installer would do without making any changes:

```bash
cd ~/dpmp
./installer/install.sh --check
```

### Upgrade
```bash
cd ~/dpmp
./installer/upgrade.sh
```
### Dry-run upgrade (no changes)

```bash
cd ~/dpmp
./installer/upgrade.sh --check
```

### Uninstall (keeps config/logs)
```bash
cd ~/dpmp
./installer/uninstall.sh
```

---

## Installer Behavior (Important)

The non-docker installer is intentionally conservative.

What `install.sh` **does**:
- Creates `~/dpmp` if missing
- Creates a Python virtualenv and installs dependencies
- Installs **systemd user services** (`dpmpv2.service`, `dpmpv2-nicegui.service`)
- Creates `dpmp/config_v2.json` from `config_v2_example.json` **only if missing**
- Enables linger and auto-starts services

What it **does NOT** do:
- Does not overwrite an existing config
- Does not touch wallets, pool credentials, or logs
- Does not require root

⚠️ **After first launch**:
Open the **DPMP** GUI interface `(e.g., browse to '<dpmp-proxy-ip>:8855/')`, click on the **Config** tab, and **update Pool A and Pool B settings** (host, port, name, wallet) before mining. Pay special attention to the wallet addresses and make sure the wallet address you add is correct for the pool you are adding it to.

## Error Reporting

If you encounter errors associated either with a particular miner or a particular pool:

- Please make sure that all events are being logged on the Config tab
- Click APPLY + RESTART to restart DPMP and recreate the error
- Click on the Download Log button on the Logs tab
- Send a description of the problem (i.e., miner-related or pool-related and identify the miner or pool) to dpmp.project@gmail.com and attach the log file.

We have tested DPMP with a number of different miners and pools but it is by no means an exhaustive list. The log data you provide will help us to expand the list of supported miners and pools.

## What DPMP v2 Does

At a high level, DPMP v2:

- Accepts Stratum connections from miners (acts like a pool)
- Maintains concurrent Stratum connections to **two upstream pools** (acts like a miner)
- Routes jobs and share submissions according to a **scheduler**
- Ensures correct propagation of:
  - difficulty
  - extranonce
  - job IDs
- Exposes detailed runtime metrics for monitoring and analysis
- Logs structured, machine-readable events for debugging and auditing

The proxy is intentionally **transparent**: miners and pools do not need to be modified or aware that DPMP is in the middle.

---

## Core Features

### 🔀 Dual-Pool Scheduling
- Simultaneous connections to Pool **A** and Pool **B**
- Weight-based scheduling (e.g. `50:50`, `70:30`)
- Realtime weighting adjustments via GUI slider
- Time-sliced switching with stickiness controls
- Safe handling of pool transitions to avoid stale or invalid submits

### 🎯 Correct Stratum Semantics
- Strict job ownership tracking per pool
- Extranonce consistency enforcement
- Difficulty forwarding with pool-aware gating
- Duplicate and stale share detection
- Graceful handling of miner reconnects

### 📊 First-Class Observability
- Built-in **Prometheus metrics endpoint**
- Tracks:
  - downstream miner connections
  - upstream pool connections
  - message RX/TX counts
  - difficulty state
  - scheduler behavior
- Designed to integrate with Grafana or custom dashboards

### 🧾 Structured Logging
- JSON-formatted logs
- Explicit event types (e.g. `pool_switched`, `share_result`, `job_forwarded`)
- Designed for both human debugging and automated analysis
- Suitable for ingestion into Loki or other log systems

### 🖥 Web UI
- **NiceGUI-based interface** (primary)
- Live config editing, view logs, and status

---

## Architecture Overview

DPMP v2 operates in three distinct roles simultaneously:

1. **Downstream Pool Role**  
   Listens for miner connections and serves Stratum jobs.

2. **Upstream Miner Role**  
   Connects to real pools, subscribes, authorizes, and receives work.

3. **Scheduler / Router**  
   Decides which pool is “active,” forwards jobs, and routes share submissions safely.

The design emphasizes:
- explicit state tracking
- defensive validation
- clear separation of responsibilities

---

## Project Status

- ✅ Actively used in real mining setups
- ✅ Stable for long-running operation
- 🔧 Configuration, install, and GUI documentation intentionally evolving
- 🚧 Future work planned around:
  - testing with additional miners to confirm compatibility
  - testing with additional pools to confirm compatibility
  - richer GUI (using NiceGUI)
  - additional scheduling strategies

---

## What This Repo Intentionally Does *Not* Include (Yet)

To keep the repository clean and safe:

- No live configuration files (local `config.json` is ignored)
- No secrets or credentials
- No logs or backups
- No Docker deployment (current focus is non-Docker installs)

Installation, configuration, and operational runbooks will be added once those workflows are finalized.

---

## Who This Is For

DPMP v2 is designed for users who:

- Run one or more ASIC or CPU/GPU miners that do not internally support dual-pool mining (i.e., Avalon Q, Avalon Nano3S, etc.)
- Want to split hash power across pools deterministically, such as mine to a Bitcoin pool and a Bitcoin Cash pool simultaneously
- Care about correctness, observability, and long-term stability
- Prefer transparent tooling over black-box pool logic

---

# Changelog

## 2.0.3 - 2026-02-11
- calculate realtime network hashrate for BTC and BCH (short-term and long-term)
- add auto-balance options to config
- add auto-balance logic to DPMP and dashboard

## 2.0.2 - 2026-02-09
- Fixes for ck-type pools and bootstrap sequence
- Add realtime hashrate allocation slider to GUI

## 2.0.1 - 2026-02-07
- Fixed mining.set_extranonce and client.reconnect issues for NerdAxe Gamma
- Fixed Braiins BM-101 initialization and handshake issues

## 2.0.0 — 2026-02-06
- Fixed reject storms during pool switches.
- Improved scheduler convergence and validation.
- Increased grace period for stale submits.
- Ensured correct single-pool behavior at 0/100 and 100/0 weights.
- Added pool failover protection.
- Added global exception handling.
- Added periodic state pruning.

---


## License

MIT License

---

## Disclaimer

This software operates at the Stratum protocol level.  
Misconfiguration can result in rejected shares or lost revenue.  
Use at your own risk and validate behavior carefully in your environment.

---

*More documentation coming soon.*
