# clean-code-kit

Audits a git repository's code at rest and writes a findings report. It runs
metric tools over a snapshot of the repository and ranks what they find; it
never edits the code it measures. Deciding what to fix is yours.

Lanes in this version:

- **duplication** -- [dryer](https://github.com/kitaekatt/dryer) finds
  functions with the same structure in more than one place.
- **complexity** -- [crapper](https://github.com/kitaekatt/crapper) ranks
  functions by cyclomatic complexity, without coverage.

Coverage-backed CRAP scores, mutation testing and dependency-direction checks
are not part of this version.

## Use

In Claude Code, ask for an audit of a repository ("audit this repo for
duplication"); the `code-audit` skill runs it. From a shell:

```
"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CLEAN_CODE_KIT_ROOT:?requires a bootstrap engine pass; run bootstrap run}/scripts/code_audit.py" run --target <repo dir>
```

`--target` may be any directory inside the repository. Add
`--lane duplication` or `--lane complexity` to run only that lane (even when
the config disables it), and `--snapshot head` or `--snapshot working-tree` to
choose what is measured. `code_audit.py lanes` lists the lane names.

Both tools read tree-sitter parsers (one bundle of about 26 MB per platform)
from `${CLAUDE_BOOTSTRAP_DATA_ROOT:-$HOME/.claude/plugins/data}/plugins-kit/clean-code-kit/parsers/`.
A bootstrap pass fetches them there, so installing the plugin opts into that
download. When the pass had no network, the first audit run fetches them
instead and needs network once.

## Configure a target

Each audited repository needs `.claude/clean-code-kit/config.yaml`:

```yaml
schema: 1
snapshot: head            # head | working-tree
languages: [python]       # subset of: python, typescript, javascript
source_roots: [src]       # relative to repo root
test_roots: [tests]
exclude:                  # globs relative to repo root
  - "**/vendor/**"
lanes:
  duplication: {enabled: true, threshold: 0.82, min_lines: 4, group_identical: true}
  complexity:  {enabled: true, coverage: false, top: 50}
```

Every key is required, and none has a default; each is described in
`"${CLEAN_CODE_KIT_ROOT:?requires a bootstrap engine pass; run bootstrap run}/skills/code-audit/references/configuring.md"`.
Exclude globs are matched case-sensitively with Python's `fnmatch.fnmatchcase`,
so `*` also matches `/`. The tools skip test files and test directories on
their own, and they also measure Go, Java, Rust and Clojure files under a
source root unless an exclude glob removes them.

## Reports

Each run writes `report.md` and `findings.json` to
`<repo>/.local-data/plugins-kit/clean-code-kit/reports/<UTC timestamp>-<HEAD>/`
and prints one line per lane, then `REPORT <that directory>` as its last
line. `findings.json` holds every finding; `report.md` lists the first `top`
complexity entries. The run refuses to write unless `.local-data/` is
git-ignored in the target -- add it to `.git/info/exclude` (this clone only)
or `.gitignore` -- and refuses when the directory already exists (a second
run in the same UTC second on the same HEAD). Nothing else is written inside
the repository.

Exit codes: 0 report written; 1 a tool failed; 2 a usage, config or
environment problem, with a message naming the file at fault.
