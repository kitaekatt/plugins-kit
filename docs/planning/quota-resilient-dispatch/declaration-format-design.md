# Design: one model-declaration format, and the multi-provider extension

Written 2026-09-16 for task `quota-resilient-dispatch`, item
`design-declaration-format`; revised the same day after two independent
reviews (opus primary; opencode/deepseek-pro cross-check) -- see "Review
disposition" at the end. Maintainer material: it lives under `docs/` because
nobody on a consumer machine needs it. The owner approves or amends this
before anything migrates. Inputs: the task's `plan.md` (owner directions
1-8), `log.md`, `model-declarations.md` (the map; its site ids A1..R2 are used
below), and `findings.md`. Where the map and findings.md disagree, the map
wins. Line numbers cite the dev tree at commit 498cc21c.

## Premise outcomes

| Premise | Outcome |
|---|---|
| Owner directions 1-8 | established; applied throughout. Direction 8 replaces the "when it cannot run" condition of 6. |
| llm-scripting-kit 0.44.0 reads an exhausted codex account as out-of-quota; orchestrate drops or moves seats (`orchestration_guidance.py:782` "moved back") | established; read at `load_quota_ranker` :689-786. |
| Map citations spot-checked | established; every citation used here was re-read. |
| hypothesis: llm-scripting-kit owns dispatch/ranking | CONFIRMED. It already owns every transport (`completion/factory.py:63-112`), the pacing verdicts (`usage_budget.py`), the ranking rule (`quota_selection.py`), reachability, seats, and halt classification. job-kit adds no provider (`select.py:141` calls `create_backend`). The two other provider-choosing paths (awesome-kit `dispatch.py:62`, content-pipeline-kit `backends.py:785`) wrap or bypass llm-scripting-kit code; neither adds a transport. |
| Plugin boundaries are hard; cross-plugin imports state REQUIRED / REFUSE / DEGRADE; opinions are configurable or registered | constraint; honoured. No skill moves. Each new import states its posture in D2 and D4. |

## Decision 1 -- Syntax: a list of registry names, one namespace, no prefixes

A model declaration is a list of strings. Each string is an entry id in the
llm-scripting-kit model registry (`default_config.yaml` `endpoints:` merged
with the user registry `~/.claude/config/model-endpoints.yaml`,
`model_endpoints.py:28-54`). A one-model declaration is a one-element list
(direction 2). A bare scalar is accepted on read as a one-element list, so
today's `model: opus` files keep parsing; the written form is always a list.

```yaml
models: [fable]                    # Claude-only plugin: one entry
models: [astra, fable]             # reviewer C, owner config
models: [sol, opus, deepseek-pro]  # orchestrate row
```

Carriers: a YAML list, a JSON list, a JS array literal in a Workflow script,
a repeated CLI flag or comma list. The shape is the format; the carrier is not.

**One namespace rule.** A name identifies WHAT model. HOW it is driven is a
property of the entry's `harness` and of the caller, never of the name:

| Entry kind | In-session caller (orchestrate, review skill) | Process caller (job-kit, CLI, content-pipeline) |
|---|---|---|
| `harness: claude` (`fable`, `opus`, `sonnet`, `haiku`) | Agent tool, `model: <name>` | `claude -p` (`ClaudeCliBackend`) |
| `harness: codex` / `opencode` | rendered `codex exec` / `opencode run` argv | `CodexCliBackend` / `OpencodeCliBackend` |
| transport (`base_url`) | not drivable (no agent loop) -- listed as unusable for a unit of work | `OpenRouterBackend` |

This resolves map finding 1 (one bare `opus`, three dispatches): `opus` is one
entry, and the caller's kind picks the mechanism. The three closed alias sets
(`orchestration_guidance.py:538`, `lane_prompts.py:61`, workflow-kit
`model.py:36`) collapse into the registry's `harness: claude` entries. The
registry must gain a shipped `haiku` entry (tier 1, family anthropic) -- the
only Claude alias in use (W3, W4) with no entry today.

