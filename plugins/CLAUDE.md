# plugins/ -- plugin implementation conventions

Implementation-level conventions for the plugin code under this directory. Repo
orientation, the publish flow, and the bootstrap engine overview live in the
root `CLAUDE.md`; this file is the home for "how to write the plugin code
itself" details that only matter when you are editing a plugin.

## Plugin-wide authoring and shipping gates

### The plugin-opinion razor

**The vision: the default is awesome and opinionated. Configurability is earned, not
assumed.** These plugins exist to expose powerful customizations that let a user produce
their best experience -- but an option nobody needs is a worse default plus a maintenance
burden. So a plugin holds its opinions confidently, and a setting appears only when the
opinion demonstrably costs a real user something.

The test that earns a config seam:

> **Can I articulate ONE SERIOUS, or TWO DISTINCT, user-preference scenarios in which this
> not being configurable leaves the user needing or wanting to uninstall the plugin, or to
> take remedial action against the default?**

The scenarios must be grounded in **realistic preferences of Claude Code power users** --
this marketplace's actual audience. Not hypothetical teams, not "someone might". A scenario
you cannot picture a power user actually having does not count, and neither does one whose
remedy is a single self-explaining error message.

If the test PASSES, the opinion must become configurable, with the opinionated default
preserved so nothing changes for everyone else. If it FAILS, leave it hardcoded -- that is
the correct outcome, not a deferred TODO.

The remediation for a passing test is always a configuration seam -- never prose telling the
reader to tolerate the default. When THIS repo cannot live with one of its own plugins'
opinions, that is a scenario, already evidenced: documenting the resulting warnings as noise
fixes one machine and converges nobody.

Criteria (OP-1..OP-7), how to detect each, worked examples of the test both passing and
failing, the findings table with per-finding verdicts, and the audit procedure:
[docs/reference/plugin-opinion-razor.md](../docs/reference/plugin-opinion-razor.md).

#### The register -- opinions that PASS the test but we decline to configure anyway

Rare by construction. An entry here concedes that real users will want this changed, and
states why we refuse regardless, plus what they should do instead. An unregistered,
unconfigurable opinion whose test passes is a finding.

- **bootstrap owns dependency provisioning.** Manifests are the single source of truth;
  there is no supported path for a consumer to hand-install into a plugin venv. A team that
  wants manual control should not enable the plugin -- partial adoption produces a machine
  whose bootstrap is permanently wrong.
- **skills-kit's Architectural rule tier is not configurable.** The type contracts are what
  make an audit comparable across projects; a project that disables them is not running the
  same audit. Optional-tier rules and thresholds ARE configurable, and
  `skills-kit/skills/md-domain/references/configuring-standards.md` documents the boundary. A team disagreeing with a
  contract adds its own criteria via an additive standards file.
- **awesome-kit:task privileges git as the automated VCS.** `archive` commits a
  dev/tasks folder's final state and removes it only where git is present. This is imposed
  rather than configured because a second VCS backend would be carried without being
  exercised: the maintainers track tasks in git, so a p4 path would ship untested and its
  first real failure would be on a consumer's machine. The degradation is deliberate and
  bounded, not silent -- outside a git repo the scripts run NO VCS commands, record the
  final state, keep the folder (`vcs_pending`), and hand submission to the agent. A
  Perforce team therefore gets a working task system whose retirement step is manual, and
  should either accept that or drive submission themselves; there is no half-working git
  path to be surprised by.

- **job-kit selects deterministically from the caller's stated preference order.** No
  scoring, no endpoint aliases, no learned or adaptive routing: a job names an ordered
  endpoint preference, requirements filter it against llm-scripting-kit's advertisement,
  and the first surviving entry runs. A user who wants "pick whichever is cheapest or
  fastest right now" has no way to express it, and that is the point -- an UNATTENDED run
  must be explainable from its inputs alone, because nobody is watching to notice that the
  runner chose differently than last time. Judgment-driven routing is a session concern:
  that user wants `awesome-kit:orchestrate`, whose whole job is deciding, not a runner
  whose job is executing a decision already made. Within a run, job-kit only ever NARROWS
  the stated order -- an endpoint that returned a persistent halt is excluded from later
  jobs -- and the ledger records every exclusion.

- **A run-level deny floor is a selection REQUIREMENT, not a best-effort request.** When a
  run declares tools an endpoint must not be able to use, an endpoint whose advertisement
  says it would silently drop that control is not selected at all. A team could reasonably
  prefer best-effort -- run on the endpoint anyway and accept the floor was not applied --
  and their only remedy today is to drop the floor from the job file entirely, which is
  exactly the outcome we refuse to make easy. A floor that is sometimes not applied is not
  a floor, and an unattended run is the case where nobody is present to notice the
  difference. The alternative for that team is to state the narrower endpoint preference
  they actually mean.

