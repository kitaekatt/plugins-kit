# Code review: the mechanically-decidable surface

Which reviewer-lane criteria a script can answer at FULL RECALL, and which
genuinely need a model. Written to bound how far the deterministic scan in
`bootstrap_lib.code_review` should extend.

The default file-local registry implements `structured_parse`, `duplicate_keys`,
and `column_counts`. The repository registry implements `local_link_targets`.
`non_ascii` and `abs_path` are personal conventions. They run in current scans
only when the user selects them. Private definitions, legacy bundle fields,
and triviality results retain compatibility with older consumers.

Table 2 is the original backlog. The task plan owns current status, ordering,
and admission: each check must replace existing reviewer work and deliver its
answer to that reviewer. The reviewer must receive instructions not to repeat
the covered question. Build execution and new reviewer responsibilities are
outside this task.

### Personal check configuration

The user-only file `~/.claude/config/mechanical_builtin_checks.yaml` selects
trusted implementations. Its complete schema is:

```yaml
checks: [non_ascii, abs_path]
```

Each listed ID runs the preserved implementation over every parsed added line.
The checks retain exact finding details, including Unicode codepoints. They
scan complete lines without the generic regex engine's length cap. An absent
file or `checks: []` selects neither check. The only accepted IDs are
`non_ascii` and `abs_path`. Selection order controls check order.

This filename first appears in bootstrap 0.112. Older versions ignore it and
retain their shipped checks. Thus one user file works across both versions
without duplicate coverage. The selector has no project or shipped layer.

Generic pattern checks remain additive through `mechanical_checks.yaml` in
the shipped, user, and project layers. Duplicate IDs across the default
registry, builtin selector, or any pattern layer are errors. The selector
accepts no custom code, patterns, or detail templates.

## Scope and provenance

This audit treats issue types and reportability gates as criteria. It excludes
transport, tool-use, and JSON-output instructions. A broad criterion stays
JUDGMENT when only a sub-case is mechanical.

`lane_prompts.py:234-362` defines the three prompts. The endpoint wrapper
delegates to `llm_scripting_kit.review_lane` (`run_review_lane.py:96-112,
164-171`). The generator inserts the same prompt constants into both review
skills (`gen_code_review_skills.py:1316-1335`). No second criterion source was
found on either dispatch path.

## Original audit premises

These premises record the pipeline before the task's implementation. The
current scans cover authored files before routing, including claimed files.

| Premise | Result | Evidence and correction |
|---|---|---|
| P1 | REFUTED as written | The only production call is under `if profile["trivial"]` (`pipeline.py:162-184`). However, the pipeline only annotates the file. The generated skill normally skips it, but an explicit full-review request overrides the skip (`gen_code_review_skills.py:211-220`). Thus the guard is confirmed. "The pipeline has decided to SKIP" and "no agent will read it" are not absolute truths. |
| P2 | CONFIRMED | `mechanical_checks` returns exactly `ascii_clean` and `no_abs_paths` (`triviality.py:336-354`). Both scan added and removed lines, not the post-image alone. |
| P3 | REFUTED as written | Claimed sections and generic chunks are disjoint (`pipeline.py:444-457`). The generated skills normally claim only `.md` (`gen_code_review_skills.py:185-206`), so normal `.yaml` and `.csv` changes miss the scanner. However, `claim_globs` accepts arbitrary patterns (`pipeline.py:389-394`). A caller can claim `.yaml` or `.csv`, after which `annotate_triviality` can call the scanner. Also, `.md` is generic when md-domain is absent. |

## Table 1: criterion inventory

This table records the original audit. "Exists" means covered by the legacy
`mechanical_checks`, unless the cell names another deterministic check.

