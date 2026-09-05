# Configuring skills-kit standards

skills-kit ships every opinion on by default. This reference is the
user-and-Claude-facing guide to tuning them: where the configuration lives, how
to disable an optional rule or tune a threshold, the full catalog of what is and
is not configurable, and how to add standards of your own. It covers
CONFIGURING the standards; authoring an additive standards file is covered by
[authoring-standards.md](authoring-standards.md), cross-referenced below.

Three tiers of opinion, one rule per row in the audit output:

- **Architectural** -- the structural spine (the YAML type contract, mixed-type
  and cross-block drift). Never configurable. Extensible only through the
  `audit-framework.yaml` registry, not through this configuration surface.
- **Optional** -- the tunable opinions (description hygiene, size signals,
  record floors, the per-type heuristic rows). Each carries a stable id you can
  disable, and some consume a tunable threshold.
- **Inoffensive** -- mechanical integrity checks (frontmatter and name presence
  and charset, reference reachability, asset-path resolution). No knob. The
  razor: disabling one could never make a correct document.

## Layer model

Configuration lives in version-controllable directories that mirror
`bootstrap.json` layering. Precedence runs lowest to highest; `config.yaml`
values deep-merge later-wins, and additive `*-standards.md` files union across
every layer (a later layer appends, never replaces).

| # | Location | Kind | Durability |
|---|----------|------|------------|
| 0 | shipped defaults (inside the plugin) | config + standards | the reference set; ships with the plugin |
| 1 | `<user_dir>/skills-kit/config.yaml` + `*-standards.md` | config + standards | version-controlled in the user's `~/.claude` repo -> fleet-wide |
| 2 | `<user_dir>/skills-kit/config.local.yaml` | config only | gitignored per-machine overlay |
| 3 | `<project>/.claude/skills-kit/config.yaml` + `*-standards.md` | config + standards | committed to the project -> team-wide |
| 4 | `<project>/.claude/skills-kit/config.local.yaml` | config only | gitignored personal overlay |

`<user_dir>` is `$CLAUDE_CONFIG_DIR` when set, else `~/.claude` (the harness
config directory).

Durability rationale, one line each:

- **User layer** (`<user_dir>/skills-kit/`) is version-controlled in the user's
  `~/.claude` repo, so its config and standards apply fleet-wide across every
  machine and project.
- **Project layer** (`<project>/.claude/skills-kit/`) is committed to the
  project, so its config and standards apply to everyone on the team.
- **`config.local.yaml`** at either layer is gitignored, so it carries
  per-machine or personal overrides that must not travel to other machines or
  teammates. It carries config only -- no standards files.

## `config.yaml` format

A layer's `config.yaml` (and its `config.local.yaml` overlay) carries three
optional top-level keys:

```yaml
rules:
  <rule-id>: off        # disable an optional rule
thresholds:
  <threshold-name>: <positive int>   # override a threshold default
adapters:
  <adapter-id>:
    <setting>: <value>  # opt an adapter in (see Adapters)
```

All three keys are optional. An absent file is skipped silently; a present file with
a malformed root, an un-tunable rule id, or an unknown threshold is a loud error
(see Troubleshooting).

### Worked example: disable an optional rule

To stop the audit from flagging descriptions that lack a "Do NOT use for..."
exclusion clause, disable `desc-exclusion-clause`:

```yaml
rules:
  desc-exclusion-clause: off
```

A rule value accepts only `off` (or `false`); the id must be one of the optional
rules below. Disabling an architectural or inoffensive rule is refused.

### Worked example: tune a threshold

To raise the SKILL.md body-size signal so a larger body does not prompt a
progressive-disclosure evaluation, raise `body_max_lines`:

```yaml
thresholds:
  body_max_lines: 750
```

A threshold override must be a positive integer and must name one of the
thresholds below. Tuning a threshold is independent of disabling a rule: a
threshold consumed by an architectural or inoffensive rule (for example
`mixed_min_score` or `name_max_chars`) is still tunable even though that rule
cannot be disabled.