**Core names are reserved.** `fable`, `opus`, `sonnet`, `haiku` are the CORE
set: valid on every installation because the harness itself defines them.
Every other name is an EXTENSION name that only the multi-provider plugin
can resolve. Because a config-layer or registry entry shadows a shipped one
(`models.py:518-521`), the core ids are RESERVED for `harness: claude`: the
validator (D2) flags a core id whose MERGED entry has another harness or a
`base_url`, and `describe` (D4) refuses to resolve it. The check runs on the
merged entry, not on a single layer, because a partial layer is legitimate:
the owner's `fable` and `opus` entries carry only `conserve_usage`
(`claude-settings/config/llm-scripting-kit.yaml`) and inherit their harness. This
split is what lets a Claude-only plugin validate without llm-scripting-kit
and is direction 3 in one sentence: the extension adds names, not syntax.

**Prefixes go.** `agent:` (A1, O1) is redundant once `opus` means the entry
and the caller kind picks the Agent tool; the renderer accepts it and
rewrites it with a note until migration step 11. `peer:<name>` (B1, O2) goes
too, for three reasons: it is an indirection over a fact the entry list can
state (`[peer:fable, fable]` on this fleet IS `[astra, fable]`); its
portability value ("resolve to whatever BESIDE seat this machine has") is
delivered by the same degradation an unresolvable name gets (D3); and under
direction 8 the independence it encoded becomes a rendered preference (D5),
not a token. The cost, for the owner (Q4): a machine whose side-by-side
reviewer for opus is not codex (an opencode entry, say) no longer DISCOVERS
it; a plain list names only what it names, so such a machine reviews on opus
itself unless its config lists the local peer. The shipped reviewer C
default `[peer:opus, opus]` becomes `[sol, opus]` (sol is the shipped BESIDE
seat of opus: tier 3, family openai). Alternatives considered: keeping
`peer:` as the one allowed selector (rejected: a second grammar inside the
format, resolvable only by the multi-provider plugin, so Claude-only
validators would have to special-case it); a structured entry
`{model, relation}` (rejected: direction 1 asks for one plain shape).

**Not declarations.** K4 `admitted_endpoints` is an allow-list for context
admission. The front-door group (L6, O4) is load balancing over identical
models; a declaration names a group as ONE entry (`qwen38`), and its own
in-group failover stays inside llm-scripting-kit. L1 `default`/
`default_endpoint`, L4, and S6 model aliases under an endpoint are addressed
in the migration (step 8).

## Decision 2 -- Where the format is specified and validated

**Specification:** one reference in bootstrap's plugin-dev skill,
`plugins/bootstrap/skills/plugin-dev/references/model-declaration.md`. Every
plugin's configuration doc cites it by path instead of restating the grammar.
Alternatives: llm-scripting-kit (rejected: a Claude-only plugin would cite the
multi-provider plugin for its format, inverting direction 3); skills-kit
(rejected: not a dependency of every plugin); `docs/` (rejected: consumers
must be able to read it, so it must ship). plugin-dev already hosts the
cross-plugin contracts (`optional-plugin-dependencies.md`, `enabling.md`),
and every plugin declares `dependencies: ["bootstrap"]`.

**Validator:** `plugins/bootstrap/bootstrap_lib/model_declaration.py`,
stdlib-only. It answers shape questions only:

```
parse(value) -> list[str]            # scalar -> [scalar]; rejects empty, non-string, duplicate
validate(names) -> Report            # per name: core | extension | malformed
                                     # malformed: not ^[a-z0-9][a-z0-9._-]*$, or contains ':'
                                     # Report.all_core: True when every name is core
                                     # Report.errors / warnings per D3
check_registry_entry(id, merged)     # core id whose MERGED entry is not harness: claude -> error (D1)
```

It never resolves an extension name and never imports llm-scripting-kit.

**Linking.** A shared lib does not carry its own edges to consumers (the
`$comment` in `plugins/content-pipeline-kit/bootstrap.json`). Today only
workflow-kit, awesome-kit, git-kit, p4-kit, content-pipeline-kit,
llm-scripting-kit and unreal-kit declare `bootstrap_lib` in
`shared_lib_imports`; job-kit declares only `["llm_scripting_kit"]`
(`plugins/job-kit/bootstrap.json:15`) and skills-kit declares none. So the
migration has a manifest step (step 0) adding `bootstrap_lib` to job-kit and
skills-kit before any code imports it -- a version bump each, validated
with `claude-dev` because it is manifest content. Posture: REQUIRED
(bootstrap is already a declared dependency of every plugin). The lazy-import
alternative (llm-scripting-kit importing the validator on demand) was not
chosen: llm-scripting-kit already links `bootstrap_lib`, and the plugins that
lack the edge are exactly the ones whose own code must call the validator.