| Reviewer | Criterion | Class | Check and current status |
|---|---|---|---|
| A | Select only the governing `CLAUDE.md` ancestor chain. | DECIDABLE | Resolve the file and walk parents to the repo root. Exists in `collect_claude_mds`, not in `mechanical_checks`. |
| A | Decide whether a written project rule applies and is violated. | JUDGMENT | Formal rules have decidable sub-cases, but rule scope, exceptions, and meaning are not mechanical in general. |
| A | Quote the exact governing rule for every finding. | DECIDABLE | Require the citation to be a substring of one governing file. Output validation checks only type and presence today. |
| A | Treat a claimed path as satisfying a matching document-current rule. | JUDGMENT | Decidable only when the rule names an exact path or a declared source-to-document map. |
| A | Report only violations introduced by this diff. | JUDGMENT | Added-line location is decidable. Causation and changed applicability are not. |
| A | Added text contains non-ASCII code points. | DECIDABLE | The optional user builtin scans added lines. Legacy `ascii_clean` also scans removed lines. |
| A | Apply the repo ASCII rule and its diagram exception. | JUDGMENT | The permitted box-drawing-in-a-diagram exception needs context. The code-point scan alone cannot settle it. |
| A | Enforce an unconditional absolute-path ban after its scope is established. | DECIDABLE | The optional user builtin detects path forms on added lines. Legacy `no_abs_paths` also scans deletions. Neither establishes that a ban applies. |
| A | Keep paired plugin and marketplace version fields synchronized. | DECIDABLE | Map a changed plugin to both manifests and compare post-image versions. This is not in `mechanical_checks`. Publish and pre-commit checks cover related cases. |
| B | Code will not compile. | JUDGMENT | A configured build is a decidable sub-case. Toolchain, platform, generated input, and build selection prevent a universal result. |
| B | Code has a syntax error. | JUDGMENT | Parsing a recognized, non-templated language is a decidable sub-case. The prompt covers arbitrary code. |
| B | Code has a type error. | JUDGMENT | A complete static program with an authoritative type checker is decidable. Dynamic types and incomplete context are not. |
| B | A required import is missing. | JUDGMENT | Literal imports in a complete module are a decidable sub-case. Dynamic and conditional imports are not. |
| B | A code reference is unresolved. | JUDGMENT | Static names under a complete resolver are decidable. Reflection, generation, and runtime registration are not. |
| B | Logic is definitely wrong for all inputs. | JUDGMENT | No full-recall general check exists. |
| B | A data or document file has malformed syntax. | JUDGMENT | YAML, JSON, and TOML parse failures are decidable sub-cases. Documentation has no universal grammar. |
| B | A mapping contains duplicate keys. | DECIDABLE | Parse while preserving key events or object pairs, then map duplicates to added lines. Does not exist. |
| B | A schema is violated. | JUDGMENT | Validation is decidable when a machine-readable schema and file-to-schema mapping exist. Prose and inferred schemas need judgment. |
| B | A CSV or TSV row has the wrong column count. | DECIDABLE | Parse the known dialect and compare each row with the declared header or schema. Does not exist. |
| B | A cross-file reference is broken. | JUDGMENT | A literal local file or Markdown target is a decidable sub-case. Semantic, generated, and dynamic references are not. |
| B | The issue is visible in the diff alone and is in a shown file. | JUDGMENT | File and line membership are decidable. Whether the evidence is sufficient is not. |
| C | Introduced logic error. | JUDGMENT | No full-recall general check exists. |
| C | Introduced concurrency bug. | JUDGMENT | Race detectors and lock rules cover sub-cases only. |
| C | Introduced lifetime bug. | JUDGMENT | Ownership analyzers cover sub-cases only. |
| C | Introduced resource leak. | JUDGMENT | Resource protocols and analyzers cover sub-cases only. |
| C | Introduced security hole. | JUDGMENT | Static security rules cover sub-cases only. |
| C | The change introduced the problem. It was not pre-existing. | JUDGMENT | Added-line attribution is decidable. Semantic causation is not. |
| All | Exclude style, subjective, linter-only, state-dependent, uncertain, and explicitly silenced findings. | JUDGMENT | Known linter suppressions and added-line position are decidable sub-cases. The full classification is not. |