- **job-kit privileges git as the only workspace-isolation VCS.** Worktree-per-attempt
  isolation is git-only, for the same reason as awesome-kit:task's entry: a second VCS
  backend would ship untested and fail first on a consumer's machine. The degradation is
  bounded and recorded, never silent -- a job whose directory is not a git repo runs with
  cwd set to that directory and the attempt row says `workspace: none`. A Perforce team
  gets a working runner whose isolation is manual, not a half-working git path.

- **Above `max_parallel: 1`, job-kit forfeits ordering and offers nothing to get it
  back.** Jobs are submitted in declaration order and complete in any order; there is no
  priority, no ordering flag, and no DAG. A team that wants "run these four in parallel
  but that one last" has no way to say it, and their remedy is two runs. We refuse an
  ordering seam because it is a DAG in disguise: the first key would be honored only
  sometimes (a job whose predecessor is still running would have to block a worker), so
  the guarantee a user would read into it is one a flat set cannot make. Ordering is
  workflow-kit's job.

- **A halt narrows dispatch, and never cancels an attempt already running.** When an
  endpoint returns a persistent halt, job-kit excludes it from jobs dispatched afterwards
  and lets every in-flight attempt on it finish. A team could reasonably want the run to
  stop dead -- they are paying for calls that are likely to fail -- and their remedy is a
  narrower `max_parallel` or a narrower endpoint preference. We refuse to make cancellation
  available because an aborted invocation cannot be truthfully recorded: the ledger's
  central claim is that one attempt row is exactly one seam invocation and reports what it
  observed, and a killed attempt has no observed outcome to report. The same reasoning
  makes Ctrl-C stop dispatch rather than work.

- **One job-kit process per ledger.** A run is driven by one process; the parallelism is
  threads inside it. Two processes over one ledger is unsupported and undetected -- there
  is no claim or lease column, so nothing refuses the second one. A team could reasonably
  want to fan a run across machines, and their remedy is one run per machine. We refuse a
  lease because a HALF lease is worse than none: the crashed-process resume path exists
  precisely to pick a run back up after its owner died, and a lease that path must ignore
  protects nothing while making the ledger claim a guarantee it does not hold. A real
  answer is a lease plus an expiry plus a fencing token, which is a distributed runner --
  a different plugin, not a column.

- **Only a reviewer lane may run on a configured endpoint.** A review profile's
  `model` may name an llm-scripting-kit endpoint instead of an Agent alias, but the runner
  accepts that for the three REVIEWER lanes only; `validator` is refused by name. (Until
  2026-09-04 the set was `reviewer_b_diff_only_bugs` alone; it was widened to all three
  reviewers by owner decision, with the two context-reading lanes further restricted to
  harness endpoints -- `LANES_REQUIRING_AGENT_LOOP` in `bootstrap_lib.code_review.lane_prompts`.)
  A team could reasonably want its validators on a cheap local model, and the only remedy we
  leave them is to wait for the set to be widened -- so this is a stance, not a good default.
  We refuse to make it a setting because the validator is the CONTROL that suppresses a weak
  reviewer's false positives: a run with both a weakened reviewer and a weakened validator
  cannot tell you which one caused a regression, and the config key would make that the
  cheapest thing to reach for. Widening the set further is a plugin change, not a line of
  YAML.
- **A failed endpoint lane fails the review's coverage; it never falls back to an Agent.**
  A team could reasonably prefer "finish the review anyway on the default model", and the
  remedy we leave them is to drop the endpoint override. We refuse the fallback because the
  rendered review looks identical either way: a silent substitution hands back a review the
  reader believes ran on the model they configured, which is a false claim about what
  examined their change rather than a degraded one. The lane is reported failed and its
  files are marked uncovered, so the reader can re-run deliberately.

- **A `conserve_usage` verdict is pinned for the session and never re-evaluated
  downward.** llm-scripting-kit computes a paced endpoint's availability once per session
  key and reuses it; a CONSERVED verdict is recomputed only once its window resets, and an
  AVAILABLE one is never recomputed at all. A team could reasonably want live
  re-evaluation -- a session running for days holds an `available` verdict computed against
  numbers that have since moved -- and the only remedy we leave them is to start a new
  session (or `llm-scripting-kit usage --no-pin`, which inspects without changing what
  `seats` returns). We refuse the seam because the alternative is the failure the feature
  exists to prevent: an endpoint that was usable when work was planned against it
  disappearing mid-run, which strands that work with no signal a caller can act on. A
  verdict that only ever improves within a session is a guarantee; one that can flip either
  way is a race.

- **Code review renders to chat and is never persisted.** git-kit and p4-kit scope
  themselves to a conversational review; a team needing PR/Swarm comments or a CI artifact
  wants a different tool, and both SKILL.md scope blocks say so rather than assuming it
  silently.

