# Risk drill report: quota re-selection (R30, R31)

**Fixture: SIMULATED.** No real codex quota window was used or waited for.
A fake `codex` executable and a scratch rollout in the real
`usage_limit_exceeded` shape stand in for an exhausted codex account (owner
ruling, 2026-09-23: "Drill: uses a simulated Codex usage-limit error and runs
after step 4, and the report says the fixture was simulated").

- Date run: 2026-09-23, against the `dev` working tree (llm-scripting-kit
  0.46.0, awesome-kit 0.62.0). The installed plugin cache on the drill
  machine held llm-scripting-kit 0.44.2, so nothing ran from the cache.
- Specification: `declaration-format-design.md`, section "Biggest risk";
  `requirements.md` R30 and R31 (the only R-ids verified by "drill").
- Test: `tests/llm-scripting-kit/test_risk_drill.py`.

## Summary

| Assertion | Process layer (A) | Session layer (B) |
|---|---|---|
| 1. Out-of-quota codex entry -> a usable entry is announced, with `<entry> failed: quota` as the reason | PASS | PASS (live agent, third attempt, 2026-09-24). See F4 for a sibling-entry gap, fixed in llm-scripting-kit 0.48.0. |
| 2. A wrong result with exit 0 on a usable entry is a task failure and is NOT re-routed | PASS | PASS (live agent, third attempt). |
| 3. A unit that halts after writing is re-run only after its workspace is reset | PASS | PASS (live agent, third attempt). |

R31 is confirmed for both layers. The session layer ran once per assertion
against a live agent on 2026-09-24 ("Third attempt" below); one run per
variant is evidence of the behaviour, not a rate.

## Layer A: process (deterministic)

### Method

`test_risk_drill.py` runs `llm_scripting_kit.run()` and `describe()` end to
end. These parts are real, not mocked:

- `CodexCliBackend`, driving a real subprocess through the default runner.
- Its non-zero-exit rollout re-read (`read_codex_pool`).
- The pinned-verdict cache (`pinned_evaluate` and `record_observed_halt`),
  redirected to a scratch file under a fixed session key.
- The git workspace snapshot and restore.

Only the second entry's backend (a scripted `claude-cli` stand-in) and the
reachability result are injected. HOME and USERPROFILE point at a per-test
temp directory (the suite's autouse fixture). `usage_budget.VERDICT_CACHE`
and `usage_budget.CODEX_SESSIONS_DIR` are redirected to scratch as well, so
the drill never reads or writes the real verdict cache or `~/.codex/sessions`.

The fake `codex` script has four modes:

- `quota`: optionally writes into its cwd, appends an exhausted rollout
  (null windows, `has_credits: false`, and a `task_complete` error with
  `codex_error_info: usage_limit_exceeded` and a "try again at" clause),
  prints the usage-limit text on stderr, and exits 1.
- `wrong`: writes `ANSWER: 5` to the `-o` file and exits 0.
- `right`: writes the expected answer to the `-o` file and exits 0.
- `broken`: prints an ordinary error and exits 1.

A healthy rollout is seeded first, so codex starts AVAILABLE and is the
default.

Declaration under test: `[codex, opus]`. The codex entry is paced
(`conserve_usage: seven_day`).

### Assertion 1: PASS

Menu before dispatch (session render, same session key):

```
  codex  codex          available  95% left, 80% of window   pace 119%   [default]
  opus   claude/agent   n/a (unpaced)
```

Attempts from `run()`:

```
codex  #1  halt=quota  outcome=halted     error="codex exec failed (exit 1)"
opus   #2  halt=None   outcome=completed
```

Menu that a later dispatch in the same session sees, after
`record_observed_halt` wrote the verdict back:

```
  codex  codex          out of quota until 2027-01-20 21:34 UTC
  opus   claude/agent   n/a (unpaced)   [default]
```

Floor-diagnostic line for the codex entry:
`codex: out-of-quota until 2027-01-20 21:34 UTC (observed quota/credit halt at dispatch)`.
The announcement that the shipped Rule line produces from these attempts is
`route: drill-unit -> opus; codex failed: quota`. That is the passing form;
see F2.

### Assertion 2: PASS

Mode `wrong` made one attempt only: `codex #1 outcome=completed`, and the
response was `ANSWER: 5`. The second entry was called 0 times, and the
caller's validation reports the wrong answer as a task failure. In mode
`broken` (non-zero exit, no quota evidence), `run()` returned
`failed` / `task error: codex exec failed (exit 1)` with one attempt and no
re-route. After both runs the codex verdict stays usable: a task failure does
not move the quota verdict.

### Assertion 3: PASS

In mode `quota` with a write, the unit changed the tracked `kept.txt` and
created `codex-scratch.txt`, then halted. The attempt recorded
`workspace reset to its launch state (tree a90b37ec5015)`. The re-run on
`opus` then saw these files:

```
kept.txt           "original\n"                (not the partial edit)
codex-scratch.txt  absent
launch-dirty.txt   "user work at launch\n"     (user's uncommitted work kept)
```

Companion check: when the workspace is not a git tree, it cannot be reset.
The run ended `failed` ("codex halted and the workspace could not be reset
to its launch state, so no other entry was dispatched: ...") and the second
entry was called 0 times.

### Each check shown to fail

`TestDrillGoesRed` breaks each guarded behaviour with a monkeypatch only.
No shipped code was edited. Each case asserts that the drill check raises.
To confirm that each check failed for the correct reason, the cases were
also run with the `pytest.raises` wrapper removed, in a temporary copy that
was deleted after the run:

| Sabotage | Check that went red | Failure observed |
|---|---|---|
| `CodexCliBackend._quota_probe` returns None (step 0 re-read lost) | 1 | `('codex', None, 'failed') != ('codex', 'quota', 'halted')`: the usage-limit prose alone is not classified, so the run fails with no re-route |
| `record_observed_halt` becomes a no-op | 1 | later menu: `assert 'available' == 'out-of-quota'` |
| `record_observed_halt` becomes a no-op, in-session form (`record-halt` verb) | 1 (in session) | later menu still `codex ... available  n/a (no reading)   [default]` |
| codex backend turns a wrong exit-0 answer into a quota halt | 2 | `assert (2 == 1)`: re-routed to opus |
| `_WorkspaceSnapshot.restore` becomes a no-op | 3 | `re-run saw another model's partial edits: {'kept': 'partial edit by codex\n', 'codex_scratch_exists': True, ...}` |

The suite was run as
`uv run --extra dev pytest tests/llm-scripting-kit/test_risk_drill.py test_declaration.py test_completion_codex_backend.py`:
108 passed.

## Layer B: session (an agent acting on prose)

### What was attempted, and why the live-agent run did not happen

The brief requires that any experiment touching HOME-relative state runs
with HOME and USERPROFILE in a scratch directory.

1. A nested `claude -p` run with CLAUDECODE cleared, and HOME and
   USERPROFILE set to a scratch directory, exited 1 with
   `Not logged in - Please run /login`. With a scratch HOME the child cannot
   find its credentials. The drill machine has no API-key variable in the
   environment.
2. One workaround gave the scratch HOME access to the login keychain. The
   harness permission classifier denied it (Credential Exploration). No
   other attempt was made to get credentials.
3. A run with the real HOME is not permitted, for three reasons:
   - The child's orchestrate render calls `pinned_evaluate` under the child's
     `CLAUDE_CODE_SESSION_ID`, which rewrites the real
     `~/.claude/plugins/data/plugins-kit/llm-scripting-kit/usage-verdicts.json`
     (real quota state).
   - The bootstrap pass under `claudx` has known escapes outside its data
     root: the machine-wide standalone interpreter's `bootstrap_lib.pth`, and
     a fetch of the real marketplace clone.
   - The child would read the real `~/.codex/sessions`, not the simulated
     rollout.

#### Second attempt, 2026-09-24, with an owner-authorized sandbox login

The owner authorized one scratch-HOME test session to use the existing
Claude Code login (ruling "1B", 2026-09-24). The grant does not cover
sending, printing, committing, or keeping the credential. Result: the
session layer is still **UNRUN**. Assertions 1, 2 and 3 were not exercised
by a live agent.

Login methods, in order of least exposure:

1. A config-dir override with HOME kept real was rejected before any
   attempt. `usage_budget.VERDICT_CACHE` and `CODEX_SESSIONS_DIR` are built
   from `Path.home()` (`usage_budget.py:145-155`), so a real HOME would
   read and write real quota state no matter where `CLAUDE_CONFIG_DIR`
   points.
2. Only the non-secret `oauthAccount` section of `~/.claude.json` was
   copied into the scratch HOME (plus `hasCompletedOnboarding`). A trivial
   `claude -p` child, with CLAUDECODE cleared and HOME and USERPROFILE on
   scratch, exited 1 with `Not logged in - Please run /login`. The account
   file is not enough, because the keychain lookup follows HOME.
3. Two steps were refused by the harness permission classifier
   (Credential Exploration), even with the owner's authorization cited:
   a metadata-only `security find-generic-password` probe (no `-w`/`-g`)
   under the scratch HOME, and a symlink from
   `<scratch HOME>/Library/Keychains` to `~/Library/Keychains` that a trap
   would remove. As the brief required, no further workaround was tried.
   Copying the credential into scratch (the last resort) was not attempted.

To unblock: the owner must allow the keychain step as its own permission
rule. The narrowest rule is a Bash allow for
`ln -s ~/Library/Keychains <scratch>/home/Library/Keychains`, for one
drill run only. It passes no secret through the agent, and the symlink is
removed when the run exits. The alternative is to set
`CLAUDE_CODE_OAUTH_TOKEN` in the child's environment from a
`claude setup-token` token that the owner creates.

Cleanup: the scratch probe directory, including the copied account file,
was deleted after the attempt. The keychain symlink was never created. No
credential was read, printed, or written.

For these reasons, no nested agent session ran. The session layer's live
behaviour (what an agent announces, and whether it re-selects correctly) is
**UNRUN**. That includes the exit-0 wrong-result variant.

#### Third attempt, 2026-09-24, owner token

Result: the session layer **RAN**. Assertions 1, 2 and 3 PASS, one live
run each. Fixture: SIMULATED, as before.

Login method. The owner created a long-lived token with
`claude setup-token` and gave it for this run (ruling "1B"). It reached the
child only as the `CLAUDE_CODE_OAUTH_TOKEN` environment variable of that one
child process. The launcher script read it from a variable set for the same
shell invocation, so the script file never held it. No keychain access and
no copied credential file were used. After the runs, every capture file was
searched for the token and none held it.

Setup, per run (three runs, each with its own fresh fixture):

- HOME and USERPROFILE on a scratch directory. The launcher printed both and
  `cd ~ && pwd -P` before the child started.
- A nested `claude -p` (Claude Code 2.1.281, `--model opus`, stream-json
  transcript), with CLAUDECODE, CLAUDE_CODE_SESSION_ID, BOOTSTRAP_PYTHON and
  BOOTSTRAP_PROJECT_PYTHON cleared, and a 1200 s bound.
- Dev-tree plugins by `--plugin-dir`: awesome-kit, llm-scripting-kit and
  skills-kit from this working copy. This deviates from `claudx` on purpose:
  the bootstrap dependency was met by a stub holding only bootstrap's
  `plugin.json` (no hooks), so no bootstrap pass ran and none of the three
  `claudx` escapes could fire. The scratch plugin-venv paths the skill and
  the `llm-scripting-kit` shim use were wrappers that run the installed
  awesome-kit venv interpreter with PYTHONPATH on the dev
  `llm-scripting-kit/lib`, `bootstrap` and `skills-kit`.
- The scratch project (a git repo) put a fake `codex` first on PATH:
  `--version` prints `codex-cli 0.146.0`; `exec` in mode `quota` copies an
  exhausted rollout (the `test_risk_drill.py` shape) into the scratch
  `~/.codex/sessions`, prints the usage-limit error and exits 1; mode
  `quota-write` first writes `kept.txt` and `codex-scratch.txt`; mode
  `wrong` writes `ANSWER: 5` to the `-o` file and exits 0. A healthy rollout
  an hour old made codex AVAILABLE at the first render. A scratch
  `llm-scripting-kit.yaml` set `conserve_usage` on `sol` and `luna`.
- Prompt: invoke `awesome-kit:orchestrate`, self alias `opus`, classify the
  unit `parallel-leaf` + `known` + `rule-applying`, dispatch by the render,
  verify the result, re-render at the end. The prompt did not mention
  `record-halt`, quota, workspace reset, or how to treat a wrong result.
  The first render's row 5 was
  `luna    codex          available  95% left, 80% of window   pace 119%   [default]`.

**Assertion 1: PASS** (unit: compute 2+2, read-only; codex mode `quota`).
Transcript order: `route: add-2-2 -> luna; default`, then
`codex exec ... -m gpt-5.6-luna ...` returned
`ERROR: You've hit your usage limit. ...` and `EXIT=1`, then
`llm-scripting-kit record-halt luna` returned
`{"budget": {"detail": "observed quota/credit halt at dispatch", ... "status": "out-of-quota", ...}, "entry": "luna", "kind": "quota", "recorded": true}`,
then `route: add-2-2 -> sonnet; luna failed: quota` and an Agent-tool
dispatch with `model: sonnet`, which returned `ANSWER: 4` (accepted). The
closing render showed:

```
5. If `parallel-leaf` + `known` + `rule-applying`:
     luna    codex          out of quota until 2027-01-20 21:34 UTC
     sonnet  claude/agent   n/a (unpaced)   [default]
```

The agent also ran `git status --porcelain` before re-selecting and found
nothing to reset.

**Assertion 2: PASS** (same unit; codex mode `wrong`). One announcement,
`route: add-2-plus-2 -> luna; default`. The codex run exited 0 and the
result file read `ANSWER: 5`. The agent made no further route, ran no
`record-halt`, and dispatched nothing else. Its join line:
`join add-2-plus-2: disposition=rejected; cause=worker; verified=result.md == "ANSWER: 4" (failed: got "ANSWER: 5")`.
It quoted the rendered rule: "A schema-invalid or wrong result from a run
that exited 0 is a task failure, not a trigger." The closing render still
showed luna `available ... [default]`.

**Assertion 3: PASS** (unit: create `answer.txt` with `ANSWER: 4`; codex
mode `quota-write`). Order: `route: answer-file -> luna; default`, the
usage-limit error with `EXIT=1`, then
`llm-scripting-kit record-halt luna; echo "EXIT=$?"; git status --porcelain`
(which listed `?? codex-scratch.txt` and `?? kept.txt`), an inspection of
both files, then `rm codex-scratch.txt kept.txt && git status --porcelain --ignored`,
and only then `route: answer-file -> sonnet; luna failed: quota` and the
sonnet Agent dispatch. The final tree held only `?? answer.txt`
(`ANSWER: 4`, checked with `od -c`) and the ignored `bin/`. The closing
render showed luna out of quota until 2027-01-20 21:34 UTC. The reset
removed exactly the files the halted run wrote; it did not use
`git clean` or a fresh worktree, which the rule also allows.

Real state check. Before and after all runs:
`~/.claude/plugins/data/plugins-kit/llm-scripting-kit/usage-verdicts.json`
mtime 1790263287 (Sep 24 10:21:27), `~/.codex/sessions` 1788018694 and
`~/.codex/sessions/2026` 1788277389, all unchanged. None of the three child
session ids appears in the real verdict cache, and no `*drill*` rollout
exists under the real `~/.codex/sessions`. The fake `codex` logged every
call, so the real `/opt/homebrew/bin/codex` was never run.

Cleanup: an exit trap in the launcher cleared the token variable and
checked both capture files; the calling shell unset it too. The scratch
HOMEs, projects, bootstrap stub and captures were then deleted.

### What ran instead: the render the agent acts on

`orchestration_guidance.py --self opus` ran from the dev tree under this
environment:

- The repository `.venv` interpreter, with PYTHONPATH set to the dev
  `llm-scripting-kit/lib`, `bootstrap`, and `skills-kit`.
- HOME and USERPROFILE set to a scratch directory. The child printed
  `Path.home()` to confirm the scratch path.
- `CLAUDE_BOOTSTRAP_DATA_ROOT` set to scratch, and `CLAUDE_CODE_SESSION_ID`
  unset.
- A fake `codex` first on PATH (`--version` returns 0; a dispatch prints the
  usage-limit error and exits 1).
- A scratch `~/.claude/config/llm-scripting-kit.yaml` that sets
  `conserve_usage: true` on `sol` and `luna`, and a scratch exhausted
  rollout.

The real verdict cache kept its mtime (13:28) through the whole drill.

**Known exhaustion (the rollout is exhausted before the first render):**

```
2. If `novel` + `load-bearing`:
     fable  claude/agent   n/a (unpaced)   [default]
     sol    codex          out of quota until 2027-01-20 21:34 UTC
5. If `parallel-leaf` + `known` + `rule-applying`:
     luna    codex          out of quota until 2027-01-20 21:34 UTC
     sonnet  claude/agent   n/a (unpaced)   [default]
```

The single-entry `cross-check` row `[sol]` renders the floor instead:
`sol: out-of-quota until 2027-01-20 21:34 UTC (codex account reports no credits remaining; ...)`.

The Rule and Re-select lines that the agent is told to follow are printed
verbatim under the rows. They carry all three assertions as prose:

- The failure kind in the announcement: `<prior entry> failed: <kind>`.
- "A schema-invalid or wrong result from a run that exited 0 is a task
  failure, not a trigger."
- "Before re-selecting a unit that may have written, reset its workspace to
  its launch state or re-run it in a fresh worktree."

Whether an agent obeys that prose is what stays UNRUN.

**Exhaustion first seen at dispatch (same session key; the rollout turns
exhausted after the first render):** see F1.

## Findings

**F1 (defect: design-conformance gap, session layer). A quota halt observed
in a session is never written back, so the menu keeps offering the exhausted
codex entry as `[default]` for the rest of the session.** Status: FIXED in
llm-scripting-kit 0.47.0 -- see "F1 fix" below.

Evidence, from the scratch-HOME render. The session key stays the same. The
first render reads a healthy rollout; a simulated dispatch then leaves an
exhausted rollout; the second render follows:

```
before:  luna    codex          available  80% left, 60% of window   pace 133%   [default]
after:   luna    codex          available  n/a (no reading)   [default]
new key: luna    codex          out of quota until 2027-01-20 21:34 UTC
         sonnet  claude/agent   n/a (unpaced)   [default]
```

Cause:

- `pinned_evaluate` returns a stored AVAILABLE verdict without re-reading
  (`plugins/llm-scripting-kit/lib/llm_scripting_kit/usage_budget.py:814-869`,
  early return at :858).
- Only `record_observed_halt` (:873) moves the verdict. Its only callers are
  `run()` (`declaration.py:870`) and job-kit (`job_kit/run.py:951`).
- orchestrate's session path has no way to record the halt. SKILL step 4
  (`plugins/awesome-kit/skills/orchestrate/SKILL.md:201-218`) and the Re-select
  line (`declaration.py:116-124`) tell the agent to re-select, but not to
  record the halt.

The design's "Stale verdict" paragraph says an observed quota halt writes
OUT-OF-QUOTA. That holds for `run` and job-kit, but not for a session caller.

Consequence: each later unit in the same session is defaulted to the spent
codex entry again. It fails once more before the agent re-selects, unless the
agent remembers the earlier halt from its context.

F1 fix. A `record-halt` CLI verb
(`llm-scripting-kit record-halt <entry> [--kind quota|credit] [--resets-at EPOCH]`)
calls `record_observed_halt` for an entry that declares `conserve_usage`,
under the current session key. The reset is `--resets-at`, else the pool
reading's reset, else the five-hour latch. The session Re-select line
(`RULE_TRIGGER_SESSION`) tells the agent to run it before re-selecting or
re-running describe with `--exclude`. The in-session drill check
(`check_assertion_1_in_session` in `test_risk_drill.py`) shows the stale
`[default]` after an unrecorded halt, then codex out of quota until its
reset and `opus` as `[default]` after `record-halt`. A paired red case makes
`record_observed_halt` a no-op and the check fails. Whether a live agent
runs the verb stays UNRUN, like the rest of the session layer.

**F2 (wording mismatch; RESOLVED).** The design and R31 checked for the
reason "codex: out of quota", while the shipped Rule line prescribes
`<prior entry> failed: <kind>`. Orchestrator decision: keep the shipped
format. The announcement reads `<entry> failed: quota` (for example
`sol failed: quota`), and that is the form a live drill passes. The design's
"Biggest risk" wording is aligned to it. The floor line (`out-of-quota`) and
the menu (`out of quota until <time>`) are separate surfaces and unchanged.

**F3 (observation, not a defect).** Process-layer quota classification
depends only on the rollout re-read. The first red case shows that the
usage-limit prose on stderr is deliberately left unclassified
(`test_completion_codex_backend.py` pins it). The re-read takes the newest
rollout under the sessions directory by mtime
(`usage_budget.py:488-495`), not the rollout of this run. Within an
exhaustion window, any non-zero codex exit is therefore classified as quota.
While the pool really is spent this is the correct answer; it is recorded
here only so a later reader does not mistake it for per-run evidence.

**F4 (gap, session layer; FIXED in llm-scripting-kit 0.48.0 and job-kit
0.9.0). A recorded halt marked only the named entry, so a sibling entry on
the same codex account stayed `[default]`.** In the third attempt's
assertion 1 run, after `record-halt luna` the closing render still showed
`sol  codex          available  n/a (no reading)   [default]`, because
`record_observed_halt` wrote the verdict for the one entry it was given and
`pinned_evaluate` returned `sol`'s stored AVAILABLE verdict without
re-reading; the cost was one extra failed dispatch per sibling entry per
session. The fix: `usage_budget.quota_pool_key` (same harness account, same
pool, same model-scoped label) is the one definition of a shared quota, used
both by `describe` for its "shares <pool> with" label and by
`record_observed_halt`, which, given the caller's `entries` registry, writes
OUT-OF-QUOTA for every sibling that declares `conserve_usage`, each under its
own spec. The `record-halt` verb, `run()` and job-kit's `_record_quota_halt`
all pass the registry. The Stale-verdict rule is unchanged: a verdict still
moves downward only on an observed halt. `test_risk_drill.py` pins it at both
layers (`codex-mini` shares `codex`'s pool and goes out of quota, `codex-5h`
on another pool stays usable, and `run()` makes no dispatch into the spent
sibling), with paired red cases that write one entry only.

## Not run

- Repeated session-layer runs. Each assertion ran once against a live
  agent, so the result shows the behaviour occurs, not how often.
- The session layer through `claudx` itself: the third attempt loaded the
  dev plugins by `--plugin-dir` with a hookless bootstrap stub, so the
  bootstrap pass and its provisioning were not exercised.
- A live `cross-check` unit after a halt on a sibling entry (F4); the fix
  is pinned by the process-layer and CLI drill cases only.
