# Configuring a target

Each audited repository carries one config file. The runner reads it on every
`run`; there is no other configuration layer.

## The file

Path, relative to the top level of the target repository:

```
.claude/clean-code-kit/config.yaml
```

The runner reads it from disk, so in `head` mode too an uncommitted edit to
the config takes effect.

The file is YAML. Every key below is required and none has a default, except
`lanes.complexity.min_complexity`, which is optional (default 1): a
missing key is an error (exit 2), and so is an unknown one, so a misspelled key
is never a silently ignored setting. A missing file is also exit 2, and the
message names the path the runner looked for.

A complete file:

```yaml
schema: 1
snapshot: head            # head | working-tree
languages: [python]       # subset of: python, typescript, javascript
source_roots: [plugins]   # relative to repo root; must exist in the snapshot
test_roots: [tests]       # validated to exist; first USED by later lanes
exclude:                  # globs relative to repo root, fnmatch semantics on posix paths
  - "**/bootstrap_guard.py"
lanes:
  duplication: {enabled: true, threshold: 0.82, min_lines: 4, group_identical: true}
  complexity:  {enabled: true, coverage: false, top: 50}
```

## Keys

| Key | Meaning |
|---|---|
| `schema` | The config format version. This version reads `1`. |
| `snapshot` | Which state of the repository is measured: `head` or `working-tree` (see "Snapshot modes"). `run --snapshot` overrides it for one run. |
| `languages` | The languages to measure: a non-empty list of names from `[python, typescript, javascript]`, each at most once (see "Languages and extensions"). Source files of a language left out of this list are removed from the snapshot before any tool runs. |
| `source_roots` | A non-empty list of directories, relative to the repository root and written with `/`, that hold the code to measure. An absolute path or a `..` part is refused. Each must exist in the snapshot. |
| `test_roots` | A list (it may be empty) of directories, relative to the repository root, that hold the tests. Each must exist in the snapshot. No lane in this version reads them; the runner still checks that each exists. |
| `exclude` | A list (it may be empty) of globs, relative to the repository root, matched case-sensitively with Python's `fnmatch.fnmatchcase` against `/`-separated relative paths. A matching file is removed from the snapshot before any tool runs. Under these rules `*` also matches `/`, and a pattern that starts with `**/` needs at least one directory before the name, so `**/x.py` does not match a top-level `x.py`. |
| `lanes` | One entry per lane, every registered lane present. The keys are the lane names `code_audit.py lanes` prints. |
| `lanes.<lane>.enabled` | `true` or `false`: whether `run` without `--lane` includes this lane. With no lane enabled, `run` without `--lane` exits 2. `--lane` runs the named lane even when it is disabled here. |
| `lanes.duplication.threshold` | A number from 0 to 1: the minimum similarity score for a pair of forms to be reported as a duplicate candidate. |
| `lanes.duplication.min_lines` | A non-negative integer: the minimum number of source lines in a form for it to be compared at all. |
| `lanes.duplication.group_identical` | `true` or `false`. When true, structurally identical forms (score 1.00) are reported once as a group rather than as every pair. |
| `lanes.complexity.coverage` | Must be `false`. `true` is refused with exit 2: "coverage-backed complexity is not implemented in this version". |
| `lanes.complexity.min_complexity` | Optional; a positive integer, default 1. `report.md`'s complexity table lists only functions with at least this complexity. `findings.json` is never filtered. |
| `lanes.complexity.top` | A positive integer: how many functions the complexity table in `report.md` lists, highest complexity first. `findings.json` holds every function regardless. |

The exact command line each lane ran is recorded as `argv` in the run's
`findings.json`, so the effect of a setting can be checked against what the
tool was actually given.

## Languages and extensions

The runner owns one table mapping a language to its file extensions. Pruning
uses it, and only it. A `languages` entry is the language's name in lower
case:

| Language | Extensions |
|---|---|
| Python | `.py` |
| TypeScript | `.ts` `.tsx` `.mts` `.cts` |
| JavaScript | `.js` `.jsx` `.mjs` `.cjs` |

Files whose extension is in the table but whose language is not in
`languages` are removed from the snapshot. Files with any other extension are
left in place, with their relative paths preserved. That includes manifests,
configs and data, and also source files of languages outside the table: the
tools read Clojure (`.clj` `.cljc` `.cljs` `.bb`, and `.cljd` for dryer), Java
(`.java`), Go (`.go`) and Rust (`.rs`) too, so such files under a source root
ARE measured. Add them to `exclude` to leave them out.

