# Configuring review profiles

`p4-code-review` selects a review profile -- reviewer roster, per-reviewer model, and
validator_models per reason -- from configuration resolved at review time, not from a table
baked into SKILL.md. The SKILL body's `review_profiles` block carries only the SELECTION
GUIDANCE and RATIONALE prose that helps pick a profile; the EXECUTABLE table lives in
bootstrap_lib's shipped defaults (reproduced below) and is resolved per review by
`bootstrap_lib.code_review.review_profiles`, invoked through this plugin's venv entry point:

    python3 ${CLAUDE_PLUGIN_ROOT}/scripts/render_review_profiles.py --project-root <project root>

## Layers

Three layers are merged from lowest to highest precedence:

| Layer | Path | Use for |
|---|---|---|
| shipped | `bootstrap_lib/code_review/defaults/review_profiles.yaml`, bundled with the bootstrap plugin's shared lib | the opinionated default reviewer/model table |
| user | `~/.claude/config/review_profiles.yaml` | this user's policy, across every project |
| project | `<project_root>/.claude/review_profiles.yaml` | policy for one repository |

`<project root>` is `bundle.project_root` from the step-2 prepare bundle. When it is unset
(a workspace prepare could not resolve one), omit `--project-root` -- the resolver then falls
back to the process working directory.

## Merge rules

- Top-level `profiles` is a list of records identified by `id`: a layer patching a known `id`
  is deep-merged into it; an unknown `id` is appended as a new profile.
- Within one profile record, `reviewers` is a list of records identified by `name`, merged the
  same way -- a higher layer only needs to restate the reviewer it is changing. A reviewer
  record's fields are `name`, `model`, `disabled`, and `peer_when_available`; any other key is
  a hard error rather than an ignored one.
- Every other mapping -- a profile's `selection`, and `validator_models` -- deep-merges key by
  key, so a higher layer states only the keys it changes.
- `validator_models` reason keys (`bug`, `claude_md`, ...) are extensible: a higher layer can
  add a new reason without restating the shipped ones.
- `disabled: true` on a profile or a reviewer record removes that record entirely from the
  resolved table, not just its fields.
- Every other list -- currently only `selection.data_only_extensions` -- is a PLAIN list, and a
  higher layer replaces it outright rather than merging entries.

Malformed or unreadable YAML in any layer is a hard error (`ConfigError`); resolution never
falls back to a partial or best-effort merge.

## Shipped defaults

```yaml
profiles:
- id: data_only
  selection:
    data_only_extensions:
    - .csv
    - .yaml
    - .yml
    - .json
    - .tsv
    - .md
  reviewers:
  - name: reviewer_a_claude_md_compliance
    model: sonnet
  - name: reviewer_b_diff_only_bugs
    model: sonnet
  validator_models:
    bug: sonnet
    claude_md: sonnet
- id: code
  selection: {}
  reviewers:
  - name: reviewer_a_claude_md_compliance
    model: sonnet
  - name: reviewer_b_diff_only_bugs
    model: opus
  - name: reviewer_c_introduced_code
    model: opus
    peer_when_available: true
  validator_models:
    bug: opus
    claude_md: sonnet
```

## What a `model` value may name

A `model` -- whether a reviewer's or a `validator_models` reason's -- is one of two things,
and which one it is decides how that lane is dispatched:

| Value | Dispatch |
|---|---|
| `sonnet`, `opus`, `haiku`, `fable` | an Agent subagent (the default) |
| anything else | an llm-scripting-kit endpoint id, run through `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/run_review_lane.py` |

Every `model` in the shipped table is an Agent alias, so a review with no user or project
override dispatches every lane as an Agent subagent -- unless the renderer substitutes a peer
seat for a lane that opted in (below). Naming an endpoint id is the whole override
mechanism -- there is no separate field to set, because `model` was already a free-form
string resolved through the three layers above.

An endpoint id is resolved by llm-scripting-kit (`create_backend`), so it may name an
OpenAI-compatible transport or a CLI harness -- whatever that plugin's configuration and
your `~/.claude/config/model-endpoints.yaml` declare. Endpoint ids are private to your
fleet; the example below uses a placeholder.

### Which lanes may take an endpoint id

The three REVIEWER lanes -- the set is `ENDPOINT_ELIGIBLE_LANES` in
`bootstrap_lib.code_review.lane_prompts`, which is the authority; this prose is not. The
runner refuses any other lane by name and exits 2 (a configuration error).

The validator is deliberately excluded. It is the control that suppresses a weak reviewer's
false positives, so replacing it in the same change as a reviewer would remove the instrument
the reviewer change has to be measured with.

Eligibility is not the only gate. `reviewer_a_claude_md_compliance` and
`reviewer_c_introduced_code` read files beyond their chunk, so they need an agent loop
(`LANES_REQUIRING_AGENT_LOOP`): the runner binds them only to a HARNESS endpoint -- one
declaring `harness:` rather than `base_url:` -- and refuses a plain-completion (`transport`)
endpoint rather than produce a reviewer that hallucinates context it cannot fetch.

### When an endpoint lane fails

It is reported as a failed lane and the review renders without it, with that lane's coverage
marked missing in a `## Lane failures` section. There is deliberately no fallback to an Agent:
a silent fallback would hand back a review you read as having run on the model you configured,
which is a false claim about what actually reviewed your change. Causes are the endpoint being
unreachable or halted, a chunk that does not fit its context window, or output that is not a
valid issue array after one repair attempt -- the stderr line says which.