## Rule-id catalog

Every rule id the audit emits (the `rule` field on a finding) belongs to exactly
one bucket. The tables below are the complete catalog.

<!-- BEGIN GENERATED: rule-catalog (gen_standards_doc.py; SSOT: rule_catalog.py + audit.py THRESHOLDS) -->

### Architectural -- never configurable

The structural contract. These rules have no config knob and are extended only
through `audit-framework.yaml`.

| Rule id | What it checks |
|---------|----------------|
| `yaml-contract` | The YAML type-contract block is recognized and validates against its schema (root key found, required keys present, rules satisfied). |
| `mixed-type` | A SKILL.md declares exactly one skill-type root -- no drift across two type contracts (consumes `mixed_min_score`). |
| `cross-block-drift` | Multiple YAML blocks in one document do not disagree about the document's type. |

### Optional -- disableable via `rules: {<id>: off}`

| Rule id | Meaning |
|---------|---------|
| `desc-160-char` | The description frontmatter field is at most `desc_max_chars` characters. |
| `desc-directive-form` | The description opens with "Use when..." or "Invoke when...". |
| `desc-exclusion-clause` | The description carries a "Do NOT use for..." exclusion clause. |
| `skill-type-tag` | A `skill-type` advisory tag is present in frontmatter (else the agent infers the type). |
| `skill-type-valid` | The `skill-type` value is one of the canonical skill types. |
| `refs-one-hop-deep` | `references/` is one hop deep (no nested references directories). |
| `body-line-count` | Reports the SKILL.md body line count (informational count row). |
| `body-token-count` | Reports the approximate SKILL.md body token count (informational count row). |
| `body-size-signal` | An over-threshold body with no `references/` directory raises a progressive-disclosure signal (consumes `body_max_lines`, `body_max_tokens`). |
| `step-tracking` | A technique-skill with more than three steps carries a tickbox checklist or a step-tracker invocation. |
| `facts-floor` | A reference-skill declares at least one fact (nested in `reference_skill:` or as a top-level `facts:` unit). |
| `facts-gotcha` | At least one fact carries a `gotchas` list. |
| `facts-example` | At least one fact carries an `example` block. |
| `caution-floor` | A technique-skill carries at least one per-technique gotcha or at least one `anti_patterns` record. |
| `claude-md-record-floor` | A CLAUDE.md declares at least one record across the `insights` / `conventions` union. |
| `ref-example-block` | A reference-skill body has an "Example" heading. |
| `ref-gotcha-block` | A reference-skill body has a "Gotcha" heading. |
| `ref-prohibited-discipline` | A reference-skill body omits discipline content (rule+counter, RED/GREEN/REFACTOR). |
| `ref-prohibited-checklist` | A reference-skill body omits a workflow tickbox checklist. |
| `pattern-recognition-block` | A pattern-skill body carries a recognition-criteria marker. |
| `pattern-counter-example` | A pattern-skill body carries a counter-example or "do NOT apply" marker. |
| `pattern-prohibited-bundle` | A pattern-skill ships no `scripts/` or `bin/` utility bundle. |
| `pattern-prohibited-checklist` | A pattern-skill body omits a workflow tickbox checklist. |
| `pattern-prohibited-rule-counter` | A pattern-skill body omits rule+counter (excuse-to-reality) pairs. |
| `technique-ordered-steps` | A technique-skill body has an ordered-step sequence. |
| `technique-prohibited-pressure-test` | A technique-skill body omits adversarial RED/GREEN/REFACTOR pressure testing. |
| `discipline-rule-counter` | A discipline-skill body carries at least one rule+counter pair. |
| `discipline-red-flags` | A discipline-skill body carries a "Red flags" list. |
| `discipline-pressure-test` | A discipline-skill applies adversarial pressure testing to its own rules. |
| `domain-identity-sentence` | A domain-skill body carries a single-sentence identity after the H1. |
| `domain-companion-declaration` | A domain-skill declares its companions (siblings, or an explicit "no sibling"). |
| `domain-orientation` | A domain-skill body carries orientation content (at least one H2 beyond the index). |
| `domain-reference-index` | A domain-skill body carries a Conditional-Loading reference index. |
| `domain-prohibited-index-only` | A domain-skill is not an index-only stub (an index with no orientation content). |