The two tools do not treat the table's extensions identically: dryer ignores
the JavaScript extensions, so `javascript` affects only the complexity lane.
See each lane's reference for what that means for its findings.

The tools also skip, on their own, test files (`test_*.py`, `*_test.py`,
`conftest.py`, `*_test.go`, `*.test.ts`, `*.spec.ts` and the like) and the
directories named `test`, `tests`, `spec`, `specs`, `__tests__`, `vendor`,
`node_modules`, `build`, `dist`, `out`, `target`, `coverage`, `testdata`,
`venv`, `.venv` and `__pycache__`, among others. That holds even inside a
source root, and it is why no lane in this version measures test code.

## Snapshot modes

The tools never run on the repository itself. The runner copies a snapshot
into a temporary directory, prunes it, runs the lanes there, and removes the
directory when the run ends, whether it succeeded or failed. Any files a tool
writes beside the code it scans land in that copy, not in the target.

| Mode | What the snapshot holds |
|---|---|
| `head` | The committed content of HEAD (`git archive HEAD`). Uncommitted edits and untracked files are not measured. |
| `working-tree` | Every file `git ls-files --cached --others --exclude-standard` lists that exists on disk: tracked files with their uncommitted edits, plus untracked files that are not git-ignored. Ignored files are not measured. |

Every report records the mode, the HEAD sha, and the paths `git status --porcelain`
lists (the config file itself is not counted; `report.md` names the paths), so a `head` report on a dirty tree says so.

## Proposing a config: `init`

`code_audit.py init --target <dir>` prints a proposed config.yaml to stdout and
writes nothing. It counts `git ls-files` extensions and proposes the languages
the lanes read. Source roots are top-level directories holding supported files, except test and
vendored directories; hidden directories count when git tracks supported files
in them. Each such top-level directory is proposed as the deepest directory
holding all its supported files: `artsource/wood-shader-lab/...` proposes
`artsource/wood-shader-lab`, and a top whose files span several subdirectories is
proposed as the top itself (`src/a/x.py` and `src/b/y.py` give `src`). A
top-level directory with exactly one supported file is not rooted: init lists
that file in a comment as a stray. Directories `test`/`tests`/
`spec` become `test_roots`. The comment block lists every source-language
extension no lane reads with its count (`.cpp`, `.gd`, ...) and one `other
files: N` line for the rest. It exits 2 when no tracked file has an extension a
lane reads or no source root can be proposed. Review the output with the user,
then save it as the config.

## What a report says about coverage

`report.md` and `findings.json` (`target.coverage`) count extensions, folded to
lower case, over the files tracked in the target (all of git's file list, not
only the source roots) against the files handed to each lane. The report table
lists every extension some lane reads (`yes`), then every known source-language
extension no lane reads (`source, unmeasured`), then one `other files: N` line
for everything else (assets, data, config). `findings.json` keeps every
extension, each with `kind`: `measured`, `source-unmeasured` or `other`. A
lane's status is `no-comparable-source` when dryer would get fewer than two
readable files.

## Where a run writes

Two locations, and nothing else:

- **The report directory, inside the target:**
  `<target>/.local-data/plugins-kit/clean-code-kit/reports/<UTC yyyymmddThhmmssZ>-<first 8 chars of HEAD>/`,
  holding `report.md` and `findings.json`. Each run creates its own directory;
  earlier reports are left alone. When the directory already exists -- a
  second run in the same UTC second on the same HEAD -- the run exits 2
  naming it, before any tool runs, and writes nothing.
- **The parser cache, outside the target:**
  `${CLAUDE_BOOTSTRAP_DATA_ROOT:-$HOME/.claude/plugins/data}/plugins-kit/clean-code-kit/parsers/`.
  Both tools load their tree-sitter parsers from it; the pack keeps one
  parser bundle (about 26 MB) per platform under
  `tree-sitter-language-pack/v1.25.0/` for the pack version the plugin pins.
  A bootstrap pass fetches the bundle into this directory once, so audits
  work offline from the first run. When that fetch failed (no network during
  the pass), the pass logs it and records a deferred requirement named
  `tree_sitter_parsers` in
  `${CLAUDE_BOOTSTRAP_DATA_ROOT:-$HOME/.claude/plugins/data}/plugins-kit/clean-code-kit/deferred_requirements.json`,
  whose `satisfied_by` is the command that fetches the bundle; the next pass
  tries again. Until then the first audit run fetches the parsers itself, and
  with no network that run exits 1 with a line saying the parsers need
  network once.

