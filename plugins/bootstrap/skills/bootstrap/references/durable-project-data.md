# Durable Project Data

How a plugin stores generated or derived data that belongs to the consuming
project and should travel with that project's source control history.

Audience: plugin authors choosing a storage location and authors of explicit
refresh actions that materialize project data.

## The discriminator

Ask one question:

> Would a teammate on a fresh clone need this data and be unable to regenerate
> it cheaply?

- Yes: it is durable project data. Put it under the project's version control.
- No: it is machine-local data. Ignore it and regenerate it on demand.

Apply both halves of the question. Data that is useful on a fresh clone but is
cheap to reproduce is still machine-local. This keeps caches and routine build
products out of source control.

## The paired locations

| Lifetime | Path | Source-control treatment |
|---|---|---|
| Durable project data | `<project>/.plugin-data/<marketplace>/<plugin>/` | Tracked by the project |
| Ephemeral project data | `<project>/.local-data/<marketplace>/<plugin>/` | Ignored and regenerated |

The paths are deliberate twins: `.plugin-data` and `.local-data` differ by one
element and share the same `<marketplace>/<plugin>` namespace. A reader who
knows either path can predict the other. The marketplace level also prevents
two marketplaces with a same-named plugin from colliding.

Do not put durable data under `.claude/`. That directory is a configuration
surface, and consuming projects may audit or validate it as configuration.
Generated data there can make unrelated configuration tooling process large
artifacts and couples adoption of this pattern to host-specific exceptions.

## Resolution and override

Bootstrap exposes the durable path as `${plugin_data_dir}` through its existing
manifest variable-expansion mechanism. The Python API is
`bootstrap_lib.config_resolve.resolve_plugin_data_dir`. Both resolve the default
path above without creating it.

The standard ephemeral project config may relocate the durable directory:

```yaml
# <project>/.local-data/<marketplace>/<plugin>/config.yaml
plugin_data_dir: Generated/PluginData
```

The override must be relative. It resolves from the project root, so the
example becomes `<project>/Generated/PluginData`. Project config has higher
precedence than the user's plugin `config.yaml`, using the standard layered
config resolver.

`${plugin_data_dir}` is available anywhere the manifest already expands
variables: `ini_settings.file`, `json_entries.reference` and `.target`, and
`pypi_packages.extract_to`. Availability is path resolution only; it does not
authorize those SessionStart phases to write durable data.

## Who writes durable data

> Bootstrap never writes durable project data. An explicit, human-invoked
> refresh action is the only writer.

This rule is the reason the pattern exists. Bootstrap runs automatically at
SessionStart. If it materialized a tracked artifact, merely starting a session
could silently dirty the consuming project's working copy, recreating the
tracked-space defect under a different directory.

The split is:

1. Bootstrap may perform a read-only freshness or presence check.
2. If the artifact is absent or stale, a custom bootstrap script records that
   state with `ctx.add_deferred_requirement` and logs it with `ctx.log_ok`.
3. The plugin's explicit refresh action resolves the same path, tells the user
   what it will update, and writes the artifact after invocation.

Do not aim an auto-remediating manifest entry at `${plugin_data_dir}`. In
particular, `pypi_packages`, `ini_settings`, and `json_entries` can write their
targets during SessionStart. Use them only with machine-local targets; use an
explicit action for durable targets.

## Size and churn gate

Durable does not automatically mean suitable for every version-control system.
Before adopting this pattern, estimate both artifact size and how much of it
changes per refresh.

- Large, frequently regenerated text creates repository growth, slow diffs,
  expensive clones, and noisy reviews in git.
- A depot already designed around large binary assets may absorb the same
  artifact acceptably, but its storage and review costs still need an explicit
  decision.
- Prefer a smaller durable source, schema, index, or generation input when a
  fresh clone can cheaply reconstruct the bulk artifact from it.

Document the expected size, refresh trigger, and churn when declaring an
artifact durable. If those costs are not acceptable for the consuming
project's VCS, keep the artifact ephemeral even when sharing it would be
convenient.
