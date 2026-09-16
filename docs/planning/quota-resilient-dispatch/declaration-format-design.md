# Design: one model-declaration format, and the multi-provider extension

Written 2026-09-16 for task `quota-resilient-dispatch`, item
`design-declaration-format`. Maintainer material: it lives under `docs/`
because nobody on a consumer machine needs it. The owner approves or amends
this before anything migrates. Inputs: the task's `plan.md` (owner directions
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

**Core names.** `fable`, `opus`, `sonnet`, `haiku` are the CORE set: valid on
every installation because the harness itself defines them. Every other name
is an EXTENSION name that only the multi-provider plugin can resolve. This
split is what lets a Claude-only plugin validate without llm-scripting-kit
(D2) and is the whole of direction 3 in one sentence: the extension adds names,
not syntax.

**Prefixes go.** `agent:` (A1, O1) is redundant once `opus` means the entry
and the caller kind picks the Agent tool; the renderer accepts it for one
release and rewrites it with a note. `peer:<name>` (B1, O2) goes too, for
three reasons: it is an indirection over a fact the entry list can state
(`[peer:fable, fable]` on this fleet IS `[astra, fable]`); its portability
value ("resolve to whatever BESIDE seat this machine has") is delivered by the
same degradation an unresolvable name gets (D3); and under direction 8 the
independence it encoded becomes a rendered preference (D5), not a token. The
shipped reviewer C default `[peer:opus, opus]` becomes `[sol, opus]` (sol is
the shipped BESIDE seat of opus: tier 3, family openai). Alternatives
considered: keeping `peer:` as the one allowed selector (rejected: a second
grammar inside the format, resolvable only by the multi-provider plugin, so
Claude-only validators would have to special-case it); a structured entry
`{model, relation}` (rejected: direction 1 asks for one plain shape).

**Not declarations.** Two constructs the map lists are not "who may do this
unit" and stay as they are: K4 `admitted_endpoints` is an allow-list for
context admission; the front-door group (L6, O4) is load balancing over
identical models and is named from a declaration as ONE entry (`qwen38`).
L1 `default`/`defaultCheap` and S6 model aliases under an endpoint are
addressed in the migration (last rows).

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
stdlib-only, linked into every venv the way `bootstrap_lib` already is
(`.pth`; job-kit, skills-kit and workflow-kit add `bootstrap_lib` to
`shared_lib_imports`). It answers shape questions only:

```
parse(value) -> list[str]            # scalar -> [scalar]; rejects empty, non-string, duplicate
validate(names) -> Report            # per name: core | extension | malformed
                                     # malformed: not ^[a-z0-9][a-z0-9._-]*$, or contains ':'
                                     # Report.all_core: True when every name is core
```

It never resolves an extension name and never imports llm-scripting-kit.
Posture for consumers importing it: REQUIRED (bootstrap is already required).
Resolution -- does this name exist here, what harness, what quota -- is D4.

## Decision 3 -- Constraint 4 when the multi-provider plugin is absent

Direction 4 as stated ("multiple entries only across providers") cannot be
enforced by the validator, because "provider" is a property of resolved
entries and only D4 resolves them. The validator can enforce one weaker,
purely syntactic rule: a declaration whose names are ALL core names is
Claude-only. What happens to a multi-entry Claude-only list depends on the
owner's answer to question Q1 (below). The design supports both readings:

- Strict reading: `all_core and len > 1` is a validation error in every plugin.
- Advisory reading (recommended): it is a WARNING in the validator's report
  (`same_provider: [opus, sonnet]`) that consumers render but do not refuse.