### The report directory must be git-ignored

Before running any tool, the runner asks the target's git whether the report
directory is ignored (`git check-ignore`). When it is not, the run stops with
exit 2 and writes nothing. Ignore `.local-data/` in the target, either way:

- `.git/info/exclude` -- applies to this clone only, and is not committed.
- `.gitignore` -- committed, so it applies to every clone of the repository.

Which one is the repository owner's choice.

## Running against a config

With the config in place, a run from any directory:

```
"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CLEAN_CODE_KIT_ROOT:?requires a bootstrap engine pass; run bootstrap run}/scripts/code_audit.py" run --target <dir>
```

`--target` may be any directory inside the repository (default: the current
directory); the target is the top level of the repository containing it.
`--lane <name>` (repeatable) limits the run to the named lanes, enabled or
not; without it every lane with `enabled: true` runs. `--snapshot
head|working-tree` overrides the config's mode.

On success stdout carries one line per lane run, in registry order
(`<lane>: <status>, <n> findings, <seconds> s`), and the last line is
`REPORT <absolute report directory>`.

## Exit codes

| Exit | Meaning |
|---|---|
| 0 | The report was written. The last stdout line is `REPORT <absolute report directory>`. |
| 1 | A tool failed: its stderr is shown verbatim, then `clean-code-kit: <lane> lane: <tool> exited <code>`; a parser download failure adds `clean-code-kit: parsers are fetched on first use and need network once (tree-sitter-language-pack, cached in <cache>)`. Also exit 1: a tool that exited 0 without writing its EDN file (`... exited 0 but wrote no .metrics/<file>.edn`), and EDN whose shape the plugin's reader does not accept (a Python traceback naming the file). |
| 2 | A usage, config or environment refusal, printed as `clean-code-kit: <message>`; the message names the file or path at fault. The refusals are listed below. |

Before any tool runs, the runner checks every precondition (config, tools,
report directory ignored and unused) and reports all failures together as
`N preconditions failed:` with one `- <message>` line each; one failure prints
its plain message. Exit status is 2 either way.

A lane whose selection holds no source files is not a failure: it reports
zero findings with status `no-source-files`.

The exit-2 messages, with `<config>` the config file's absolute path:

| Cause | Message |
|---|---|
| No config file | `no config at <config>; write one as described in references/configuring.md of the clean-code-kit code-audit skill` |
| Unknown key | `<config>: unknown key '<key>' in <where>; allowed keys: <keys>` |
| Missing key | `<config>: missing key '<key>' in <where>` |
| A value of the wrong kind | `<config>: lanes.<lane>.<key> must be <description>, got <value>` (and similar for the top-level keys) |
| `coverage: true` | `<config>: coverage-backed complexity is not implemented in this version` |
| No lane enabled, no `--lane` | `<config>: no lane is enabled; enable one or pass --lane` |
| A root missing from the snapshot | `<config>: source_roots entry '<root>' does not exist in the <mode> snapshot` (or `test_roots`) |
| `--target` not a directory | `--target <dir> is not a directory` |
| `--target` outside a repository | `git rev-parse --show-toplevel failed in <dir>: <git's message>` |
| Tool not installed | `` <tool> is not installed at <path>: the plugins-kit:bootstrap plugin has not provisioned clean-code-kit's venv. Start a new session (or run `bootstrap run`) so it can, then retry. `` |
| Installed tool at another commit | `installed tool does not match the pinned commit; run bootstrap: <tool> installed <sha>, pinned <sha> in <pyproject.toml>` |
| Report directory not ignored | `` <target>/.local-data/plugins-kit/clean-code-kit/reports is not git-ignored in <target>; add `.local-data/` to <target>/.git/info/exclude or to <target>/.gitignore, then retry `` |
| Report directory exists | `report directory <dir> already exists: a run in the same UTC second on the same HEAD wrote it; retry after a second` |

An unknown `--lane` or `--snapshot` value is rejected by the argument parser,
also with exit 2.
