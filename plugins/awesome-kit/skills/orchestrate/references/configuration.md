# Configuring orchestration policy

The `orchestrate` skill renders its variable policy from configuration. The shipped values
live in [../defaults/orchestration.yaml](../defaults/orchestration.yaml), which is a worked
example. User and project files are sparse overrides.

## Two halves

| Half | Keys | What it is |
|---|---|---|
| decision | `resolution`, `lexicon`, `shape`, `routing`, `agent_types`, `effort`, `announce` | per-unit policy and its ordered routing rows |
| machine | `backends`, `capacity` | detected tools, launch mechanics, and account-wide usage data |

The decision half renders as numbered blocks in this order: shape, routing, Agent type, effort,
and announcement. Resolution appears above those blocks. The machine half follows as
`## Dispatch backends` and `## Capacity`.

The decision half is hand-written configuration. `references/lexicon.md` is the consumer-facing
vocabulary reference; it is not a build input. The two policy halves have separate ownership:
routing rows state which model entry to try, while backend records state how a detected harness
is driven.

## Layers

Three layers are merged from lowest to highest precedence:

| Layer | Path | Use for |
|---|---|---|
| shipped | `<plugin>/skills/orchestrate/defaults/orchestration.yaml` | defaults bundled with the plugin |
| user | `~/.claude/plugins/data/plugins-kit/awesome-kit/orchestration.yaml` | this user's machine policy |
| project | `<project_root>/.claude/orchestration.yaml` | policy for one repository |

Mappings merge key by key. The record lists `lexicon`, `tests`, `gates`, `pulls`, `items`,
`notes`, `examples`, `backend_notes`, and `backends` merge by `id`; a known id is patched and
an unknown id is appended. A record with `disabled: true` is removed from rendered output.
The `routing` list is a plain list and therefore replaces the value from lower layers in full.
Other lists and scalar values replace outright. Project-layer executable fields are stripped;
the project file cannot select a program for the renderer to execute.

Use `--explain` to print layer provenance, detection results, model-discovery notes, routing
notes, and the resolved configuration. Use `--paths` to print the three layer paths.

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

## `routing[]`

Each row has `shape`, `models`, and optional `gate` and `guards` fields:

```yaml
routing:
- shape: [cross-check]
  models: [sol]
- shape: [novel, load-bearing]
  models: [agent:fable, sol]
  gate: write the justification before dispatch
  guards:
  - Keep the high-cost route for work that meets its bar.
- shape: [fan-out]
  models: [luna]
- shape: []
  models: [agent:sonnet]
```

Rows are evaluated in declaration order. The first matching shape wins. Models within one row
are tried in declaration order; a launch or transport error falls through to the next model.
The empty shape is the default row.

There are exactly two model namespaces:

- `agent:<name>` is reserved for the Agent tool's fixed menu: `fable`, `opus`, `sonnet`, and
  `haiku`.
- Every other name is unprefixed and resolves against the model entries exposed by
  `llm-scripting-kit`. The harness belongs to the resolved entry, not to the configuration
  name. A registry entry named `sol` is announced as `codex/sol` when its harness is Codex.

An unknown model, an unknown `agent:` member, or a model whose harness is unavailable is
skipped within its row. A row with no surviving models is skipped. If the shared library is
absent, all registry rows disappear and Agent-tool rows remain. A harness section appears only
when its CLI resolves through the command detector; model-server liveness is not used as a
presence test.

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

## `agent_types`, `effort`, and `announce`

`agent_types.items[]` contains `{id, name, text}` records rendered as role bullets.

`effort` may contain `title`, `intro`, `backend_notes[]`, `note`, `up_effort_note`,
`raise_when`, and `lower_when`. Backend notes render only when their backend is detected. A
plain string remains renderable as a compact effort block.

`announce` contains `title`, `form`, `rule`, and `examples[]`. The shipped form is:

```
delegating <what> to <target> (<the matched row's shape terms>)
```

The target is the Agent-tool model name for an `agent:` member and `<harness>/<entry-id>` for
a registry member. The empty shape uses `(default)`. A fallback appends
`; fell through from <id>` inside the parenthetical. Examples may set
`requires_backend: <id>` to disappear when that backend is absent.

## `backends[]`

Backend records describe detected tools. An available record renders its name, detection note,
capabilities, model entries for its harness, existing command text, dispatch prose, and
gotchas. A record that fails detection is omitted from the artifact; `--explain` reports its
reason. Request-only records can carry `selection`, which tells the reader to use that backend
only when the stated condition holds. The shipped Grok and model-endpoints records remain
request-only records.

Recognized capability keys, in display order, are:

`isolation`, `effort`, `network`, and `returns`.

Quote `yes` and `no` in YAML when a string is intended. `command` is existing machine-half
text. The routing configuration does not author or duplicate it.

Detection rules include:

```yaml
detect: {always: true}
detect: {command: [codex, --version]}
detect: {path: "~/bin/my-runner"}
detect: {model_endpoints: true, require_commands: [codex]}
```

Command detection is fail-closed and resolves bare commands through `PATHEXT` on Windows.
The `model_endpoints` rule and its probe machinery remain part of the machine half. It reads
`~/.claude/config/model-endpoints.yaml`, or the path in `MODEL_ENDPOINTS_REGISTRY`, and reports
the registry roster. `require_commands` must resolve before the registry can make the backend
available.

The optional `llm_scripting_kit` dependency is feature-detected. An importable stale or
version-skewed copy is insufficient: the renderer requires the model-discovery callable and
the harness entry-kind markers. A missing feature causes registry rows and their model section
to disappear while Agent-tool rows continue to work.

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

`tiers`, `default_tier`, `backend_selection`, `implementation`, `pool_economics`, `ladders`,
`rungs`, and `backend`.

Those values do not affect schema 3 routing. Port the decision to `routing`; keep vocabulary,
brief shaping, effort, announcement, backend, and capacity concerns in their corresponding
schema-3 sections. The warning names the keys and the override layers involved.
