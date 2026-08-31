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
