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

Full-suite wall time on a 24-core non-Windows box:

| Worker setting | Wall time |
| --- | --- |
| `-n 8` | 4:00 |
| `-n 12` | **2:54** |
| `-n 16` | 3:10 |
| `-n auto` (=24) | 3:21 |

Roughly half the core count is the sweet spot. `-n auto` is portable but not
optimal on a many-core machine.

### Windows

On Windows 11 with Git Bash, representative full-suite runs on 2026-10-07
took 27:53 and 35:32 with `-n 12`. The worker-count sweep was not repeated on
Windows. The slow tests were mostly the `tests/secrets-kit` seed, entry,
rotation-recovery, `operation_lock`, and `dest_guard` tests, plus
`tests/repo-scripts/test_publish.py::TestMasterOnlyGuardAgainstAModel` cases.
These tests drive real git, age stand-ins, and `icacls` subprocesses; process
spawning is the cost on Windows.

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

`engine._phase_shared_libs` suppresses the owner broadcast into the machine-wide
standalone interpreter when `CLAUDE_BOOTSTRAP_DATA_ROOT` is set. It still publishes
source and links consumer venvs inside the redirected root. A redirected pass does
not rewrite the standalone interpreter's shared-library `.pth` files.

Redirection also needs the consumer's interpreter: the shared-library root alone
does not determine Python's imports. Before the fail-loud guard, a missing dev
venv let `bootstrap_guard.reexec_under_plugin_venv` return to the caller, whose
standalone interpreter could import an older machine-wide `bootstrap_lib`. The
guard exits with status 2 before shared imports when the redirected plugin
interpreter is missing, including when a re-exec loop guard is inherited. The
missing-venv mechanism is reproduced; the exact invocation behind the earlier
`claudx -p` observation was not captured and remains unproven.

Containment remains partial: `CLAUDE_BOOTSTRAP_DATA_ROOT` redirects what bootstrap
owns, not everything it reaches. These remaining escapes were observed on
2026-09-20 (marketplace and shell writes) and 2026-10-02 (exported plugin roots):

- **Marketplace refresh hits the real clone.** Bootstrap's own
  `bootstrap.json` sets `"alwaysUpdate": true`, so `_phase_marketplaces` runs
  `git fetch` against the real `~/.claude/plugins/marketplaces/plugins-kit`.
  That fetch is also where a test session can hang on network I/O, leaving a
  stalled engine holding the lock.
- **`session-bootstrap.sh` writes `~/.local/bin` and the Windows PATH
  registry**, regardless of the data root. `BOOTSTRAP_SKIP_SHELL_INTEGRATION=1`
  suppresses the rc-file and registry persistence but does not suppress
  marketplace refresh or the `~/.local/bin` writes.
- **`<PLUGIN>_ROOT` points at the installed plugin.** Bootstrap exports one
  `<PLUGIN>_ROOT` variable per plugin, and `SKILLS_KIT_ROOT` resolved to
  `~/.claude/plugins/cache/plugins-kit/skills-kit/0.83.0` inside a `claudx`
  session. `--plugin-dir` repoints Claude Code's loading of skills and hooks,
  not the exported variable. So a command anchored on `"${SKILLS_KIT_ROOT}"`
  runs the installed plugin's script. A green `claudx` run can therefore
  exercise the installed plugin instead of the dev tree, the same false pass
  as the enablement-filtering trap.

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

## Bytecode isolation

`tests/conftest.py` sets `sys.pycache_prefix` to a fresh temporary directory
(prefix `plugins-kit-pycache-`) when the process starts, and removes it at exit.
Every pytest run therefore compiles every module, including the assertion-rewritten
test modules, from source. No run can reuse bytecode left by an earlier run. Under
`-n`, each xdist worker imports the conftest and owns its own directory.

Why: Python and the pytest assertion rewriter validate a cached `.pyc` by the
source mtime (whole seconds) and size. A revert-to-red check mutates a source file,
runs the test, restores the file, and runs again. When the mutated and restored
sources have the same length and fall in the same second, the stale `.pyc` passes
validation and the second run executes the wrong code. The result is a vacuous
check (see [vacuous-checks.md](vacuous-checks.md)).

Cost: every run recompiles, a small fixed amount (about 0.05 s on a 45-test slice,
measured 2026-09-30).

Opt out by setting `PYTHONPYCACHEPREFIX` (or `-X pycache_prefix=...`) before the
run. The conftest then leaves it alone and bytecode persists in that location.
Only an explicit prefix opts out. `PYTHONDONTWRITEBYTECODE` is not an opt-out: it
stops Python from writing `.pyc` files but still reads existing ones, so a stale
`__pycache__` from an earlier run would be reused. With it set, the fresh prefix
stays empty and nothing stale is read.
