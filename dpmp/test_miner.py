#!/usr/bin/env python3
"""
Tiny Stratum v1 client to test DPMP without an ASIC.
Sends: subscribe, authorize(worker-only), then a dummy submit.
"""
import json, socket, time

HOST="127.0.0.1"
PORT=3350
WORKER="TestWorker1"

def send(s, obj):
    line=(json.dumps(obj, separators=(",",":"))+"\n").encode()
    s.sendall(line)

with socket.create_connection((HOST, PORT), timeout=5) as s:
    s.settimeout(5)
    send(s, {"id": 1, "method": "mining.subscribe", "params": []})
    send(s, {"id": 2, "method": "mining.authorize", "params": [WORKER, "x"]})
    time.sleep(1)

    # dummy submit: most pools will reject; we're just verifying the path and logging
    send(s, {"id": 3, "method": "mining.submit", "params": [WORKER, "jobid", "ex2", "ntime", "nonce"]})

    # read a few lines
    for _ in range(10):
        try:
            data = s.recv(4096)
            if not data:
                break
            for line in data.splitlines():
                print(line.decode(errors="replace"))
        except Exception:
            break
