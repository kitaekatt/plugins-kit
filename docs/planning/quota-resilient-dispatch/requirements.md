# Requirements: the declaration-format migration

Numbered, testable requirements for task `declaration-format-migration`. Each
requirement states one observable behavior. "Source" cites the owner
direction number(s) (verbatim in `dev/tasks/declaration-format-migration/log.md`
and quoted in `declaration-format-design.md`'s "Owner decisions" section) and,
where the requirement is a design choice rather than a direct instruction, the
design decision id (D1-D6) that made it. "Implemented by" cites the migration
table step (0-12) in `declaration-format-design.md`. "Verified by" names the
test file(s) from that step's table row, or "drill" for the biggest-risk
check, which has no unit test. This document holds the WHAT; the design doc
holds the HOW and is the one to read for mechanism, rationale, and code
citations.

## Format and syntax

- **R1.** Every plugin declares "which model(s) may do this unit of work" as
  a list of registry ids, in one format; a plugin that uses only Claude
  models still writes the list format, holding one id.
  Source: directions 1, 2, 9 (D1).
  Implemented by: step 2.
  Verified by: bootstrap validator shape tests (step 2 row).
- **R2.** A bare scalar declaration is accepted on read as a one-element
  list; the format always produces a list on write.
  Source: direction 2 (D1).
  Implemented by: step 2.
  Verified by: bootstrap validator shape tests; `tests/bootstrap/code_review/test_review_profiles.py`.
- **R3.** Entries name registry ids directly; the `agent:` and `peer:`
  prefixes are not required to write or read a declaration (accepted only
  through the deprecation window, R36).
  Source: directions 9, 12.
  Implemented by: steps 2, 4.
  Verified by: `tests/bootstrap/code_review/test_review_profiles.py`, `test_orchestration_guidance.py`.
- **R4.** A plugin may extend what ids a declaration can name (llm-scripting-kit
  adds codex, opencode, and OpenAI-compatible endpoints) without changing the
  list syntax itself.
  Source: direction 3 (D3).
  Implemented by: step 3.
  Verified by: `test_model_endpoints.py`.
- **R5.** Cross-entry selection and failover logic lives only in
  llm-scripting-kit's `describe`/`run` API; consuming plugins do not
  reimplement it.
  Source: direction 5 (D4).
  Implemented by: step 3.
  Verified by: `test_quota_selection.py`, `test_completion_factory.py`.
- **R6.** A declaration may name multiple ids served by one provider; no
  validator or `describe` call rejects or warns on same-provider entries
  (the original single-provider-per-list constraint is retired).
  Source: directions 4 (retired), 9.
  Implemented by: step 3.
  Verified by: `test_quota_selection.py` (no same-pool warning).
- **R7.** `fable`, `opus`, `sonnet`, and `haiku` are routable by every plugin
  through the harness alone (Agent tool, Workflow `agent()`, background task,
  `claude -p`), with no llm-scripting-kit dependency required.
  Source: direction 9.
  Implemented by: steps 3 (shipped `haiku` registry entry), 8 (core ids
  compile to `agent()`).
  Verified by: `test_model_endpoints.py`, `tests/workflow-kit/test_compiler.py`.
- **R8.** A core id whose merged registry entry carries a different harness
  or a `base_url` is classified shadowed/unroutable and contributes to the
  floor diagnostic; it does not abort manifest load or structural validation.
  Source: direction 9 (D1, D2).
  Implemented by: step 3.
  Verified by: `test_model_endpoints.py`, `test_endpoints.py`.

## Validation

- **R9.** The shared validator (`bootstrap_lib/model_declaration.py`)
  performs shape, parse, and normalize checks only; it never performs
  registry discovery, known-id checking, or an llm-scripting-kit import.
  Source: directions 13, 16 (supersede direction 9's loud-error clause).
  Implemented by: step 2.
  Verified by: new bootstrap validator tests asserting no known-id,
  path-drift, copy-drift, or shipped-defaults behavior.
- **R10.** A literal empty declaration list is a validation-time error.
  Source: D2.
  Implemented by: step 2.
  Verified by: new empty-list validator test.
- **R11.** A declaration containing the same id twice is a validation-time
  error.
  Source: D2.
  Implemented by: step 2.
  Verified by: new duplicate-id validator test.
- **R12.** Every plugin whose own code calls the validator or the
  llm-scripting-kit API declares the corresponding shared-lib import
  (`bootstrap_lib`, `llm_scripting_kit`) in its own `bootstrap.json` before
  that code ships.
  Source: D2 (supports directions 1, 3).
  Implemented by: step 1.
  Verified by: `tests/job-kit/test_bin.py`, `tests/skills-kit/test_asset_dependencies.py`.

## Selection and pace

- **R13.** `pace = remaining / window_remaining`, shown as a percentage,
  computed from `Budget`'s stored halves.
  Source: direction 11.
  Implemented by: step 3.
  Verified by: `test_quota_selection.py`, `test_llm_scripting_cli.py`.
- **R14.** Within the filtered/rendered set, entries that have a pace
  reading are re-sorted highest-pace-first among their own working
  positions; entries without one (out-of-quota, unpaced, near-reset, or no
  reading) keep their declared places, and ties keep declaration order.
  Source: direction 10.
  Implemented by: step 3.
  Verified by: `test_quota_selection.py` (ordering example), `test_model_endpoints.py`.
- **R15.** An unattended caller (job-kit, `run`) takes the first usable
  entry of the pace-ordered list; the run is explainable from the declared
  list plus the logged pace readings.
  Source: direction 10.
  Implemented by: steps 3, 6.
  Verified by: `tests/job-kit/test_select.py`, `test_model.py`, `test_runner.py`.
- **R16.** An in-session caller (`describe`) is shown the same pace-ordered
  rendered subset with its first usable entry marked default, and may choose
  any usable entry by judgment, announcing the choice as
  `route: <unit> -> <entry>; <reason>`.
  Source: directions 6, 8.
  Implemented by: steps 3, 4.
  Verified by: `test_orchestration_guidance.py`, `tests/repo-scripts/test_agent_directives.py`.
- **R17.** `describe` marks an entry matching the caller's own model
  `[author]`; guidance prefers a non-author entry, and allows the author only
  when no other usable entry exists, stating so.
  Source: direction 6.
  Implemented by: step 3 (seats data), step 4 (render/prose).
  Verified by: `test_seats.py`.

## Skip, render, and floor

- **R18.** An id that does not resolve to a registry entry is skipped at
  selection with no visible notice.
  Source: direction 13.
  Implemented by: steps 3, 4, 6, 8.
  Verified by: disposition/no-notice tests in `test_model_endpoints.py`,
  `test_orchestration_guidance.py`, `tests/job-kit/test_select.py`,
  `tests/workflow-kit/test_compiler.py`.
- **R19.** A resolvable id this plugin cannot route (wrong harness for the
  caller, or llm-scripting-kit absent) is skipped at selection identically
  to an unresolved id, with no visible notice.
  Source: direction 16.
  Implemented by: steps 3, 4.
  Verified by: the same disposition/no-notice tests as R18.
- **R20.** The rendered menu (`describe`, its CLI, orchestrate rows) lists
  only usable entries and out-of-quota entries with their reset time;
  unresolved, unroutable-here, requirements-mismatch, and excluded entries
  are hidden from the render, though the floor error (R23) still itemises
  them.
  Source: direction 17.
  Implemented by: steps 3, 4.
  Verified by: filtered-render tests in `test_model_endpoints.py`,
  `test_endpoints.py`, `test_orchestration_guidance.py`.
- **R21.** An entry probed `unreachable` stays visible in the render,
  distinct from the hidden categories in R20, because it is real on this
  machine and may return.
  Source: direction 17 (lead ruling).
  Implemented by: step 3.
  Verified by: disposition/filtered-render tests (step 3 row).
- **R22.** Out-of-quota does not count toward the usable set; a declaration
  whose every surviving entry is out of quota reaches the floor error (R23).
  Source: direction 15.
  Implemented by: step 3.
  Verified by: floor-propagation tests (step 3 row).
- **R23.** When no usable rendered entry remains, `describe()` raises a
  typed `NoUsableRoutingTarget` itemising every declared entry and its
  disposition in declaration order; every caller propagates it.
  Source: direction 14.
  Implemented by: step 3 (raised); steps 6, 9, 10, 11 (propagated).
  Verified by: the floor-propagation tests named in the step 3, 6, 9, 10,
  and 11 table rows.
- **R24.** `max_attempts` bounds executions only; exhausting it on quota
  halts is reported as an attempt-limit failure, never as
  `NoUsableRoutingTarget`.
  Source: direction 15.
  Implemented by: step 3 (`run` API), step 6 (job-kit).
  Verified by: `tests/job-kit/test_runner.py`.

## Quota halt and re-selection

- **R25.** A codex usage-limit exit is classified `HALT_QUOTA` (not `None`)
  from a rollout re-read, writing an out-of-quota verdict with reset time
  back to the pinned state.
  Source: direction 7.
  Implemented by: step 0.
  Verified by: `test_completion_halt.py`, `test_completion_codex_backend.py`,
  `test_usage_budget.py`.
- **R26.** A pinned AVAILABLE verdict never flips downward on a later
  re-read; only an observed quota/credit halt moves a verdict to
  OUT-OF-QUOTA, and a passed reset time reverts it to no-data.
  Source: direction 7.
  Implemented by: step 0, step 6 (register reword).
  Verified by: `test_usage_budget.py`, `test_completion_halt.py`.
- **R27.** On a mid-run quota/credit halt, the halted entry is recorded
  out-of-quota with its reset time, excluded, and `describe()` is called
  again; a remaining usable entry is dispatched, otherwise the R23 floor
  error propagates.
  Source: direction 7.
  Implemented by: step 0, step 3, step 6.
  Verified by: `tests/job-kit/test_runner.py`.
- **R28.** For an in-session caller, any unexplained dispatch failure
  (non-zero exit, Agent-tool error, no output) triggers re-selection; only a
  schema-invalid or wrong result from a run that completed (exit 0) is a
  task failure, not a trigger, and the announcement names the failure kind.
  Source: direction 7.
  Implemented by: step 4, step 5.
  Verified by: `test_orchestration_guidance.py`,
  `tests/repo-scripts/test_agent_directives.py`,
  `tests/git-kit/test_run_review_lane.py`.
- **R29.** For an unattended caller, only classified halts (quota, rate
  limit, auth, insufficient-credit-as-quota, launch/transport) move
  selection to the next entry; an unclassified task error or transport
  timeout stays a failed attempt.
  Source: direction 7.
  Implemented by: step 0, step 6.
  Verified by: `test_completion_halt.py`, `tests/job-kit/test_select.py`.
- **R30.** Before re-selecting a unit that may have written, its workspace
  is reset to its pre-launch state or the unit is re-run in a fresh
  worktree; a re-run is never silently layered on another model's partial
  edits.
  Source: direction 7 (reviewer finding 3).
  Implemented by: step 3 (`run`'s mechanical rule), step 4 (session rule).
  Verified by: drill.
- **R31.** Under a real or simulated codex quota exhaustion, a multi-entry
  row's re-selection announces a usable entry with reason "codex: out of
  quota"; a unit that exits 0 with a wrong result is reported as a task
  failure and not re-routed; a unit halted after writing is re-run only
  after R30 applies.
  Source: direction 7 (Biggest risk section); task log 2026-09-23 ruling
  (simulated fixture).
  Implemented by: drill, scheduled after step 4.
  Verified by: drill.

## Migration and deprecation

- **R32.** git-kit's and p4-kit's generated review skills dispatch by each
  declared entry's harness, call `describe`/choose/announce, and print
  `Ranking.rule` verbatim, with the prior "try A, then B" sentence and the
  non-routable-warning prose removed.
  Source: directions 8, 13, 16.
  Implemented by: step 5.
  Verified by: `test_skill_drift.py`, `test_lane_retry_prose.py`,
  `tests/git-kit/test_run_review_lane.py`.
- **R33.** Every generated and hand-written model literal in skills-kit
  emits and validates against the one-format list declaration (one-entry
  lists where single-valued), and a drift check ties each hand-written
  literal to its declared id.
  Source: directions 1, 2.
  Implemented by: step 7.
  Verified by: `tests/skills-kit/test_emit_audit_jobs.py`, `test_workflow_js_drift.py`.
- **R34.** workflow-kit's `model:` field accepts any structurally valid id,
  compiles a core id to `agent()`, and silently skips (no compile notice) an
  id that is unresolved or unroutable at compile time.
  Source: directions 9, 13, 16.
  Implemented by: step 8.
  Verified by: `tests/workflow-kit/test_loader.py`, `test_compiler.py`.
- **R35.** The openrouter node and any endpoint-level default resolve to a
  declared registry id rather than a bare endpoint alias; `default_endpoint`
  becomes a one-entry declaration instead of a separate syntax.
  Source: direction 9 (D1 "not declarations").
  Implemented by: step 9.
  Verified by: `test_openrouter_run.py`, `test_model_resolve.py`.
- **R36.** `agent:`, `peer:`, job-kit's old keys, content-pipeline-kit's old
  envs, and the `choose`/`--endpoint` CLI aliases keep working, unchanged in
  meaning, through step 11, and are removed only at step 12, after the
  owner's claude-settings migration (step 11) lands.
  Source: direction 12; task log 2026-09-23 ruling item 2.
  Implemented by: steps 2, 4, 6, 9, 10 (accept); step 12 (remove).
  Verified by: the compatibility tests named in the step 2, 4, 6, 9, and 10
  table rows; step 12's row records that those same tests lose their
  compatibility cases.
- **R37.** content-pipeline-kit's model/backend/endpoint env triple is
  replaced by one `CONTENT_PIPELINE_LLM_MODELS` declaration resolved
  through `run()`; the old envs are honoured with a deprecation line until
  step 12.
  Source: directions 1, 2, 12.
  Implemented by: step 10.
  Verified by: `test_llm_backends.py`, `test_llm_model_endpoint.py`,
  `test_llm_platform.py`, `test_run_cli.py`.

## Owner configuration

- **R38.** claude-settings declarations are written as prefix-free lists
  (for example `[astra, fable]`), and stale prose describing quota or
  independence behavior that predates this design is corrected.
  Source: direction 12; D1.
  Implemented by: step 11.
  Verified by: no automated test (owner config); floor-propagation tests
  exercise the shape indirectly.

## Done means

Every step 0-12 of the migration table is published (each with its named
test shown to fail before the change and pass after, and a clean code
review, per the task's 2026-09-23 publish ruling), and the biggest-risk
drill (R31) has run with all three of its assertions confirmed.
