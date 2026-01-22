# Anchor A — dpmpv2 System Invariants and Philosophy

This system is live, stateful, and correctness-critical.

dpmpv2 prioritizes **correct rejection over incorrect acceptance**.
If context is uncertain (job ownership, extranonce, difficulty), the share is dropped.
Misrouting is always worse than loss.

State is real and fragile.
Logs and metrics define truth, not assumptions.

Invariants matter more than throughput:
- Jobs belong to pools.
- Extranonce defines session context.
- Difficulty defines validity.

Workflow discipline:
Think first.  
Design second.  
Patch last.  
Verify always.  
Commit memory immediately.

Tools serve clarity, not speed.
Repo docs are the source of truth.
