# Umbrel Community App Store (DPMPv2)

This document is the authoritative “how we ship Umbrel builds” guide for DPMPv2.

---

## Repos (two-repo model)

### A) DPMPv2 app code repo (source of truth)
- Repo: https://github.com/ckryza/dpmpv2
- Contains: proxy code, NiceGUI, installer/, services/, docs/

### B) Umbrel Community App Store repo (Umbrel installer metadata)
- Repo: https://github.com/ckryza/ckryza-umbrel-app-store
- **Store URL you add in Umbrel** (git clone URL):
  - https://github.com/ckryza/ckryza-umbrel-app-store.git
- Contains: `umbrel-app-store.yml` + app folders:
  - `ckryza-dpmpv2/`  (Umbrel “DPMP v2” app)
  - `ckryza-dpmp/`    (legacy/older packaging experiments; keep separate)

---

## Golden rule: GHCR only (NOT docker.com)

We publish images to **GitHub Container Registry (GHCR)**:
- `ghcr.io/ckryza/dpmpv2:<tag>`
- `ghcr.io/ckryza/dpmpv2:latest`

Do **not** use `docker login` for docker.io / docker.com for this project.

### GHCR login (for pushing images)
You must use a GitHub token with permission to write packages (classic PAT usually needs `write:packages`).