### Inoffensive -- no knob

Mechanical integrity checks. The razor: disabling one could never make a correct
document, so they carry no config knob.

| Rule id | What it checks |
|---------|----------------|
| `frontmatter-present` | A leading frontmatter block exists. |
| `name-present` | `frontmatter.name` is present. |
| `name-length` | `frontmatter.name` is at most `name_max_chars` characters. |
| `name-charset` | `frontmatter.name` uses the allowed charset. |
| `name-reserved` | `frontmatter.name` is not a reserved name. |
| `desc-present` | `frontmatter.description` is present. |
| `refs-cited-exist` | Every reference cited in the body resolves to a file. |
| `asset-paths-resolve` | Every declared asset-dependency and `tools[].tests` path resolves. |
| `refs-reachable` | Every file under `references/` is reachable from SKILL.md. |

## Thresholds

Five named thresholds carry the numeric limits some rules apply. Override any of
them in `thresholds:`; an override must be a positive integer.

| Threshold | Default | Consumed by |
|-----------|---------|-------------|
| `name_max_chars` | 64 | `name-length` |
| `desc_max_chars` | 160 | `desc-160-char` |
| `body_max_lines` | 500 | `body-size-signal` |
| `body_max_tokens` | 3000 | `body-size-signal` |
| `mixed_min_score` | 2 | `mixed-type` |

<!-- END GENERATED: rule-catalog -->

## Adapters

An adapter is task-specific prompt context that is admitted only for the
model-task pairs it was MEASURED on. Adapters are configured under `adapters:`,
keyed by adapter id.

| Adapter id | Setting | Default | What it does |
|------------|---------|---------|--------------|
| `md-audit-evidence-pack` | `admitted_endpoints` | empty list | Endpoint ids whose project-doc audit jobs get the pre-computed md-audit evidence pack attached to their prompt. |

```yaml
adapters:
  md-audit-evidence-pack:
    admitted_endpoints:
      - local-27b-endpoint      # a placeholder; use your own endpoint ids
```

Three things to know before you add an id:

- **The default is empty, and empty is safe.** With no ids admitted, no job ever
  gets the pack -- exactly the behaviour of not having the adapter. Endpoint ids
  differ per user and per fleet, so skills-kit ships none.
- **An id here is a CLAIM that the adapter was measured for that endpoint on
  markdown audit** -- not that the endpoint is small, local, or likely to
  benefit. Attaching the pack to a model that does not need it costs tokens for
  no gain, so do not widen the set to make a run attach a pack.
- **A mixed preference list is an error, not a guess.** An
  `endpoint_preference` list naming both admitted and non-admitted endpoints
  fails the emit (`emit_audit_jobs.py`, exit 4), because the endpoint is
  resolved at run time and either choice would be wrong. Emit one job file per
  endpoint class instead.

## Additive standards files

Beyond disabling and tuning skills-kit's own rules, a layer may ADD standards of
its own -- opinions skills-kit did not ship. Each is a markdown file carrying one
fenced `standards_set:` block, one file per file-type primitive, discoverable by
a filename convention:

| Filename | `applies_to` primitive |
|----------|------------------------|
| `SKILL-standards.md` | `skill_md` |
| `CLAUDE-md-standards.md` | `claude_md` |
| `reference-standards.md` | `reference_doc` |
| `doc-standards.md` | `plain_md` |

