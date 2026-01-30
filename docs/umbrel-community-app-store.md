# Building a Custom Umbrel Community App Store (DPMP Example)

This document describes how the DPMP Umbrel Community App Store was created,
tested, and published. It is intended as a repeatable reference for creating
additional Umbrel apps or stores in the future.

---

## Overview

An Umbrel Community App Store is simply a public Git repository that contains:

- `umbrel-app-store.yml` (store metadata)
- One or more app folders
  - Each app folder contains:
    - `umbrel-app.yml` (app manifest)
    - `docker-compose.yml`
    - `icon.png`
    - `1.png`, `2.png`, `3.png` (gallery images)

Umbrel clones this repository locally and renders the UI directly from its
contents.

---

## Repository Structure

Example (DPMP):



ckryza-umbrel-app-store/
├── umbrel-app-store.yml
└── ckryza-dpmpv2/
├── umbrel-app.yml
├── docker-compose.yml
├── icon.png
├── 1.png
├── 2.png
└── 3.png


---

## Store Metadata (`umbrel-app-store.yml`)

Minimal and sufficient:

```yaml
id: "ckryza"
name: "Chris's"


Umbrel automatically appends “App Store” in the UI.

App Manifest (umbrel-app.yml)

Key points:

id must start with the store id (ckryza-*)

manifestVersion must be a valid semantic version

description, tagline, and releaseNotes are rendered verbatim in the UI

icon and gallery must be public HTTPS URLs (GitHub raw URLs work)

Example ports note (important for mining software):

Umbrel installs may remap host ports to avoid conflicts, while the container
continues to listen on its internal defaults.

Docker Image Hosting (GHCR)

DPMP is published to GitHub Container Registry:

ghcr.io/ckryza/dpmpv2:latest


Umbrel users do not need credentials to pull public images.

Note: A GitHub token is only required for pushing images during CI or manual
publishing. Token expiration does not affect Umbrel installs.

Port Mapping Strategy (DPMP)

Inside container:

3351 — Stratum

9210 — Metrics

8855 — Web UI

Umbrel host mapping:

3352 → 3351

9211 → 9210

8855 → 8855

This avoids conflicts with an existing standalone DPMP instance.

Testing Strategy (Strongly Recommended)

Use three Umbrel boxes if possible:

BitNode1 — Production BTC node / pool

BitNode2 — Production BCHN + MiningCore

BitNode3 — Clean Umbrel test box (no apps installed)

Always validate:

Fresh install

UI loads correctly

Uninstall removes app + icon cleanly

Reinstall works without manual cleanup

Common Failure Modes

Port already in use → container fails at ~1%

Missing icon.png or gallery images → UI “Something went wrong”

App ID not prefixed with store ID → app silently filtered

Private container image → pull failures

Final Notes

Once the app installs cleanly on a fresh Umbrel box, it is considered
production-ready.

Tag releases after store + app manifests are stable.