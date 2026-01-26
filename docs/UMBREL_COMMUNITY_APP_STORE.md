# Umbrel Community App Store (DPMPv2 notes)

This document captures what we learned packaging DPMPv2 as an Umbrel Community App and publishing it via a custom community app store repo.

## Our Umbrel boxes (lab)
- **192.168.0.24 – BitNode1**: BTC node + a pool
- **192.168.0.25 – BitNode2**: BCHN node + Miningcore (authoring box used earlier)
- **192.168.0.26 – BitNode3**: Clean install test box (used to validate install + uninstall)

## Repos involved
- **DPMPv2 app code repo**: https://github.com/ckryza/dpmpv2
- **Community app store repo**: https://github.com/ckryza/ckryza-umbrel-app-store

### Store URL (the one you add in Umbrel)
Use the **git clone URL** of the store repo:
- https://github.com/ckryza/ckryza-umbrel-app-store.git

(That’s the URL Umbrel clones into `/home/umbrel/umbrel/app-stores/...`.)

## Store structure (what Umbrel expects)
In the **store repo**:
- `umbrel-app-store.yml` (store metadata: id + name)
- One folder per app, e.g. `ckryza-dpmpv2/`
  - `umbrel-app.yml` (app manifest shown in the store UI)
  - `docker-compose.yml` (used by Umbrel legacy-compat installer)
  - `icon.png` and `1.png`, `2.png`, `3.png` (gallery images referenced by the manifest)

## DPMPv2 port mappings (Umbrel vs bare-metal)
DPMPv2 inside the container listens on:
- Stratum: **3351**
- Metrics: **9210**
- NiceGUI: **8855**

But on Umbrel installs, we map host ports to avoid conflicts with an already-running DPMP instance:
- Host **3352 -> 3351** (Stratum)
- Host **9211 -> 9210** (metrics)
- Host **8855 -> 8855** (NiceGUI)

So on Umbrel you connect to:
- Stratum: **umbrel-host:3352**
- Metrics: **http://umbrel-host:9211/metrics**
- UI: **http://umbrel-host:8855**

## Install / Uninstall behavior (observed)
- Install succeeded on the clean test box (BitNode3).
- Uninstall removed the Umbrel home icon and deleted `/home/umbrel/umbrel/app-data/ckryza-dpmpv2`.
- Docker volumes may persist depending on permissions/visibility; verifying with `sudo docker volume ls` is required.

## Notes on GHCR / tokens
The Umbrel installer pulls `ghcr.io/ckryza/dpmpv2:latest`.
As long as the image is **public**, installs do **not** rely on a short-lived GH token.
Tokens are only needed for *pushing* images, not for end-users pulling them.

