# Deprecation policy for public plugin names

Binds plugin authors in this repository. Consumers live in other repositories
and upgrade on their own schedule, so a name that disappears in one release
breaks them with no warning. This is maintainer material: it stays under
`docs/`, never inside a shipped skill.

## Rule

1. A public name is anything a consumer can import or call across a plugin
   boundary: a module-level name, a class, a method or constructor argument, a
   module constant, or an environment variable the plugin reads.
2. Removing, renaming, or changing the signature of a public name keeps a shim
   under the old name for **two minor versions** after the release that
   deprecates it. The shim emits `DeprecationWarning` (with `stacklevel=2`) and
   delegates to the replacement. Remove it no earlier than the second minor
   release after the deprecating one (deprecated in 0.31.0, removable in 0.33.0).
3. Changing a call that used to succeed into one that raises follows the same
   window: the old behavior stays reachable behind a `DeprecationWarning` for
   two minor versions. A call that now raises is a removal, not a refinement.
4. A removal that ships without the window is a defect to fix forward by
   restoring a shim, not a precedent.

## Why two

One minor window is skipped by a consumer that upgrades every other release,
which is the normal pace for a repository that pins plugin versions. Two
covers that pace and costs the author one extra shim release. Three keeps dead
code for a long time. Two is the smallest number that survives a skipped
release.

## Evidence

Removals that shipped without a shim: content-pipeline-kit 0.25.0
(`backends.BACKEND_ENV`, 4ad89c1c), 0.28.0 (`cli.budget.guarded_sweep`,
6fd94fcf; `RunAdapter.reconcile`, 9916e7ad), and p4-kit 0.39.0, where
`owning_changeset` began to raise (cef63379). Each broke a consumer at import
or call time with no earlier signal.

## Contract listing

A kit that publishes a library keeps a machine-readable listing of its public
surface so authors and consumers see the same facts. content-pipeline-kit is
the reference implementation:

- Listing: `plugins/content-pipeline-kit/contract/public-surface.json`.
  JSON, one entry per line, each `{"name", "status", ...}` where `name` is a
  dotted `module.name` or `module.Class.attr`.
  - `public`
  - `deprecated` with `since`, `remove_after`, `replacement`
  - `removed` with `since`, `replacement`
- Consumer checker: `plugins/content-pipeline-kit/scripts/check_consumer_contract.py`,
  stdlib only. It scans a consumer's source for deprecated or removed names and
  exits 1 when a removed name is used.
- Guard: `tests/content-pipeline-kit/test_public_surface_contract.py`. A public
  or deprecated name that no longer resolves fails; a removed name that resolves
  again fails; a deprecated entry past `remove_after` fails; a name exported in
  a module's `__all__` but absent from the listing fails.

## Author workflow

- Deprecating: add the shim, change the entry to `deprecated` with `since` and
  `remove_after` (the second minor after `since`), and bump the minor version.
- Removing after the window: delete the shim, change the entry to `removed`,
  keep it in the listing permanently so a consumer's checker still explains it.
- Adding a name to `__all__`: add the matching `public` entry.

Keep the listing in step with the code in the same commit as the change.
