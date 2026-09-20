# Design: one model-declaration format, and the multi-provider extension

Written 2026-09-16 for task `quota-resilient-dispatch`, item
`design-declaration-format`; revised the same day after two independent
reviews (opus primary; opencode/deepseek-pro cross-check) and again after
the owner answered the design's questions (plan.md directions 9-12, verbatim
in the task's `log.md`). Revised 2026-09-20 for directions 13-17 and the
third cross-check. Maintainer material: it lives under `docs/` because
nobody on a consumer machine needs it. Inputs: the task's `plan.md` (owner
directions 1-17), `log.md`, `model-declarations.md` (the map; its site ids
A1..R2 are used below), and `findings.md`. Where the map and findings.md
disagree, the map wins. Line numbers cite the dev tree at commit 498cc21c.

## Premise outcomes

| Premise | Outcome |
|---|---|
| Owner directions 1-17 | established; applied throughout. Direction 8 replaces the "when it cannot run" condition of 6; direction 9 retires the constraint in 4; direction 10 settles unattended ordering; directions 13-17 settle silent skip, hidden render, and the itemised floor. |
| llm-scripting-kit 0.44.0 reads an exhausted codex account as out-of-quota; orchestrate drops or moves seats (`orchestration_guidance.py:782` "moved back") | established; read at `load_quota_ranker` :689-786. |
| Map citations spot-checked | established; every citation used here was re-read. |
| hypothesis: llm-scripting-kit owns dispatch/ranking | CONFIRMED. It already owns every transport (`completion/factory.py:63-112`), the pacing verdicts (`usage_budget.py`), the ranking rule (`quota_selection.py`), reachability, seats, and halt classification. job-kit adds no provider (`select.py:141` calls `create_backend`). The two other provider-choosing paths (awesome-kit `dispatch.py:62`, content-pipeline-kit `backends.py:785`) wrap or bypass llm-scripting-kit code; neither adds a transport. |
| Plugin boundaries are hard; cross-plugin imports state REQUIRED / REFUSE / DEGRADE; opinions are configurable or registered | constraint; honoured. No skill moves. Each new import states its posture in D2 and D4. |

## Decision 1 -- Syntax: a list of registry ids, one namespace, no prefixes

A model declaration is a list of strings. Each string is an entry id in the
llm-scripting-kit model registry (`default_config.yaml` `endpoints:` merged
with the layered config and the user registry
`~/.claude/config/model-endpoints.yaml`, `model_endpoints.py:28-54`). A
one-model declaration is a one-element list (direction 2). A bare scalar is
accepted on read as a one-element list, so existing `model: opus` files keep
parsing; the written form is always a list. Any plugin may list any ids,
including several served by one provider (direction 9).

```yaml
models: [fable]                                        # Claude-only plugin
models: [astra, fable]                                 # reviewer C, owner config
models: [qwen3.8-5090, opus, astra, qwen3.8-m5pro]     # the owner's example
```

Carriers: a YAML list, a JSON list, a JS array literal in a Workflow script,
a repeated CLI flag or comma list. The shape is the format; the carrier is not.

**One namespace rule.** An id identifies WHAT model. A PROVIDER is what
serves a set of ids (codex serves `luna`, `sol`, `astra`; the Claude harness
serves `fable`, `opus`, `sonnet`, `haiku`; a local server serves
`qwen3.8-5090`). HOW an id is driven is a property of its entry's `harness`
and of the caller, never of the id:

| Entry kind | In-session caller (orchestrate, review skill) | Process caller (job-kit, CLI, content-pipeline) |
|---|---|---|
| `harness: claude` (`fable`, `opus`, `sonnet`, `haiku`) | the harness: Agent tool `model: <id>`, Workflow `agent()`, background task | the harness: `claude -p` (`ClaudeCliBackend` when llm-scripting-kit is present) |
| `harness: codex` / `opencode` | rendered `codex exec` / `opencode run` argv | `CodexCliBackend` / `OpencodeCliBackend` |
| transport (`base_url`) | not drivable (no agent loop) -- listed as unusable for a unit of work | `OpenRouterBackend` |

This resolves map finding 1 (one bare `opus`, three dispatches): `opus` is
one entry, and the caller's kind picks the mechanism. The three closed alias
sets (`orchestration_guidance.py:538`, `lane_prompts.py:61`, workflow-kit
`model.py:36`) collapse into the registry's `harness: claude` entries. The
registry must gain a shipped `haiku` entry (tier 1, family anthropic) -- the
only Claude alias in use (W3, W4) with no registry entry.

**Core ids are reserved.** `fable`, `opus`, `sonnet`, `haiku` are the CORE
set: valid on every installation because the harness itself defines them,
and routable by every plugin without llm-scripting-kit (direction 9: Claude
routing belongs to the harness). Because a config-layer or registry entry
shadows a shipped one (`models.py:518-521`), the core ids are RESERVED for
`harness: claude`: `check_registry_entry` (D2) classifies a core id whose
MERGED entry has another harness or a `base_url` as shadowed/unroutable. It
contributes to the floor diagnostic; it does not abort at load or validation.
The check runs on the merged entry, not on a single layer, because a partial
layer is legitimate: the owner's `fable` and `opus` entries carry only
`conserve_usage` (`claude-settings/config/llm-scripting-kit.yaml`).

**Prefixes go.** `agent:` (A1, O1) is redundant once `opus` means the entry
and the caller kind picks the Agent tool; the renderer accepts it and
rewrites it with a note until migration step 11. `peer:<id>` (B1, O2) goes by
direction 12; lists name ids directly. The shipped reviewer C default
`[peer:opus, opus]` becomes `[sol, opus]` (sol is the shipped BESIDE seat of
opus: tier 3, family openai). The cost, accepted by the owner: a machine
whose cross-family peer for opus is a non-codex entry must list it.

