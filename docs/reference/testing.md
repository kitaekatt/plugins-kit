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

## claudx containment mechanics

`claudx` reaches manifest content two ways: a synthetic dev-layout
`installed_plugins.json` written into `plugins/` (gitignored), discovered only
by an engine running from this working copy (`_find_plugins_dir` walks up from
its own plugin root), and `CLAUDE_BOOTSTRAP_DATA_ROOT`, which moves everything
bootstrap owns -- venvs, `_shared_libs`, logs, stamps, cooldowns, config --
into a separate tree.

The containment is real but PARTIAL: `CLAUDE_BOOTSTRAP_DATA_ROOT` redirects what bootstrap OWNS, not
what it REACHES OUT TO. Three escapes are known, all observed on a 2026-09-20
run:

- **The shared-lib link is the dangerous one.** `shared_lib.py`'s
  `link_shared_lib` registers `<pkg>.pth` pointing at `<shared_root>/<name>/`
  on the TARGET INTERPRETER. The shared root follows the data root; the
  interpreter does not. So a test session rewrites
  `bootstrap_lib.pth` in the standalone interpreter's site-packages
  (`~/.local/share/python-standalone/python/Lib/site-packages` on Windows,
  `.../python/lib/python3.12/site-packages` on macOS/Linux)
  -- the machine-wide standalone interpreter every plugin on the fleet imports
  through -- to point INTO the test's data root.
- **Marketplace refresh hits the real clone.** Bootstrap's own
  `bootstrap.json` sets `"alwaysUpdate": true`, so `_phase_marketplaces` runs
  `git fetch` against the real `~/.claude/plugins/marketplaces/plugins-kit`.
  That fetch is also where a test session can hang on network I/O, leaving a
  stalled engine holding the lock.
- **`session-bootstrap.sh` writes `~/.local/bin` and the Windows PATH
  registry**, regardless of the data root. `BOOTSTRAP_SKIP_SHELL_INTEGRATION=1`
  suppresses the rc-file and registry persistence but gates neither of the two
  escapes above.

What `claudx` still does not test, by construction: anything whose output IS
the machine -- package-manager tool installs, PATH / rc-file / registry
writes, marketplace clone refreshes, `claude plugin install/update`,
`env.json` personalization, and the version-bump -> cache -> auto-update
delivery path. A green run means "my plugin works", never "my plugin ships
correctly".

## A MOVING victim is a leak, not a flake

A distinct failure shape from host-dependence: the suite fails, and the test
that fails CHANGES between runs. That is never load and never a bad assertion
in the victim -- it is one test writing outside its sandbox and corrupting
whichever test is in flight. The chain that produced it, named in
`tests/conftest.py`'s autouse guard docstring: a bootstrap engine run that is
not HOME-isolated discovers the developer's REAL `installed_plugins.json`,
iterates the enabled plugins, and runs claude-ui-kit's `install_statusline.py`
against the real `~/.claude/settings.json`, rewriting its `statusLine` to a
pytest temp path. Cut at the source in claude-ui-kit 0.12.0 (c52e4113):
`install()` refuses any data root that is not the canonical
`~/.claude/plugins/data`, and a pytest temp dir never is.

Two things to carry. First, when a victim moves, go looking for the WRITER --
do not triage the victim, which is innocent by construction. Second: this
failure had been recorded for months as an environmental fact about the host,
with a documented rule for judging slices around it. Once a failure has an
accepted name it stops being read as evidence, and the mechanism had been
sitting in a guard docstring in plain prose the whole time. A standing caveat
can be a finding wearing a workaround. If a moving victim reappears, that
refutes the fix rather than restoring the caveat.