Without llm-scripting-kit, any extension name is unresolvable. The rule for
every consumer: a declaration with NO resolvable name is a configuration error
(REFUSE, naming the owner plugin and the install command, as
`run_review_lane.py` does today); a declaration with SOME unresolvable names
DEGRADES -- the unresolvable ones are listed as `unresolved: llm-scripting-kit
absent` in the rendered artifact (orchestrate's `Degraded render.` heading,
the review's `## Lane failures`), never silently dropped. This is the posture
the plugin-dev table already assigns to orchestrate and the review runner;
the design extends it to every site.

## Decision 4 -- The one API, owned by llm-scripting-kit

New module `plugins/llm-scripting-kit/lib/llm_scripting_kit/declaration.py`,
two functions and one CLI verb. `quota_selection.rank_candidates` stays as
the pure ranking core it is; `choose_endpoint` (L3) becomes a thin caller.

```
describe(names, *, project_root, self_ref=None, exclude=()) -> Ranking
    Ranking.entries: EntryState per name, DECLARATION ORDER PRESERVED
        name, resolved, kind, harness, model, family, tier
        reachability: reachable | unreachable | unknown   (reachability.py)
        budget: Budget | None                             (pinned_evaluate)
        pace: float | None                                (D5 formula)
        usable: bool        # resolved and reachable and budget.usable
        default: bool       # first usable entry, exactly one or none
        is_self: bool       # matches self_ref (D5 independence)
        drive: agent | claude-cli | codex | opencode | http | none
    Ranking.warnings: same-pool pairs, unresolved names, single-provider note

run(names, request, *, project_root, on_attempt=None) -> RunResult
    Unattended dispatch. Tries entries in describe() order skipping unusable
    ones; on a halt (D6) records it, writes the verdict back (D6), and moves
    to the next usable entry; returns every attempt so a ledger can record it.
```

CLI: `llm-scripting-kit describe <name>... [--self x] [--json]` renders the
per-entry lines of D5 for in-session callers (the review skill's step 6, an
agent checking before it dispatches) and shell scripts.

Two consumer kinds, one API. IN-SESSION callers (orchestrate rows, consult
seats, review lanes) call `describe` and let Claude choose (direction 8).
UNATTENDED callers (job-kit, content-pipeline-kit, workflow-kit's openrouter
node, `emit_audit_jobs`) call `run`, which is where detection and re-selection
live (direction 5). No consumer walks a fallback chain itself any more; the
generated review step 6 prose "walk `model_fallbacks` in order" (G1 source
`gen_code_review_skills.py:212-221`) is replaced by "call `describe`, choose,
announce" for Agent lanes and by `run` inside `review_lane.py` for endpoint
lanes.

Bringing the other provider-choosing paths under it:

- awesome-kit `dispatch.py` (A3): `--model` becomes a declaration name resolved
  through `create_backend`; the hardcoded `gpt-5.6-sol` default is replaced by
  the row's chosen entry passed by the agent. Posture: DEGRADE is impossible
  here (it launches codex), so REQUIRED via the existing `bootstrap_lib.codex`
  edge plus llm-scripting-kit for resolution; absence exits with the owner
  named, as it does today for `bootstrap_lib.codex`.
- content-pipeline-kit (C1, R-m): `CONTENT_PIPELINE_LLM_BACKEND` (provider
  KIND) plus `..._MODEL` (vendor id) are replaced by one
  `CONTENT_PIPELINE_LLM_MODELS` declaration resolved by `run`. The kind env is
  honoured for one release and mapped to the shipped entry of that harness
  with a deprecation line. `route()` keeps its `mock` seam untouched. Posture
  stays REQUIRED at the manifest (`install: auto`).
- The front door (L6) keeps its own HTTP failover; it is inside
  llm-scripting-kit already and a declaration names a group as one entry.

Register consequences (plugin-opinion razor): the entries "A failed lane
falls over only along the chain its own configuration named" and "job-kit
selects deterministically" (`plugins/CLAUDE.md:90-102, :193-204`) stay true
under `run`; the pinned-verdict entry (:205-217) is amended per D6.

## Decision 5 -- Direction 8 rendered

**Quota-velocity.** Both halves exist on `Budget` (`usage_budget.py:211-212`):
`remaining` = 1 - used_pct/100, `window_remaining` = seconds_left /
window_seconds (`_verdict` :365-396). The design defines

    pace = remaining / window_remaining        (shown as a percentage)

100% = exactly on pace; above 100% = ahead (AVAILABLE); below = behind
(UNDER-QUOTA, the same comparison `_verdict` makes with `_PACE_EPSILON`);
0% = OUT-OF-QUOTA; no reading = "n/a" and the entry is treated as available
(the fail-open rule at `usage_budget.py:110-112` is unchanged). The
alternative `remaining - window_remaining` (headroom in percentage points) is
easier to read but is not comparable across a 5-hour and a 7-day window; the
ratio is. The name is Q3 for the owner.

**What the agent is shown**, per entry, declaration order kept, nothing
reordered or dropped:

```
Row 3 -- novel + load-bearing: choose one; default is marked.
  fable   claude/agent   available    72% left, 50% of window   pace 144%   [default]
  astra   codex          out of quota until 2026-09-19 15:34
  sol     codex          out of quota until 2026-09-19 15:34
  opus    claude/agent   under quota  38% left, 50% of window   pace 76%    [author]
Rule: any usable entry may be chosen; prefer higher pace; the default applies
only when you have no preference. Announce every choice from a multi-entry
declaration: "route: <unit> -> <entry>; <reason>".
```

What this replaces: `load_quota_ranker`'s reorder-and-drop
(`orchestration_guidance.py:689-786`), the "try A, then B" sentence
(:1105-1121), SKILL.md step 4's "tried in declaration order", and the seats
render that omits out-of-quota seats (:1705-1723). Out-of-quota entries stay
visible with their reset time, because "when does it come back" is a fact the
agent can plan on and a hidden entry cannot be reasoned about.
`ORCHESTRATE_QUOTA_ROUTING=0` keeps its meaning for tests: render no quota
column.

**Announce.** One line at dispatch, `route: <unit> -> <entry>; <reason>`,
where reason is one of `default`, `higher pace`, `independence`, `<prior
entry> halted: <kind>`, or the agent's own clause. A one-entry declaration
needs no announcement. This generalizes the existing fallback announcement
("names the model immediately before the fallback") to every choice.

**Reviewer independence as a preference (direction 6).** `describe` marks
`[author]` on an entry whose model matches `self_ref` (the session's own
model, as `--self` already supplies to seats). Rendered guidance: "prefer a
non-author entry; the author may review when no other usable entry exists,
and says so." `seats.py:276` keeps excluding self from CONSULT seats (a
consult is by definition someone else); the review profile and orchestrate
prose that, as of 2026-09-16, say "never the authoring model" (findings.md
"Authorship independence") change to the preference wording in the
`independence-as-preference` item.