An opinion that FAILS the test needs neither a register entry nor a seam -- it is simply a
good default. Opinions that PASS and are still unconfigurable are findings, tracked with
per-finding verdicts in the reference above; known examples are the task system's durability
roots and git privilege, and the code-review reviewer roster and model tiers.

**Submit gate:** Apply the plugin-opinion razor to every workflow opinion this change adds or hardcodes -- for each, either name the config key and its default, or state the scenarios you tried and why the test fails.
Applies to:
- plugins/

`plugins/` is where plugin development happens, and everything under each published
`plugins/<name>/` directory ships to other developers. The razor only works if it is applied per opinion at submit time, while the
change is still cheap to reshape -- once an opinionated default is published, teams have
built around it. One line per opinion discharges this, and "no opinions added by this change" is a valid and
common answer. Criteria and audit procedure:
[docs/reference/plugin-opinion-razor.md](../docs/reference/plugin-opinion-razor.md).

### Instructions we ship to Claude must be checkable

The razor's companion, one level over: it governs the OPINIONS a plugin imposes,
this governs the INSTRUCTIONS a plugin gives the agent. Some of our text arrives in a
consumer's session as `additionalContext` -- the same channel that carries untrusted
content -- so a receiving agent cannot tell a real standing authorization from injected
text claiming one, except by checking it. Three requirements, and they are not
negotiable by tone:

1. **True** as written, without a qualifier the reader does not have.
2. **Checkable** -- any claim of policy, permission, or prior user agreement names a
   file in version control the agent could open. A pointer into the same document that
   makes the claim is self-certification, not verification.
3. **Non-suppressive** -- never direct the agent to withhold from the user, or to move
   past a checkpoint the user would otherwise have. Text restraining CLAUDE on the
   user's behalf ("do not run it yourself, it needs their elevation") is the opposite
   and is always fine.

Two traps worth naming. When no record of user agreement exists, do not manufacture one
-- ground the instruction in what actually authorizes it and cite the document
describing that job; claimed consent that was never given is worse than none. And an
acknowledgement emitted before an action resolves ("Running.") paired with an
instruction not to elaborate makes a failed call indistinguishable from a success --
report outcomes, not intentions.

**Submit gate:** For every instruction to Claude this change adds or edits, confirm it is true, names a file backing any authority it claims, and neither withholds from the user nor bypasses them -- or state which criterion you judged not to apply.
Applies to:
- plugins/

Criteria AD-1..AD-5, detection methods, the findings table with per-site verdicts, and
the audit procedure:
[docs/reference/agent-directive-standards.md](../docs/reference/agent-directive-standards.md).

### Optional use of another plugin

A plugin that imports another plugin's shared library when it is installed, and does without it when it is not, answers two questions before it ships. Can it do its job without the library at all? If not, the library is REQUIRED and is declared `install: "auto"`. If so, can the action still hand the user an artifact that is true as read when the library is missing? If the artifact would be read as if the library had participated, the unit that needs it REFUSES with a diagnosis; if the gap can be stated inside the artifact, the unit is omitted and the artifact DISCLOSES it. Neither branch substitutes silently. A shared-lib link pins no version (the mechanism is described under "The bootstrap-provisioned venv and shared libs" below), so "absent", "too old" and "stale after uninstall" are three states an `import` cannot distinguish: probe the newest symbol you use and diagnose them apart, following the probe-failure message rule stated with that mechanism.

**Submit gate:** For every cross-plugin import this change adds or edits, state REQUIRED / REFUSE / DEGRADE and the reason the artifact stays true without the library; confirm the probe targets the newest symbol used and that the absent and too-old messages differ.
Applies to:
- plugins/

The decision rule, the mechanics each branch requires, the three runtime states, and the
reviewer checklist:
[bootstrap/skills/plugin-dev/references/optional-plugin-dependencies.md](bootstrap/skills/plugin-dev/references/optional-plugin-dependencies.md).

### Published-plugin boundaries

**A published plugin ships to other developers -- keep this repo's build machinery out of it.** Everything under `plugins/<name>/` is copied into a consumer's plugin cache, so a file that only makes sense inside plugins-kit is noise at best and misleading at worst: a generated fingerprint or baseline whose header names a `scripts/` tool the consumer does not have, a design doc recording our derivation rounds and remaining work, or generator plumbing embedded in a reference a consumer reads for guidance. Before adding content to a shipped plugin, ask **who reads this on a machine that is not ours** -- if the honest answer is "nobody", it belongs in the repo (`docs/`, `scripts/`, or a task folder), not in the plugin. The trap is incremental: maintainer material rarely arrives as its own file, it accretes inside a reference that already ships, so a file can double in size without anyone deciding to publish the additions. Watch for it particularly when a build step colocates its inputs with the artifact for convenience -- that convenience is a publishing decision.

