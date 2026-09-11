# Code review: the mechanically-decidable surface

Which reviewer-lane criteria a script can answer at FULL RECALL, and which
genuinely need a model. Written to bound how far the deterministic scan in
`bootstrap_lib.code_review.triviality` should extend.

As of bootstrap 0.100.0 the scan implements exactly two of these -- `non_ascii`
and `abs_path` -- and runs them on every file a reviewer reads. Table 2 is the
uncovered remainder, and is the backlog anything widening the scan should work
from. Do not re-derive it; extend it.

## Scope and provenance

This audit treats issue types and reportability gates as criteria. It excludes
transport, tool-use, and JSON-output instructions. A broad criterion stays
JUDGMENT when only a sub-case is mechanical.

`lane_prompts.py:234-362` defines the three prompts. The endpoint wrapper
delegates to `llm_scripting_kit.review_lane` (`run_review_lane.py:96-112,
164-171`). The generator inserts the same prompt constants into both review
skills (`gen_code_review_skills.py:1316-1335`). No second criterion source was
found on either dispatch path.

## Premises

| Premise | Result | Evidence and correction |
|---|---|---|
| P1 | REFUTED as written | The only production call is under `if profile["trivial"]` (`pipeline.py:162-184`). However, the pipeline only annotates the file. The generated skill normally skips it, but an explicit full-review request overrides the skip (`gen_code_review_skills.py:211-220`). Thus the guard is confirmed. "The pipeline has decided to SKIP" and "no agent will read it" are not absolute truths. |
| P2 | CONFIRMED | `mechanical_checks` returns exactly `ascii_clean` and `no_abs_paths` (`triviality.py:336-354`). Both scan added and removed lines, not the post-image alone. |
| P3 | REFUTED as written | Claimed sections and generic chunks are disjoint (`pipeline.py:444-457`). The generated skills normally claim only `.md` (`gen_code_review_skills.py:185-206`), so normal `.yaml` and `.csv` changes miss the scanner. However, `claim_globs` accepts arbitrary patterns (`pipeline.py:389-394`). A caller can claim `.yaml` or `.csv`, after which `annotate_triviality` can call the scanner. Also, `.md` is generic when md-domain is absent. |

## Table 1: criterion inventory

"Exists" means covered by `mechanical_checks`, unless the cell names another
existing deterministic check.

| Reviewer | Criterion | Class | Check and current status |
|---|---|---|---|
| A | Select only the governing `CLAUDE.md` ancestor chain. | DECIDABLE | Resolve the file and walk parents to the repo root. Exists in `collect_claude_mds`, not in `mechanical_checks`. |
| A | Decide whether a written project rule applies and is violated. | JUDGMENT | Formal rules have decidable sub-cases, but rule scope, exceptions, and meaning are not mechanical in general. |
| A | Quote the exact governing rule for every finding. | DECIDABLE | Require the citation to be a substring of one governing file. Output validation checks only type and presence today. |
| A | Treat a claimed path as satisfying a matching document-current rule. | JUDGMENT | Decidable only when the rule names an exact path or a declared source-to-document map. |
| A | Report only violations introduced by this diff. | JUDGMENT | Added-line location is decidable. Causation and changed applicability are not. |
| A | Added text contains non-ASCII code points. | DECIDABLE | Scan added post-image lines. `ascii_clean` exists, but it also scans removed lines. |
| A | Apply the repo ASCII rule and its diagram exception. | JUDGMENT | The permitted box-drawing-in-a-diagram exception needs context. The code-point scan alone cannot settle it. |
| A | Enforce an unconditional absolute-path ban after its scope is established. | DECIDABLE | Tokenize added text and detect platform path forms. `no_abs_paths` exists, but scans deletions and is not proof that a ban applies. |
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