Trailing whitespace is mechanically decidable, but no prompt asks for it.
The guardrails exclude linter-only findings unless a governing standard creates
a separate compliance obligation.

## Table 2: uncovered decidable checks

This order estimates likely use in this repo from its file mix and workflow. It
is not a measured finding rate. Rows marked "sub-case" come from a JUDGMENT row
in Table 1 and do not replace its remaining model work.

| Order | Criterion not covered by `mechanical_checks` | Implementation sketch |
|---:|---|---|
| 1 | Plugin, marketplace, and package version pairing | Reuse the existing manifest and staged-version logic against the review range and reconstructed post-images. |
| 2 | YAML, JSON, and TOML parse failures (sub-case) | Reconstruct each post-image, select a strict parser by declared file type, and attribute parser spans to added lines. |
| 3 | Recognized code syntax or build failure (sub-case) | Run the configured parser or build for supported file types and keep diagnostics attributable to added lines. |
| 4 | Duplicate YAML, JSON, or TOML keys | Use token or pair-preserving parsers. Reject repeated explicit keys in one mapping and attribute the later key. |
| 5 | Machine-readable schema violations (sub-case) | Resolve an explicit file-to-schema mapping, validate the post-image, and retain only added-line diagnostics. |
| 6 | Static missing imports or unresolved names (sub-case) | Run the authoritative resolver for supported languages and map definite diagnostics to added lines. |
| 7 | Literal local file and Markdown targets (sub-case) | Resolve relative targets from the source file, include same-change deletes, and validate local anchors. |
| 8 | CSV or TSV column counts | Use the declared dialect and header or schema width. Parse quoted fields before comparing row widths. |
| 9 | Exact `CLAUDE.md` citation | Confirm that the citation occurs verbatim in the governing chain already attached to that file. |

## Seam B: repository snapshot and changed-set checks

Seam A's registration boundary is
`bootstrap_lib/code_review/mechanical/__init__.py`: one check receives one
`MechanicalSnapshot`. Seam B is a second registry and dispatcher for checks
that also need repository paths, other file contents, or the complete set of
path effects. This is a new contract. It does not widen `MechanicalSnapshot`
or let a shared check read the live filesystem.

### Findings from the current implementation

- `assemble_bundle` receives diff sections and file records, but no repository
  reader. The comments in both `prepare_review.py` files name a future
  `bootstrap_lib.code_review.vcs_adapter`; that module does not exist.
- Git records `range` and the current short `head_sha`. It can resolve the base
  SHA with `_range_base_sha`, but it does not emit a post-snapshot identity.
  `fetch_changed_files` also keeps only the new path of a rename or copy.
- P4 has a `shelf_fingerprint` bundle field for auto-created shelves and folds
  an observed fingerprint into `ledger_baseline`. The action map used by
  `extract_diff` is not a bundle field. Generic file records lose `action`, and
  the existing `#have` pre-image path is intentionally unavailable for a
  submitted or foreign-client review.
- Both front halves already own bounded VCS execution and exact-content reads:
  Git has `run_git` and `git show <base>:<path>`; P4 has `run_p4`, `p4 where`,
  and `p4 print`. These are enough for on-demand tree queries, but they are not
  yet exposed behind a common API.
- A version 2 `mechanical_scan` record is already per source file and supports
  several findings from one cross-file check. It remains adequate while every
  finding has one changed source file and line. A repository finding with no
  unique changed source, or one whose coverage applies to the repository as a
  whole, requires a new result schema rather than a fabricated file or line.

### Inputs and ownership

The VCS front half owns snapshot capture. Before calling `assemble_bundle`, it
must supply these new, explicit inputs:

1. `snapshot_seed`: an opaque, printable VCS identity used to derive the final
   `snapshot_identity` for diagnostics, serialization, and tests.