**Plugin boundaries are hard boundaries for cohesion work.** Never move content between plugins -- or into another plugin -- to achieve skill cohesion. Plugins are independently versioned, installed, and bootstrapped units; relocating a skill/reference across a plugin boundary to satisfy CCP/CRP/ADP breaks that independence (cross-plugin caches, dependency edges, version coupling) and is never worth the cohesion gain. Cohesion refactors operate *within* a plugin only. When you spot a genuine cohesion opportunity that spans plugins -- two doer-skills in different plugins sharing a subject (e.g. git-kit `git-code-review` + p4-kit `p4-code-review`), a reference duplicated across plugins, a shared substrate two plugins both consume -- **surface it as an insight** (a `claude_md:` insight or a note in the relevant skill), do **not** act on it by relocation or by spawning a unifying plugin. Sharing across plugins is done through a library both depend on (e.g. `bootstrap_lib.code_review`), not by merging the skills.

**Reference file design** (within a skill): each reference serves a single audience and changes for a single reason (same cohesion framework). See `plugins/bootstrap/skills/bootstrap/` for the gold standard -- references split by audience with clean change boundaries.

## The bootstrap-provisioned venv and shared libs

The bootstrap plugin provisions a dedicated venv per plugin at a stable path
that does not change across versions:

```
Windows:     ~/.claude/plugins/data/<marketplace>/<plugin>/.venv/Scripts/python.exe
macOS/Linux: ~/.claude/plugins/data/<marketplace>/<plugin>/.venv/bin/python
```

A plugin can share a library with other plugins by declaring it in
`bootstrap.json`:

```json
"shared_lib_imports": ["bootstrap_lib"]
```

Bootstrap links the shared lib onto that plugin's venv via a `.pth` file. The
shared lib is therefore importable ONLY under the provisioned venv -- a
uv-managed venv (`uv run`) or a bare `python` builds a different environment
that has no such `.pth`, so the import fails there.

### Why shared libs rather than published packages

Sharing SOURCE at a stable path and linking it with a `.pth` is deliberate, not
a workaround for lacking an index. Every consumer is in-fleet, so one publish of
the OWNING plugin updates the source every consumer resolves -- no version bump,
no dependency constraint, no reinstall anywhere. The cost is the other side of
that same coin: consumers cannot pin, so a breaking change to a shared lib
reaches all of them at once.

Publish ordering: bootstrap must ship before or with llm-scripting-kit, or
CodexCliBackend raises ModuleNotFoundError on every consumer -- shared libs
resolve to the INSTALLED copy via .pth, not the dev tree.

The `.pth` linker pins no version, so an owner's change reaches every
consumer's venv on its next bootstrap pass, whether or not that consumer asked
for it. A consumer that needs a symbol only a particular owner version added
must not assume it is there: probe for it at import (a guarded `getattr` /
`try`/`except ImportError`) and fail with a message naming the owning plugin,
the version the symbol first shipped in, and the `claude plugin install` or
`update` command that fixes it -- never a manifest the consumer cannot edit --
rather than crash on a raw `AttributeError` deep in a call path. job-kit's
`select.py` is the worked example of this guard against
`llm_scripting_kit.completion`. This is the one statement of that rule; the
optional-dependency section above defers to it.

#### Who talks to an LLM, and through what

| Plugin | Imports from llm-scripting-kit | Owns above the seam | Published |
|---|---|---|---|
| content-pipeline-kit | `llm_scripting_kit.completion` (lazy/optional, via `content_pipeline.llm.platform` and `.llm.backends`) | Batch-run policy: retry, cost accounting, budgeting, concurrency, caching | Yes |
| job-kit | `llm_scripting_kit.completion` (`BackendSelection`, `Capabilities`, `adapter_capabilities`, `create_backend`, `match_capabilities`) | Deterministic endpoint selection from a job's preference order and requirements | Yes |
| workflow-kit | `llm_scripting_kit.completion.OpenRouterBackend` (via `scripts/openrouter_run.py`) | The `openrouter` node strategy: one non-Claude model call per workflow node | Yes |
| awesome-kit (orchestrate) | `llm_scripting_kit` (harness-model discovery, lazy/optional, via `orchestration_guidance.py`) | Backend/model advisory text for the orchestrate skill's routing decisions | Yes |
| bootstrap | `llm_scripting_kit.seats.discover_seats` (lazy/optional, via `bootstrap_lib.code_review.review_profiles`) | Peer-seat discovery for review profiles (`peer_when_available`); it never talks to an LLM | Yes |
| git-kit, p4-kit | `llm_scripting_kit.review_lane.main` via each kit's thin `scripts/run_review_lane.py` wrapper | Bootstrap setup and the REFUSE probe for the shared library; the lane's prompt lives in `bootstrap_lib.code_review.lane_prompts` and its guards in `llm_scripting_kit.review_lane` | Yes |
| yaml-data-editor-kit | none directly -- reaches it via content-pipeline-kit's `content_pipeline` (the dispatch binding in `dispatch/`) | The editor's dispatch planner, not the completion transport | No (`published: false`) |

