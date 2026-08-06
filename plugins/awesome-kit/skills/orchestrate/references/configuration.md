# Configuring orchestration policy

The `orchestrate` skill renders its variable half -- model tiers, dispatch backends, usage
capacity -- from configuration. This file is the schema reference. The shipped values live in
[../defaults/orchestration.yaml](../defaults/orchestration.yaml), which is also commented and
is the best worked example.

Audience: a user tuning policy for their machine, or a developer changing the schema.

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
- `tiers` and `backends` merge **by record `id`** -- a record with a known id patches that
  record field-by-field, a record with a new id is appended
- `disabled: true` on a tier or backend removes it from the rendered guidance
- scalars and plain lists (e.g. `capabilities.tiers`) replace outright

Inspect the result with `--explain`, which prints each layer's path and status followed by the
fully resolved config; `--paths` prints just the three paths.

## Top-level keys

| Key | Type | Meaning |
|---|---|---|
| `schema_version` | int | Schema generation. Version `1`. |
| `default_backend` | str | Backend id used when a unit does not call for a specific one. |
| `default_tier` | str | Tier id assumed until something argues for another. |
| `tiers` | list | Model-selection criteria. See below. |
| `backends` | list | Where delegated units run. See below. |
| `pool_economics` | str | Prose rendered under the tier table. |
| `effort` | str | Prose about the effort knob, orthogonal to tier. |
| `capacity` | map | Usage-capacity reporting. See below. |

## `tiers[]`

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Merge key and display name (`workhorse`, `top`, ...). |
| `model` | | Example model for the tier. Names are illustrative, not a contract -- the authority is the dispatch surface's own model enum. |
| `effort` | | Reasoning effort this tier assumes. Adds an Effort column when any tier sets it. |
| `use_for` | | What this tier is for. Rendered in the table. |
| `escalate_when` | | The test for moving up a tier. Rendered in the table. |
| `avoid_when` | | The test for NOT using this tier, rendered as an explicit negative instruction. Use it on expensive tiers -- a bar stated only positively reads as an invitation. |
| `note` | | Longer nuance, rendered as a bullet under the table. |
| `backend` | | Which ladder the tier belongs to. Omit for the `default_backend`'s ladder. The tier is hidden entirely when its backend is not detected. |
| `disabled` | | `true` removes the tier. |

Tiers render in list order within their ladder, so insert a new one where it
belongs in the ordering rather than appending it.

### Ladders

Tiers group into **one ladder per backend**, rendered as separate tables with the
default backend's first. Rungs are meant to be compared *within* a ladder; across
ladders the decision is the **backend**, not the model, because what differs there
is dispatch shape, pool, and independence rather than capability. A single flat
cost-ordered table invites exactly the wrong comparison -- "is this Codex model
better than that Claude one" -- which has no stable answer and no clear best-for
case.

That framing also sets the bar for adding a rung: a tier needs a case where it is
the *best* choice, stated in terms an agent can act on. "Comparable to X at lower
cost" is not such a case; "the default once you are on this backend for
dispatch-shape reasons" is.

```yaml
tiers:
  - id: codex-top
    model: sol
    effort: max
    backend: codex
```

When a backend fails detection its whole ladder disappears -- not greyed out, not
mentioned. This is deliberate: a tier that cannot be dispatched to is worse than
absent, because naming it invites an attempt. The same rule applies to the backend
itself (below), so a machine without the tool sees no trace of it anywhere in the
rendered policy. Cross-references between tiers should therefore avoid naming a
gated tier -- write "a tier below this one" rather than the id, or the reference
outlives the tier it points at.

Retargeting a model is a one-key override:

```yaml
tiers:
  - id: workhorse
    model: my-preferred-model
```

## `backends[]`

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Merge key, e.g. `agent`, `codex`. |
| `name` | | Display name. |
| `detect` | | Availability rule (below). Absent means always available. |
| `prefer_for` | | When to choose this backend over the default. |
| `capabilities` | | Map rendered as bullets. Recognised keys: `tiers` (list of tier ids, empty = no tier selection), `isolation`, `effort`, `network`. Quote `yes`/`no` -- YAML reads them as booleans otherwise. |
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
name, mechanics, and gotchas all go, along with any tier gated on it. Run `--explain` to see
detection status and the reason a backend is missing; that diagnostic view is deliberately
separate from the guidance the skill reads.

Adding a backend is the intended way to support a custom orchestrator:

```yaml
backends:
  - id: my-runner
    name: My runner
    detect: {command: [my-runner, --version]}
    prefer_for: Long offline refactors.
    capabilities:
      tiers: []
      isolation: none -- one worktree per parallel writer
      network: "no"
    dispatch: |
      my-runner run --brief <file> --out <file>
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
| `tier_overrides` | Map of tier id to `available` / `limited` / `unavailable`. |

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
so no snapshot can tell you that one specific tier's usage is spent. That case is what
`tier_overrides` is for -- set it yourself:

```yaml
capacity:
  tier_overrides:
    top: unavailable
```

The rendered policy then marks that tier UNAVAILABLE and instructs against dispatching to it.