2. `path_effects`: every post-change path effect, independent of claims and
   chunk routing. Each record has a normalized repository path, `add`, `edit`,
   or `delete`, and the review identifier. A Git rename contributes a delete
   for the old path and an add for the new path; a copy contributes only the
   new add. P4 maps every action, including `move/delete` and `move/add`, before
   assembly.
3. A short-lived `SnapshotReader` implementation with batched `stat(paths)`
   and `read(paths)` operations. Results are `file`, `directory`, `missing`,
   `unsupported`, or `error`; a file read returns bytes plus its pinned
   object/revision identity. `file`, `directory`, and `missing` are answered
   queries. `unsupported` or `error` means no coverage.

The shared pipeline owns the overlay. It installs VCS-captured post-images for
additions and edits over the base reader, and installs deletion tombstones
before any check runs. Git derives those bytes from its accepted diff and
pinned base; P4 supplies verified shelf bytes. Overlay lookup always wins.
Thus an added target exists even when the base reader says it is missing, and
a deleted or old rename path is missing even when it exists in the base. An
unavailable changed post-image is `error`, not `missing`.

Put the shared types and registry in a new
`bootstrap_lib/code_review/mechanical_repository/` package. A
`RepositoryCheck` has `check_id`, `phrase`, `collect(sources)`, and
`evaluate(sources, view, requests)`. `sources` maps every authored changed
identifier to its existing `MechanicalSnapshot`. The collector returns all
normalized metadata and content requests, grouped by source. The dispatcher
unions and bounds them, resolves metadata in batches, freezes the view, and
then permits content reads only for those resolved requests. `view` exposes
overlay-first `stat_many` and `read_many`; asking for a path that collection
did not declare is an execution error. Evaluation returns a `CheckOutcome` for
every eligible source: either complete findings with `ran=True`, or one
diagnostic with `ran=False`. `scan_repository` runs registry order, validates
that every returned file and finding belongs to an input source, and returns
file-keyed version 2 records for the pipeline to merge. This two-phase shape
deduplicates queries across files and prevents one VCS process per link.

For Git, resolve every ref endpoint to a full object ID before capture. A
two-dot range uses the resolved left commit as base and right commit as post;
a three-dot range resolves both endpoints and their merge base. Staged mode
captures `git write-tree` once and diffs that immutable tree against the
resolved `HEAD`. A working, merge, or rebase review has no immutable Git tree,
so capture the same binary, full-index diff twice against one resolved `HEAD`
and proceed only when the byte digests match; otherwise retry once and then
leave all Seam B checks uncovered. The accepted diff bytes are the review
snapshot.

Every capture pins `diff.noprefix=false`, disables external diff and textconv,
and requests `--binary --full-index --find-renames --find-copies`. Object-backed
modes run that command once between resolved object IDs. Working-like modes run
that exact command twice. User diff configuration must not change the format or
rename/copy classification used for effects.

Parse path effects from that one accepted diff, including its extended
`rename from`, `rename to`, `copy from`, and `copy to` headers; do not run a
separate `--name-status` query for Seam B. Preserve both sides. A rename with
no hunks reads its post-image from the old path's pinned base blob; a copy does
the same without a tombstone. Full-index blob IDs may verify reconstructed
post-images, but they do not replace the captured overlay in working mode.
Decode Git's quoted path bytes strictly as UTF-8. Any invalid UTF-8 path or
replacement character makes the complete Seam B run unsupported; the current
text subprocess path cannot represent that repository identity safely.

Git identity is `git:<base-object-id>:<post-object-id-or-diff>:<sha256>`; the
digest covers the exact accepted diff bytes and canonical ordered effects.
Normal and symmetric ranges use the resolved post commit, staged mode uses the
written tree, and working-like modes use `diff`. Batch base queries through
one `git cat-file --batch-check` process and fetch only requested blobs through
`git cat-file --batch`; never consult refs, index, or worktree after capture.