The code-review kits are the one entry whose consumer is a thin wrapper around a
shared library. `bootstrap_lib.code_review.lane_prompts` owns the LLM-neutral half
(prompts, issue schema, and dispatch classification). The seam-calling half lives
in `llm_scripting_kit.review_lane`, which owns endpoint resolution, backend calls,
the context-window and output-contract guards, retries, and the result envelope.
Each kit vendors only the wrapper that performs bootstrap setup and hosts the REFUSE
probe for the shared library. This split is by DEPENDENCY, not by subject: importing
the seam into `bootstrap_lib` would make `openai` transitive through BOOTSTRAP,
which every other plugin depends on (`tests/bootstrap/test_dependency_completeness.py`
walks the first-party closure and enforces this). Reach for the same split whenever
shared machinery would otherwise drag a transport dependency down into a layer that
does not need one.

`bootstrap_lib/codex.py` is stdlib-only because `bootstrap_lib` is imported from
contexts where no third-party dependency is guaranteed to exist (SessionStart
hooks, a plugin whose venv has not been provisioned yet), so nothing here may
import outside the stdlib. `orchestrate` deliberately does NOT consume it.
orchestrate's `detect_backend` stays stdlib-only and generic on purpose: coupling a policy
renderer to a codex-specific module would cost a manifest change, a version bump
and a venv re-exec guard to dedupe three lines.

The venv-scoping above is the ordinary consequence of a per-venv install rather
than fragility -- a `.pth` written into one environment no more appears in
another than a `pip install` does. The re-exec rule below is how a script
satisfies that precondition itself instead of pushing it onto its caller.

**The source is shared; third-party dependencies are not.** A plugin that
imports a shared lib declares that lib's third-party requirements in its OWN
`pyproject.toml` -- a consumer driving `llm_scripting_kit`'s OpenRouter path
declares `openai` itself. One shared lib is linked into several independently
provisioned venvs, so shipping its pins with it would impose a single resolution
on every consumer, and a consumer using only the paths that need no SDK would
install one anyway. `tests/bootstrap/test_dependency_completeness.py` catches an
omission.

### Plugin dependencies on bootstrap (declared + guarded)

Every plugin in this marketplace rides on **bootstrap** (venv, `bootstrap_lib`, `uv`, installed config). We make that dependency explicit in **two complementary layers**:

1. **Declared dependency (install-time).** The Claude Code plugin spec supports inter-plugin dependencies -- installing a dependent auto-installs/enables its dependencies, blocks disabling a still-needed dependency, and honors version constraints. Every plugin that depends on bootstrap declares it in its `.claude-plugin/plugin.json` as a **bare string** (bootstrap lives in the *same* marketplace, so `name` resolves within `plugins-kit`):
   ```json
   "dependencies": ["bootstrap"]
   ```
   This is the canonical fix for "user installed the plugin without bootstrap." Official docs (source of truth -- fetch when in doubt): https://code.claude.com/docs/en/plugin-dependencies and the `dependencies` field in https://code.claude.com/docs/en/plugins-reference.
   - **Same-marketplace deps are bare strings.** Do NOT add a `"marketplace"` field for a dep in this marketplace -- that field is *only* for a **different** marketplace and triggers the `allowCrossMarketplaceDependenciesOn` allowlist (a same-marketplace value gets treated as cross-marketplace and can fail installs).
   - **Unversioned on purpose.** A version constraint (`{ "name": "bootstrap", "version": "~0.12" }`) resolves against `{plugin}--v{version}` git tags (`claude plugin tag --push`), which this repo does not use -- pinning would cause `no-matching-tag`. Bare = "whatever the marketplace provides."
   - Declare it on **every** plugin **except** bootstrap itself -- whether or not the plugin ships a `bootstrap.json`. The edge is universal by design, so anything built on "bootstrap is present wherever a plugin is" holds without a per-plugin check; the fleet-wide user posture bootstrap owns ([docs/reference/first-run-experience.md](../docs/reference/first-run-experience.md)) is the load-bearing case. The former carve-out for `bootstrap.json`-less plugins is **retired** -- `agent-glue` was its only occupant when the carve-out was retired and declares the edge like everything else. Enforced at pre-commit by `scripts/check_bootstrap_dependency.py` (chained from `pre-commit-version-check.sh`; spec mirrored in `tests/repo-scripts/test_bootstrap_dependency.py`) and again, unbypassably, in `publish.py`'s preflight -- the hook can be skipped with `--no-verify`, a publish cannot.
   - It belongs in **both** `plugin.json` and the generated marketplace entry; `scripts/regen_marketplace.py` propagates it automatically. A `dependencies` edit is a manifest change: it needs a version bump to reach consumers (same rule as any `plugin.json`/`bootstrap.json` edit).

