# Lane: duplication (dryer)

The `duplication` lane runs `dryer` over the pruned snapshot and reports code
that has the same structure in more than one place. The descriptions of what
dryer does below come from dryer's own help text (`dryer --help`) at the
commit the plugin pins.

## What it measures

dryer parses each source file, takes each function and method, and normalizes
it: call and method names, operators and the shape of the syntax tree are
kept; local names, field names and literals are dropped. It then compares
every normalized form with the other forms of the same language. The score of
a pair is the Jaccard similarity of their structural fingerprints, from 0.0 to
1.0.

- A score of **1.00** means the two forms are structurally identical after
  normalization. Their text may still differ: renamed locals or changed
  literals do not lower the score.
- A score below 1.00 means the shapes overlap without matching. Only pairs at
  or above the config's `threshold` are reported.
- Forms shorter than the config's `min_lines` are not compared, and neither
  are forms with fewer than 20 normalized syntax nodes (dryer's own
  `--min-nodes` default, which the lane does not change).
- A form is only compared with forms in the same language, so a Python
  function and a TypeScript function are never paired.

With `group_identical: true`, forms with identical structure are reported once
as a set of locations rather than as every pair inside the set, so N copies of
one helper read as one finding with N locations instead of N*(N-1)/2 pairs.

## Reading the report

`report.md` opens this lane's section with a count line --
`Candidates: <n> (<g> with three or more copies, <p> pairs). Ranked by copies,
then score.` -- and then a table with the columns Rank, Copies, Score,
Language and Locations. Sets with more copies come first, then higher scores;
ties are ordered by the first location. Each row is one finding:

| Column | Meaning |
|---|---|
| Rank | Position in the lane's ranking; 1 is reported first. |
| Copies | How many locations share the structure: 2 for a pair, more for a grouped set. |
| Score | The similarity score described above, to two decimals. |
| Language | The language the forms were parsed as; a set never mixes languages. |
| Locations | Each copy as `path:start_line-end_line`, relative to the repository root. For a set, every member; for a pair, both sides. |

`findings.json` holds the same findings with exact values: each finding's
`path`, `start_line` and `end_line` (its first location), its `metrics`
(`score`, `copies`, `language`, `left_nodes`, `right_nodes`), its `rank`, and
its `group` list of `{path, start_line, end_line}` records, one per copy. The lane's entry under `lanes` records dryer's commit (`tool_commit`),
the exact `argv`, the exit status, the run time and the lane `status`.

A status of `no-comparable-source` means the selection held fewer than two
files dryer reads, so a duplicate could not exist and the tool was not run;
`report.md` names the extensions dryer ignores (`lanes[].detail.ignored_extensions`
in `findings.json`). It is not the same as `ok` with zero findings. A status of `no-source-files` with zero findings means the pruned selection
held no file dryer reads. It is not an error; see the languages-read limit below
for the usual cause.

## Known limits

- **Languages read.** dryer reads Python, TypeScript, JavaScript
  (`.js .jsx .mjs .cjs`), GDScript (`.gd`) and C++ (`.cpp .cc .cxx .hpp .hh .h`).
  It skips JavaScript files named `*.test.*` or `*.spec.*`, and the coverage
  table counts them as not measured by duplication. It does not read the C++
  extensions `.c++` and `.hxx`; `report.md` lists them as ignored. The first
  run of each grammar needs the parser bundle, which bootstrap pre-fetches.
- **`.d.ts` declaration files, test files and test directories are
  skipped** by dryer itself, as are vendored and build directories; files in
  languages outside the plugin's table (Go, Java, Rust, Clojure) under a
  source root are compared. Both lists are in `configuring.md`, "Languages
  and extensions".
- **Deliberately vendored copies dominate the ranking.** A helper copied
  byte-for-byte into several packages on purpose shows up as a score-1.00 set
  with many copies, at the top of the table. When the copying is intended,
  add the copies to the config's `exclude` list (for example
  `"**/vendored_helper.py"`) so the report shows duplication nobody decided
  on. Excluding is the user's decision; propose it, do not edit the config
  unasked.
- **Parsers come from the plugin's parser cache.** A bootstrap pass fetches
  them once; when that pass had no network, the first run downloads them and,
  with no network either, exits 1 and says so. The cache location and the
  deferred requirement a failed fetch records are in `configuring.md`, "Where a
  run writes".
- **Only the snapshot is measured.** In `head` mode uncommitted edits are not
  seen; the report records how many paths were dirty.

## A sensible follow-up task

The audit stops at the report. When the user wants a finding acted on, a
follow-up task for one duplicate set (or a few related sets) states:

- the locations, exactly as the report gives them, and the score;
- the question to settle first: is the duplication incidental, deliberate
  (then the remedy is an `exclude` entry, not a code change), or a candidate
  for one shared implementation;
- for an extraction, where the shared code would live and which callers
  change, as the user's decision rather than the audit's;
- the check that closes it: a fresh run of this lane in which the set is
  gone, or is excluded with a stated reason.