**Not declarations.** K4 `admitted_endpoints` is an allow-list for context
admission. The front-door group (L6, O4) is load balancing over identical
models; a declaration names a group as ONE id (`qwen38`), and its in-group
failover stays inside llm-scripting-kit. L1 `default`/`default_endpoint`,
L4, and S6 model aliases under an endpoint are addressed in the migration
(step 8).

## Decision 2 -- Where the format is specified and validated

**Specification:** one reference in bootstrap's plugin-dev skill,
`plugins/bootstrap/skills/plugin-dev/references/model-declaration.md`. Every
plugin's configuration doc cites it by path instead of restating the grammar.
Alternatives: llm-scripting-kit (rejected: a Claude-only plugin would cite the
multi-provider plugin for its format, inverting directions 3 and 9);
skills-kit (rejected: not a dependency of every plugin); `docs/` (rejected:
consumers must be able to read it). plugin-dev already hosts the
cross-plugin contracts (`optional-plugin-dependencies.md`, `enabling.md`),
and every plugin declares `dependencies: ["bootstrap"]`. bootstrap's part
ends here: the spec and the validator. It adds no routing.

**Validator:** `plugins/bootstrap/bootstrap_lib/model_declaration.py`. It
never imports llm-scripting-kit. Its remaining job is declaration shape and
structural validation, including the literal empty-list error and duplicate
detection. Duplicates remain an error because they are a fact about the
declaration's shape, independent of whether an id resolves or can route.

```
parse(value) -> list[str]         # scalar -> [scalar]; rejects empty, non-string, duplicate
validate(names) -> Declaration    # structural checks only; no registry discovery
check_registry_entry(id, merged)  # shadowed core -> shadowed/unroutable disposition
```

Unknown ids and valid-but-unroutable ids are resolved at runtime. They are
silently skipped and recorded for the floor diagnostic; neither is a
validation error or a visible notice. Usable-set emptiness is also a runtime
question, not a validation question.

**Linking.** A shared lib does not carry its own edges to consumers (the
`$comment` in `plugins/content-pipeline-kit/bootstrap.json`). job-kit
declares only `["llm_scripting_kit"]` in `shared_lib_imports`
(`plugins/job-kit/bootstrap.json:15`) and skills-kit declares none, so
migration step 0 adds `bootstrap_lib` to both manifests before any code
imports it (version bump each, validated with `claude-dev` because it is
manifest content). Posture: REQUIRED (bootstrap is already a declared
dependency of every plugin). The lazy-import alternative was not taken:
llm-scripting-kit already links `bootstrap_lib`, and the plugins lacking the
edge are the ones whose own code must call the validator.

## Decision 3 -- Two layers of routing: the harness, and llm-scripting-kit

Direction 9 in one sentence: not everything depends on llm-scripting-kit.
The former constraint 4 (multi-entry only across providers) is RETIRED; no
validator or `describe` refuses or warns about same-provider ids. Shared
quota is shown as information only (D5: "shares `seven_day` with opus").

**Without llm-scripting-kit** a plugin routes Claude ids through the harness
and nothing else: the Agent tool, Workflow `agent()`, background tasks,
`claude -p`. The design adds NO infrastructure for Claude-to-Claude
routing; the harness already knows how. Such a plugin's `routable_harnesses`
is `{claude}`. A valid-but-unroutable id is silently skipped and is absent
from the rendered menu. It remains in the complete disposition set used only
if the usable set is empty. An unresolved id, a requirements mismatch, an
excluded id, and an out-of-quota id are also silently skipped at selection;
the render and floor rules are defined in D4 and D5. There is no degraded
render and no non-routable notice.

**With llm-scripting-kit** (imported directly, or by a plugin that depends on
it) every id the registry resolves is reachable through D4: its tooling and
knowledge are the out-of-harness routing layer. Its harness entries for
Claude (`sonnet`, `opus`, `fable`, shipped in `default_config.yaml:121-141`)
exist for PROCESS callers that need `claude -p` behind the completion seam;
an in-session caller still drives a core id through the harness even when
llm-scripting-kit is present.

**Reading of "all plugins that do routing need to be able to route to
claude."** This design reads it as a CAPABILITY requirement on the plugin --
every routing plugin can drive a core id, which the harness gives for free
-- not as a content rule that every list must contain a Claude id. A list
such as `[sol]` stays legal; when codex is out, the runtime floor applies.
The remedy is the owner's list, not a validator rule.

Every migration step is checked against the sentence: steps 1, 6, 7 add no
llm-scripting-kit import (bootstrap, skills-kit, workflow-kit route Claude
ids through the harness; workflow-kit's openrouter node already REFUSEs
without it); steps 2, 5, 8, 9 are plugins that already depend on it; steps
3 and 4 keep their DEGRADE / REFUSE postures.

## Decision 4 -- The one API, owned by llm-scripting-kit

New module `plugins/llm-scripting-kit/lib/llm_scripting_kit/declaration.py`.
`quota_selection.rank_candidates` (two bands, stable) is replaced by the
pace ordering of D5 (`order_by_pace`); `choose_endpoint` (L3) becomes a thin
caller.

