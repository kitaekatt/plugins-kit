# Configuring orchestration policy

The `orchestrate` skill renders its variable half from configuration. The shipped values live
in [../defaults/orchestration.yaml](../defaults/orchestration.yaml), which is also commented
and is the best worked example.

Audience: a user tuning policy for their machine, or a developer changing the schema.

## Two halves

| Half | Keys | What it is |
|---|---|---|
| decision | `resolution`, `lexicon`, `shape`, `backend`, `ladders`, `agent_types`, `effort`, `announce` | a DECISION TREE, derived from the orchestration principles and stated in the controlled vocabulary of `lexicon` |
| machine | `backends`, `capacity` | what this machine has and how to drive it -- not derived from anything |

The decision half renders as numbered blocks in principle order -- shape, backend, tier, agent
type, effort, announcement -- resolved by **ordered elimination: first match wins**. The
machine half renders after it as `## Dispatch backends` and `## Capacity`.

**Authorship of the decision half is one-way.** Change a principle, then re-derive the data.
Never edit a rendered tree and back-fill a principle to match it; that inverts the audit trail
and produces criteria that exist only because a phrasing survived.

## Layers

Three layers, later winning:

| Layer | Path | Use for |
|---|---|---|
| shipped | `<plugin>/skills/orchestrate/defaults/orchestration.yaml` | the defaults; replaced on every plugin update -- never edit |
| user | `~/.claude/plugins/data/plugins-kit/awesome-kit/orchestration.yaml` | this machine's policy |
| project | `<project_root>/.claude/orchestration.yaml` | one repo's policy |

Override files are **sparse**: write only the keys you are changing. Everything else keeps
tracking the shipped defaults, including keys added by future plugin versions. (This is why
the defaults are read from the plugin rather than copied into the data dir on first run -- a
copy would freeze your policy at the version that made it.)

Merge rules:

- mappings deep-merge, key by key
- record lists merge **by record `id`** -- a record with a known id patches that record
  field-by-field, a record with a new id is appended. The keys treated as record lists are
  `lexicon`, `ladders`, `rungs`, `tests`, `gates`, `pulls`, `items`, `notes`, `examples`,
  `backend_notes`, `backends` (and legacy `tiers`), at any nesting depth.
- a list under one of those keys whose members carry no `id` replaces outright, which is what
  keeps plain lists such as `capabilities.tiers` behaving as scalars
- `disabled: true` on any record removes it
- everything else replaces outright

So patching one rung is a three-line override:

```yaml
ladders:
  - id: agent
    rungs:
      - id: workhorse
        model: my-preferred-model
```

Inspect the result with `--explain`, which prints each layer's path and status, the detection
status of every backend, the visibility of every rung, and the fully resolved config;
`--paths` prints just the three paths.

## Top-level keys

| Key | Type | Meaning |
|---|---|---|
| `schema_version` | int | Schema generation. Version `2`. See "Migrating from schema 1" below. |
| `default_backend` | str | Backend id used when a unit does not call for a specific one. |
| `resolution` | str | The resolution semantics, rendered at the TOP of the artifact. |
| `lexicon` | list | The controlled vocabulary. See below. |
| `shape` | map | Block 0 -- shaping the unit. |
| `backend` | map | Block 1 -- where the work runs. |
| `ladders` | list | Block 2 -- one tier ladder per backend. |
| `agent_types` | map | Block 3 -- which dispatch, Claude-side. |
| `effort` | map | Block 4 -- effort, orthogonal to tier. |
| `announce` | map | Block 5 -- the dispatch announcement form. |
| `backends` | list | Machine data: where delegated units run. |
| `capacity` | map | Machine data: usage-capacity reporting. |

Two rendering flags apply to **any** record in the decision half:

| Flag | Effect |
|---|---|
| `disabled: true` | the record is removed from the merged config entirely |
| `render_scope: principles-only` | the record stays in the config and is available to `--explain`, but does NOT render. Use it for genuine policy that is not a per-unit routing decision -- procedure the orchestrator applies once does not earn tokens in a file read once per orchestration. |

## `lexicon[]`

The vocabulary every criterion is stated in, and the only vocabulary permitted in dispatch
announcements. A term earns its place by having a test answerable at dispatch time.

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | The term itself, e.g. `known`, `fan-out`. Merge key, and what renders. |
| `kind` | | `skill` (default) selects a branch and renders; `concept` justifies a choice already made and NEVER renders. |
| `render` | | `bare` (default) or `glossed`. |
| `test` | | The dispatch-time test. Audit trail; not rendered. |
| `gloss` | | The rendered compression of the test. Required for `glossed`, and must be absent for `bare`. |

**Glossing is first-occurrence.** A `glossed` term renders as ``` `term` (gloss) ``` the first
time it appears in document order and bare thereafter. That says it once and costs less than a
glossary block, which would pay for the term name twice. Two consequences:

- blocks must render in document order, because glossing is stateful
- a term used in both variants may not have its only gloss inside a block one variant omits
  (see [Variants](#variants)); move the first use into a block both variants render

Choose `bare` only when the term's NATURAL reading already matches its test -- the lexicon is
never loaded into an orchestration context and every orchestration is a fresh read, so there is
no accumulated vocabulary and no surrounding prose to correct a misreading.

Prose fields anywhere in the decision half may reference a term as `{term-id}`; the reference
expands to the term, glossing it if this is its first occurrence:

```yaml
text: "{known} takes a SPECIFICATION brief; {open} takes a QUESTION brief."
```

## `shape`

| Field | Meaning |
|---|---|
| `title` | Block heading (the number is assigned at render time). |
| `intro` | One line under the heading. |
| `tests[]` | The shaping tests, rendered as bullets in list order. |

Each `tests[]` record:

| Field | Meaning |
|---|---|
| `id` | Merge key. |
| `principle` | Which principle it derives from. Audit trail; not rendered. |
| `text` | The rendered bullet. May reference terms. |
| `guard` | Marks the record as a negative guard. Documentation only -- guards render whether or not they are flagged. |
| `without_backend` | Map of backend id to a clause appended ONLY when that backend is absent. |

`without_backend` is how a known hole gets disclosed rather than papered over: with no Codex
backend a genuine `fan-out` has no route, and saying so in one clause costs a few tokens and
invents nothing, where silence reads as an oversight and invites the reader to invent an
answer.

## `backend`

The where-does-this-run block. Omitted entirely when there is nothing to choose.

| Field | Meaning |
|---|---|
| `title` | Block heading. |
| `requires_backend` | The block renders only when this backend id is detected. |
| `intro` | One line under the heading. |
| `default` | Backend id rendered as the default. Dropped if that backend is absent. |
| `gates_intro` / `pulls_intro` | Sentence prefixes; the backend name and the term list are appended to them, so they end mid-sentence by design (`... Any one resolves to`). |
| `gates[]` | Disqualifiers, rendered first. |
| `pulls[]` | Preferences, rendered second. Any one firing is enough. |

Each `gates[]` / `pulls[]` record is `{id, term, backend}` -- a term id and the backend it
resolves to. Rows naming an undetected backend are dropped; rows are grouped by backend, one
rendered line per group.

## `ladders[]`

One ladder per backend, keyed by the **backend id**. Rungs are tested in list order and the
first match wins, so insert a new rung where it belongs rather than appending it.

| Field | Meaning |
|---|---|
| `id` | The backend id this ladder belongs to. The whole ladder disappears when that backend is not detected. |
| `label` | Display name, used in the `### <label> ladder` heading. The heading is emitted only when more than one ladder renders. |
| `rungs[]` | The rungs, in test order. |
| `guards` | Plain strings, rendered under the ladder. Negative guards -- a rung that does not exist, a debiasing rule -- belong here. |
| `notes[]` | `{id, text}` records rendered with the guards; tag one `render_scope: principles-only` to keep it out of the artifact. |

### `rungs[]`

| Field | Meaning |
|---|---|
| `id` | Merge key. Also the key used by `capacity.tier_overrides` and `backends[].capabilities.tiers`. |
| `model` | The model, rendered in bold. This is what a user retargets. |
| `effort` | Rendered as ``at `<effort>` effort``. Set it only where effort is actually dialable on that backend. |
| `criteria` | The rung's test. See below. |
| `shape` | A term id restricting the rung to one brief shape, rendered as ``` `open` work only ```. |
| `terminal` | `true` for the last rung, the one reached only by fall-through. A terminal rung is the only one allowed to state no criteria. |
| `text` | Extra prose after the criteria, e.g. what a terminal rung is. |
| `announce_as` | List of term-name lists, rendered literally as ``` Announced as `(known, default)` or `(open, condensation)` ```. |
| `gate` | A procedure that must be satisfied before the rung is taken, rendered as a `Gate:` sub-bullet. |
| `notes[]` | `{id, text}` sub-bullets. |
| `guards` | Plain-string sub-bullets. What this rung must NOT be used for. |

`criteria` is a list of **OR'd groups**; the terms within a group are AND'd. A group is either
a list of term ids or a mapping carrying a qualifying clause:

```yaml
criteria:
  - [cross-check]                       # `cross-check`
  - terms: [novel, unverifiable]        # ; or `novel` + `unverifiable`
    where: up-effort would not resolve it   #   where up-effort would not resolve it
```

**Unresolvable ids fail CLOSED, at group granularity.** A group is a conjunction, so an id that
names a `concept` term, a disabled term, or no term at all invalidates **the whole group** --
never just itself. Dropping the one conjunct would render a strictly WIDER test than the data
specifies, which on this ladder silently widens the gate on the most expensive rung: the exact
direction every guard in the policy exists to prevent. A rung with one surviving group still
renders that group; a **non-terminal** rung left with no group at all raises `UnrenderableRung`
rather than rendering an empty test, because an empty test under first-match-wins reads as an
unconditional match rather than a missing one.

So a typo in a term id inside `criteria` removes a whole alternative, loudly if it removes the
last one. (A `{term-id}` reference in *prose* degrades to the bare id instead -- prose carries no
test, so a typo there shows up in the output rather than changing a decision.)

**Negative guards always render.** A rung something must not be used for, or a rung that does
not exist, is a decision rather than rationale -- without it a reader who knows the model exists
invents the dispatch. There is no flag that suppresses them and none that is required to emit
them.

## `agent_types`

| Field | Meaning |
|---|---|
| `title` / `intro` | Heading and lead line. |
| `items[]` | `{id, name, text}`, rendered as `` `name` -- text ``. |

## `effort`

| Field | Meaning |
|---|---|
| `title` / `intro` | Heading and lead line, e.g. the effort scale. |
| `backend_notes[]` | `{id, backend, text}` -- rendered only when that backend is detected. |
| `note` | The backend-independent statement. |
| `up_effort_note` | Rendered after `note`. |
| `raise_when` / `lower_when` | Plain string lists, rendered as one `- Raise:` / `- Lower:` line each, semicolon-joined. |

A plain string in place of the whole map still renders, so an override written against the
older prose schema does not silently vanish.

## `announce`

| Field | Meaning |
|---|---|
| `title` | Heading. |
| `form` | The one-line form, rendered in a code block. |
| `rule` | The terms-only rule. |
| `backend_notes[]` | `{id, backend, text}` -- rendered only when that backend is detected. |
| `examples[]` | `{id, text}`, rendered in one code block. Add `requires_backend: <id>` to drop an example with its backend. |

This is the one place a worked example renders, because here the form IS the content.

## Variants

The renderer produces two variants from one source, decided by detection:

- **all backends present** -- everything renders
- **a backend absent** -- its ladder, its `backend`-block rows, its `backend_notes` and its
  `requires_backend` examples all drop; the `backend` block itself drops when its
  `requires_backend` is the missing one; block numbering closes the gap; and any
  `without_backend` clause is added

Never author the variants separately. Anything that depends on an absent backend must say so
in its own record.

**Nothing that is not installed is ever named.** A rung that cannot be dispatched to is worse
than absent, because naming it invites an attempt. Cross-references between rungs should
therefore avoid naming a gated rung -- write "a rung below this one" rather than the id, or the
reference outlives the rung it points at.

## `backends[]`

Machine data. Which backends exist here and how to drive them; not derived from the principles.

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Merge key, e.g. `agent`, `codex`. Also the ladder id. |
| `name` | | Display name. |
| `detect` | | Availability rule (below). Absent means always available. |
| `selection` | | When this backend may be chosen at all. Rendered first, above the mechanics. Set it on a backend the decision tree must never route to (see below). |
| `prefer_for` | | One line about what this backend is. |
| `capabilities` | | Map rendered as bullets. Recognised keys: `tiers` (list of rung ids, filtered to the ones that render), `isolation`, `effort`, `network`, `returns`. Quote `yes`/`no` -- YAML reads them as booleans otherwise. |
| `command` | | One-line invocation, rendered as a code block. |
| `dispatch` | | The mechanics, rendered verbatim. Use a literal block (`\|`) to keep formatting. |
| `gotchas` | | List of one-line traps. |
| `disabled` | | `true` removes the backend. |

`detect` forms:

```yaml
detect: {always: true}                # always available
detect: {command: [codex, --version]} # available if the command exits 0
detect: {path: "~/bin/my-runner"}     # available if the path exists
```

Command detection resolves through `PATHEXT`, so a `foo.cmd` installed by npm or scoop is
found from the bare name on Windows. The command's first output line becomes the "Detected:"
note, which is how the Codex backend reports its version.

**A backend that fails detection is omitted from the rendered guidance entirely** -- its
name, mechanics, and gotchas all go, along with its ladder. Run `--explain` to see detection
status and the reason a backend is missing; that diagnostic view is deliberately separate from
the guidance the skill reads.

Detection **fails closed**: a `detect` mapping declaring no recognised rule, or one this script
cannot evaluate, means unavailable. An undetectable backend that rendered anyway would
advertise mechanics for a tool that is not installed.

The project layer may not declare an executable field. `detect.command` and `capacity.command`
are stripped from it, because that layer is a file inside whatever repository happens to be the
cwd -- machine-level trust is a different question from repo-level trust.

### Request-only backends

A backend with no ladder has no rungs, so `capabilities.tiers` renders as "n/a (no tier
selection)" and no tier decision can land on it. That is necessary but not sufficient: the
skill's procedure picks a BACKEND before it picks a tier, so a ladder-less backend is still a
candidate at that step, and it is the only candidate visible at all when the tree's backend
block is itself gated on a backend that is absent.

`selection` is what closes that. State the condition under which the backend may be chosen;
the skill treats a backend carrying it as off the routing table except under that condition.
The shipped `grok` record is the worked example -- present on the machine, fully documented,
and reachable only when the user names it.

That is a default, not a verdict on the backend. If you want a request-only backend to become
an ordinary routing target -- an xAI subscription you would rather spend than your Claude
pool, say -- override it in your user or project layer: clear the restriction and give it a
ladder, and it participates like any other backend.

```yaml
backends:
  - id: grok
    selection: null              # drop the restriction
    capabilities: {tiers: [grok-workhorse]}
ladders:
  - id: grok                     # ladder id must equal the backend id
    label: Grok
    rungs:
      - id: grok-workhorse
        model: grok-4.6
        criteria: []
        terminal: true
        text: terminal default.
```

The same layer is where you change a pinned model or the launch `command` -- both are ordinary
machine-half fields, so a new model release does not have to wait on a plugin publish.

Adding a backend is the intended way to support a custom orchestrator. Give it a ladder too, or
it has no rungs:

```yaml
backends:
  - id: my-runner
    name: My runner
    detect: {command: [my-runner, --version]}
    prefer_for: An offline CLI runner.
    capabilities:
      tiers: [my-default]
      isolation: none -- one worktree per parallel writer
      network: "no"
    dispatch: |
      my-runner run --brief <file> --out <file>

ladders:
  - id: my-runner
    label: My runner
    rungs:
      - id: my-default
        model: whatever-it-runs
        criteria: []
        terminal: true
        text: terminal default.
```

## `capacity`

| Field | Meaning |
|---|---|
| `source` | `auto` (read `snapshot_path`), `none` (no reporting), or `command` (run `command`, parse stdout as the snapshot JSON). |
| `snapshot_path` | Where the rate-limit snapshot lives. `~` expanded. |
| `command` | Argv list or string, used when `source: command`. |
| `max_age_minutes` | Older than this and the snapshot is reported as stale rather than trusted. |
| `thresholds.warn_remaining_pct` | At or below this remaining %, a window renders `low`. |
| `thresholds.critical_remaining_pct` | At or below this, `CRITICAL`. |
| `tier_overrides` | Map of **rung id** to `available` / `limited` / `unavailable`. |

Snapshot shape:

```json
{"captured_at": 1754500000, "rate_limits": {
  "five_hour": {"used_percentage": 42.5, "resets_at": 1754510000},
  "seven_day": {"used_percentage": 61.0, "resets_at": 1755000000}}}
```

`plugins-kit:claude-ui-kit` writes exactly this from the statusline hook payload, which is the
only place Claude Code surfaces rate limits. The contract is the file, not an import: awesome-kit
reads it opportunistically and reports "capacity unknown" when it is absent, so neither plugin
depends on the other.

**These windows are account-wide, not per-model.** Claude Code exposes no per-model breakdown,
so no snapshot can tell you that one specific rung's usage is spent. That case is what
`tier_overrides` is for -- set it yourself:

```yaml
capacity:
  tier_overrides:
    top: unavailable
```

The rendered policy then lists that rung as unavailable and instructs against dispatching to
it. An override naming a rung that does not render is dropped, so it cannot leak a gated rung
through the one section that does not otherwise consult the gate.

## Migrating from schema 1

Schema 1 stated the policy as prose characterising each model tier. Schema 2 states it as a
decision tree derived from data, so the decision half was reshaped and these top-level keys are
**no longer read**:

`tiers`, `default_tier`, `backend_selection`, `implementation`, `pool_economics`

The layering half is unchanged, which means a schema-1 override still merges cleanly -- it just
contributes nothing. That silence is the dangerous case: your policy is not in force and the
rendered artifact looks fine. So the renderer detects those keys and prints a **Stale override --
NOT IN FORCE** warning in the artifact footer, naming the keys and the layer that set them.

Where the old keys went:

| Schema 1 | Schema 2 |
|---|---|
| `tiers[]` (one flat list, `use_for` / `escalate_when` prose) | `ladders[].rungs[]`, ordered, with `criteria` stated in `lexicon` terms |
| `default_tier` | the ladder's `terminal: true` rung, reached by fall-through |
| `backend_selection` (`gates` / `pulls`) | `backend.gates` / `backend.pulls`, stated in terms |
| `implementation` | absorbed into rung `criteria` -- specification quality is decided in `shape`, not per-rung |
| `pool_economics` | a ladder `notes[]` entry |

`backends` (detection, capabilities, launch mechanics, gotchas) and `capacity` are **unchanged**;
an override against those keeps working as written.