2. **Runtime guard (provision-time).** A declared dependency guarantees bootstrap is *installed*, not that it has *run* -- on first install bootstrap provisions each plugin's venv at the next SessionStart (and the cooldown can defer it). For that "installed-but-not-yet-provisioned" window, plugins that would otherwise crash with a raw `ModuleNotFoundError`/missing-interpreter error use the vendored **`bootstrap_guard.py`** (canonical: `plugins/bootstrap/bootstrap_lib/bootstrap_guard.py`). It is **stdlib-only** and **must never import `bootstrap_lib`** (that's the thing that may be missing); it detects absence via the per-plugin `~/.claude/plugins/data/<marketplace>/<plugin>/bootstrap.log` and exits with one actionable "install/enable plugins-kit:bootstrap" message instead of a raw traceback. It is **vendored** per plugin (copied next to the entry script, or into the plugin's `lib/` import path, and imported as a plain module), exactly like `path_repair.py`, with a drift test asserting copies match the canonical.

### Shared-lib scripts must re-exec under the plugin venv

**Rule:** a standalone script that hard-imports a bootstrap shared lib (e.g.
`from bootstrap_lib... import ...`) MUST call
`bootstrap_guard.reexec_under_plugin_venv("<plugin>")` at module top, BEFORE the
shared-lib import:

```python
from bootstrap_guard import reexec_under_plugin_venv   # vendored, stdlib-only
reexec_under_plugin_venv("p4-kit")

from bootstrap_lib.code_review.chunking import ...      # now resolvable
```

**Why:** a script must not trust the interpreter that launched it. Skills name a
script as `tool: ${CLAUDE_PLUGIN_ROOT}/scripts/foo.py` with no interpreter, so an
agent runs it under `python` / `uv run python` -- neither carries the shared-lib
`.pth`. Without the re-exec the import fails and the except-handler emits a
MISLEADING "bootstrap has not provisioned ... (missing: bootstrap_lib)" message
*even though provisioning succeeded* -- the venv just was not the one running.
`reexec_under_plugin_venv` re-execs into the provisioned venv (a no-op when
already there), making the script invocation-method-agnostic. This was the
actual `p4-kit` / `git-kit` `prepare_review.py` failure mode (fixed 2026-06-02).

`bootstrap_guard` is stdlib-only, so importing it can never itself trip the
missing-shared-lib failure (the vendoring discipline that keeps it that way is
the next section).

**Answer "am I already in the venv?" with `sys.prefix`, never by comparing
interpreter paths.** `uv` makes `.venv/bin/python` a symlink to the base
interpreter, so a resolved-path comparison false-positives in the common case.
The full causal chain and the PEP 405 reasoning are in the comment on
`bootstrap_guard.reexec_under_plugin_venv`. Surfaced 2026-08-21 (`hue-kit
discover` reporting `zeroconf` unprovisioned while it sat installed in that
venv); fixed in bootstrap 0.86.3, pinned by
`test_reexec_happens_when_venv_python_symlinks_to_the_running_base`.

The SKILL.md-side companion (write the explicit venv path in skill examples
rather than `uv run python`) is documented in the root CLAUDE.md insight
`host_python_via_plugin_venv`. With the script-side re-exec in place, the
SKILL.md guidance is a nicety, not a load-bearing requirement.

**Test gotcha: this same re-exec silently short-circuits pytest.** Importing
`prepare_review.py` triggers `reexec_under_plugin_venv`, which on a machine with
the plugin's venv provisioned calls `os.execv` and abandons the pytest process
ITSELF, not just the import -- so the run stops at collection with **exit 0 and
no output at all: a false green**. Setting `_BOOTSTRAP_GUARD_VENV_REEXEC=1`
makes the re-exec a no-op (see `_REEXEC_GUARD_ENV` in `bootstrap_guard.py`),
matching how the real script is invoked once the guard has already fired.

**Set it in the test package's `conftest.py`, not at the invocation.** Every
affected test dir does this at import time, so a bare `pytest tests/<dir>` is
safe with nothing to remember:

```python
os.environ.setdefault("_BOOTSTRAP_GUARD_VENV_REEXEC", "1")
```

Current setters: `tests/awesome-kit`, `tests/git-kit`, `tests/p4-kit`,
`tests/unreal-kit`. A dir whose tests import a re-execing script and which does
NOT set this is a latent false green, and the failure hides itself: in a
full-suite run an earlier conftest (alphabetically, `tests/awesome-kit`) sets
the var first, so the dir looks healthy and only breaks when run ALONE -- i.e.
in exactly the targeted TDD loop, never in CI. `tests/p4-kit` sat in that state
and silently ran 0 of its 188 tests (fixed 2026-08-09). When adding a
module-level `reexec_under_plugin_venv` to a script, check its test dir.

## bootstrap_guard.py is vendored byte-for-byte

`bootstrap_guard.py` is a stdlib-only guard that must run when `bootstrap_lib`
itself may be absent, so each consuming plugin ships its own copy rather than
importing the canonical. The canonical lives at
`plugins/bootstrap/bootstrap_lib/bootstrap_guard.py`; vendored copies live next
to the script that imports them (e.g. `plugins/p4-kit/scripts/bootstrap_guard.py`).

**Rule:** edit the canonical, then copy it byte-for-byte into every vendored
location. `tests/bootstrap/test_bootstrap_guard.py` asserts every copy matches
the canonical, and the guard must never `import bootstrap_lib`. Current vendored
copies: `git-kit/scripts`, `p4-kit/scripts`, `skills-kit/scripts`,
`unreal-kit/lib`, `hue-kit/scripts`, `awesome-kit/skills/task/scripts`,
`awesome-kit/skills/orchestrate/scripts`.

**The test globs `plugins/**/bootstrap_guard.py`, so it is the authority on that
list and this prose is not.** Sync by running the glob, never by working through
the names above: two copies (`hue-kit`, `awesome-kit/skills/orchestrate`) were
added without the list being updated, so an edit propagated by hand to the five
documented copies left both stale and only the test caught it.

`path_repair.py` follows the same vendoring discipline.

## Plugin shell code runs under bash 3.2 and zsh

The root CLAUDE.md rule "Shell scripts must survive bash 3.2 and zsh" applies
here with one extra surface: besides the scripts a plugin RUNS, it also owns
every shell line it GENERATES and hands to something else -- a hook, a
launcher shim, a command passed to `osascript`'s `do script` or a terminal
emulator's `-e`. Those run under the user's LOGIN shell, so a generated line
gets zsh even when the generating script is bash.

Both incidents behind that rule were ours:

- bootstrap 0.86.1 -- the SessionStart wrapper used the plain `"${ARR[@]}"` on
  an `ENGINE_FLAGS` that is empty on every unflagged SessionStart, immediately
  before launching the engine, so on every Mac the engine was never launched
  at all.
- secrets-kit 0.8.2 -- the spawned passphrase window's hold-open used
  `read -r -p`, so under zsh it died and the window dropped back to a prompt,
  scrolling the error it existed to display past as ordinary shell noise. The
  user reasonably concluded no window had opened.

Beyond `read -p`, the same bash-only class: `mapfile` / `readarray`,
`declare -A`, `${var^^}` / `${var,,}`, `&>>`, `wait -n`.

Both cases are pinned by
`tests/bootstrap/test_sessionstart_detach.py::test_possibly_empty_arrays_use_the_bash32_guard`
and
`tests/secrets-kit/test_terminal.py::test_posix_hold_open_avoids_the_bash_only_read_dash_p`.

## Plugin shell code also meets BSD userland, not just an older bash

The section above is about the shell DIALECT. This one is about the TOOLS the
shell calls, and it is a separate axis that the bash 3.2 / zsh rule does not
cover. macOS ships the BSD userland, so a GNU tool a script reaches for is
either absent or takes different flags -- and the failure is usually silent
rather than loud, because a script written defensively swallows the error.

The case that produced this section: `timeout(1)` is GNU coreutils and does
not exist on stock macOS at all; Homebrew installs it as `gtimeout`.
claude-ui-kit's statusline probes for both and, finding neither, skips every
`*.sh` segment rather than running one unbounded and stalling each prompt
render. Skipping is correct. Skipping SILENTLY was not: an absent segment and
"this machine cannot run segments" rendered identically, so a user's bar
quietly lost cells with nothing saying why (fixed in claude-ui-kit 0.11.0,
which renders one `[segments off: no timeout(1)]` marker instead).

Two rules follow.

- **A tool that may be absent must announce its absence, not degrade into
  silence.** This is the same rule as "when success and failure are both silent
  they are indistinguishable" in the section below, reached from a different
  direction. Absent OUTPUT and absent CAPABILITY are different facts and must
  not render identically.
- **A test may not assume a binary the script under test documents as
  OPTIONAL.** The statusline's own comment says the no-timeout path is real,
  yet `tests/claude-ui-kit/test_statusline.py` assumed `timeout` exists -- so
  the suite passed everywhere a developer ran it and failed on the one platform
  the branch was written for. Where a script probes for a tool, give it a
  `BOOTSTRAP_BIN_<TOOL>` override (the `BOOTSTRAP_BIN_JQ` precedent) and drive
  BOTH branches from the test. Set-but-empty declares "this machine has no such
  binary"; `${VAR+set}` is the POSIX set-vs-empty test and is safe under bash
  3.2, zsh, and `set -u`.

Others in the same class, none of which a Windows or Linux session will catch:
`sed -i` (BSD requires an argument), `stat -c` vs `stat -f`, `date -d` vs
`date -r`, `readlink -f`, `grep -P`, `mktemp` templates, and `base64 -w`.

## A detached process must keep an error channel

Session readiness is held by the hook's process exit AND stdout-pipe EOF, so a
child inheriting that pipe blocks exactly like foreground work -- mechanics and
measurement in
[engine-internals.md](bootstrap/skills/bootstrap/references/engine-internals.md).
Backgrounding with the fds redirected away is therefore correct. The trap is
discarding **stderr** along with stdout.

bootstrap dispatched provisioning with `2>&1` onto `/dev/null`; a fatal shell
error inside it reached no log, no stderr and no display file, so the failure
was indistinguishable from a clean pass -- and a healthy pass is also silent
(bootstrap 0.86.1; mechanism in engine-internals.md).

Three rules follow:

- Send a detached child's stderr to a FILE, not `/dev/null`. A file costs
  nothing against the measured constraint, because what holds the session is
  inheriting the parent's PIPE, not holding an fd on disk.
- Give the wrapper its own crash path, so a failure BEFORE the real program
  starts still reports.
- Never return an unconditional success string from a fire-and-forget spawn.
  secrets-kit's macOS launcher reported "opened a new Terminal window" for any
  `osascript` that merely STARTED, catching only a spawn `OSError` -- so an
  Automation-permission denial read as success (fixed in secrets-kit 0.8.3).

The general rule: **when success and failure are both silent they are
indistinguishable, and the failure gets attributed to something else.**

## Duplicated seam types across a shared-lib boundary

When one plugin's library adapts another's, the seam types are duplicated on
purpose: the consumer keeps its own `Response` / `Options` / protocol types as
its caller-facing surface and adapts field-for-field across the boundary, so a
shape change surfaces at the adapter instead of propagating silently.
content-pipeline-kit's `llm/backends.py` does exactly this over
`llm_scripting_kit.completion`.

The duplication has a cost to state wherever the seam is documented: two
identically-named types in a dependency chain are still two types. Until
content-pipeline-kit 0.20.0, `llm_scripting_kit.completion.halt.HaltError` and
`content_pipeline.llm.platform.HaltError` shared a name and nothing else, so a
consumer that caught the former around a `content_pipeline` call got a handler
that never fired and no error anywhere. content-pipeline-kit's type is
`PipelineHaltError` (`HaltError` remains there as a compatibility alias). When
adding a duplicated type to a seam, name it distinctly or document which side
a caller must import from.

## A caller that sets the deadline owns the timeout

A plugin above `llm_scripting_kit.completion` passes its own budget down as
`BackendOptions.timeout_s`. When that budget expires, the exception coming back
is evidence about the CALLER's deadline and says nothing about the endpoint --
so classifying it as a provider halt makes the consumer exclude a perfectly
healthy endpoint, and (where the halt is persistent) collapse a retry budget it
believes it still has.

job-kit hit this twice, with two different transports and the same reasoning:

- CLI backends -- llm-scripting-kit maps `AgentTimeoutError` to
  `HALT_RATE_LIMIT`, which is correct for a caller that did NOT set the
  timeout ("CLI-layer backoff is functionally a rate limit"). job-kit did, so
  `max_attempts: 3` silently became one attempt.
- HTTP backends -- `OpenRouterBackend` began honoring `timeout_s` as the
  OpenAI client timeout, and `openai.APITimeoutError` is a SUBCLASS of
  `APIConnectionError`, so a consumer treating connection errors as
  "endpoint unreachable" excluded a merely slow endpoint for the whole run.

Two rules. Classify the caller's own deadline BEFORE any transport-error test,
because the timeout type is frequently a subclass of the broader one. And when a
transport starts honoring `timeout_s`, re-check every consumer's exception
classification -- the change is invisible from below and looks like a bug in the
consumer's retry policy from above.

The fix belongs on the CONSUMER side in both cases; plugin boundaries are hard
boundaries, and llm-scripting-kit's mapping is right for the callers that did
not set the deadline.

## Describing a plugin

Describe a plugin by the question it answers and the altitude it holds, in one
sentence, before anything else. llm-scripting-kit answers which endpoint,
model, key, and transport, then makes one call; content-pipeline-kit answers
what a run of many calls needs. State the dependency direction and the reason
for it in the same breath: a concern sits above the line when it is a policy
question -- what a cache is keyed on, what a budget is measured against, how
many calls run at once, what a valid output looks like -- that the lower layer
would have to answer once on behalf of every caller by guessing.

Three descriptions to avoid, because each one leaves the reader unable to
decide whether to adopt: by packaging (what files or subpackages it ships), by
size (line or module counts, "the largest module"), and by negation (what it is
not, or what it does not do). A missing capability is only worth naming when it
names the owner instead -- "run-level retry belongs to the caller" is a
boundary; "no retry" is a gap.