```
describe(names, *, project_root, caller, self_ref=None,
         requirements=None, capabilities=None, backend_factory=None,
         exclude=(), reachability_cache=None) -> Ranking
    caller: "session" | "process"     # picks drive (D1 table) and the trigger set (D6)
    requirements: a requirement mapping, applied with
                  completion.match_capabilities exactly as select.py:143-162 does
    capabilities: the advertisement records to match against; a caller that
                  checks capabilities at execution passes the SAME records
                  here (job-kit: run.py:593-594, `_capabilities_for` :301-309),
                  so selection and execution never read different records --
                  the disagreement select.py:151-157 exists to prevent
    backend_factory: the factory used to resolve an id (default create_backend);
                  job-kit's injection seam, kept
    exclude: ids the caller has ruled out (job-kit's run-level halt ledger
             plus same-job halts, run.py:596-615, :968-1001)
    reachability_cache: a caller-scoped mapping the probe results are read from
             and written to; without it every call probes every entry live
             (reachability.check_many has no cache; HTTP entries hit /models)
    Ranking.rendered_entries: EntryState for the rendered subset, in PACE ORDER (D5 rule)
        id, declared_index, resolved, kind, harness, model, family, tier, drive
        reachability: reachable | unreachable | unknown   (reachability.py)
        usability: pinned verdict status                  (pinned_evaluate)
        pace: float | None                                (D5; unpinned evaluate)
        shares_quota_with: [ids]                          (information only)
        usable: resolved and not unreachable (unknown counts, fail-open) and
                pinned.usable and requirements match and not excluded
        default: bool       # first usable entry of the ordered list
        is_self: bool       # matches self_ref (D5 independence)
    Ranking.dispositions: complete per-declaration disposition set, in
                          DECLARATION ORDER; not rendered, read only by floor
                          error handling
    Ranking.rule: the rendered choice rule and trigger text (D5, D6)

    NoUsableRoutingTarget: typed error raised by describe() when no usable
                           rendered entry remains; carries every declaration
                           entry and its disposition in declaration order.

run(names, request, *, project_root, requirements=None, exclude=(),
    max_attempts=1, on_attempt=None) -> RunResult
    Unattended dispatch for callers with NO loop of their own. Takes the
    FIRST usable entry of describe(caller="process"); each attempt is
    reported through on_attempt with its pace reading and counted against
    max_attempts, which limits EXECUTIONS only and is not the floor. A halt
    (D6, process set) writes the verdict back and moves to the next usable
    entry of the same pace-ordered rendered subset.
    describe() raises NoUsableRoutingTarget when the floor is reached, and
    run() and every caller propagate that typed error.
```