**Unattended.** `run` does not judge: it takes entries in declaration order,
skipping unusable ones, exactly as job-kit does today. Whether that should
change is Q2.

## Decision 6 -- What "cannot run" means, and re-selection

An entry is unusable BEFORE dispatch when: it does not resolve; its harness
CLI or endpoint is `unreachable` (`reachability.py:61-68`; `unknown` counts as
usable, fail-open); or its pinned verdict is OUT-OF-QUOTA.

An attempt HALTS -- and `run` moves to the next usable entry, an in-session
agent re-chooses and announces -- on:

| Trigger | Classified today | Change |
|---|---|---|
| quota halt (pool spent) | codex `usage_limit_exceeded` is NOT a halt: `test_completion_codex_backend.py:449` pins the prose to None | add `HALT_QUOTA` to `halt.py`, classified STRUCTURALLY from the codex `task_complete` error payload and the rollout `rate_limits` shape (the 0.44.0 read), never from prose; the prose pin stays None. Mirror in content-pipeline `platform.py:378-386`. |
| rate limit | `HALT_RATE_LIMIT` (429 envelopes, CLI backoff, timeouts) | unchanged |
| auth | `HALT_AUTH` | unchanged; re-select but do NOT write a quota verdict |
| insufficient credit | `HALT_INSUFFICIENT_CREDIT` | treated as quota for verdict write-back |
| launch/transport | CLI missing, connection refused, non-zero exit with no output (content-pipeline `HALT_UNREACHABLE`) | `run` treats as halt; reachability cache marks the entry unreachable for the run |
| Agent-tool error in-session | not classified anywhere | the spec lists the harness strings that count (usage limit, rate limit, model unavailable); anything else is a TASK failure and never triggers re-selection |

