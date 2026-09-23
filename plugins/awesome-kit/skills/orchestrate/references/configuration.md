# Configuring orchestration policy

The `orchestrate` skill renders its variable policy from configuration. The shipped values
live in [../defaults/orchestration.yaml](../defaults/orchestration.yaml), which is a worked
example. Machine, user, and project files are sparse overrides.

## Two halves

| Half | Keys | What it is |
|---|---|---|
| decision | `resolution`, `lexicon`, `shape`, `routing`, `agent_types`, `effort`, `announce`, `review_overlap` | per-unit policy and its ordered routing rows |
| machine | `backends`, `capacity` | detected tools, launch mechanics, and account-wide usage data |

The decision half renders as numbered blocks in this order: shape, routing, Agent type, effort,
and announcement. Resolution appears above those blocks. The machine half follows as
`## Dispatch backends` and `## Capacity`.

The decision half is hand-written configuration. `references/lexicon.md` is the consumer-facing
vocabulary reference; it is not a build input. The two policy halves have separate ownership:
routing rows state which model entry to try, while backend records state how a detected harness
is driven.

## Layers

Four layers are merged from lowest to highest precedence:

| Layer | Path | Use for |
|---|---|---|
| shipped | `<plugin>/skills/orchestrate/defaults/orchestration.yaml` | defaults bundled with the plugin |
| machine | `~/.claude/plugins/data/plugins-kit/awesome-kit/orchestration.yaml` | machine-global values, such as observed resource limits |
| user | `~/.claude/config/orchestration.yaml` | this user's policy, across every project |
| project | `<project_root>/.claude/orchestration.yaml` | policy for one repository |

Mappings merge key by key. The record lists `lexicon`, `tests`, `gates`, `pulls`, `items`,
`notes`, `examples`, `backend_notes`, and `backends` merge by `id`; a known id is patched and
an unknown id is appended. A record with `disabled: true` is removed from rendered output.
The `routing` list is a plain list and therefore replaces the value from lower layers in full.
Other lists and scalar values replace outright. Project-layer executable fields are stripped;
the project file cannot select a program for the renderer to execute.

The user layer lives in `~/.claude/config/`, the conventional home for portable user
configuration, so it travels with a config repo. The machine layer lives in the plugin data
directory, which is machine-local by charter. Bootstrap's `manifest-reference.md` describes
the split between the two locations. Use the machine layer for machine-local observations and
machine-specific policy. User and project layers override it when they define the same key. Keep
portable decision-half policy in the user layer; `--explain` notes when the machine file contains
decision-half keys.

Use `--explain` to print layer provenance, detection results, model-discovery notes, routing
notes, and the resolved configuration. Use `--paths` to print the four layer paths.

## Top-level keys

| Key | Type | Meaning |
|---|---|---|
| `schema_version` | int | Schema generation. Shipped configuration uses version `3`. |
| `resolution` | str | Resolution semantics, rendered above the numbered blocks. |
| `lexicon` | list | Controlled vocabulary and glosses. |
| `shape` | map | Brief-shaping tests and per-machine hole disclosures. |
| `routing` | list | Ordered shape-to-model rows. |
| `agent_types` | map | Agent-tool role guidance. |
| `effort` | map or str | Effort guidance, independent of row matching. |
| `announce` | map | Dispatch announcement form, rule, and examples. |
| `backends` | list | Machine records for detected dispatch tools. |
| `capacity` | map | Account-wide usage reporting. |
| `review_overlap` | map | Review overlap posture; `mode` defaults to `premise-safe`. |

## `routing[]`

Each row has `shape`, `models`, and optional `gate` and `guards` fields:

```yaml
routing:
- shape: [cross-check]
  models: [sol]
- shape: [novel, load-bearing]
  models: [fable, sol]
  gate: write the justification before dispatch
  guards:
  - Keep the high-cost route for work that meets its bar.
- shape: [fan-out]
  models: [luna, sonnet]
- shape: []
  models: [sonnet]
```

Rows are evaluated in declaration order. The first matching shape wins. The empty shape is the
default row.