CLI: `llm-scripting-kit describe <id>... [--self x] [--requirements f]
[--exclude n] [--json]` renders the per-entry lines of D5 for in-session
callers (the review skill's step 6, an agent checking before it dispatches).

`describe` builds the complete disposition set first, then filters hidden
entries from the render. It retains usable entries, out-of-quota entries with
their reset time, and unreachable entries; it hides unresolved,
unroutable-here, requirements-mismatch, and excluded entries. It pace-orders
that filtered block, marks its first usable entry `[default]`, and raises
`NoUsableRoutingTarget` when no usable entry remains. The error itemises every
declared entry in declaration order as unresolved, unroutable, out-of-quota
(with reset time), unreachable, requirements-mismatch, excluded, or malformed
reserved-core entry. SKIP is silent; the floor is the only selection error.

Two consumer kinds. IN-SESSION callers (orchestrate rows, consult seats,
review lanes) call `describe(caller="session")` and let Claude choose
(direction 8). PROCESS callers split: job-kit KEEPS its attempt loop, its
`max_attempts`, and its halt ledger, and calls
`describe(caller="process", requirements=selection_job.requirements,
capabilities=advertised, backend_factory=backend_factory,
exclude=halts.current() | same_job_halts, reachability_cache=<run-scoped>)`
where `select_endpoint` filters the declaration, taking the first usable entry of the
ordered list (direction 10). `selection_job` is the job with the run-level
deny floor already applied (`_require_floor_subjects`, run.py:344,
:601-603) -- passing the bare `job.requirements` would drop the floor -- and
`advertised` is the same capability mapping execution checks
(run.py:593-594). So the registered deny-floor requirement
(`plugins/CLAUDE.md:103`), run-level halt narrowing, and job-kit's injection
seams are preserved as inputs, not re-implemented. The reachability cache
is scoped to the run (one probe per entry per run), matching the register's
"confirm with two attempts, then exclude" rule. Callers with no loop
(workflow-kit's openrouter node, content-pipeline's `route()`) call `run`.

What changes for job-kit: its current selector reads neither quota nor reachability
(`select.py:120-162` filters by requirements and halts only); under
`describe` the filtered list is pace-ordered first, and an out-of-quota or
unreachable entry is skipped before the first attempt. Its register entry (`plugins/CLAUDE.md:
90-102`) is reworded: "job-kit selects deterministically: a run is
explainable from the declared list plus the logged pace readings." No
consumer walks a fallback chain itself any more; the generated review step 6
prose (`gen_code_review_skills.py:212-221`) becomes "call `describe`,
choose, announce" for Agent lanes, with `run_review_lane.py` calling
`describe` for endpoint lanes and the skill re-dispatching per D6.

Bringing the other provider-choosing paths under it:

- awesome-kit `dispatch.py` (A3) is not a completion caller: it builds the
  argv through `bootstrap_lib.codex.build_codex_exec_argv` and keeps its own
  dispatch cache (`dispatch.py:50-110`). `--model` becomes a declaration id
  resolved to a `harness: codex` entry through `discover_model_entries`
  (`models.py:613`); the hardcoded `gpt-5.6-sol` default goes, the agent
  passes the row's chosen entry. Posture: REFUSE -- absent llm-scripting-kit
  it exits naming the owner plugin, as it already does for a too-old
  `bootstrap_lib.codex`. (REQUIRED would need an `install: auto` edge in
  awesome-kit's manifest, which step 3 does not add.)
- content-pipeline-kit (C1, R-m): `CONTENT_PIPELINE_LLM_BACKEND` (provider
  KIND), `..._MODEL` (vendor id) and `..._ENDPOINT` (registry id) are replaced
  by one `CONTENT_PIPELINE_LLM_MODELS` declaration resolved by `run`. The old
  envs are honoured until step 11 and mapped to the shipped entry of that
  harness (or the named registry entry) with a deprecation line. `route()`
  keeps its `mock` seam untouched. Posture stays REQUIRED at the manifest.
- The front door (L6, O4) keeps its own HTTP failover inside
  llm-scripting-kit; a declaration names a group as one id.

Register consequences (plugin-opinion razor): "a failed lane falls over only
along the chain its own configuration named" (`plugins/CLAUDE.md:193-204`)
stays true; the job-kit entry is reworded as above; the pinned-verdict entry
(:205-217) is amended per D6.

## Decision 5 -- Pace, and the one ordering rule

**Pace (direction 11).** Both halves exist on `Budget`
(`usage_budget.py:211-212`): `remaining` = 1 - used_pct/100,
`window_remaining` = seconds_left / window_seconds (`_verdict` :365-396).

    pace = remaining / window_remaining        (shown as a percentage)

100% = exactly on pace; above = ahead (AVAILABLE); below = behind
(UNDER-QUOTA, the same comparison `_verdict` makes with `_PACE_EPSILON`).
The alternative `remaining - window_remaining` (headroom in points) is not
comparable across a 5-hour and a 7-day window; the ratio is.

**"Has a pace"**, per case -- this decides who moves in the ordering rule:

| Case | pace | Ordering |
|---|---|---|
| fresh `evaluate()` returns both `remaining` and `window_remaining`, pinned verdict usable | the number | moves |
| fresh read says remaining 0 while the pinned verdict is usable | 0% (sorts last among paced entries) | moves; still usable |
| pinned verdict OUT-OF-QUOTA (including the codex exhaustion Budget, `remaining=0.0`, `window_remaining=None`, :679-707) | none; rendered "out of quota until <resets_at>" | keeps its place; not available |
| no reading, or the entry declares no `conserve_usage` (transport ids, unpaced harness ids) | none; rendered "n/a" | keeps its place; usable (fail-open, :110-112) |
| `window_remaining` below 1% of the window | none; rendered "about to reset" | keeps its place |

Pace comes from an UNPINNED `evaluate()` at render time; `pinned_evaluate`
returns the STORED halves and never recomputes an AVAILABLE verdict
(:836-859), so a pinned pace would be frozen at session start. Only
USABILITY (the status) stays pinned: the displayed status, `usable`, and
`[default]` come from the pinned verdict; the fresh read supplies only the
number. A pinned verdict moves only on an observed halt (D6).
llm-scripting-kit 0.44.1 stores `resets_at = event time + 5h` for an
exhaustion read with no parseable reset text, so a pinned OUT-OF-QUOTA
always expires.

**The ordering rule (direction 10).** The pipeline is explicit: build the
complete disposition set; filter out hidden entries; retain usable,
out-of-quota, and unreachable entries; pace-order that filtered block; then
mark its first usable entry `[default]`. Hidden entries are unresolved,
unroutable here, requirements-mismatch, and excluded. Each `EntryState` keeps
its original `declared_index` as provenance; only working/render positions
compact. Entries that have a pace are re-sorted by pace, highest first,
among their working positions; entries without a pace keep their places; ties
keep declaration order. The owner's example, with opus at 76% and astra at
120%, is unchanged because it contains no hidden entry:
`[qwen3.8-5090, opus, astra, qwen3.8-m5pro]` ->
`[qwen3.8-5090, astra, opus, qwen3.8-m5pro]`. One rule for every caller:

- UNATTENDED callers (job-kit, `run`) take the FIRST AVAILABLE entry of the
  ordered list. A run is explainable from the declared list plus the logged
  pace readings.
- IN-SESSION `describe` shows the same pace-ordered rendered subset; its first usable entry
  is `[default]`, and Claude may still choose any usable entry by judgment
  and announce it (direction 8).

This rule REPLACES awesome-kit 0.56.0's quota reordering
(`load_quota_ranker`, `orchestration_guidance.py:689-786`: drop
out-of-quota, move under-quota "back" behind peers) and the two-band
`rank_candidates`. It differs in three ways: hidden entries are removed
before ordering slots are assigned; out-of-quota entries remain visible with
their reset time, while the RENDERED SUBSET is still distinct from the FULL
DECLARATION; and the sort is by the number, not by band. Unpaced entries
never move. `ORCHESTRATE_QUOTA_ROUTING=0` keeps its meaning for tests: no
reads, no reordering, no quota column.

**What the agent is shown.** On the owner's config fable and opus both read
the all-model `seven_day` pool (`claude-settings/config/llm-scripting-kit.yaml`),
so they carry the same pace and tie in declared order; sonnet is unpaced:

```
Row 3 -- novel + load-bearing (ordered by pace): choose one; default is marked.
  fable   claude/agent   under quota  38% left, 50% of window   pace 76%   [default]  shares seven_day with opus
  astra   codex          out of quota until 2026-09-19 15:34
  sol     codex          out of quota until 2026-09-19 15:34
  opus    claude/agent   under quota  38% left, 50% of window   pace 76%   [author]   shares seven_day with fable
  sonnet  claude/agent   n/a (unpaced)
Rule: any usable entry may be chosen; the default applies only when you have
no preference. Announce every choice from a multi-entry declaration:
"route: <unit> -> <entry>; <reason>".
```

`describe` EMITS the rule text and the D6 trigger text (`Ranking.rule`), so
they are tested where they are owned (`tests/llm-scripting-kit`) and
awesome-kit's render test asserts only pass-through. What this replaces on
the render side: the "try A, then B" sentence (:1105-1121), SKILL.md step 4's
"tried in declaration order", and the seats render that omits out-of-quota
seats (:1705-1723; `SeatsResult.out_of_quota` already exists).

**Announce.** One line at dispatch, `route: <unit> -> <entry>; <reason>`,
reason one of `default`, `higher pace`, `independence`, `<prior entry>
failed: <kind>`, or the agent's own clause. A one-entry declaration needs no
announcement.

**Reviewer independence as a preference (direction 6).** `describe` marks
`[author]` on an entry whose model matches `self_ref`. Rendered guidance:
"prefer a non-author entry; the author may review when no other usable
entry exists, and says so." `seats.py:276` keeps excluding self from
CONSULT seats; the prose that, as of 2026-09-16, says "never the authoring
model" (findings.md "Authorship independence") changes to the preference
wording in the `independence-as-preference` item.

## Decision 6 -- What "cannot run" means, and re-selection

An entry is unusable BEFORE dispatch when: it does not resolve; its harness
CLI or endpoint probes `unreachable` (`reachability.py:61-68`; `unknown`
counts as usable, fail-open -- D4's `usable` says "not unreachable" for this
reason); its pinned verdict is OUT-OF-QUOTA; it is excluded or fails the
caller's requirements.

Two trigger sets, chosen by `caller`, because the cost of a missed trigger
differs. Direction 7 says no agent stops because a provider is out; a
classifier that misses one string must not be the thing that stops a lane.

**Session set (orchestrate units, review lanes):** ANY dispatch failure the
launch-correction rule (`references/configuration.md`) does not explain
re-selects -- a non-zero exit, an Agent-tool error, a launch that produced
no output. This is the existing review-lane rule (`gen_code_review_skills.py:212`)
kept whole, so `plugins/CLAUDE.md:193-196` stays true. Only a schema-invalid
or wrong RESULT from a lane or unit that ran to completion (exit 0) is a
task failure, not a trigger. The agent announces the re-selection with the
failure kind, classified when a marker matches and "unexplained exit"
otherwise.

**Partial edits.** This set is wider than the existing orchestrate rule ("a
launch or transport error", `orchestration_guidance.py:1106-1109`), so it
also covers units that WRITE, and a mid-unit quota halt always leaves
whatever the unit wrote before it stopped. Rule: before re-selecting a unit
that may have written, the agent inspects the unit's workspace (`git status`
/ `git diff` against the state at launch) and either resets it to that state
or re-runs the unit in a fresh worktree; a unit re-run on top of another
model's partial edits is never silent. Review lanes are read-only and skip
this step. `run` applies the same rule mechanically when the request names
a workspace: reset or fresh worktree, recorded on the attempt.

**Process set (bulk `run`, job-kit):** only CLASSIFIED halts move on, because
an unattended runner that re-dispatched on every non-zero exit would spend
its budget re-running a job that is simply broken:

| Trigger | Classified in the existing implementation | Change |
|---|---|---|
| quota (pool spent) | codex `usage_limit_exceeded` is NOT a halt: `test_completion_codex_backend.py:449` pins the prose to None | add `HALT_QUOTA` to `halt.py`. Source: `CodexCliBackend` runs codex WITHOUT `--json` (`codex_backend.py:111-116`), so no `task_complete` payload reaches it; on a non-zero codex exit the backend re-reads the rollout tail through `read_codex_pool` (the 0.44.0 exhaustion shape: null windows plus no credits, or the `usage_limit_exceeded` event with its reset text). `read_codex_pool` takes a `ConserveSpec` (:626-628): the entry's own when it declares `conserve_usage`, else the default `ConserveSpec(pool=seven_day)` -- the pool the codex reader remaps to `primary` -- used for the read only; an entry with no opt-in still gets no pacing verdict, only the halt. The stderr prose pin stays None. Mirror in content-pipeline `platform.py:378-386`. |
| rate limit | `HALT_RATE_LIMIT` (429 envelopes, CLI backoff, timeouts) | unchanged |
| auth | `HALT_AUTH` | unchanged; move on, but write NO quota verdict |
| insufficient credit | `HALT_INSUFFICIENT_CREDIT` | treated as quota for verdict write-back |
| launch/transport | CLI missing, connection refused, exit with no output (content-pipeline `HALT_UNREACHABLE`) | `run` moves on; the entry is marked unreachable for the run |

A task error, schema-invalid output, or a transport timeout with no marker
stays a failed attempt in the process set.

`max_attempts` limits EXECUTIONS, not the size of the routing pool. If a
quota halt consumes the last allowed attempt, the result is "attempt limit
reached", not `NoUsableRoutingTarget`; those are different failures, and the
floor must not describe a broken job as an empty pool.

**Zero-entry branch after a mid-run quota halt.** Record OUT-OF-QUOTA for
the entry with its reset time, exclude it, and apply the Partial edits rule
where it applies. Call `describe()` again. If another usable entry remains,
dispatch it; otherwise propagate the itemised `NoUsableRoutingTarget` floor.

**Stale verdict.** An observed halt never updates the pinned verdict
(register entry `plugins/CLAUDE.md:205-217`: "never re-evaluated downward").
The design amends that entry: a verdict is re-evaluated downward ONLY on an
observed quota/credit halt, which writes OUT-OF-QUOTA for that entry with
the reset time when the halt carries one, else `event time + 5h` (the 0.44.1
rule). An AVAILABLE verdict still never flips on a re-read, so the
guarantee the entry protects holds; only an actual failure moves it. A
reset time passing already flips OUT-OF-QUOTA back to no-data, which covers
a mid-session codex reset in the other direction.

## Owner decisions (2026-09-16, plan.md directions 9-12)

- **Direction 9 -- any ids, providers serve them, invalid is loud, Claude
  routing is the harness's.** Applied in D1 (no prefixes, provider defined),
  D2 (structural validation only), and D3 (constraint 4 retired). The
  invalid-id loud-error clause is superseded by direction 13 below.
  Resolves former Q1.
- **Direction 10 -- unattended takes the first available of the
  pace-ordered list; unpaced entries keep their places.** Applied in D4
  (`run`, job-kit) and D5 (the one ordering rule, replacing 0.56.0's
  reordering). Resolves former Q2.
- **Direction 11 -- the name is "pace".** Applied throughout. Resolves Q3.
- **Direction 12 -- drop `peer:`.** Applied in D1 and migration steps 1 and
  11. Resolves Q4.

- **Direction 13 -- An unknown id is SILENTLY SKIPPED** (2026-09-20;
  supersedes the "invalid id is a loud error" clause of 9). A declaration is
  a list of candidates: resolve what resolves, work with what is left.
  Silent, not a visible notice -- the owner chose silent explicitly. Applied
  in D2-D5: unknown ids are runtime dispositions, hidden from render, and
  itemised only by the floor.
- **Direction 14 -- An empty list of routing targets IS a loud error.** The
  floor stays. Applied by `NoUsableRoutingTarget` in D4, raised in
  `describe()`.
- **Direction 15 -- Out of quota does not count as a routing target.**
  Emptiness in 14 is evaluated on USABLE entries, not merely resolvable ones,
  so a list whose every surviving entry is out of quota hits the same loud
  error. The only thing that stops a unit is having genuinely run out of
  road. Applied in D4-D6; `max_attempts` remains a separate execution limit.
- **Direction 16 -- A valid-but-unroutable id is ALSO silently skipped**
  (2026-09-20; completes 13). SKIPPING is silent for every non-usable entry,
  whatever the reason -- unresolved, valid but unroutable here, or out of
  quota. No error, no notice, no interruption; selection simply passes over
  it. This overrides D3's visible notice AT DISPATCH. Applied by removing
  degraded-render and notice paths; RENDER, SKIP, and FLOOR remain separate.
  Three surfaces, and they must not be conflated (an earlier wording of this
  direction did conflate them and contradicted direction 8):
  - RENDER (`describe` output, the menu an agent chooses from) still shows
    each declared entry and its state, out-of-quota with its reset time.
    That is direction 8 and it stands: the render is information, not a
    warning.
  - SKIP (selection, dispatch) is silent, per this direction.
  - FLOOR (14, 15: nothing usable left) is the loud error, and it itemises
    every declared entry and its disposition.
  The question this direction left open -- whether the RENDER also lists
  unresolved and unroutable ids alongside usable ones -- is settled by
  direction 17 below.
- **Direction 17 -- Unrecognized and not-routable ids are HIDDEN from the
  render** (2026-09-20; settles the question direction 16 left open). The
  rendered menu shows only entries that are real on this machine: usable now,
  or out of quota and usable again when it resets -- direction 8 keeps those
  visible with their reset time. An id that resolves to nothing, or that this
  plugin cannot route, does not appear in the render at all. The floor error
  (14, 15) still itemises EVERY declared entry including the hidden ones;
  that is where a typo surfaces. Applied in D4-D5. The lead's ruling also
  hides requirements-mismatch and excluded entries, while keeping unreachable
  entries visible because they are real on this machine and may return.

Direction 9's loud-error clause is superseded; no owner question remains.

## Biggest risk

The Agent tool is driven by prose, so an in-session re-selection depends on
Claude acting on a failure it sees rather than on a classifier. D6's session
set makes any unexplained failure a trigger, which removes the missed-string
risk but adds a new one: an agent re-selecting on a failure that was really
the unit's own bug. The check: the announcement must carry the failure kind.
The drill needs the next exhaustion OR a simulated halt as its fixture, and
until it runs the risk check is UNRUN. It must dispatch a multi-entry row and
confirm the announcement names a
usable entry with "codex: out of quota" as the reason, while a deliberately
broken unit on a usable entry -- one that exits 0 with a wrong result -- is
reported as a task failure and NOT re-routed, and a unit that halts after
writing is re-run only after its workspace is reset or replaced.
`tests/llm-scripting-kit` pins the rule and trigger text that `describe`
emits and the ordering rule against the owner's example. A second risk is
the `haiku` entry and the `agent:` rewrite landing in different releases;
the order below keeps the registry step first.

## Migration table

Ordered so each step publishes on its own; every step needs a version bump
and a test shown to fail first. "Gen" names the generator that must change.

| Step | Sites | New declaration | Owner plugin | Gen | Tests pinning existing behavior |
|---|---|---|---|---|---|
| 0 | job-kit, skills-kit manifests | add `bootstrap_lib` to `shared_lib_imports`; validate with `claude-dev` | job-kit, skills-kit | -- | `tests/job-kit/test_bin.py`, `tests/skills-kit/test_asset_dependencies.py` (manifest shape) |
| 1 | spec + validator | `model-declaration.md`; `bootstrap_lib/model_declaration.py` parses and normalizes declarations, performs structural checks, rejects a literally-empty list and duplicates; no known-id errors, registry-file discovery, `SHIPPED_EXTENSION_IDS`, notices, drift tests, or shipped-defaults property test | bootstrap | -- | new: shape, empty list, duplicates; no known-id, path-drift, copy-drift, or shipped-defaults tests |
| 1 | B1 reviewer `model` | list of ids; `peer:` accepted and rewritten until step 11 | bootstrap | -- | `tests/bootstrap/code_review/test_review_profiles.py` (`peer:` resolution, `model_fallbacks`) |
| 1 | B2 `validator_models` | reason -> one-element list (scalar accepted; `review_profiles.py:348-360` is scalar-only) | bootstrap | -- | same test file |
| 1 | `lane_prompts.py:61` alias set | replaced by the core set from the validator | bootstrap | -- | `test_lane_prompts.py` |
| 2 | L2 registry | add shipped `haiku` (claude, tier 1); `check_registry_entry` classifies shadowed core entries as shadowed/unroutable; add filtered-render and itemised-floor tests | llm-scripting-kit | -- | `test_model_endpoints.py`, `test_endpoints.py`, filtered-render, floor-propagation and disposition tests |
| 2 | L3 `choose --prefer` | `describe <id>...`; `order_by_pace` replaces `rank_candidates`; `choose` kept as alias | llm-scripting-kit | -- | `test_quota_selection.py` (two-band order), `test_llm_scripting_cli.py` |
| 2 | L4 `resolve` / `complete --endpoint --model` | `--models <declaration>`; `--endpoint` kept as alias; `--model` stays a per-entry override | llm-scripting-kit | -- | `test_llm_scripting_cli.py`, `test_completion_factory.py` |
| 2 | halt kinds | `HALT_QUOTA` from a rollout re-read; verdict write-back | llm-scripting-kit | -- | `test_completion_halt.py`, `test_completion_codex_backend.py:432-455`, `test_usage_budget.py` |
| 2 | L5 `review_lane --model` | one id; endpoint lanes call `describe` | llm-scripting-kit | -- | `test_review_lane.py` |
| 2 | A5 seats data | `describe` marks `[author]`; `SeatsResult` unchanged | llm-scripting-kit | -- | `test_seats.py` |
| 2 | L6, O4 front-door groups | unchanged; a group is one declaration id | llm-scripting-kit | -- | `test_frontdoor.py` |
| 3 | A1 rows | `models: [..]` without `agent:`; renderer filters hidden entries, retains usable/out-of-quota/unreachable entries, then pace-orders the rendered subset; no non-routable notice | awesome-kit | -- | `test_orchestration_guidance.py` (filtered render, reset time, no notice; no "moved back" or drops-nothing expectation) |
| 3 | A2 `requires_model` | same ids, no prefix strip | awesome-kit | -- | same |
| 3 | A3 `dispatch.py --model` | an id resolved via `discover_model_entries`; no hardcoded default; REFUSE posture | awesome-kit | -- | `test_dispatch.py` |
| 3 | A4 backend `command:` | unchanged (adapter supplies the model) | awesome-kit | -- | -- |
| 3 | A5 seats render (:1705-1723) | show out-of-quota seats with reset time | awesome-kit | -- | `test_orchestration_guidance.py` |
| 3 | orchestrate SKILL.md step 4, seat rule :91 | choose-and-announce wording; rule text passed through from `describe`; hidden skip is silent | awesome-kit | -- | `test_orchestration_guidance.py`, `tests/repo-scripts/test_agent_directives.py` (no non-routable notice) |
| 3 | R1 `check_model_dispatch.py` | reads ids, no `agent:` | repo script | -- | `tests/repo-scripts/test_check_model_dispatch.py` |
| 4 | G1, G2, step 6 model-kind rule, stale "no Agent fallback" line (:506) | dispatch by entry harness; `describe`, choose, announce; print `Ranking.rule` verbatim; drop warning prose and the gotcha | git-kit, p4-kit | `scripts/gen_code_review_skills.py` | `test_skill_drift.py`, `test_lane_retry_prose.py`, `tests/git-kit/test_run_review_lane.py` (no warning prose) |
| 5 | J1 `endpoint_preference` and aliases | `models: [..]` (old keys accepted); `select_endpoint` calls `describe(caller="process", requirements, capabilities, exclude)` and takes the first usable entry of the pace-ordered list; loop and ledger unchanged; pace readings logged | job-kit | -- | `tests/job-kit/test_select.py`, `test_model.py`, `test_runner.py` (same-job halts :604-611) |
| 5 | register entries :90-102, :205-217 | reword job-kit determinism ("declared list plus logged pace readings"); amend pinned-verdict per D6; skip reasons remain silent | plugins/CLAUDE.md | -- | floor propagation and silent-skip tests; no notice expectations |
| 6 | K3 `DEFAULT_ENDPOINTS`, `--endpoint` | `--models`; emitted as J1's new key | skills-kit | -- | `tests/skills-kit/test_emit_audit_jobs.py` |
| 6 | K2 remediate literals (4) | `model: 'sonnet'` emitted from a one-entry declaration in the generator; structural validation only, no known-id or usable-set expectation, no llm-scripting-kit import | skills-kit | `plugins/skills-kit/scripts/gen_workflow_js.py` | `test_workflow_js_drift.py`, validator empty-list and duplicate expectations |
| 6 | K1 hand-written literals (9) | one-entry declarations; a drift check that each literal equals the declared id | skills-kit | `check_shared_chunks` extended | `test_workflow_js_drift.py` |
| 6 | K4 | unchanged (allow-list, not a declaration) | -- | -- | -- |
| 7 | W1 `model:` | any structurally valid id; core ids compile to `agent()`; an unresolved or unroutable id is silently skipped | workflow-kit | -- | `tests/workflow-kit/test_loader.py`, `test_compiler.py` (silent skip, no compile notice) |
| 7 | W3, W4 `haiku` | one-entry declaration; frontmatter/preamble emit the scalar carrier | workflow-kit | -- | `test_compiler.py` |
| 8 | W2 openrouter node; L1 `default`/`defaultCheap`/`default_endpoint`; S6 aliases | ids must be transport ENTRIES: ship the `openrouter` sub-aliases as entries (`or-gpt-mini`, `or-qwen`), keep `models:` under an endpoint as a per-entry override only; `default_endpoint` becomes the one-entry default declaration | workflow-kit, llm-scripting-kit | -- | `test_openrouter_run.py`, `test_model_resolve.py`, floor propagation tests |
| 9 | C1 env triple (`BACKEND`, `MODEL`, `ENDPOINT`), C2 run record | `CONTENT_PIPELINE_LLM_MODELS` via `run`; old envs mapped until step 11; run record stores the chosen entry; propagate the typed floor | content-pipeline-kit | -- | `test_llm_backends.py`, `test_llm_model_endpoint.py`, `test_llm_platform.py`, `test_run_cli.py`, floor propagation tests |
| 9 | C3 `extra_launch_args --model` | an id from the same declaration (no in-repo caller) | content-pipeline-kit | -- | `test_execution_driver_claude_bg.py` |
| 9 | Y1 `PlannerPolicy.model` | a declaration routed through the new C1 env | yaml-data-editor-kit (dev-only) | -- | `tests/yaml-data-editor-kit` |
| 10 | O1 rows, O2 lanes, O3, O5, O6 preface and prose | drop `agent:`; `[astra, fable]`; fix the stale "never reads conserve_usage" preface and presence-autonomy :118; propagate the typed floor | claude-settings | -- | none (owner config); floor propagation tests |
| 10 | R2 bakeoff `--model` | an id | repo script | -- | -- |
| 11 | deprecations | remove `agent:`, `peer:`, job-kit old keys, content-pipeline old envs, `choose`/`--endpoint` aliases | bootstrap, awesome-kit, job-kit, content-pipeline-kit, llm-scripting-kit | -- | the tests above lose their compatibility cases |

## Review disposition (2026-09-16)

Two independent reviews; every finding is addressed in place. Where an
offered alternative was not taken, the choice is stated so it can go back to
the reviewer.

| Finding | Handling |
|---|---|
| 1 `run` API drops requirements, exclusion, job-kit loop | fixed in D4: `requirements=`, `exclude=`, `caller=`; job-kit keeps its loop, ledger and `max_attempts` and calls `describe`; `run` is for loop-less callers and counts attempts. The false "exactly as job-kit does in the existing implementation" is replaced by the stated behaviour change (quota/reachability pre-filter). |
| 2 halt-only re-selection regresses review lanes | fixed in D6: session set = any unexplained failure; process set = classified halts. |
| 3 pace: pinned freeze, None inputs, unbounded, same-pool example | fixed in D5: unpinned `evaluate()` for pace, pinned status only; edge rules for zero/None/near-reset; example corrected (fable = opus on `seven_day`, sonnet n/a); 0.44.1 referenced, not designed around. |
| 4 `bootstrap_lib` not linked in job-kit / skills-kit | fixed: step 0 manifest edge. Lazy-import alternative not taken (reason in D2). |
| 5 D3 too lenient; reviewers split | SUPERSEDED by directions 9 and 13-17; the final D3 path is silent skip, hidden render, and itemised floor. |
| 6 coverage and ownership gaps | fixed: rows for L4, L6/O4, C1 `ENDPOINT`, L1 `default_endpoint`; K1 = 9; A5 render moved to step 3; `lane_prompts.py:61` moved to step 1; Y1 owner corrected; B2 citation corrected. |
| 7 A3 is not a completion caller; posture | fixed in D4: `discover_model_entries`; REFUSE. |
| 8 deprecation window vs owner config at step 10 | fixed: removals are step 11, after the owner config step. |
| 9 core-name shadowing | fixed in D1/D2: core ids reserved; `check_registry_entry`. |
| 10 trigger text tested only in awesome-kit | fixed in D5: `describe` emits `Ranking.rule`; tested in llm-scripting-kit. |
| 11 `HALT_QUOTA` source | fixed in D6: rollout re-read after a non-zero codex exit, because the backend runs without `--json`. |
| 12 Q4 cost | stated in D1; the owner accepted it (direction 12). |

Disagreements: none. Two choices among offered alternatives -- finding 1
(job-kit keeps its loop rather than `run` re-implementing it) and finding 4
(manifest edge rather than lazy import) -- are argued in D4 and D2.

**Second pass (opus re-check, 2026-09-16): approve with fixes.**

| Finding | Handling |
|---|---|
| 1 job-kit must pass the floor-applied job | fixed in D4: `selection_job.requirements` (run.py:344, :601-603), with the drop-the-floor consequence stated. |
| 2 `describe` needs `capabilities=` and `backend_factory=` | fixed in D4: both parameters; job-kit passes the execution-time `advertised` mapping and its factory seam. |
| 3 partial edits on session re-selection; drill contradiction | fixed in D6 ("Partial edits": inspect and reset, or fresh worktree; `run` does it mechanically); drill's broken unit defined as exit 0 with a wrong result. |
| 4 fresh read vs pinned verdict | fixed in D5: displayed status, `usable` and `[default]` are pinned; the fresh read supplies only pace. |
| 5 `read_codex_pool` spec for un-opted entries | fixed in D6: the entry's own spec, else a default `seven_day` spec for the read only; no pacing verdict is created. |
| 6 live probes per attempt | fixed in D4: `reachability_cache=` parameter, run-scoped in job-kit. |
| 7 `usable` vs `unknown` | fixed: D4 says "not unreachable (unknown counts)"; D6 cross-references it. |
| 8 step 4 restates the trigger set | fixed: generated prose prints `Ranking.rule` verbatim. |
| 9 reservation check on the merged entry | fixed in D1 and D2 (`check_registry_entry(id, merged)`), with the owner's partial entries as the reason. |
| 10 keep the disposition table | kept. |

**Third pass (owner directions 9-12, 2026-09-16).** Only one reviewer
finding changed as a result: first-pass finding 5 (strict vs advisory
enforcement of constraint 4) is superseded -- direction 9 retires the
constraint, so neither reading is implemented and the same-pool warning is
gone; shared quota remains as information (`shares_quota_with`). Second-pass
finding 4 (pinned vs fresh) also feeds the "has a pace" table in D5
without changing its resolution.

**Lead review (2026-09-16).** The earlier conflict about shipped ids and
validator discovery is superseded by directions 13-17. The validator no
longer discovers registry files or carries shipped-id copies; runtime
resolution classifies unknown and unroutable entries, and the floor retains
their dispositions in declaration order.