The block's `applies_to:` key is authoritative; the filename is the
discoverability convention. Standards files live in a layer directory (user or
project, not the gitignored `config.local.yaml` overlay) and union across every
layer. Each criterion carries a stable `id`, a `statement`, a `severity`, and a
keyword cluster. For the full block schema, the severity and enforcement
semantics, and a complete example, see
[authoring-standards.md](authoring-standards.md).

## How disables and standards surface in audit reports

Every audit finding quotes the id of the rule or criterion that produced it. A
finding against a skills-kit rule quotes its `rule` id (one of the catalog ids
above); a finding against an additive criterion quotes that criterion's `id`.
This lets a reader go straight from a finding to the config line that would
disable it: read the id, decide the opinion is not for this project, add
`rules: {<id>: off}` (for a skills-kit rule) or disable the criterion.

At the design level, the audit lanes consume the resolved configuration as
follows:

- A per-run resolver (`scripts/resolve_standards.py`) computes, for the file
  under audit, the disabled-rule set, the threshold overrides, and the additive
  standards that apply to that file's primitive.
- Additive criteria are enforced by the detect lane and reported under taxonomy
  `N_user_standard_violation`, modeled on the ancestor-CLAUDE.md convention
  mechanism: the finding emits the criterion's exact `statement` text verbatim
  together with the source path of the standards file that declared it. The
  agent does not infer a rule the standards file does not state and does not
  restate a criterion in its own words.
- Disabled optional rules are suppressed via a `disabledCriteria` set threaded
  into the detect lane, so a finding for a disabled id never reaches the report.

## Troubleshooting

Configuration errors are loud, never silent. The resolver raises rather than
degrading to an empty config, and the message names the problem:

- **Disabling an un-tunable rule.** `rules: {yaml-contract: off}` (architectural)
  or `rules: {name-length: off}` (inoffensive) raises an error naming the id and
  its bucket -- only optional rules are configurable.
- **An unknown rule id.** A typo'd or removed id in `rules:` raises an error
  naming the id (bucket `unknown`).
- **An unknown threshold.** A `thresholds:` key not among the five above raises
  an error listing the valid threshold names.
- **A bad value.** A rule value other than `off`/`false`, or a threshold value
  that is not a positive integer, raises an error naming the offending value.
- **An unknown adapter or adapter setting.** An `adapters:` id that is not in
  the table above, a setting that adapter does not take, or an
  `admitted_endpoints` value that is not a list of non-empty strings, raises an
  error naming the offending id or value. It is loud rather than ignored
  because a typo'd endpoint list admits nothing, which looks identical to the
  empty default.
- **Malformed config.** A `config.yaml` that is not valid YAML, or whose root is
  not a mapping, raises an error naming the path.
- **An invalid standards file.** A `*-standards.md` whose `standards_set:` block
  is missing or fails schema validation raises an error naming the path and the
  validation failure.
- **pyyaml unavailable.** Resolution degrades to defaults with a note (no config
  or standards applied) rather than crashing -- the same posture as the audit's
  contract-staged state.
- **Custom standards ignored by an unattended run's acceptance check.** The
  project-doc audit contract (`scripts/check_project_doc_audit.py`, described in
  [lanes/audit-lane.md](lanes/audit-lane.md)) parses the valid criterion and
  taxonomy ids from ONE standards document. It does not run the layer resolver,
  so it defaults to the shipped
  `references/standards/project-doc-standards.md`. When a layer supplies your
  own project-doc standards, pass that resolved path with `--standards` or the
  check validates findings against the shipped id set.

## Source of truth

The rule-id catalog and threshold table above are GENERATED (the marked
region): rule ids, buckets, and descriptions come from
`skills_kit_lib/rule_catalog.py` (`RULES`), threshold defaults from
`skills_kit_lib/audit.py` (`THRESHOLDS`). Edit those sources, then run
`scripts/gen_standards_doc.py`; never hand-edit the generated region.
`tests/skills-kit/test_standards_doc_drift.py` fails when the region is
stale. The resolver's reject-un-tunable-rule check reads the same module
directly, so the doc and the enforcement cannot disagree.