### Worked endpoint override

To run the diff-only bug reviewer on a local endpoint for every project, add to
`~/.claude/config/review_profiles.yaml`:

```yaml
profiles:
- id: code
  reviewers:
  - name: reviewer_b_diff_only_bugs
    model: my-local-endpoint
```

`my-local-endpoint` is a placeholder: use an id your llm-scripting-kit configuration or
`~/.claude/config/model-endpoints.yaml` actually declares. Everything else about the review
is unchanged -- the other two reviewers and all validators stay on their Agent models
(except that `reviewer_c_introduced_code` still takes its shipped `peer_when_available`
substitution when llm-scripting-kit reports a reachable BESIDE seat, below), so the endpoint
reviewer's findings still pass through the same validation.

## `peer_when_available`: running a reviewer on a peer seat

A reviewer record may carry one more field:

```yaml
- name: reviewer_c_introduced_code
  model: opus
  peer_when_available: true
```

It is a boolean, it defaults to false, and it merges by name like every other reviewer field
-- a layer patching only `model` leaves a shipped `peer_when_available` in place, and setting
it to `false` opts back out.

Set, it asks the renderer to run that lane on a reachable PEER of the stated model: an
endpoint in the SAME tier but a DIFFERENT model family. The point is independence. A second
reviewer reading the same change on the same family largely agrees with the first, so the
lane that looks for problems the author introduced is the one worth moving off-family. The
shipped table sets it on `reviewer_c_introduced_code` in the `code` profile, and nowhere else.

### What actually happens

The renderer asks llm-scripting-kit (`llm_scripting_kit.seats.discover_seats`) for the seats
around the stated model, takes the first reachable `BESIDE` seat, and writes that seat's
endpoint id into the lane's `model` in the table it prints. Nothing downstream changes: the
value is an endpoint id, so the lane dispatches through `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/run_review_lane.py` under the ordinary
model-kind rule, and the agent-loop constraint above still applies -- a `BESIDE` seat is
always a harness endpoint, which is what this lane needs.

Every substitution is announced on STDERR, one line per lane, naming the profile, the lane,
the stated model, the endpoint that replaced it, and the mechanism:

    peer_when_available: profile 'code' lane 'reviewer_c_introduced_code' runs on
    llm-scripting-kit endpoint 'sol' instead of its stated model 'opus' -- a reachable BESIDE
    seat (same tier, different model family) reported by llm_scripting_kit.seats.discover_seats.

(Wrapped here for width; it is emitted as a single line.) `p4-code-review` carries that line
into the review header, so a review never claims to have run on a model it did not use.
When llm-scripting-kit is present but no reachable BESIDE seat is found, the renderer emits
one unconditional stderr line -- `peer_when_available: no reachable BESIDE seat was found,
so every opted-in lane runs on its stated model.` -- and the review carries it into the same
header, so a reader is told the opt-in lane ran on its stated model rather than left to
assume a substitution happened.

### When llm-scripting-kit is not there

This is an OPTIONAL edge. The lane simply runs on the model the table states, which is what
the table already said it would do, so the rendered table stays true as read and there is
NOTHING to disclose. Accordingly the renderer says nothing at all -- absent, too old, and
left over from an uninstall are all silent, and no review ever tells you to go install a
plugin you did not ask for.

Those states are still told apart, in a diagnostic channel rather than in the review:

    python3 ${CLAUDE_PLUGIN_ROOT}/scripts/render_review_profiles.py --project-root <project root> --explain-peer-seats

prints, on stderr, whether the plugin is absent (with the `claude plugin install` command) or
present but predating `llm_scripting_kit.seats.discover_seats`, which first shipped in
llm-scripting-kit 0.28.0 (with the `claude plugin update` command). A discovery call that
raises is reported there too. None of it changes the table.

The renderer never fails a review over this: no probe error, owner exception, or unexpected
result shape escapes it, and every one of them degrades to the stated model. The probe runs
fresh on each render and its result is never cached between reviews, so removing the plugin
takes effect on the very next review.

## Worked override example

To run the `code` profile's `reviewer_c_introduced_code` on Sonnet instead of Opus for one
project (cheaper, lower-fidelity), add to `<project_root>/.claude/review_profiles.yaml`:

```yaml
profiles:
- id: code
  reviewers:
  - name: reviewer_c_introduced_code
    model: sonnet
```

Only the changed reviewer needs restating -- `reviewer_a_claude_md_compliance` and
`reviewer_b_diff_only_bugs` keep their shipped models via the by-name merge, and `selection`
and `validator_models` are untouched because the patch omits them. `peer_when_available` is
inherited from the shipped record by the same merge, so this lane still takes a peer
substitution when one is reachable; add `peer_when_available: false` to the same record to
pin it to Sonnet.

## Inspecting the resolved table

    python3 ${CLAUDE_PLUGIN_ROOT}/scripts/render_review_profiles.py --project-root <project root>

prints the merged `profiles` table as YAML, then a `---` separator, then which layers were
applied and (for any absent override) the path that would create it. This is the same
resolution step 4 of `p4-code-review` performs -- never merge the layers by hand.

Add `--explain-peer-seats` to see why a `peer_when_available` lane kept its stated model. That
output is diagnostics, never part of the table.