For a locally owned pending P4 change, bracket capture with
`fetch_shelf_fingerprint`: record fingerprint F0, fetch one shelved describe,
its `(revision, action)` records, and the changed source post-images needed for
collection. After collection, fetch any other requested changed target blobs,
then record F1. Accept only F0 == F1 and only when every fetched non-delete
blob's digest matches F0. Retry the whole sequence once; a second mismatch or
scan failure leaves every Seam B check uncovered. Never mix describe data,
requests, or content across attempts.

The revision in the accepted shelved describe action record is the changed
file's base depot revision. Record it for every edit, delete, integrate, and
move/delete, and fetch that exact `depot#revision`; adds, branches, imports,
and move/adds have no base. Do not use workspace `#have` to reconstruct a
shelved changed file. Fetch its post-image as `depot@=<CL>` and verify it
against F0. The action pair supplies rename/move tombstones and additions.

For unchanged requested paths, batch `p4 -ztag where` and `p4 -ztag have`,
retain the exact `depotFile#haveRev`, and print only that revision. A `have`
miss is not immediately `missing`. Group up to 100 unresolved targets and run
one bounded descendant probe for the group:
`p4 -ztag fstat -Rh -m 1000 -T depotFile,haveRev`, followed by one to
100 `<escaped-target>/...` filespec arguments. Thus directory probing uses at
most `ceil(unresolved-targets / 100)` commands.

Retain each target's depot prefix from `p4 where`. Map every returned
`depotFile` to each requested target whose depot prefix is its ancestor at a
`/` component boundary; one such descendant proves that target is a
`directory`. Parse complete records even when the aggregate return code is
nonzero. If 1,000 records are returned, the result may be truncated: proven
parents remain `directory`, but every still-unresolved target in that batch is
`unsupported`. Only when fewer than 1,000 records are returned and the only
diagnostic is Perforce's documented `no such file(s)` result may a target with
no mapped descendant be `missing`. Malformed records, a timeout, or any other
diagnostic make every target not already proven by a complete record `error`.
The first consumer ignores a proven directory with valid coverage and can
never turn it into a broken-target finding.

After metadata capture, freeze identity as `p4:<client>:<sha256>`, covering
F0, canonical describe action/base records, changed-content digests, and
sorted exact unchanged base revisions. Content reads use only those recorded
specs. Submitted changes and foreign-client shelves remain uncovered until
their base view can be pinned without borrowing the reviewer's workspace.
This restriction matches the current pre-image guard and must be a diagnostic,
never a clean result.

`SnapshotReader` exists only during preparation. No callback, credentials, or
VCS command is serialized into `bundle.json`. The finalized
`snapshot_identity` is bundle metadata, not a new mechanical-result schema;
normal mechanical results remain version 2. P4 auto-shelf cleanup keeps its
existing fingerprint guard after all Seam B checks finish.

### Paths, coverage, and failure

Repository paths use Unicode strings with `/` separators and no leading `/`.
Normalize lexically, without `Path.resolve`. After CommonMark decoding, split
at the first literal `#`, then split the pre-fragment portion at the first
literal `?`; this yields path, query, and fragment components. Only then
percent-decode each component once as strict UTF-8. Thus `a%23b.md` names a
file containing `#`, while `a.md#b` has fragment `b`. Require `/` as the
separator, remove `.` segments, collapse `..`, and
reject NUL, invalid UTF-8, backslashes, absolute filesystem paths, URL schemes,
and any escape above the repository root. Preserve case and Unicode code
points; the VCS adapter, not the host filesystem, decides identity. The query
does not participate in repository lookup.

Run Seam B checks after the overlay is complete and before files are divided
among claimed, generic, and machine-emitted routes. A check may inspect every
authored changed file without claiming it. Claims continue to mean that a
specialist audits a file shape; they are unrelated to mechanical reach.

Merge Seam B results into the same version 2 file record used by Seam A:
`{file, checks_run, findings, diagnostics?}`. Preserve registry order when
merging `checks_run` and findings, and add Seam B phrases to the existing
top-level `mechanical_check_phrases`. Generic records travel on their diff
chunk as they do today. Every claimed entry must add this exact field:

```text
"mechanical_scan": {
  "schema_version": 2,
  "files": [{"file": "<identifier>", "checks_run": [], "findings": []}]
}
```

The array has exactly one record for that claimed entry; include diagnostics
on that record when present. Extend all three md-domain Workflow `files[]`
schemas with optional `mechanicalScan` equal to that file record, and add the
bundle phrase map once as top-level `mechanicalCheckPhrases`. The generated
git-kit and p4-kit skills pass both fields for every non-trivial claimed file.
Each md-domain detect lane renders the same coverage/findings preamble as
`lane_prompts.format_mechanical_findings`, tells its model not to repeat a
listed check for that file, and retains the scan in its per-file result so the
main workflow can render it. The main workflow directly renders the same field
for a trivial claimed file, because no specialist launches for that file. A
version-skew fallback that removes a claim discards this placement and obtains
the scan from the rebuilt generic chunk.

Machine-emitted files stay disclosed but unscanned because their generator is
the review target. `local_link_targets` must not ship until the claimed-file
bundle field, all three Workflow schemas/prompts/results, generated skill
instructions, endpoint/native paths, and drift tests are updated together.
Markdown is the normal claimed shape, so generic-chunk-only transport would
compute its result and then deliver it to no reviewer.

Coverage is transactional per source file and check. Record the check in
`checks_run` only after parsing the whole eligible source and answering every
required target query. A missing target is a finding. A parse failure, VCS
error, timeout, unsupported snapshot, limit breach, or unavailable target
content emits a diagnostic and omits the check from that source's
`checks_run`; it must not return partial findings. An eligible file with no
in-scope references is covered and clean. Results inform reviewer lanes and
never suppress one.

Order sources by normalized repository path using Unicode code-point order;
within a source, order candidates by `(line, token occurrence, normalized
target, fragment)`. Preflight all 4 MiB source limits before collection. An oversized
source alone is uncovered and contributes no requests. Deduplicate the
remaining requests by normalized target, retain the ordered list of dependent
sources, and set `need-content` to the OR of every dependent request before
target counting, metadata queries, or byte charging. A target needed both for
existence and for an anchor is one target and one content charge.

The global bounds are 10,000 distinct normalized targets and 32 MiB of target
content. There is no covered-prefix rule. If the deduplicated target count is
over 10,000, issue no VCS query and leave every otherwise eligible source
uncovered. Otherwise run metadata only; charge a shared target's byte size
once, to the review, before any content fetch. If the sum for content-bearing
requests is over 32 MiB, fetch no content and leave every otherwise eligible
source uncovered. A missing target has zero content charge. This all-or-none
rule prevents source ordering or shared targets from changing coverage.

Use the front half's existing VCS timeout for each bounded batch and P4's
existing 100-path batch ceiling. A query or fetch failure invalidates every
source that depends on that target; independent sources may still complete.
Work is linear in changed source bytes, distinct targets, and fetched target
bytes; it never lists or materializes the repository tree.

### First consumer: `local_link_targets`

The first Seam B registry entry has phrase `local file and Markdown link
targets`. Its precondition is a reconstructed Markdown post-image and a
supported snapshot. Bootstrap has no CommonMark parser dependency today. Add
`markdown-it-py` to bootstrap for this check and use its CommonMark token stream
and block line maps; do not build a regex-only Markdown parser. Parse inline
links, images, autolinks, and reference definitions. Inspect an inline
destination when its complete syntax is in a one-line inline block and that
line is added. Each added reference use is a candidate located on its own use
line. An added one-line reference definition is one candidate at the
definition line when at least one use exists anywhere in the post-image;
unchanged repeated uses do not multiply that candidate. An added definition
and added uses are separate introduction sites and may each report. Collapse
only identical `(line, target, fragment, detail)` findings, preserving token
order for repeated uses on one line.

