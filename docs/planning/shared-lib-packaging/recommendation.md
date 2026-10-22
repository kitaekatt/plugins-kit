# Shared library packaging: go / no-go recommendation

Date: 2026-10-22. Status: the user ruled GO for a bootstrap-independent databench on 2026-10-22, superseding the timing NO-GO below
("but i want databench in its own .venv the context is i want to make databench installable via npm and i don't want
people to have to install bootstrap or plugins-kit to run databench"); the plan is `packaging-plan.md`. Owning task:
`shared-lib-packaging` (feasibility study; parent `task-kit-extraction`, item
`databench-integration`). Every measurement below is a scratch measurement, 2026-10-22
(uv 0.12.5, CPython 3.12.9, macOS arm64): builds and installs from scratch copies; no
tracked plugin, version, tag or consumer was changed.

## Question and outcome options

Can `llm_kit` (`plugins/llm-kit/lib/llm_kit/`) and `bootstrap_lib`
(`plugins/bootstrap/bootstrap_lib/`) be installed into a consumer's own `.venv` as versioned
packages, in place of (or beside) bootstrap's `shared_lib_imports` `.pth` link? The consumer
that reopened the question is `$DEVROOT/databench`.

- (a) Proceed with versioned packages for both libraries.
- (b) Proceed in part.
- (c) Do not proceed; keep the `.pth` mechanism.

## Recommendation: NO-GO NOW (timing); switch atomically and pin deliberately when the .venv install is wanted

This is a timing no-go, not a rejection of the architecture. The measured design stands
(outcome (b) in shape: package and pin `llm-kit` only). Reasons it is not worth doing now:

- The remaining benefits (reproducibility, upgrade control, removing the signature probe)
  are real, but no present failure or costly churn justifies the migration. Plugin releases
  are already controlled by the sole user; removing the probe is cleanup, not user value.
- The CLI split-brain arises only once imports are pinned while the CLI floats, so the
  switch must be atomic. It is a reason for atomicity, not for starting now.
- Reproducibility would still be partial while `bootstrap_lib` stays linked and llm-kit's
  user-level registry files keep moving.
- The `.pth` path works and follows the installed plugin automatically.

Triggers to run the plan below: (1) installing databench into its own `.venv` becomes an
actual distribution requirement, or (2) real version drift between the plugin copy and
databench causes observed failures.

### Ready-to-run plan for a trigger

Run all five steps in one change (never pin imports while the CLI still floats). Package and pin `llm-kit` for databench, inside the bootstrap-managed environment databench
already runs in. Bootstrap dependence is accepted during the transition and a pinned `.venv`
is the intended end state (`dev/tasks/task-kit-extraction/plan.md:274`), so detached
distribution is not a precondition. The first increment, in order:

1. Add an exact-SHA `llm-kit` direct dependency (with the `config` and `sdk` extras at least)
   to databench's `pyproject.toml`; regenerate its lock.
2. Remove only `llm_kit` from databench's `shared_lib_imports`; delete the stale
   `<venv>/.../site-packages/llm_kit.pth` (bootstrap will not). Keep the plugin edge,
   `bootstrap`, and the `bootstrap_lib` link.
3. Route every llm-kit invocation through one place that uses databench's own venv
   interpreter and an explicit-argv child entrypoint calling `llm_kit.cli.main(argv)`.
4. Replace the llm-kit signature probe with an installed-version check; the SHA lives in the
   lock.
5. Test the actual models, completion and jobs verbs with a plugin venv present, plus `.pth`
   shadowing.

Value delivered by the increment alone: reproducible llm-kit behavior; no signature probe;
databench chooses when to absorb llm-kit changes (upgrade control). Cost: small-to-medium
(dependency and lock changes, stale `.pth` removal, invocation centralization, API wrappers,
cross-environment tests). **Work may stop after this increment.**
The complete bootstrap-independent destination is medium cross-repository work and is not
part of this recommendation.

Fingerprint cost (measured and dismissed): `generator_fingerprint`
(`index_pages_worktree.py:845-920`) hashes five llm_kit module files, so an llm-kit release
makes each configured repository run one model-free index-pages pass (about 20 extra git
processes; pages are rewritten only when their own inputs change). The user agreed this is
not a factor; it is not a benefit of pinning and not a reason for it. Its withdrawal is what
turned the verdict from proceed to no-go now.

## Interface decision

- In-process versioned Python API for bounded calls: `models endpoints|describe|resolve`,
  `completion request-schema|repair-json`. Typed results and errors; keep
  `describe(..., allow=...)` for databench's M6 consent filtering. Where only the CLI exposes
  a call, a small public wrapper is needed (not measured here).
- Subprocess from databench's own pinned environment for `completion complete` and
  `jobs run|resume`: completion stays killable on timeout, jobs keep process isolation and
  durable execution. The child is `llm_kit.cli.main(explicit_argv)`, which does not re-exec
  (measured; see the evidence table).
- This shrinks the permanent CLI protocol contract to complete/jobs; argv, envelope, stderr
  and exit-code contracts for the pure calls disappear.
- Rejected: MCP server and HTTP API. Each adds service discovery, lifecycle, serialization
  and versioning, while databench is already planned to become the per-machine service (its
  plan M7). A daemon would be a second one. The user was open to alternatives to the CLI; none
  beats in-process plus subprocess for these call shapes.

## Deferred, and why

- `execution_event` extraction (second distribution under `plugins/bootstrap` with a
  re-export shim). Assessed viable: stdlib-only, 1026 lines, zero in-package imports, a
  versioned self-describing JSONL envelope; six importers (databench, workflow-kit, job-kit,
  content-pipeline-kit, llm-kit, llm-scripting-kit). Deferred: it matters only when databench
  drops bootstrap, and no plugin ships two distributions (a precedent to set; five consumer
  manifests, `_FIRST_PARTY_LIBS`, nested-pyproject release enforcement).
- `bootstrap_lib` edges `model_declaration` (models, jobs) and `interrupt_contract` (jobs)
  reached from llm-kit CLI paths. On-fleet bootstrap satisfies them; they must be packaged,
  extracted or removed before any bootstrap-independent state.
- A CLI protocol contract (schemas or golden fixtures) beyond complete/jobs. Parity tests pin
  llm-kit to the legacy CLIs, not to databench's usage; `public-surface.json` covers Python
  names only.
- Per-plugin tags in `publish.py`: SHA pins plus a lock give immutability for one controlled
  consumer; tags are convenience.
- A full `bootstrap` distribution (defects D2, D3) and a bootstrap-independent llm-kit
  (defect D1, `review_lane`): unneeded while bootstrap stays installed.

## Answers to the prior objections

User objection 1, version management (user's words: "a mix of i think version management
concerns and the lack of a true consumer who needs the libraries"):

- pyproject version equals plugin version for both libraries and the gates reject drift
  (`scripts/check_pyproject_sync.py`; `scripts/publish.py:681-690`). Wheel METADATA matched
  (llm-kit 0.4.0, bootstrap 0.158.3).
- A master SHA is an immutable pin; `uv add` wrote it and `uv.lock` recorded `rev` and
  resolved SHA. Exact identity is package version plus SHA.
- Compatibility: pinned, deliberate bumps, two-minor deprecation policy
  (`docs/reference/deprecation-policy.md`), Python surface in `public-surface.json`.
  Gap: the gates require only "some version change", not a breaking-versus-compatible call.

User objection 2, no true consumer: databench imports four llm_kit modules
(`pipeline.execution.{deadlines,failure_cache,scheduler}`, `pipeline.runtime.gate`) plus
`bootstrap_lib.execution_event`, and its signature probe is a measured cost of the
`.pth` path (no version is carried). The consumer inventory also found `bootstrap_lib.managed_state` in
claude-settings and codex-settings, so databench is not the only external consumer of
`bootstrap_lib`.

Mode 4 ("Off-fleet pip install: Not supported",
`plugins/bootstrap/skills/bootstrap/references/library-consumption.md`), three reasons:

- No real version to pin: partly holds (no tags exist), but lockstep versions plus a locked
  SHA give a reproducible record for a single controlled consumer.
- No update channel: true and intended. Deliberate bumps are the benefit, not the defect.
- Dependency closure absent from pyproject: confirmed (D1, 19 import sites). The increment
  leaves bootstrap on the fleet, which satisfies the closure; a fresh venv with llm-kit alone
  fails `import llm_kit.dispatch.review_lane`.

If the user says go, Mode 4 should be amended for explicitly package-ready distributions
(llm-kit first). This study does not edit it.

## Evidence

| Claim | Measurement | Result |
|---|---|---|
| Both libraries build wheels | `uv build --wheel` from scratch copies | `llm_kit-0.4.0` (174 entries), `bootstrap-0.158.3` (101), exit 0 |
| llm-kit is install-clean for databench's modules | fresh venv, the interpreter in isolated mode (`-I`), 4 consumer modules | import OK; zero third-party modules loaded |
| D1 undeclared `bootstrap_lib` | llm-kit-only venv, walk of 168 modules | 2 fail: `dispatch.review_lane`, `pipeline.runtime.yaml_extract` (pyyaml only in extras) |
| D2 data files missing | `review_profiles.resolve_config` in bootstrap venv | ConfigError (`defaults/*.yaml` absent from wheel) |
| D3 plugin-layout paths | read `shell_hook.py:51`, `harvest.py:63` | break outside the plugin tree; not hit by `execution_event` |
| Git-subdirectory install with SHA pin | `uv add "llm-kit @ git+...@SHA#subdirectory=plugins/llm-kit"` | exit 0; 44.7 s cold (whole repo fetched, ~69 MB), 0.5 s warm; lock pins SHA |
| `@master` | same, ref `master` | lock stores branch and resolved SHA; moves only on `uv lock --upgrade` |
| `tool.uv.sources` vs `Requires-Dist` | built consumer wheel both ways | sources are not carried into the wheel; direct URLs in `project.dependencies` install via `uv tool install` |
| Repo visibility, tags | `gh repo view`; `git ls-remote --tags` | PUBLIC; 0 tags; `publish.py` creates none |
| Re-exec blocker | stub plugin venv under scratch HOME | console script and the module entry `-m llm_kit.cli` both exec into the plugin venv; `main(argv=[...])` does not |
| `.pth` shadows the wheel | isolated-mode interpreter (`-I`), scratch lib copy with marker | bootstrap `pth_line` does `sys.path.insert(0, ...)` (`shared_lib.py:105-132`): `.pth` copy wins; plain-path `.pth` loses; reinstall leaves it; metadata still reports the wheel |
| Consumer inventory | grep of manifests and imports | databench; claude-settings and codex-settings (`managed_state`); six `execution_event` importers |
| databench's CLI use | read call sites | 10 sites in `index_pages_runner.py`, `generate_config.py`, `markup.py`; no consumer-CLI contract in llm-kit |

## Risks

- Split-brain: pinned imports with a subprocess that re-execs into the moving plugin venv.
  Mitigation: step 3 plus step 5 run with a plugin venv present.
- Stale `llm_kit.pth` keeps shadowing the pin, and reports the wheel version while loading the
  `.pth` code. Bootstrap has no revocation (`docs/reference/shared-lib-architecture.md`,
  Finding 3). Mitigation: step 2 and a shadowing regression test.
- Missing extras: `models endpoints` needs pyyaml; transport completions need `sdk`.
- Pinned library, moving registry: model endpoints, consent and API-key files are user-level
  and shared by the pinned and plugin copies; a format change could reach an older pin. Not
  measured; the deprecation policy is the mitigation.
- Cold fetch fetches the whole repository (~69 MB); every additional machine or CI pays it.
- A private repository would need SSH or token credentials on every machine (general
  knowledge, not tested). The repository is public.
- Unmeasured: PyPI acceptance of direct-URL `Requires-Dist` (irrelevant to a private
  consumer); the full-verb wheel test is step 5.

## Decisions for the user

1. Accept the timing no-go (recommended: accept; keep the `.pth` path until a trigger), or
   override and run the five-step increment now.
2. Confirm pinned-with-deliberate-bumps as the policy for the eventual switch (recommended:
   pinned). Rule: an exact SHA, bumped on purpose. Reasons: repeatable environments,
   reviewed upgrades, straightforward rollback, and an unrelated plugin update cannot
   silently change databench. Follow-latest would keep most of today's coupling while adding
   packaging machinery, which removes the reason to switch.

## Hypotheses

- H1, versioned packages in a `.venv`: confirmed mechanically (build, git-subdirectory
  install, SHA pin, wheel install); not sufficient alone because of the CLI re-exec.
- H2, npm: not tested; no case for it was found.
- H3, two-copy drift: the named files (`scheduler`, `FailureCache`, `atomic_json`) take
  caller-supplied paths and are not shared; the real drift path is executable code, the CLI
  re-exec, followed by shared user-level registry files.
- H4, `execution_event` extraction: viable, deferred.

## Advisor record

- Sol, versioning (cross-check 1): AMEND on all five points; lockstep for llm-kit, SHA pins
  and no tags, define the promised surface, fix D1 and D2 or avoid full bootstrap, found the
  CLI drift path. Lean: proceed in part.
- Sol, go / no-go (cross-check 2): AMEND; no-go unless the user chooses a
  bootstrap-independent product; found the extra `model_declaration` and `interrupt_contract`
  edges and a medium cost.
- Sol, follow-up (cross-check 3): after the user ruled that bootstrap dependence is
  acceptable, PROCEED IN PART incrementally; pinned bumps. That ruling listed the
  fingerprint rerun as a benefit, which the user then dismissed.
- Sol, re-ruling (cross-check 3, binding; 2026-10-22): NO-GO NOW (timing). Keep the measured
  design; run the five steps atomically when the `.venv` install is a distribution
  requirement or real drift causes failures; pin an exact SHA with deliberate bumps.
- No Astra or Fable escalation: Sol judged none warranted.
