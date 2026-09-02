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
  same way -- a higher layer only needs to restate the reviewer it is changing.
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

The shipped table is entirely Agent aliases, so a review with no user or project override
dispatches every lane as an Agent subagent. This is the whole override
mechanism -- there is no separate field to set, because `model` was already a free-form
string resolved through the three layers above.

An endpoint id is resolved by llm-scripting-kit (`create_backend`), so it may name an
OpenAI-compatible transport or a CLI harness -- whatever that plugin's configuration and
your `~/.claude/config/model-endpoints.yaml` declare. Endpoint ids are private to your
fleet; the example below uses a placeholder.

### Which lanes may take an endpoint id

Only `reviewer_b_diff_only_bugs` -- the set is `ENDPOINT_ELIGIBLE_LANES` in
`bootstrap_lib.code_review.lane_prompts`. The runner refuses any other lane by name and exits
2 (a configuration error), for two different reasons:

- `reviewer_a_claude_md_compliance` and the validator are not qualified on a non-Claude
  model. The validator especially: it is the control that suppresses a weak reviewer's
  false positives, so replacing it in the same change as a reviewer would remove the
  instrument the reviewer change has to be measured with.
- `reviewer_c_introduced_code` reads files beyond its chunk, so it needs an agent loop. Should
  it become eligible, the runner refuses to bind it to a plain-completion (`transport`)
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
is unchanged -- the other two reviewers and all validators stay on their Agent models, so
the endpoint reviewer's findings still pass through the same validation.

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
and `validator_models` are untouched because the patch omits them.

## Inspecting the resolved table

    python3 ${CLAUDE_PLUGIN_ROOT}/scripts/render_review_profiles.py --project-root <project root>

prints the merged `profiles` table as YAML, then a `---` separator, then which layers were
applied and (for any absent override) the path that would create it. This is the same
resolution step 4 of `p4-code-review` performs -- never merge the layers by hand.