A row's `models` is a model declaration: a list of model ids in preference order, in the one
format every plugin in this marketplace uses (bootstrap's plugin-dev skill,
`references/model-declaration.md`). The core ids `fable`, `opus`, `sonnet`, and `haiku` are the
Claude harness's own, driven by the Agent tool. Every other id resolves against the model
entries exposed by `llm-scripting-kit`. The harness belongs to the resolved entry, not to the
id: a registry entry named `sol` whose harness is Codex is announced as `codex/sol`. A
deprecated `agent:<id>` entry is still read as `<id>`, and `--explain` notes each one.

### How a row's menu is rendered

With `llm-scripting-kit` 0.46.0 or later, each row's menu is that library's `describe` output
for an in-session caller:

- An id that resolves to nothing, or that this policy cannot drive, is left out of the render
  without comment. A registry id is drivable only when its harness has an active
  `backends[]` record that yields an adapter-rendered command, a record `command`, or
  dispatch prose; CLI presence proves the tool exists, not that the policy can drive it.
- An entry whose subscription pool is spent (llm-scripting-kit `conserve_usage`) stays listed
  as out of quota until its reset time and is not usable until then. An entry whose harness
  CLI is not detected stays listed as unreachable.
- Entries with a pace reading (quota remaining divided by window remaining) are re-sorted,
  highest pace first; entries without one keep their declared places. The first usable entry
  is marked `[default]`, and an entry matching `--self` is marked `[author]`.
- The `Rule:`, `Re-select:`, and `Independence:` lines under the rows are printed by
  llm-scripting-kit; they say how to choose, announce, and re-select.

Quota verdicts are evaluated once per session and pinned; only an observed quota halt moves
one down.

A row with no usable entry renders the floor instead of a menu: it names every declared id
and why it cannot run here. The row is kept rather than deleted, because deleting it would
send the unit to a row chosen for a different shape. A unit that matches it stops, and the
floor goes to the user.

When `llm-scripting-kit` is absent or older than 0.46.0, each row lists only its core ids, in
declared order, without pace ordering or rule lines, and the rendered policy says so under
**Degraded render**. Setting `ORCHESTRATE_QUOTA_ROUTING=0` turns quota reads off deliberately
and emits no note: no entry shows a pace or an out-of-quota status. Use it when a policy
render must not vary with the machine's live balance.

### Launch corrections and re-selection

Correct a local invocation error and relaunch the SAME entry only with positive evidence that
no worker started and no provider request was sent: a shell quoting error, a wrong flag, or a
path the launcher rejected before dispatch. Keep the same entry, effort, and brief. A non-zero
exit alone is not that evidence. Every other dispatch failure -- a non-zero exit, an Agent-tool
error, a launch that produced no output -- is handled by the `Re-select:` line the rendered
policy prints: choose another usable entry of the same row and announce the prior entry with
its failure kind. A unit that ran to completion and returned a wrong or schema-invalid result
is a task failure, not a re-selection trigger.

Do not add a `command` field to routing. The machine record's existing `command` text is read
through the renderer's command-text provider, so harness-specific command construction has one
replacement seam.

## `lexicon[]`

The lexicon is the vocabulary for shape rows and announcements. A term with `kind: skill`
selects a route and renders; a `kind: concept` term explains a choice but does not select a
route. `render: glossed` adds the `gloss` at first occurrence; `render: bare` uses the term as
written. Every glossed term must have a gloss, and bare terms must not.

Prose fields may reference a term as `{term-id}`. The renderer expands it and applies the
first-occurrence gloss. The shape terms in a routing row must resolve to live skill terms.

## `shape`

`shape.title` and `shape.intro` control the block heading and lead. `shape.tests[]` contains
`{id, text}` records. `text` may use vocabulary references. A test with
`render_scope: principles-only` stays in the resolved configuration and `--explain` output but
does not consume space in the per-unit artifact. `without_backend` maps a backend id to a
clause shown only when that backend is absent; use it to disclose a real routing hole.

`parallel-development-razor` is the shipped parallelism knob. Override that test record by id
to tune when implementation is split; do not copy the whole `shape.tests` list. The shipped
`[parallel-leaf, known, rule-applying]` row routes each admitted leaf independently. Higher
priority rows keep `unverifiable` or `mutating` leaves on a stronger worker. To use a local
implementation worker, replace the complete `routing` list and put its discovered
llm-scripting-kit model-entry id first in that row; keep a second model in the row for machines
where the entry or harness is absent.

## `review_overlap`

