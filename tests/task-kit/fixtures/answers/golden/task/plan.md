# Plan

Redo the punch-down at the closet patch panel, then re-test the run.

```yaml
task_items:
  items:
    - id: buy-tools
      title: "Buy a punch-down tool"
      state: blocked-user
      priority: P1
      note: "waiting on the hardware store"
    - id: reterminate-run
      title: "Re-terminate the closet run"
      state: available
      priority: P2
    - id: retest-run
      title: "Re-test the run"
      state: deferred
```
