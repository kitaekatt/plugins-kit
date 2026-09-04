# Configuring review profiles

`git-code-review` selects a review profile -- reviewer roster, per-reviewer model, and
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
  record's fields are `name`, `model`, and `disabled`; any other key is a hard error rather
  than an ignored one.
- Every other mapping -- a profile's `selection`, and `validator_models` -- deep-merges key by
  key, so a higher layer states only the keys it changes.
- `validator_models` reason keys (`bug`, `claude_md`, ...) are extensible: a higher layer can
  add a new reason without restating the shipped ones.
- `disabled: true` on a profile or a reviewer record removes that record entirely from the
  resolved table, not just its fields.
- Every other list -- `selection.data_only_extensions`, and a reviewer's `model` when it is
  stated as a priority list -- is a PLAIN list, and a higher layer replaces it outright rather
  than merging entries. So a layer stating `model: fable` means fable, full stop: it replaces
  whatever list the layer below stated, entries and all.

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
    model:
    - peer:opus
    - opus
  validator_models:
    bug: opus
    claude_md: sonnet
```

## What a `model` value may name

A resolved `model` -- whether a reviewer's or a `validator_models` reason's -- is one of two
things, and which one it is decides how that lane is dispatched:

| Value | Dispatch |
|---|---|
| `sonnet`, `opus`, `haiku`, `fable` | an Agent subagent (the default) |
| anything else | an llm-scripting-kit endpoint id, run through `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/run_review_lane.py` |

Every model the shipped table can resolve to is an Agent alias, so a review with no user or
project override dispatches every lane as an Agent subagent -- unless a `peer:` entry resolves
for the one lane that states one (below). Naming an endpoint id is the whole override
mechanism -- there is no separate field to set, because `model` was already a free-form
value resolved through the three layers above.

A reviewer's `model` may also be an ORDERED PRIORITY LIST (`validator_models` values may not
-- a validator is never endpoint-eligible, so it has nothing to fall back to). The list is
described in its own section below; the renderer always prints ONE resolved string per lane,
so everything on this page about dispatching a `model` reads the resolved value.

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
(except that `reviewer_c_introduced_code` still resolves its shipped `peer:opus` entry when
llm-scripting-kit reports a reachable BESIDE seat, below), so the endpoint reviewer's findings
still pass through the same validation.

## Model priority lists: running a reviewer on a peer seat

A reviewer's `model` may be an ordered list instead of a single name:

```yaml
- name: reviewer_c_introduced_code
  model:
  - peer:opus
  - opus
```

The entries are evaluated IN ORDER and the first one that RESOLVES becomes that lane's model.
There are two kinds of entry:

| Entry | Resolves to | When |
|---|---|---|
| `<name>` | itself -- an Agent alias or an endpoint id | always |
| `peer:<name>` | a reachable PEER endpoint of `<name>` | only when llm-scripting-kit is installed, current, and reports one |

A PEER is a seat in the SAME tier as `<name>` but a DIFFERENT model family. The point is
independence. A second reviewer reading the same change on the same family largely agrees with
the first, so the lane that looks for problems the author introduced is the one worth moving
off-family. The shipped table states `[peer:opus, opus]` for `reviewer_c_introduced_code` in
the `code` profile, and a plain string everywhere else.

A single string is exactly a one-entry list, so `model: sonnet` means what it always meant.
`model: peer:opus` is legal too -- it simply has nothing to fall back to, so it is a
configuration error whenever no seat is reachable (below).

The list is a PLAIN list under the merge rules above, which is the whole reason it is shaped
this way: a higher layer's `model` REPLACES it wholesale. `model: fable` in your user layer
means fable, with no peer probe and no leftover preference inherited from the shipped record.

### What actually happens

For a `peer:<name>` entry the renderer asks llm-scripting-kit
(`llm_scripting_kit.seats.discover_seats`) for the seats around `<name>`, takes the first
reachable `BESIDE` seat, and writes that seat's endpoint id into the lane's `model` in the
table it prints. Nothing downstream changes: the value is an endpoint id, so the lane
dispatches through `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/run_review_lane.py` under the ordinary model-kind rule, and the agent-loop
constraint above still applies -- a `BESIDE` seat is always a harness endpoint, which is what
this lane needs.

A resolved `peer:` entry is announced on STDERR, one line per lane, naming the profile, the
lane, the entry, the endpoint that resolved it, and the mechanism:

    model-priority: profile 'code' lane 'reviewer_c_introduced_code' runs on llm-scripting-kit
    endpoint 'sol' -- priority entry 'peer:opus' resolved to a reachable BESIDE seat (same
    tier, different model family) reported by llm_scripting_kit.seats.discover_seats.

(Wrapped here for width; it is emitted as a single line.) `git-code-review` carries that line
into the review header, so a review never claims to have run on a model it did not use.

When llm-scripting-kit IS present and the probe ran but no reachable `BESIDE` seat exists, the
skip is announced the same way, naming the entry that was skipped and the entry that ran
instead:

    model-priority: profile 'code' lane 'reviewer_c_introduced_code' skipped priority entry
    'peer:opus' (no reachable BESIDE seat) and runs on 'opus'.

so a reader is told the lane took a later entry rather than left to assume the first one ran.

### When no entry resolves

A list of `peer:` entries with no plain name after them is a configuration error when nothing
is reachable: the renderer exits non-zero naming the profile, the lane, and the list, and
prints no table. Put a plain name last -- it always resolves -- unless you genuinely want the
review to stop rather than run off-peer.

### When llm-scripting-kit is not there

This is an OPTIONAL edge. A `peer:` entry does not resolve, the next entry does, and the
rendered table states the model that will run -- so it stays true as read and there is NOTHING
to disclose. Accordingly the renderer says nothing at all -- absent, too old, and left over
from an uninstall are all silent, and no review ever tells you to go install a plugin you did
not ask for.

Those states are still told apart, in a diagnostic channel rather than in the review:

    python3 ${CLAUDE_PLUGIN_ROOT}/scripts/render_review_profiles.py --project-root <project root> --explain-peer-seats

prints, on stderr, whether the plugin is absent (with the `claude plugin install` command) or
present but predating `llm_scripting_kit.seats.discover_seats`, which first shipped in
llm-scripting-kit 0.28.0 (with the `claude plugin update` command). A discovery call that
raises is reported there too. None of it changes the table.

The renderer never fails a review over a probe: no probe error, owner exception, or unexpected
result shape escapes it, and every one of them falls through to the next entry. The probe runs
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
and `validator_models` are untouched because the patch omits them. The scalar `model` REPLACES
the shipped `[peer:opus, opus]` list outright, so this lane is pinned to Sonnet and no peer is
probed for. To keep the peer preference on a different tier, state the list you want instead:
`model: [peer:sonnet, sonnet]`.

## Inspecting the resolved table

    python3 ${CLAUDE_PLUGIN_ROOT}/scripts/render_review_profiles.py --project-root <project root>

prints the merged `profiles` table as YAML, then a `---` separator, then which layers were
applied and (for any absent override) the path that would create it. This is the same
resolution step 4 of `git-code-review` performs -- never merge the layers by hand.

Add `--explain-peer-seats` to see why a `peer:` entry did not resolve. That output is
diagnostics, never part of the table.
