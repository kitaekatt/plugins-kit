# Where a Plugin Writes Its Own Data

How a plugin stores what it generates: data belonging to the consuming project
(durable if it should travel with that project's source control history,
ephemeral otherwise), and data belonging to no project at all.

Audience: plugin authors choosing a storage location, authors of explicit
refresh actions that materialize project data, and anyone auditing a plugin's
write paths.

Most of this document concerns PROJECT data; "Data that is not the project's"
below covers the user- and machine-scoped case and the rule that a write path
is never derived from the script's own location.

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

## Data that is not the project's

Everything above concerns data ABOUT the consuming project. A plugin also
writes data about the USER or the MACHINE -- a credential cache, a captured API
response, a snapshot another plugin reads. That data has no project to belong
to, so neither `.plugin-data` nor `.local-data` is its home. It belongs in the
plugin's own user-scoped data directory, which bootstrap already provisions:

```
~/.claude/plugins/data/<marketplace>/<plugin>/
```

### Never derive a WRITE path from the script's own location

A write path is named, not discovered. Resolving one from `BASH_SOURCE`,
`__file__`, or `$0` binds where the data lands to where the code happens to be
executing -- and every published plugin runs from at least two places: the
installed copy under the data root, and a developer's checkout of the source
repo.

Reading beside the script is correct, because assets ship with the code.
Writing beside it is not. From a checkout, "beside the script" is a git working
tree, so the plugin deposits user data into a source repository -- and if the
repo is public, that is a disclosure, not just untidiness.

Observed: claude-ui-kit's statusline derived its rate-limit snapshot directory
from `BASH_SOURCE/..`. That is the plugin data dir when installed and a
deliberately public git repo when run from a dev checkout, so an account's
usage percentages landed in the repo while every consumer kept reading the
empty canonical path.

### One contract path, named identically at both ends

When the data is exchanged with other plugins it is a FILE CONTRACT, and a
contract has one absolute path. Writer and readers must name the SAME literal:
if the readers hardcode it, the writer hardcodes it too. A path that merely
COINCIDES with the contract under the common configuration is not a contract --
it is a bug waiting for the uncommon one.

An environment variable is the seam for a caller who genuinely wants the data
elsewhere. Honouring a ROOT variable in the writer that no reader honours does
not relocate the pair, it desyncs it.

### Auditing a plugin against this

Search the plugin for a directory resolved from `BASH_SOURCE`, `__file__`, or
`$0`, then ask of each whether it is read from or written to. A read needs no
further thought. A write is a finding unless the resolved path is provably
under one of the three homes above -- the project's `.plugin-data` or
`.local-data`, or the plugin's user-scoped directory. Where the artifact has outside readers, confirm the
writer's literal path matches theirs rather than reproducing it.

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

## A refresh action must handle checkout semantics

Durable data lives under the *project's* version control, not the plugin's.
Once it has been committed once, some VCSes lock the on-disk file until it is
explicitly checked out -- Perforce marks a synced/submitted file read-only,
for example. Git has no equivalent, which is easy to miss if the pattern was
only ever exercised in a git checkout: the first refresh (destination absent)
succeeds regardless of VCS, and the defect only shows up on the SECOND refresh
against a VCS with checkout semantics, when the destination already exists and
is locked.

A refresh action therefore cannot assume the destination is writable just
because it resolved a path for it. Before writing:

- If the destination does not exist, write it -- nothing to check.
- If the destination exists and its content already matches the new value,
  there is nothing to write; report "already up to date" and stop rather than
  touching a file that may be locked for no reason.
- If the destination exists, differs, and is read-only, do not attempt the
  write -- it fails as a raw `PermissionError` (or platform equivalent) deep
  inside a copy call, not as a message the user can act on. Fail cleanly
  instead, naming the actual path and the concrete next step (e.g. `p4 edit
  <path>` when a Perforce workspace is confidently detected; a generic
  "check it out of version control, or clear the read-only flag" otherwise).

Do not auto-checkout or clear the read-only bit on the user's behalf. A
refresh action may detect and instruct; only the user decides to mutate their
own VCS state. See `plugins/unreal-kit/lib/unreal_stub.py`
(`refresh_durable_stub`, `DestinationNotWritableError`) for a worked
implementation of this check.