`review_overlap.mode` controls whether implementation units may overlap an in-flight review.
The default is `premise-safe`, which preserves the premise-based overlap behavior. Set it to
`strict` when the workflow needs a review gate before implementation units. Both modes still
permit investigation units when their brief meets the selected rule in the rendered policy.

## `agent_types`, `effort`, and `announce`

`agent_types.items[]` contains `{id, name, text}` records rendered as role bullets.

`effort` may contain `title`, `intro`, `backend_notes[]`, `note`, `up_effort_note`,
`raise_when`, and `lower_when`. Backend notes render only when their backend is detected. A
plain string remains renderable as a compact effort block.

`announce` contains `title`, `form`, `rule`, and `examples[]`. The shipped form is:

```
delegating <what> to <target> (<the matched row's shape terms>)
```

The target is the id for a core id and `<harness>/<entry-id>` for a registry entry. The empty
shape uses `(default)`. A choice from a multi-entry row, and every re-selection, is also
announced with the `route:` line the rendered rule gives. Examples may set
`requires_backend: <id>` to disappear when that backend is absent, and `requires_model: <id>`
(or a list) to disappear unless that entry is usable in some row.

## `backends[]`

Backend records describe detected tools. An available record renders its name, detection note,
capabilities, model entries for its harness, existing command text, dispatch prose, and
gotchas. A record that fails detection is omitted from the artifact; `--explain` reports its
reason. A `backends[]` record is a routing target only when it yields drivable mechanics: an
adapter-rendered command, a record `command`, or dispatch prose. Registry models resolve in
routing rows only when their active harness record meets that condition (see `routing[]`
above). Request-only records can carry `selection`, which tells the reader to use that backend
only when the stated condition holds. A request-only record is not a routing target; it is
documented for an explicitly named backend.

Recognized capability keys, in display order, are:

`isolation`, `effort`, `network`, `concurrency`, and `returns`.

Quote `yes` and `no` in YAML when a string is intended. `command` is existing machine-half
text. The routing configuration does not author or duplicate it.

Detection rules include:

```yaml
detect: {always: true}
detect: {command: [codex, --version]}
detect: {path: "~/bin/my-runner"}
```

Command detection is fail-closed and resolves bare commands through `PATHEXT` on Windows.
A `detect` mapping must name `always`, `command`, or `path`; an unrecognized mapping fails
closed and omits the backend.

The optional `llm_scripting_kit` dependency is feature-detected. An importable stale or
version-skewed copy is insufficient: the renderer requires the model-discovery callable and
the harness entry-kind markers. A missing feature causes registry rows and their model section
to disappear while Agent-tool rows continue to work.

Consult seats use a separate degradation ladder: no `llm-scripting-kit` -> no section;
`llm-scripting-kit` without `discover_seats` (< 0.28.0) -> no section; entries without tier
or family are listed as `unclassified`, never `BESIDE`; nothing reachable ->
`none reachable -- decide and say so`. The section makes no claim when the optional library
is absent, so its absence is not a degraded answer; `--explain` reports why it was skipped.
The backends list claims to be complete, so a missing registry is disclosed, while an absent
seats section claims nothing.

## `capacity`

| Field | Meaning |
|---|---|
| `source` | `auto`, `none`, or `command`. |
| `snapshot_path` | File read for `auto`; `~` is expanded. |
| `command` | Argv used for `command`; project-layer values are stripped. |
| `max_age_minutes` | Age limit for a snapshot before it is marked indicative. |
| `thresholds.warn_remaining_pct` | Remaining percentage at which a window is `low`. |
| `thresholds.critical_remaining_pct` | Remaining percentage at which a window is `CRITICAL`. |

Claude Code exposes account-wide windows, not per-model usage. The renderer reports usable
windows when a snapshot is available and says `capacity unknown` when it is not. The former
`capacity.tier_overrides` field has no routing consumer and is retired; configuration carrying
it is ignored and should be removed.

## Legacy override warning

The renderer warns with **Stale override -- NOT IN FORCE** when the merged configuration
contains one of these retired top-level keys:

`tiers`, `default_tier`, `default_backend`, `backend_selection`, `implementation`,
`pool_economics`, `ladders`, `rungs`, and `backend`.

Those values do not affect schema 3 routing. Port the decision to `routing`; keep vocabulary,
brief shaping, effort, announcement, backend, and capacity concerns in their corresponding
schema-3 sections. The warning names the keys and the override layers involved.
