# dpmpv2 Roadmap

## Short Term
- Review dpmpv2.py with Continue for clarity and risk.
- Improve inline documentation.
- Reduce reject rate where safely possible.

## Medium Term
- Replace FastAPI GUI with NiceGUI.
- Add live log viewer.
- Add lightweight metrics dashboard (no Grafana).

## Long Term
- Package as Umbrel Community App.
- Simplify configuration UX.
- Harden multi-miner handling.

## Post-Reject Investigation Cleanup
- Fix dpmpv2.service shutdown behavior:
  - systemctl stop currently times out and requires SIGKILL
  - investigate graceful cancellation of async tasks and faster exit
  - ensure clean stop before restart
