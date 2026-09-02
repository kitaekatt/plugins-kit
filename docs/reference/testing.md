# Test parallelism measurements

This reference supports the test-workflow rule in the root `CLAUDE.md`. Use it
for pytest parallelism choices and failures that appear only under load. The
root file retains the runnable targeted-test and full-suite command blocks.
This document records the measurements and the reasons for those commands.

## Why `-n` stays explicit

`pytest-xdist` is in the `dev` extra. `-n` is deliberately NOT in `addopts`.
`addopts` with `-n` creates a regression, not a convenience. Worker
startup has a fixed ~1.6-2.9s toll. This toll is free on a 13-minute run but
ruins a targeted run. Measured on a 24-core box,
a single small bootstrap test file alone cost **0.40s serial, 2.03s at `-n 12`,
3.33s at `-n auto`**. (That measurement was taken against
`tests/bootstrap/test_cache.py`, a file the suite has since dropped; the ratio is
what carries, not the file.) A config-level `-n` makes the tight TDD loop 5-8x
SLOWER. It makes the full run faster. Pass `-n` explicitly per run.

## Worker-count measurements

More workers are not always better. The suite is process-spawn-bound because
tests run real `git`, `uv`, and Git Bash subprocesses. Past a point, extra
workers contend for the same process spawns.

Full-suite wall time on the 24-core box:

| Worker setting | Wall time |
| --- | --- |
| `-n 8` | 4:00 |
| `-n 12` | **2:54** |
| `-n 16` | 3:10 |
| `-n auto` (=24) | 3:21 |

Roughly half the core count is the sweet spot. `-n auto` is portable but not
optimal on a many-core machine.

## Consequences of parallelism

Timing-sensitive tests can fail under load and pass serially. The SessionStart
display hook spawns ~10 Git Bash processes, and its foreground takes 11-22s on
a saturated machine against ~1s idle. `tests/bootstrap/test_sessionstart_rescue.py`
is hardened for this with generous *polling* for positive assertions and a
causal observable instead of a fixed sleep for negative ones. A test that
waits on a subprocess must follow that pattern. Never use a bare `time.sleep`
sized for an idle machine.

The three root-conftest leak guards run in ONE worker under `-n` because they
snapshot machine-global state that xdist cannot isolate. Leak detection is
therefore complete only in a SERIAL run. If the question is whether something
leaks into the real Claude data directory, run serially.