Because the parser does not expose inline source offsets, a local link in a
multi-line inline block or a local multi-line reference definition makes the
whole check uncovered for that source until an exact locator exists. Never
reconstruct its line by searching for the URL text. Apply the parser's
CommonMark backslash and character-reference decoding once, then percent-
decode the path and fragment once as strict UTF-8. Ignore HTTP(S), mail,
protocol-relative, and empty destinations. Treat a fragment-only destination
as the source file. Directory destinations, templated destinations, and
non-Markdown fragments are outside the first version and do not produce
findings.

For `docs/guide.md` containing an added `[setup](../README.md#setup)`, normalize
the target to `README.md`. The overlay answers in this order:

1. A same-change delete or rename-away of `README.md` yields a finding at the
   added link line, even if the base tree still contains it.
2. A same-change add or edit supplies its reconstructed post-image, so the link
   and its anchor can pass without a VCS read.
3. Otherwise the reader checks the pinned base path and fetches its content
   only because a fragment is present.

For Markdown targets, compare a decoded fragment exactly and case-sensitively
against explicit HTML `id`/`name` values and `mechanical-slug-v1` heading
slugs. Parse raw HTML start tags with Python's `html.parser.HTMLParser`, accept
its quoted and unquoted attribute forms, match attribute names ASCII-case-
insensitively, and use the attribute value returned by `HTMLParser` directly;
it has already decoded character references. Do not call `html.unescape` on it
again. Do not case-fold or Unicode-normalize either explicit values or the
incoming fragment.

`mechanical-slug-v1` takes rendered heading text from the CommonMark inline
children: keep text, code text, and image alt text; discard formatting wrappers
and raw HTML tags; use already decoded character references. Apply Python
`str.lower()`, trim leading/trailing Unicode whitespace, remove every Unicode
punctuation-category character except ASCII `-` and `_`, and replace each
remaining Unicode whitespace run with one `-`. Preserve symbols and non-ASCII
letters. Allocate the bare slug if unused; otherwise try `-1`, `-2`, and so on
until unused, so a literal heading ending in `-1` also occupies that name. An
empty result produces no generated anchor. Pin this named dialect with
fixtures; do not call it GitHub-exact and do not guess anchors by searching for
heading text.

A missing file reports `target does not exist in review snapshot`; a missing
anchor reports `anchor does not exist in target in review snapshot`. Both use
the applicable added-line location above.

Deliberately deferred are reverse-reference scanning for unchanged source
files broken by a changed deletion, directory targets, platform-specific
case-folding, generated or templated links, non-Markdown fragments, remote URL
health, submodule contents, and submitted or foreign-client P4 base views.
Those widen either the covered question or the snapshot contract and must not
be implied by `local_link_targets` coverage.

## Conclusions

Reviewer B is reduced most. Its structured-data scope is mostly parser,
duplicate-key, schema, column-count, and literal-reference work. After those
checks move out, about half of its general defect scope remains: semantic
references, incomplete compile or type cases, and definitely-wrong logic. On a
pure structured-data chunk with a known schema and literal references,
reviewer B can be empty.

No lane is empty for all changes. Reviewer C remains judgment work. Reviewer A
still interprets rule scope and exceptions. Reviewer B can become empty for the
narrow structured-data shape above, so that lane need not run for that chunk.
The current profile already applies the same principle by omitting reviewer C
from data-only reviews.

A script produces false positives when its preconditions are implicit. An
ancestor walk fails when its repo root or symlink boundary is wrong. Scanning
removed lines flags a change that deletes a bad byte or path. The ASCII scan
also rejects permitted box drawings. Strict parsers reject templates or JSONC.
Duplicate-key checks can mistake YAML merge overrides for duplicates. CSV
checks fail on the wrong dialect or preamble. Link checks fail on generated
targets, platform case rules, or derived anchors. Schema checks fail with the
wrong schema mapping. Build and import checks fail on optional platforms,
generated modules, or conditional imports. Version checks fail on unlisted or
held-back plugins unless they model publish status. Citation checks fail after
harmless whitespace normalization unless the contract truly requires byte-for-
byte text.