## Decision 3 -- Constraint 4 when the multi-provider plugin is absent

Direction 4 ("multiple entries only across providers") has two enforceable
layers, and the reviewers split on how far to take the first:

- **All-core multi-entry lists** (`[opus, sonnet]`): the validator can see
  these without llm-scripting-kit (`Report.all_core and len > 1`). No site in
  the map has one as of 2026-09-16, so a STRICT rule (validation error)
  costs nothing at adoption and reads direction 4 literally. The cross-check reviewer preferred
  ADVISORY everywhere (a warning, never a refusal), on the ground that a
  Claude-only plugin may still want a model ladder and Claude Code's own
  `--fallback-model` shows one-model outages are real.
- **Same-pool pairs inside a mixed list** (`[fable, astra, sol, opus]`):
  only `describe` can see these (pool is a resolved fact). Both reviewers
  accept ADVISORY here: `describe` warns "X and Y draw on pool P; the second
  adds no quota resilience" and nothing refuses, so the owner's ladders stay
  legal and stay honest.

Recommendation: STRICT for all-core lists, ADVISORY for same-pool pairs. The
owner decides in Q1; the validator implements either with a one-line change.

Absent llm-scripting-kit, any extension name is unresolvable. Rule for every
consumer: a declaration with NO resolvable name is a configuration error
(REFUSE, naming the owner plugin and the install command, as
`run_review_lane.py` does today); a declaration with SOME unresolvable names
DEGRADES -- the unresolvable ones are listed as `unresolved: llm-scripting-kit
absent` in the rendered artifact (orchestrate's `Degraded render.` heading,
the review's `## Lane failures`), never silently dropped. This is the posture
the plugin-dev table already assigns to orchestrate and the review runner.

## Decision 4 -- The one API, owned by llm-scripting-kit

New module `plugins/llm-scripting-kit/lib/llm_scripting_kit/declaration.py`.
`quota_selection.rank_candidates` stays as the pure ranking core;
`choose_endpoint` (L3) becomes a thin caller.

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
    backend_factory: the factory used to resolve a name (default create_backend);
                  job-kit's injection seam, kept
    exclude: names the caller has ruled out (job-kit's run-level halt ledger
             plus same-job halts, run.py:596-615, :968-1001)
    reachability_cache: a caller-scoped mapping the probe results are read from
             and written to; without it every call probes every entry live
             (reachability.check_many has no cache; HTTP entries hit /models)
    Ranking.entries: EntryState per name, DECLARATION ORDER PRESERVED
        name, resolved, kind, harness, model, family, tier, drive
        reachability: reachable | unreachable | unknown   (reachability.py)
        usability: pinned verdict status                  (pinned_evaluate)
        pace: float | None                                (D5; unpinned evaluate)
        usable: resolved and not unreachable (unknown counts, fail-open) and
                pinned.usable and requirements match and not excluded
        default: bool       # first usable entry, exactly one or none
        is_self: bool       # matches self_ref (D5 independence)
    Ranking.rule: the rendered choice rule and trigger text (D5, D6)
    Ranking.warnings: same-pool pairs, unresolved names, shadowed core ids

run(names, request, *, project_root, requirements=None, exclude=(),
    max_attempts=1, on_attempt=None) -> RunResult
    Unattended dispatch for callers with NO loop of their own. Takes
    describe(caller="process") entries in order; each attempt is reported
    through on_attempt and counted against max_attempts; a halt (D6, process
    set) writes the verdict back and moves to the next usable entry.
```

CLI: `llm-scripting-kit describe <name>... [--self x] [--requirements f]
[--exclude n] [--json]` renders the per-entry lines of D5 for in-session
callers (the review skill's step 6, an agent checking before it dispatches).

Two consumer kinds. IN-SESSION callers (orchestrate rows, consult seats,
review lanes) call `describe(caller="session")` and let Claude choose
(direction 8). PROCESS callers split: job-kit KEEPS its attempt loop, its
`max_attempts`, and its halt ledger, and calls
`describe(caller="process", requirements=selection_job.requirements,
capabilities=advertised, backend_factory=backend_factory,
exclude=halts.current() | same_job_halts, reachability_cache=<run-scoped>)`
where `select_endpoint` filters today, taking the first usable entry.
`selection_job` is the job with the run-level deny floor already applied
(`_require_floor_subjects`, run.py:344, :601-603) -- passing the bare
`job.requirements` would drop the floor -- and `advertised` is the same
capability mapping execution checks (run.py:593-594). So the registered
deny-floor requirement (`plugins/CLAUDE.md:103`), run-level halt narrowing,
and job-kit's injection seams are preserved as inputs, not re-implemented.
The reachability cache is scoped to the run (one probe per entry per run),
matching the register's "confirm with two attempts, then exclude" rule. Callers with no loop (workflow-kit's
openrouter node, content-pipeline's `route()`) call `run`. What changes for
job-kit, and must be stated in its register entry: today it reads neither
quota nor reachability (`select.py:120-162` filters by requirements and
halts only); under `describe` an out-of-quota or unreachable entry is
skipped before the first attempt, with the reason in the ledger. No consumer
walks a fallback chain itself any more; the generated review step 6 prose
(`gen_code_review_skills.py:212-221`) becomes "call `describe`, choose,
announce" for Agent lanes, with `run_review_lane.py` calling `describe` for
endpoint lanes and the skill re-dispatching per D6.

Bringing the other provider-choosing paths under it:

- awesome-kit `dispatch.py` (A3) is not a completion caller: it builds the
  argv through `bootstrap_lib.codex.build_codex_exec_argv` and keeps its own
  dispatch cache (`dispatch.py:50-110`). `--model` becomes a declaration name
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
  llm-scripting-kit; a declaration names a group as one entry.

Register consequences (plugin-opinion razor): "a failed lane falls over only
along the chain its own configuration named" and "job-kit selects
deterministically" (`plugins/CLAUDE.md:90-102, :193-204`) stay true, with
the job-kit entry gaining the quota/reachability pre-filter sentence above;
the pinned-verdict entry (:205-217) is amended per D6.

## Decision 5 -- Direction 8 rendered

**Pace (the owner's "quota-velocity").** Both halves exist on `Budget`
(`usage_budget.py:211-212`): `remaining` = 1 - used_pct/100,
`window_remaining` = seconds_left / window_seconds (`_verdict` :365-396).

    pace = remaining / window_remaining        (shown as a percentage)

100% = exactly on pace; above = ahead (AVAILABLE); below = behind
(UNDER-QUOTA, the same comparison `_verdict` makes with `_PACE_EPSILON`).
Edge rules, because the inputs are not always both present:

- `remaining == 0` (including the codex exhaustion Budget, which carries
  `remaining=0.0` and `window_remaining=None`, :679-707): render
  "out of quota until <resets_at>"; pace is not computed.
- either input None (no-data, unpaced entry): render "n/a"; the entry is
  treated as available (the fail-open rule at :110-112 is unchanged).
- `window_remaining` below 1% of the window: render "about to reset" instead
  of a number, so a near-reset reading does not show an unbounded ratio.
- Pace is computed from an UNPINNED `evaluate()` read at render time.
  `pinned_evaluate` returns the STORED `remaining`/`window_remaining` and
  never recomputes an AVAILABLE verdict (:836-859), so a pinned pace would be
  frozen at session start. Only USABILITY (the status) stays pinned, which
  is what the register entry protects. The two reads can disagree: a fresh
  read may say out-of-quota while the pinned verdict still says usable, even
  for the `[default]` entry. The DISPLAYED status, `usable`, and `[default]`
  come from the pinned verdict; the fresh read supplies only the pace
  number. A pinned verdict moves only on an observed halt (D6). llm-scripting-kit 0.44.1 stores
  `resets_at = event time + 5h` for an exhaustion read with no parseable
  reset text, so a pinned OUT-OF-QUOTA always expires; nothing here designs
  around the earlier latch.

The alternative `remaining - window_remaining` (headroom in percentage
points) is easier to read but not comparable across a 5-hour and a 7-day
window; the ratio is. The name is Q3.

**What the agent is shown**, per entry, declaration order kept, nothing
reordered or dropped. On the owner's config fable and opus both read the
all-model `seven_day` pool (`claude-settings/config/llm-scripting-kit.yaml`),
so they show the same numbers; sonnet is unpaced:

```
Row 3 -- novel + load-bearing: choose one; default is marked.
  fable   claude/agent   under quota  38% left, 50% of window   pace 76%   [default]
  astra   codex          out of quota until 2026-09-19 15:34
  sol     codex          out of quota until 2026-09-19 15:34
  opus    claude/agent   under quota  38% left, 50% of window   pace 76%   [author]
  sonnet  claude/agent   n/a (unpaced)
Rule: any usable entry may be chosen; prefer higher pace; the default applies
only when you have no preference. Announce every choice from a multi-entry
declaration: "route: <unit> -> <entry>; <reason>".
```

`describe` EMITS the rule text and the D6 trigger text (`Ranking.rule`), so
they are tested where they are owned (`tests/llm-scripting-kit`) and
awesome-kit's render test asserts only that it passes them through. What
this replaces: `load_quota_ranker`'s reorder-and-drop
(`orchestration_guidance.py:689-786`), the "try A, then B" sentence
(:1105-1121), SKILL.md step 4's "tried in declaration order", and the seats
render that omits out-of-quota seats (:1705-1723; `SeatsResult.out_of_quota`
already exists). Out-of-quota entries stay visible with their reset time,
because "when does it come back" is a fact the agent can plan on.
`ORCHESTRATE_QUOTA_ROUTING=0` keeps its meaning for tests: render no quota
column.

**Announce.** One line at dispatch, `route: <unit> -> <entry>; <reason>`,
reason one of `default`, `higher pace`, `independence`, `<prior entry>
failed: <kind>`, or the agent's own clause. A one-entry declaration needs no
announcement. This generalizes the existing fallback announcement to every
choice.

**Reviewer independence as a preference (direction 6).** `describe` marks
`[author]` on an entry whose model matches `self_ref`. Rendered guidance:
"prefer a non-author entry; the author may review when no other usable
entry exists, and says so." `seats.py:276` keeps excluding self from
CONSULT seats; the prose that, as of 2026-09-16, says "never the authoring
model" (findings.md "Authorship independence") changes to the preference
wording in the `independence-as-preference` item.

**Unattended.** `run` and job-kit's loop do not judge: declaration order,
skip unusable, a failure moves on. Whether that should change is Q2.

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
no output. This is today's review-lane rule (`gen_code_review_skills.py:212`)
kept whole, so `plugins/CLAUDE.md:193-196` stays true. Only a schema-invalid
or wrong RESULT from a lane or unit that ran to completion (exit 0) is a
task failure, not a trigger. The agent announces the re-selection with the
failure kind, classified when a marker matches and "unexplained exit"
otherwise.

**Partial edits.** This set is wider than today's orchestrate rule ("a
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

| Trigger | Classified today | Change |
|---|---|---|
| quota (pool spent) | codex `usage_limit_exceeded` is NOT a halt: `test_completion_codex_backend.py:449` pins the prose to None | add `HALT_QUOTA` to `halt.py`. Source: `CodexCliBackend` runs codex WITHOUT `--json` (`codex_backend.py:111-116`), so no `task_complete` payload reaches it; on a non-zero codex exit the backend re-reads the rollout tail through `read_codex_pool` (the 0.44.0 exhaustion shape: null windows plus no credits, or the `usage_limit_exceeded` event with its reset text). `read_codex_pool` takes a `ConserveSpec` (:626-628): the entry's own when it declares `conserve_usage`, else the default `ConserveSpec(pool=seven_day)` -- the pool the codex reader remaps to `primary` -- used for the read only; an entry with no opt-in still gets no pacing verdict, only the halt. The stderr prose pin stays None. Mirror in content-pipeline `platform.py:378-386`. |
| rate limit | `HALT_RATE_LIMIT` (429 envelopes, CLI backoff, timeouts) | unchanged |
| auth | `HALT_AUTH` | unchanged; move on, but write NO quota verdict |
| insufficient credit | `HALT_INSUFFICIENT_CREDIT` | treated as quota for verdict write-back |
| launch/transport | CLI missing, connection refused, exit with no output (content-pipeline `HALT_UNREACHABLE`) | `run` moves on; the entry is marked unreachable for the run |

A task error, schema-invalid output, or a transport timeout with no marker
stays a failed attempt in the process set.

**Stale verdict.** Today an observed halt never updates the pinned verdict
(register entry `plugins/CLAUDE.md:205-217`: "never re-evaluated downward").
The design amends that entry: a verdict is re-evaluated downward ONLY on an
observed quota/credit halt, which writes OUT-OF-QUOTA for that entry with
the reset time when the halt carries one, else `event time + 5h` (the 0.44.1
rule). An AVAILABLE verdict still never flips on a re-read, so the
guarantee the entry protects holds; only an actual failure moves it. A
reset time passing already flips OUT-OF-QUOTA back to no-data, which covers
a mid-session codex reset in the other direction.

## Owner questions

**Q1 -- What is a "provider" in direction 4, and how strict is the rule?**
(a) transport kind (claude / codex / opencode / HTTP) forbids `[fable, ..,
opus]` and `[astra, sol]`, which the owner's rows use (O1 :117); (b) quota
pool as `usage_budget` reads it forbids the same pairs on this account
(fable and opus share `seven_day`; astra and sol share codex `primary`);
(c) registry entry makes the constraint vacuous. Two readings of strictness
(D3): STRICT for all-core lists and ADVISORY for same-pool pairs in mixed
lists (opus review), or ADVISORY everywhere (deepseek review).
Recommendation: definition (b), STRICT for all-core lists (costs nothing
today, literal direction 4, enforceable without llm-scripting-kit), ADVISORY
for same-pool pairs (keeps the owner's quality ladders legal and honest).

**Q2 -- Unattended selection: deterministic or judgment?** Both reviewers
agree with the recommendation: deterministic (declaration order, skip
unusable, a halt moves on), for the reason the register gives -- nobody is
watching, so a run must be explainable from its inputs. Pace is recorded per
attempt in the ledger so the explanation includes what a judging agent would
have seen.

**Q3 -- Name for quota-velocity.** Candidates: "pace" (the vocabulary of
`usage_budget.py`: "ahead of pace", "behind pace"), "headroom" (fits the
difference form), "quota-velocity". Recommendation, agreed by both
reviewers: **pace**, `remaining / window_remaining`, as a percentage.

**Q4 -- Drop `peer:`?** Recommended yes, at the cost stated in D1: a
machine whose cross-family peer for opus is a non-codex entry stops
discovering it and must list it. Reversible: if kept, `peer:` stays the
single permitted selector and resolves only through `describe`.

## Biggest risk

The Agent tool is driven by prose, so an in-session re-selection depends on
Claude acting on a failure it sees rather than on a classifier. D6's session
set makes any unexplained failure a trigger, which removes the missed-string
risk but adds a new one: an agent re-selecting on a failure that was really
the unit's own bug. The check: the announcement must carry the failure kind,
and a live drill on this machine while codex is exhausted (until 2026-09-19
15:34) dispatches a multi-entry row and confirms the announcement names a
usable entry with "codex: out of quota" as the reason, while a deliberately
broken unit on a usable entry -- one that exits 0 with a wrong result -- is
reported as a task failure and NOT re-routed, and a unit that halts after
writing is re-run only after its workspace is reset or replaced. `tests/llm-scripting-kit` pins the rule and trigger text that
`describe` emits. A second risk is the `haiku` entry and the `agent:`
rewrite landing in different releases; the order below keeps the registry
step first.

## Migration table

Ordered so each step publishes on its own; every step needs a version bump
and a test shown to fail first. "Gen" names the generator that must change.

| Step | Sites | New declaration | Owner plugin | Gen | Tests pinning today |
|---|---|---|---|---|---|
| 0 | job-kit, skills-kit manifests | add `bootstrap_lib` to `shared_lib_imports`; validate with `claude-dev` | job-kit, skills-kit | -- | `tests/job-kit/test_bin.py`, `tests/skills-kit/test_asset_dependencies.py` (manifest shape) |
| 1 | spec + validator | `model-declaration.md`; `bootstrap_lib/model_declaration.py` | bootstrap | -- | new: shape, core/extension, reserved ids, D3 rule |
| 1 | B1 reviewer `model` | list of names; `peer:` accepted and rewritten until step 11 | bootstrap | -- | `tests/bootstrap/code_review/test_review_profiles.py` (`peer:` resolution, `model_fallbacks`) |
| 1 | B2 `validator_models` | reason -> one-element list (scalar accepted; `review_profiles.py:348-360` is scalar-only) | bootstrap | -- | same test file |
| 1 | `lane_prompts.py:61` alias set | replaced by the core set from the validator | bootstrap | -- | `test_lane_prompts.py` |
| 2 | L2 registry | add shipped `haiku` (claude, tier 1); `check_registry_entry` on load | llm-scripting-kit | -- | `test_model_endpoints.py`, `test_endpoints.py` (mirror sync) |
| 2 | L3 `choose --prefer` | `describe <name>...`; `choose` kept as alias | llm-scripting-kit | -- | `test_quota_selection.py`, `test_llm_scripting_cli.py` |
| 2 | L4 `resolve` / `complete --endpoint --model` | `--models <declaration>`; `--endpoint` kept as alias; `--model` stays a per-entry override | llm-scripting-kit | -- | `test_llm_scripting_cli.py`, `test_completion_factory.py` |
| 2 | halt kinds | `HALT_QUOTA` from a rollout re-read; verdict write-back | llm-scripting-kit | -- | `test_completion_halt.py`, `test_completion_codex_backend.py:432-455`, `test_usage_budget.py` |
| 2 | L5 `review_lane --model` | one name; endpoint lanes call `describe` | llm-scripting-kit | -- | `test_review_lane.py` |
| 2 | A5 seats data | `describe` marks `[author]`; `SeatsResult` unchanged | llm-scripting-kit | -- | `test_seats.py` |
| 2 | L6, O4 front-door groups | unchanged; a group is one declaration name | llm-scripting-kit | -- | `test_frontdoor.py` |
| 3 | A1 rows | `models: [..]` without `agent:`; renderer annotates, never reorders | awesome-kit | -- | `test_orchestration_guidance.py` (routing text, "moved back", dropped) |
| 3 | A2 `requires_model` | same names, no prefix strip | awesome-kit | -- | same |
| 3 | A3 `dispatch.py --model` | a name resolved via `discover_model_entries`; no hardcoded default; REFUSE posture | awesome-kit | -- | `test_dispatch.py` |
| 3 | A4 backend `command:` | unchanged (adapter supplies the model) | awesome-kit | -- | -- |
| 3 | A5 seats render (:1705-1723) | show out-of-quota seats with reset time | awesome-kit | -- | `test_orchestration_guidance.py` |
| 3 | orchestrate SKILL.md step 4, seat rule :91 | choose-and-announce wording; rule text passed through from `describe` | awesome-kit | -- | `test_orchestration_guidance.py`, `tests/repo-scripts/test_agent_directives.py` |
| 3 | R1 `check_model_dispatch.py` | reads names, no `agent:` | repo script | -- | `tests/repo-scripts/test_check_model_dispatch.py` |
| 4 | G1, G2, step 6 model-kind rule, stale "no Agent fallback" line (:506) | dispatch by entry harness; `describe`, choose, announce; the prose prints `Ranking.rule` verbatim rather than restating the trigger set; drop the gotcha | git-kit, p4-kit | `scripts/gen_code_review_skills.py` | `test_skill_drift.py`, `test_lane_retry_prose.py`, `tests/git-kit/test_run_review_lane.py` |
| 5 | J1 `endpoint_preference` and aliases | `models: [..]` (old keys accepted); `select_endpoint` calls `describe(caller="process", requirements, exclude)`; loop and ledger unchanged | job-kit | -- | `tests/job-kit/test_select.py`, `test_model.py`, `test_runner.py` (same-job halts :604-611) |
| 5 | register entries :90-102, :205-217 | amend per D4 and D6 | plugins/CLAUDE.md | -- | -- |
| 6 | K3 `DEFAULT_ENDPOINTS`, `--endpoint` | `--models`; emitted as J1's new key | skills-kit | -- | `tests/skills-kit/test_emit_audit_jobs.py` |
| 6 | K2 remediate literals (4) | `model: 'sonnet'` emitted from a one-entry declaration in the generator | skills-kit | `plugins/skills-kit/scripts/gen_workflow_js.py` | `test_workflow_js_drift.py` |
| 6 | K1 hand-written literals (9) | one-entry declarations; a drift check that each literal equals the declared name | skills-kit | `check_shared_chunks` extended | `test_workflow_js_drift.py` |
| 6 | K4 | unchanged (allow-list, not a declaration) | -- | -- | -- |
| 7 | W1 `model:` enum | core set incl. `fable`; list-of-one accepted | workflow-kit | -- | `tests/workflow-kit/test_loader.py`, `test_compiler.py` |
| 7 | W3, W4 `haiku` | one-entry declaration; frontmatter/preamble emit the scalar carrier | workflow-kit | -- | `test_compiler.py` |
| 8 | W2 openrouter node; L1 `default`/`defaultCheap`/`default_endpoint`; S6 aliases | names must be transport ENTRIES: ship the `openrouter` sub-aliases as entries (`or-gpt-mini`, `or-qwen`), keep `models:` under an endpoint as a per-entry override only; `default_endpoint` becomes the one-entry default declaration | workflow-kit, llm-scripting-kit | -- | `test_openrouter_run.py`, `test_model_resolve.py` -- least certain step; may stay a scalar alias if the owner prefers |
| 9 | C1 env triple (`BACKEND`, `MODEL`, `ENDPOINT`), C2 run record | `CONTENT_PIPELINE_LLM_MODELS` via `run`; old envs mapped until step 11; run record stores the chosen entry | content-pipeline-kit | -- | `test_llm_backends.py`, `test_llm_model_endpoint.py`, `test_llm_platform.py`, `test_run_cli.py` |
| 9 | C3 `extra_launch_args --model` | a name from the same declaration (no in-repo caller today) | content-pipeline-kit | -- | `test_execution_driver_claude_bg.py` |
| 9 | Y1 `PlannerPolicy.model` | a declaration routed through the new C1 env | yaml-data-editor-kit (dev-only) | -- | `tests/yaml-data-editor-kit` |
| 10 | O1 rows, O2 lanes, O3, O5, O6 preface and prose | drop `agent:`; `[astra, fable]`; fix the stale "never reads conserve_usage" preface and presence-autonomy :118 | claude-settings | -- | none (owner config) |
| 10 | R2 bakeoff `--model` | a name | repo script | -- | -- |
| 11 | deprecations | remove `agent:`, `peer:`, job-kit old keys, content-pipeline old envs, `choose`/`--endpoint` aliases | bootstrap, awesome-kit, job-kit, content-pipeline-kit, llm-scripting-kit | -- | the tests above lose their compatibility cases |

## Review disposition (2026-09-16)

Two independent reviews; every finding is addressed in place. Where an
offered alternative was not taken, the choice is stated so it can go back to
the reviewer.

| Finding | Handling |
|---|---|
| 1 `run` API drops requirements, exclusion, job-kit loop | fixed in D4: `requirements=`, `exclude=`, `caller=`; job-kit keeps its loop, ledger and `max_attempts` and calls `describe`; `run` is for loop-less callers and counts attempts. The false "exactly as job-kit does today" is replaced by the stated behaviour change (quota/reachability pre-filter). |
| 2 halt-only re-selection regresses review lanes | fixed in D6: session set = any unexplained failure; process set = classified halts. |
| 3 pace: pinned freeze, None inputs, unbounded, same-pool example | fixed in D5: unpinned `evaluate()` for pace, pinned status only; edge rules for zero/None/near-reset; example corrected (fable = opus on `seven_day`, sonnet n/a); 0.44.1 referenced, not designed around. |
| 4 `bootstrap_lib` not linked in job-kit / skills-kit | fixed: step 0 manifest edge. Lazy-import alternative not taken (reason in D2). |
| 5 D3 too lenient; reviewers split | fixed: both readings in D3 and Q1; recommendation = strict all-core, advisory same-pool. |
| 6 coverage and ownership gaps | fixed: rows for L4, L6/O4, C1 `ENDPOINT`, L1 `default_endpoint`; K1 = 9; A5 render moved to step 3; `lane_prompts.py:61` moved to step 1; Y1 owner corrected; B2 citation corrected. |
| 7 A3 is not a completion caller; posture | fixed in D4: `discover_model_entries`; REFUSE. |
| 8 deprecation window vs owner config at step 10 | fixed: removals are step 11, after the owner config step. |
| 9 core-name shadowing | fixed in D1/D2: core ids reserved; `check_registry_entry`. |
| 10 trigger text tested only in awesome-kit | fixed in D5: `describe` emits `Ranking.rule`; tested in llm-scripting-kit. |
| 11 `HALT_QUOTA` source | fixed in D6: rollout re-read after a non-zero codex exit, because the backend runs without `--json`. |
| 12 Q4 cost | stated in D1 and Q4. |

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