Not a trigger: schema-invalid output, a task error, a timeout with no halt
marker on a transport (today's rule), or a `model_fallbacks` walk (removed).

**Stale verdict.** Today an observed halt never updates the pinned verdict
(register entry `plugins/CLAUDE.md:205-217`: "never re-evaluated downward").
The design amends that entry: a verdict is re-evaluated downward ONLY on an
observed quota/credit halt, which writes OUT-OF-QUOTA for that entry with the
reset time when the halt carries one (codex "try again at"), else the
existing 5-hour latch. An AVAILABLE verdict still never flips on a re-read,
so the guarantee the entry protects (work planned against a model does not
lose it to a re-read) holds; only an actual failure moves it. A reset time
passing already flips OUT-OF-QUOTA back to no-data (0.44.0), which covers the
mid-session codex reset in the other direction.

## Owner questions

**Q1 -- What is a "provider" in direction 4?** Options: (a) transport kind
(claude / codex / opencode / HTTP): forbids `[fable, .., opus]` and
`[astra, sol]`, which the owner's rows use (O1 :117) and which Claude Code's
own `--fallback-model` shows have value when one MODEL is unavailable;
(b) quota pool as `usage_budget` reads it: on this account fable and opus
share `seven_day` and astra and sol share codex `primary`, so (b) forbids the
same pairs; (c) serving endpoint / registry entry: makes the constraint
vacuous. Recommendation: keep (b) as the DEFINITION -- it is what failover
actually cares about -- and make the constraint ADVISORY: `describe` warns
"entries X and Y draw on pool P; the second adds no quota resilience" and no
validator refuses. The owner's quality ladders stay legal and stay honest.

**Q2 -- Unattended selection: deterministic or judgment?** Recommendation:
deterministic (declaration order, skip unusable, halt moves on), for the
reason the register already gives: nobody is watching, so a run must be
explainable from its inputs. Judgment lives where a session can announce
it. `run` should still record pace per attempt in the ledger so the
explanation includes what a judging agent would have seen.

**Q3 -- Name for quota-velocity.** Candidates: "pace" (already the vocabulary
of `usage_budget.py`: "ahead of pace", "behind pace"), "headroom" (fits the
difference form better than the ratio), "quota-velocity". Recommendation:
**pace**, as `pace = remaining / window_remaining`, shown as a percentage.

**Q4 (raised by D1) -- Drop `peer:`?** Recommended yes; reversible: if kept,
it stays the single permitted selector and resolves only through `describe`.

## Biggest risk

The Agent tool is driven by prose, so an in-session re-selection depends on
Claude recognising a quota error string that this repo does not control and
the spec can only list. The check that catches it: a rendered-policy test in
`tests/awesome-kit/test_orchestration_guidance.py` asserting the trigger
strings appear verbatim in the rendered rule, plus a live drill on this
machine while codex is exhausted (until 2026-09-19 15:34): dispatch a
multi-entry row, confirm the announcement names a usable entry and that the
codex entries render as out of quota with that reset time. A second risk is
the `haiku` entry and the `agent:` rewrite landing in different releases;
the migration order below keeps the registry step first.

## Migration table

Ordered so each step publishes on its own; every step needs a version bump
and a test shown to fail first. "Gen" names the generator that must change.

| Step | Sites | New declaration | Owner plugin | Gen | Tests pinning today |
|---|---|---|---|---|---|
| 1 | spec + validator | `model-declaration.md`; `bootstrap_lib/model_declaration.py` | bootstrap | -- | new: shape, core/extension, warning |
| 1 | B1 reviewer `model` | list of names; `peer:` accepted and rewritten for one release | bootstrap | -- | `tests/bootstrap/code_review/test_review_profiles.py` (`peer:` resolution, `model_fallbacks`) |
| 1 | B2 `validator_models` | reason -> one-element list (scalar accepted) | bootstrap | -- | same file (:348-360 scalar-only) |
| 2 | L2 registry | add shipped `haiku` (claude, tier 1) | llm-scripting-kit | -- | `test_model_endpoints.py`, `test_endpoints.py` (mirror sync) |
| 2 | L3 `choose --prefer` | `describe <name>...`; `choose` kept as alias | llm-scripting-kit | -- | `test_quota_selection.py`, `test_llm_scripting_cli.py` |
| 2 | halt kinds | `HALT_QUOTA`; codex structural classification; verdict write-back | llm-scripting-kit | -- | `test_completion_halt.py`, `test_completion_codex_backend.py:432-455`, `test_usage_budget.py` |
| 2 | L5 `review_lane --model` | one name; endpoint lanes run through `run` | llm-scripting-kit | -- | `test_review_lane.py` |
| 2 | A5 seats | `describe` marks `[author]`; render keeps out-of-quota seats with reset time | llm-scripting-kit | -- | `test_seats.py` |
| 3 | A1 rows | `models: [..]` without `agent:`; renderer annotates, never reorders | awesome-kit | -- | `test_orchestration_guidance.py` (routing text, "moved back", dropped) |
| 3 | A2 `requires_model` | same names, no prefix strip | awesome-kit | -- | same |
| 3 | A3 `dispatch.py --model` | a name, resolved through the registry; no hardcoded default | awesome-kit | -- | `test_dispatch.py` |
| 3 | A4 backend `command:` | unchanged (adapter supplies the model) | awesome-kit | -- | -- |
| 3 | orchestrate SKILL.md step 4, seat rule :91 | choose-and-announce wording | awesome-kit | -- | `test_orchestration_guidance.py`, `test_agent_directives.py` |
| 3 | R1 `check_model_dispatch.py` | reads names, no `agent:` | repo script | -- | `tests/repo-scripts/test_check_model_dispatch.py` |
| 4 | G1, G2, step 6 model-kind rule, stale "no Agent fallback" line (:506) | dispatch by entry harness; `describe`, choose, announce; drop the gotcha | git-kit, p4-kit | `scripts/gen_code_review_skills.py` | `test_skill_drift.py`, `test_lane_prompts.py` (alias set), `test_lane_retry_prose.py`, `tests/git-kit/test_run_review_lane.py` |
| 5 | J1 `endpoint_preference` and aliases | `models: [..]` (old keys accepted); selection through `run` | job-kit | -- | `tests/job-kit/test_select.py`, `test_model.py`, `test_runner.py` (same-job halts :604-611) |
| 5 | register entries :90-102, :205-217 | amend per D4 and D6 | plugins/CLAUDE.md | -- | -- |
| 6 | K3 `DEFAULT_ENDPOINTS`, `--endpoint` | `--models`; emitted as J1's new key | skills-kit | -- | `tests/skills-kit/test_emit_audit_jobs.py` |
| 6 | K2 remediate literals | `model: 'sonnet'` emitted from a one-entry declaration in the generator | skills-kit | `plugins/skills-kit/scripts/gen_workflow_js.py` | `test_workflow_js_drift.py` |
| 6 | K1 hand-written literals (8) | one-entry declarations; a drift check that each literal equals the declared name | skills-kit | `check_shared_chunks` extended | `test_workflow_js_drift.py` |
| 6 | K4 | unchanged (allow-list, not a declaration) | -- | -- | -- |
| 7 | W1 `model:` enum | core set incl. `fable`; list-of-one accepted | workflow-kit | -- | `tests/workflow-kit/test_loader.py`, `test_compiler.py` |
| 7 | W3, W4 `haiku` | one-entry declaration; frontmatter/preamble emit the scalar carrier | workflow-kit | -- | `test_compiler.py` |
| 7 | W2 openrouter node, L1 `default`/`defaultCheap`, S6 aliases | names must be transport ENTRIES; ship `openrouter` sub-aliases as entries (`or-gpt-mini`, `or-qwen`) and deprecate `models:` under an endpoint | workflow-kit, llm-scripting-kit | -- | `test_openrouter_run.py`, `test_model_resolve.py` -- least certain row; may stay a scalar model alias if the owner prefers |
| 8 | C1 env pair, C2 run record, Y1 | `CONTENT_PIPELINE_LLM_MODELS`; kind env mapped for one release; run record stores the chosen entry | content-pipeline-kit (Y1 dev-only) | -- | `test_llm_backends.py`, `test_llm_platform.py`, `test_run_cli.py` |
| 8 | C3 `extra_launch_args --model` | a name from the same declaration (no in-repo caller today) | content-pipeline-kit | -- | `test_execution_driver_claude_bg.py` |
| 9 | O1 rows, O2 lanes, O3, O5, O6 preface and prose | drop `agent:`; `[astra, fable]`; fix the stale "never reads conserve_usage" preface and presence-autonomy :118 | claude-settings | -- | none (owner config) |
| 10 | R2 bakeoff `--model` | a name | repo script | -- | -- |
