# Lane: complexity (crapper, without coverage)

The `complexity` lane runs `crapper` over the pruned snapshot without
coverage and ranks functions by cyclomatic complexity. The descriptions of
what crapper does below come from crapper's help text (`crapper --help`) and
source at the commit the plugin pins.

## What it measures

crapper parses each source file, finds each function and method, and computes
its cyclomatic complexity (CC): a count of the decision points in its body,
so it rises with branching. Which constructs count is crapper's own rule per
language.

crapper is built around the CRAP score, `CC^2 * (1 - coverage)^3 + CC`, which
combines complexity with test coverage. This lane runs it without coverage
("Score complexity only. Coverage and CRAP are N/A." in crapper's help), so:

- the ranking is by complexity alone;
- the coverage and CRAP values carry no number in this version, and must not
  be read as zero coverage or as a CRAP score;
- a high-complexity function is not thereby untested -- this lane cannot say
  either way. Coverage-backed CRAP scores are not available in this version,
  and a config with `coverage: true` is refused.

## Reading the report

`report.md` opens this lane's section with a count line (`Functions scored:
<n>. Showing the top <k> by cyclomatic complexity. ...`), then a table of the
first `top` functions (from the config), highest complexity first; ties are
ordered by namespace, then name. The columns are Rank, Complexity, Namespace,
Function and Location:

| Column | Meaning |
|---|---|
| Rank | Position in the ranking; 1 is the most complex function. |
| Complexity | The function's cyclomatic complexity. |
| Namespace | crapper's name for the function's module. For Python it is the dotted module path relative to the source root crapper walked, with `.ClassName` appended for a method. |
| Function | The function or method name (`symbol` in `findings.json`). |
| Location | `path:line`: the function's file, relative to the repository root, and its 1-based start line. |

`findings.json` holds EVERY scored function, not only the first `top`; `top`
limits the `report.md` table alone. A complexity finding's `path` and
`start_line` locate the function and `end_line` is `null` (crapper reports no end line); its `namespace` and `symbol` identify
it, and its `metrics` hold `complexity`, plus `coverage` and `crap`, which are
`null` in this version. The lane's
entry under `lanes` records crapper's commit (`tool_commit`), the exact
`argv`, the exit status, the run time and the lane `status`.

A status of `no-source-files` with zero findings means the pruned selection
held no file crapper reads. It is not an error.

## Known limits

- **No end line.** crapper reports each function's file and start line, not
  its end line, so `end_line` is `null`. Never present a guessed end line as
  the report's.
- **JavaScript is scored as TypeScript.** crapper treats `.js .jsx .mjs .cjs`
  as TypeScript, as well as `.ts .tsx .mts .cts`. JavaScript functions are
  ranked here even though the duplication lane does not read those files,
  so the `javascript` language affects this lane only.
- **`.d.ts` declaration files, test files and test directories are
  skipped** by crapper itself, as are vendored and build directories; Go,
  Java, Rust and Clojure files under a source root are scored. Both lists are
  in `configuring.md`, "Languages and extensions".
- **Deliberately vendored copies are scored once per copy.** A helper copied
  into several packages appears once for each copy. Add intended copies to the
  config's `exclude` list when they crowd the ranking; propose it, do not edit
  the config unasked.
- **Parsers come from the plugin's parser cache.** A bootstrap pass fetches
  them once; when that pass had no network, the first run downloads them and,
  with no network either, exits 1 and says so. The cache location and the
  deferred requirement a failed fetch records are in `configuring.md`, "Where a
  run writes".
- **Only the snapshot is measured.** In `head` mode uncommitted edits are not
  seen; the report records how many paths were dirty.

## A sensible follow-up task

The audit stops at the report. When the user wants a finding acted on, a
follow-up task for one function (or one module's worst functions) states:

- the namespace, the symbol and the complexity, exactly as the report gives
  them, and the file once it has been located by search;
- the question to settle first: whether the complexity is inherent (an option
  parser or a dispatch over many cases can be legitimately branchy) or a
  candidate to split or simplify;
- whether tests cover the function, checked by hand, because this lane
  does not measure coverage;
- the check that closes it: a fresh run of this lane in which the function's
  complexity has dropped, or a recorded reason it stays.
