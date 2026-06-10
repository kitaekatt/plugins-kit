# Vendored files in this directory

`path_repair.py` here is a **byte-identical vendored copy** of the canonical at `plugins/bootstrap/bootstrap_lib/path_repair.py` (vendored, not imported, so unreal-kit can call `repair_path()` without `bootstrap_lib` on the active venv). The mirror-edit and `_VENDORED` drift-test rules live in the `claude_md:` insights and conventions below.

## Insights

```yaml
claude_md:
  _schema_version: "1"
  scope:
    directory: plugins/unreal-kit/lib
    covers:
      - vendored files in unreal-kit/lib
      - byte-identity invariants between vendored copies and their canonicals
      - test coverage for vendored-copy drift
    excludes:
      - the canonical bootstrap_lib code (lives under plugins/bootstrap/bootstrap_lib)
      - unreal-kit skill content (lives under plugins/unreal-kit/skills)
  insights:
    - id: path_repair_vendored_not_imported
      keywords: [path_repair.py, vendored, byte-identical, bootstrap_lib, import-free, dependency isolation]
      summary: path_repair.py in this directory is a byte-identical vendored copy of plugins/bootstrap/bootstrap_lib/path_repair.py, not an import, so unreal-kit can call repair_path() without requiring bootstrap_lib in the active venv.
      detail: |
        The canonical lives at plugins/bootstrap/bootstrap_lib/path_repair.py.
        unreal-kit needs repair_path() but cannot guarantee bootstrap_lib is
        importable from whatever venv is active when unreal-kit runs, so the
        file is vendored (copied byte-for-byte) into this directory. Treat
        the local copy as read-only mirror state -- any change must be
        mirrored to the canonical in the same commit, and vice versa.
      origin: Enforced by tests/bootstrap/test_path_repair.py::test_vendored_copies_match_canonical.
      added: "2026-05-19"
    - id: vendored_copy_drift_test
      keywords: [test_vendored_copies_match_canonical, _VENDORED, drift, byte-identity, test coverage]
      summary: A test enforces byte-identity between vendored copies and their canonicals; new vendored copies must be added to its _VENDORED list.
      detail: |
        tests/bootstrap/test_path_repair.py::test_vendored_copies_match_canonical
        compares each vendored copy to its canonical and fails loudly if the
        bytes drift. When adding (or removing) a vendored copy anywhere in the
        repo, update the _VENDORED list in that test so the byte-identity
        check covers the new (or no-longer-present) file. Skipping this step
        leaves the new copy unprotected against silent drift.
      origin: Enforced by tests/bootstrap/test_path_repair.py (the _VENDORED byte-identity check).
      added: "2026-05-19"
  conventions:
    - rule: When editing path_repair.py in this directory, mirror the same change to plugins/bootstrap/bootstrap_lib/path_repair.py in the same commit (and vice versa).
      keywords: [path_repair.py, vendored, mirror change, canonical, bootstrap_lib]
      why: The byte-identity invariant is enforced by a test; a one-sided edit leaves the repo broken until the mirror catches up.
    - rule: When adding or removing a vendored copy anywhere in the repo, update the _VENDORED list in tests/bootstrap/test_path_repair.py.
      keywords: [_VENDORED, test coverage, vendored copies, byte-identity, drift]
      why: The byte-identity test only protects files listed in _VENDORED; unlisted vendored copies can drift silently.
```

