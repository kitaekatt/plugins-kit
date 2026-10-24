# Package releases and plugin consumers

[katsoft-libs](https://github.com/kitaekatt/katsoft-libs) owns the source, tests,
builds and PyPI releases of `katsoft-contracts` and `katsoft-llm-kit`.
Its [package release guide](https://github.com/kitaekatt/katsoft-libs/blob/dev/docs/reference/package-release.md)
owns the release procedure. Library versions are independent of plugin versions.

This repository consumes released wheels. `scripts/publish.py` validates plugin
requirements and publishes the marketplace; it does not build libraries, create
package tags, upload to PyPI or update the authoritative release ledger.

## Consumer targets

`data/package-targets.json` is the committed, generated consumer artifact.
Its `source.repository` and `source.commit` identify the library repository and
the exact commit of its authoritative `packages/released.json` ledger.
The artifact lists the highest verified final release of each distribution
within each major version.

`scripts/import_package_targets.py` reads that fixed ledger commit. It checks
each selected wheel's filename, SHA256 and non-yanked status against PyPI before
writing the artifact. Pending library versions never become consumer targets.

Bootstrap reads the artifact from the installed marketplace root and selects
the highest imported target admitted by each consumer requirement. It makes no
network request to discover targets. Library repository access is needed only
for the explicit import operation.

The publisher reads targets from the frozen plugin commit. It refuses a
shipping `katsoft-*` requirement with no imported target in range.
Requirements use `name[extras]>=A.B[.C],<N`; declare the extras each plugin uses
in its `pyproject.toml`. A target change alone justifies a marketplace publish,
so consumers can advance within their existing ranges without plugin bumps.

## Update order

1. Change and test library behavior in katsoft-libs.
2. Release the library through that repository's approved release procedure.
3. Import its verified targets into this repository.
4. Review and commit the artifact with any consumer changes.
5. Publish the marketplace through its normal approval gate.

From this repository, import a verified library commit:

```bash
uv run --extra dev python scripts/import_package_targets.py \
  --library-repo "${DEVROOT:?set DEVROOT}/katsoft-libs" \
  --ref "${LIBRARY_RELEASE_COMMIT:?set the verified katsoft-libs commit}"
```

Then run the plugin preflight:

```bash
uv run --extra dev python scripts/publish.py --check
```

A plugin smoke session with `claudx` loads plugin code from this working tree
and library code from installed released wheels. Editing a library checkout
does not alter that session. Run library source checks in katsoft-libs, then
test plugin integration with the released package.

## Bootstrap aliases

`bootstrap_lib._contracts_alias` aliases `execution_event`,
`interrupt_contract` and `model_declaration` from `katsoft_contracts`.
`bootstrap_lib._llm_kit_alias` aliases the supported `llm_kit` seams.
These aliases preserve module identity; they do not vendor library source.
A consumer that reaches an alias must install the corresponding distribution
in its own venv.

Package imports remain `katsoft_contracts` and `llm_kit`; the llm-kit console
script remains `llm-kit`. Runtime modes and public APIs are documented by
katsoft-libs and the llm-kit plugin's user references.
