# DPMP Handoff (LATEST)

## Current goal
Get Nano3S mining through DPMP with normal reject rate (rejects currently ~80%+), and ensure correct downstream difficulty + job/extranonce forwarding.

## Environment
- Host: UmbrelOS
- DPMP path: ~/dpmp
- venv: ~/dpmp/.venv
- DPMP logs: ~/dpmp/dpmp_run.log
- DPMP listens:
  - Miner downstream: 0.0.0.0:3350
  - Metrics: 0.0.0.0:9109
- Upstreams observed:
  - Miningcore (B): 192.168.0.25:3333
  - Other upstream (A): 127.0.0.1:2018

## Restart / control commands
### Stop dpmp
cd ~/dpmp
pgrep -af 'dpmp\.py' | awk '{print $1}' | xargs -r sudo kill -9

### Start dpmp
cd ~/dpmp
nohup .venv/bin/python -u dpmp.py > dpmp_run.log 2>&1 &
sleep 1
pgrep -af 'dpmp\.py' || true

### Quick health snapshot
pid=$(pgrep -f 'dpmp\.py' | head -n1)
sudo ss -lntp | egrep ':3350|:9109' || true
sudo ss -ntpe | egrep "pid=$pid" || true
tail -n 80 ~/dpmp/dpmp_run.log

## What we observed
- DPMP is now forwarding jobs and receiving submits:
  - miner_method includes mining.submit
  - submit_jid routes to pool B
  - share_result shows both accepted:true and accepted:false with error code 23 "low difficulty share"
  - job_forwarded seq increments, extranonce_set observed
- Nano3S shows ~80%+ rejects in its dashboard.
- Miningcore shows Nano3S authorized, but previously it was closing "zombie-worker idle-timeout exceeded" (config clientConnectionTimeout was 600).
- Miningcore config updated:
  - bch1 clientConnectionTimeout increased to 3600
  - jobRebroadcastTimeout set to 10
  - miningcore restarted afterward.

## Leading hypothesis
High "low difficulty share" rejects suggest miner is hashing with a lower target than pool expects (difficulty mismatch).
This commonly happens when DPMP does not reliably send mining.set_difficulty to the miner after subscribe/authorize/reconnect,
or when it sends the wrong diff/timing/session.
Occasional accepts are consistent with miner submitting many below-target shares.

## Next debug steps
1) Confirm dpmp_run.log contains downstream_diff_set events for the active sid (and what diff value).
   - If missing, fix DPMP to always send downstream difficulty whenever active pool changes or a miner (re)connects.
2) Optionally tcpdump port 3350 to confirm mining.set_difficulty is actually on the wire.
3) If diff is correct but rejects persist, investigate:
   - job/extranonce coherence per sid
   - potential duplicate submit forwarding or response mapping