Login command:
```bash
docker login ghcr.io -u ckryza
# password = your GitHub token (PAT)
Notes:

This token is required for pushing images.

End users pulling public images from GHCR generally do not need tokens.

Ports: container vs host mappings (Umbrel)
Inside the container (DPMP service binds)
Stratum: 3351

Metrics: 9210

NiceGUI: 8855

On Umbrel host (we map to avoid conflicts with non-docker installs)
Umbrel app ckryza-dpmpv2/docker-compose.yml maps:

Host 3352 → 3351 (Stratum)

Host 9211 → 9210 (metrics)

Host 8855 → 8855 (NiceGUI)

So from your LAN you use:

Stratum: umbrel-ip:3352

Metrics: http://umbrel-ip:9211/metrics

UI: http://umbrel-ip:8855

Critical gotcha: DPMP_METRICS_URL (inside-container)
NiceGUI runs inside the container and reads metrics from inside the container.

Therefore, in Umbrel docker-compose, this must be:

DPMP_METRICS_URL=http://127.0.0.1:9210/metrics ✅

NOT :9211 (9211 is the host mapping, not the container port).

Config + logs persistence (Umbrel)
Umbrel app stores config/logs in a Docker volume mounted to /data in the container:

/data/config_v2.json

/data/dpmpv2_run.log

/data/dpmpv2_gui.log

What persists across uninstall/reinstall?
Umbrel “Uninstall” usually removes /home/umbrel/umbrel/app-data/ckryza-dpmpv2

Docker volumes may persist depending on how Umbrel removes the app.

If the volume persists, your config_v2.json may come back after reinstall.

How to truly reset DPMPv2 Umbrel app to defaults
On the Umbrel host:

# Stop/remove containers for the app
sudo docker ps -a --format '{{.Names}}' | grep -E '^ckryza-dpmpv2_' | xargs -r sudo docker rm -f

# Remove the app-data directory Umbrel uses
sudo rm -rf /home/umbrel/umbrel/app-data/ckryza-dpmpv2

# OPTIONAL: remove the docker volume (THIS is what wipes /data/config_v2.json)
sudo docker volume ls --format '{{.Name}}' | grep -E 'ckryza-dpmpv2' || true
# then remove the matching volume(s), e.g.
# sudo docker volume rm ckryza-dpmpv2_dpmpv2_data
If Umbrel UI still shows “Open” when you think it’s gone:

sudo systemctl restart umbrel
Release / update procedure (step-by-step)
This is the exact flow to ship a new Umbrel build.

Step 1 — Update code in dpmpv2 repo
On your dev box (BitNode1 typically):

cd ~/dpmpv2   # or wherever the dpmpv2 repo lives
git status -sb
# make changes
git commit -am "your message"
git push origin main
Step 2 — Build + push GHCR images
Choose a tag. We strongly prefer tagging by git short SHA.

cd ~/dpmpv2

GIT_SHA="$(git rev-parse --short HEAD)"
echo "Tag=$GIT_SHA"

# Build locally
sudo docker build -t ghcr.io/ckryza/dpmpv2:latest -t ghcr.io/ckryza/dpmpv2:${GIT_SHA} .

# Login to GHCR (uses GitHub token as password)
sudo docker login ghcr.io -u ckryza

# Push tags
sudo docker push ghcr.io/ckryza/dpmpv2:latest
sudo docker push ghcr.io/ckryza/dpmpv2:${GIT_SHA}
Step 3 — Bump the Umbrel store repo to the new image tag
Edit the store repo:

Repo: ckryza/ckryza-umbrel-app-store

File: ckryza-dpmpv2/docker-compose.yml

Update:

image: ghcr.io/ckryza/dpmpv2:<NEW_TAG> (pin to the SHA tag you just pushed)

Ensure DPMP_METRICS_URL remains http://127.0.0.1:9210/metrics

Then:

cd ~/umbrel-appstore/store   # wherever you keep the store repo checkout
git status -sb
git commit -am "ckryza-dpmpv2: bump image to <NEW_TAG>"
git push
Step 4 — Verify Umbrel pulled the updated store
On the target Umbrel box:

cd /home/umbrel/umbrel/app-stores
ls -la | grep ckryza

# Enter the cached store repo (name varies)
cd /home/umbrel/umbrel/app-stores/ckryza-ckryza-umbrel-app-store-github-*/ckryza-dpmpv2

# Confirm docker-compose.yml references the new tag
grep -n "image:" docker-compose.yml
grep -n "DPMP_METRICS_URL" docker-compose.yml
If it didn’t update yet:

sudo systemctl restart umbrel
Step 5 — Clean install test on a fresh Umbrel (BitNode3)
Uninstall from Umbrel UI (preferred), then optionally wipe volumes (see reset steps above).

Reinstall from the store.

Confirm UI shows running and ports are correct.

Verification commands:

sudo docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' | grep ckryza-dpmpv2 || true
sudo docker exec -it ckryza-dpmpv2_app_1 sh -lc 'tail -n 30 /data/dpmpv2_run.log'
sudo docker exec -it ckryza-dpmpv2_app_1 sh -lc 'curl -sS http://127.0.0.1:9210/metrics | grep -E "^dpmp_downstream_connections|^dpmp_shares_submitted_total" || true'
Minimal tooling on clean Umbrel installs
Some test boxes don’t have common tools. On Debian-based Umbrel OS:

sudo apt-get update
sudo apt-get install -y procps wget ripgrep
(If you don’t want to install ripgrep, use grep everywhere.)


---

## 2) Update `docs/PROJECT_STATE.md` (append this near the end)

Paste this as a new section near the bottom (or right after the “Dockerized Version” note):

```md
## Umbrel App (dockerized) packaging notes (ckryza-dpmpv2)

- Umbrel store repo: https://github.com/ckryza/ckryza-umbrel-app-store
- Umbrel store URL (what you add in Umbrel UI): https://github.com/ckryza/ckryza-umbrel-app-store.git
- App folder: `ckryza-dpmpv2/`
- Host ports (Umbrel): 3352 (stratum), 9211 (metrics), 8855 (NiceGUI)
- Container ports (inside app container): 3351 (stratum), 9210 (metrics), 8855 (NiceGUI)
- Important: NiceGUI runs inside the container, so `DPMP_METRICS_URL` MUST be `http://127.0.0.1:9210/metrics` (container port), not 9211.
- Config persistence: `/data/config_v2.json` may persist across uninstall/reinstall if the docker volum

