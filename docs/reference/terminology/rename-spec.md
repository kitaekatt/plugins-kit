# rename-spec: executing the ratified terminology framework

**Status:** specification only. Nothing in this document has been applied.
**Input:** `docs/reference/terminology/terminology-audit.md` (the 27-row
divergence table). This document does not redo that analysis; it converts the
owner's ratified decisions into executable packets.

**Audience: a cheap, non-inferring implementer.** Every instruction names an exact
token or an exact heading. Nothing here requires a judgement call. If you find
yourself deciding something, STOP and report it as a spec defect rather than
choosing.

**Keyed on tokens, not line numbers.** A concurrent unit is editing the same tree.
Locate every edit by searching for the OLD token. Where a row is prose-only, it may
already be converted -- in that case the search returns nothing and the row is a
NO-OP. That is expected, not an error. Applying this spec twice must be a no-op the
second time.

---

## 0. The ratified decisions this spec executes

Baked in. Do not reopen any of these.

1. `author` -> `generate` throughout md-domain (lane ids, dispatch keys, the lane
   file, tests, and the `prototypes` consumer).
2. The verb wins the "generated" collision. The machine-emitted sense yields and
   takes a qualifier.
3. `coverage` keeps its name and stops being a peer verb; it becomes the DISCOVERY
   PHASE of generation. Its report-only contract is unchanged and the non-licence
   must be stated explicitly.
4. `"kind": "md_audit"` is NOT renamed (persisted consumer ledger state). A
   backward-compatible migration is specified separately in section H as OPTIONAL.
5. `references/audit-framework.{md,yaml}` do NOT move or change name. Prose inside
   may change.
6. CV-8's lexical anchor is load-bearing. No rename that would erase it is
   specified; see section G.

### 0.1 The chosen machine-emitted term

**`machine_emitted` (snake_case) / `machine-emitted` (kebab and prose) /
`Machine-emitted` (sentence-initial prose).**

One-line justification: it is the exact phrase the already-landed
`dec_20_audit_and_generation_vocabulary` record uses three times for this sense, so
the rename promotes settled prose rather than coining a new word; it contains no
`generat` substring, so the verb's word is fully freed; and "machine-" is precisely
the qualifier the ratified resolution direction said the narrower sense should take.

### 0.2 State of the tree as found (2026-08-10)

Verified by reading, so the implementer knows which rows are already NO-OPs:

- `dec_20_audit_and_generation_vocabulary` EXISTS in
  `plugins/skills-kit/skills/md-domain/references/provenance/skill-authoring-decisions.md`.
  Its closing paragraph says the rename is UNDECIDED and that "the verb token stays
  `author`". **That paragraph is now false and packet A must amend it** -- see A-13.
- The `coverage_is_a_report_only_third_verb` insight in
  `plugins/skills-kit/skills/md-domain/CLAUDE.md` has ALREADY been amended (it now
  carries "AMENDMENT 2026-08-09", "DISCOVERY step, not a third peer", and an explicit
  report-only non-licence paragraph). Decision 3's prose is largely landed.
- `md-domain/SKILL.md` already carries the discovery-phase framing in its opening
  and the "an inventory renders no verdict, so it is NOT an audit" guards. Group C of
  the audit is largely landed.
- `references/lanes/authoring-lane.md` already carries
  `## Procedure: author or refine an md artifact (generate, or regenerate when it exists)`.
- A stray editor temp file
  `plugins/skills-kit/skills/md-domain/references/lanes/coverage-lane.md.tmp.8400.c328dfc53045`
  was present. Do not commit it; do not delete another session's temp file either --
  just exclude it from every pathspec.

---

## A. Token map

### A.1 Verb tokens: `author` -> `generate`

| # | Old token (search for this) | New token | Kind |
|---|---|---|---|
| T1 | `author_skill` | `generate_skill` | lane id |
| T2 | `author_claude_md` | `generate_claude_md` | lane id |
| T3 | `author_project_doc` | `generate_project_doc` | lane id |
| T4 | `verb: author` | `verb: generate` | YAML lane-record field value |
| T5 | `author x skill` | `generate x skill` | dispatch-table row key (parsed verbatim by tests) |
| T6 | `author x claude-md` | `generate x claude-md` | dispatch-table row key |
| T7 | `author x project-doc` | `generate x project-doc` | dispatch-table row key |
| T8 | `author x references` | `generate x references` | dispatch-table row key (the no-lane row) |
| T9 | `[audit\|author\|coverage]` | `[audit\|generate\|coverage]` | CLI token inside `argument-hint:` frontmatter |
| T10 | `` `audit` \| `author` \| `coverage` `` | `` `audit` \| `generate` \| `coverage` `` | CLI verb enum in Argument grammar prose |
| T11 | `references/lanes/authoring-lane.md` | `references/lanes/generation-lane.md` | bound procedure path (contract) |
| T12 | `authoring-lane.md` (bare, in relative links) | `generation-lane.md` | relative cross-reference |
| T13 | `id: authoring_lane` | `id: generation_lane` | SKILL.md `index.references` id |
| T14 | `/md-domain author skill` | `/md-domain generate skill` | copy-pasteable command string |
| T15 | `/md-domain author skill\|claude-md\|project-doc` | `/md-domain generate skill\|claude-md\|project-doc` | command string |
| T16 | `"author a new skill"` | `"generate a new skill"` | `invocation_phrasings` entry |
| T17 | `"author a CLAUDE.md for this directory"` | `"generate a CLAUDE.md for this directory"` | `invocation_phrasings` entry |
| T18 | `"author a README for this project"` | `"generate a README for this project"` | `invocation_phrasings` entry |
| T19 | `test_author_references_lane_is_absent` | `test_generate_references_lane_is_absent` | pytest function name |
| T20 | `test_authoring_lanes_bind_standards_and_procedure` | `test_generation_lanes_bind_standards_and_procedure` | pytest function name |
| T21 | `"verb": "author"` / `{"verb": "author"` | `"verb": "generate"` | Python dict literal in `EXPECTED_LANES` |

**Prose forms** (INCONSIST rows A6-A10; convert only where the word names md-domain's
VERB or ITS LANE):

| # | Old prose | New prose |
|---|---|---|
| T22 | "the authoring lane" / "the author lanes" | "the generation lane" / "the generation lanes" |
| T23 | "the authoring verb" / "author" as the verb name | "the generation verb" / "generate" |
| T24 | "the authoring direction" / "the AUTHORING direction" | "the generation direction" / "the GENERATION direction" |
| T25 | "the producing direction" / "PRODUCING direction" | "the generation direction" / "the GENERATION direction" |
| T26 | "Authoring is not a separate mode" (greeting) | "Generation is not a separate mode" |
| T27 | "two verbs (**audit**, **author**)" | "two verbs (**audit**, **generate**)" |

### A.2 Machine-emitted tokens: `generated*` -> `machine_emitted*`

| # | Old token | New token | Kind |
|---|---|---|---|
| M1 | `generated_artifact` | `machine_emitted_artifact` | per-artifact role id (placement spine) |
| M2 | `generated_artifact_provenance` | `machine_emitted_artifact_provenance` | cohesion `audit_rules` id |
| M3 | `generated_files` | `machine_emitted_files` | JSON bundle field |
| M4 | `generated_axis` | `machine_emitted_axis` | JSON bundle field |
| M5 | `generated_signature` | `machine_emitted_signature` | JSON bundle field |
| M6 | `--review-generated` | `--review-machine-emitted` | CLI flag |
| M7 | `review_generated` | `review_machine_emitted` | Python keyword argument / local |
| M8 | `bootstrap_lib/code_review/generated.py` | `bootstrap_lib/code_review/machine_emitted.py` | module filename |
| M9 | `bootstrap_lib/code_review/generated_paths.py` | `bootstrap_lib/code_review/machine_emitted_paths.py` | module filename |
| M10 | `detect_generated` | `detect_machine_emitted` | public function |
| M11 | `bootstrap_lib.code_review.generated` | `bootstrap_lib.code_review.machine_emitted` | import path / docstring reference |
| M12 | `bootstrap_lib.code_review.generated_paths` | `bootstrap_lib.code_review.machine_emitted_paths` | import path / docstring reference |
| M13 | `tests/bootstrap/code_review/test_generated.py` | `tests/bootstrap/code_review/test_machine_emitted.py` | test filename |
| M14 | `tests/bootstrap/code_review/test_generated_paths.py` | `tests/bootstrap/code_review/test_machine_emitted_paths.py` | test filename |
| M15 | `## Generated artifacts (not reviewed)` | `## Machine-emitted artifacts (not reviewed)` | rendered report section heading |
| M16 | `### PD-10. Generated artifacts: provenance only (generated_artifact role)` | `### PD-10. Machine-emitted artifacts: provenance only (machine_emitted_artifact role)` | standards heading (PD-10 id UNCHANGED) |

**M2 and M16 are INFERRED inclusions**, not tokens the owner named. They are compounds
built on `generated_artifact`, so leaving them would split the role's name across two
spellings. If the owner vetoes them, drop M2 and M16 and change nothing else; no other
row depends on them.

### A.3 OVER-MATCH LIST -- tokens a naive replace would destroy. DO NOT TOUCH.

A blind `s/author/generate/` or `s/generated/machine_emitted/` breaks all of these.
Every one of them was verified present in the tree.

**Never rename (`author` family):**

| Token | Why it must survive |
|---|---|
| `author: christina` in `md-domain/SKILL.md` frontmatter | A frontmatter METADATA field naming a person. Renaming it breaks the SKILL.md schema. |
| `authoritative`, `authoritatively` | Unrelated word containing the substring. Present in `coverage-detect.js` and elsewhere. |
| `authorization`, `authorized` | Unrelated word. Present in `md-domain/SKILL.md`. |
| `authorial` | "requires a human writer" sense. Present in `tests/skills-kit/golden_corpus/expected-lanes/skill-lanes.json`. |
| `author` meaning A PERSON | e.g. `the author's choice`, `the code author, not the doc`, `an author may add`, `user-authored standards`, `hand-authored`, `the AUTHOR's call` in the code-review kits. These are the human writer, not md-domain's verb. |
| `authored-doc criteria`, `authored artifact`, `not authored` | The "written by a human" sense in `cohesion-principles.md` and `project-doc-standards.md`. Contrasts WITH machine-emitted; renaming inverts the meaning. |
| `md-authoring`, `md-audit`, `claude-md-authoring` | Names of DELETED skills, kept as history. |
| `skill-authoring-decisions.md` (filename) | Provenance log filename; not the verb. |
| `skill-authoring/CLAUDE.md`, `skill-authoring framework.md` | Historical example paths inside `cohesion-principles.md` worked examples. |
| `references/authoring-standards.md` | See refusal R1. |
| `references/authoring-patterns/` and every path under it | See refusal R2. |
| `id: authoring_standards`, `id: authoring_patterns` | Index ids pointing at the two unrenamed surfaces above. |
| `skill_authoring_patterns` URL in `glossary.md` | An external URL. |

**Never rename (`generated` family):**

| Token | Why it must survive |
|---|---|
| `M_generated_missing_provenance` | A finding-code RULE ID in the `project-doc-detect.js` enum. Rule ids are preserved verbatim by standing decision. See refusal R3. |
| the bare `"generated"` boolean field | Output field of `scripts/discover_project_doc.py`, pinned in `tests/skills-kit/golden_corpus/expected/project-doc-signals.json`. See refusal R4. |
| `"generated"` in the skip-directory name list in `discover_project_doc.py` | A literal directory NAME on disk. |
| `generated (?:analysis\|by\|from\|with)` and `auto-?generated` regexes | Detection patterns matching text IN OTHER PEOPLE'S FILES. Changing them breaks detection. |
| `generated-by banner` | A signature LABEL string asserted in tests; describes the banner text found in foreign files. |
| the word "generator" | Correct and unchanged everywhere. "review of generated output belongs on the GENERATOR" becomes "review of machine-emitted output belongs on the GENERATOR". |
| `GENERATED` in `docs/reference/orchestrate/*` | Unrelated subject (the orchestration.yaml generator). Out of scope entirely. |
| `generated` in `gen_workflow_js.py`'s own docstring ("generated-not-copied files", "NOT fully generated") | Describes the JS generator, not the code-review sense. |

**Safe forms.** Use anchored searches, never bare substrings:

- For the verb: search `author_`, `verb: author`, `author x `, `"verb": "author"`,
  `authoring-lane`, `authoring lane`, `/md-domain author`, `authoring_lane`.
  Then hand-review every remaining `\bauthor` hit against the table above.
- For machine-emitted: search `generated_files`, `generated_axis`,
  `generated_signature`, `generated_artifact`, `review_generated`,
  `review-generated`, `detect_generated`, `code_review.generated`.
  Never search bare `generated`.

---

## B. Per-plugin work packets

Five packets. Each names its files, its edits, and its own verification command.

### Packet A -- skills-kit (verb rename + role rename + dec_20 amendment)

Owns every file under `plugins/skills-kit/` and `tests/skills-kit/`.
**A and B/C/D/E share no file. A is parallel-safe against all of them.**

The verb rename and the role rename are ONE packet, not two, because
`references/cohesion-principles.md` carries both (`/md-domain author skill|...` near
the top AND the `generated_artifact` role) and
`references/provenance/skill-authoring-decisions.md` carries both in `dec_20`.

Files and edits:

- **A-1** `plugins/skills-kit/skills/md-domain/SKILL.md`
  - T9 in `argument-hint`. T5/T6/T7/T8 in the `## Dispatch table` rows. T11 in the
    three dispatch-table Procedure cells. T1/T2/T3 in the Lane id cells and in the
    `**\`author x references\` has no lane, deliberately.**` paragraph beneath it.
  - T1/T2/T3 as `- id:` values and T4 as `verb:` values in the three lane records
    under `### Lane records`. T11 as their `procedure:` values.
  - T16/T17/T18 in those records' `invocation_phrasings`.
  - T10 in the `## Argument grammar` verb bullet; and in the Artifact bullet change
    `**Artifact** (audit / author only)` to `**Artifact** (audit / generate only)`.
  - T13 and T11 in the `index.references` record for the lane, plus T22/T24/T25 in
    that record's `keywords` and `summary`.
  - T22/T23/T26 in the opening paragraphs, the bare-invocation greeting, the "The
    author lanes still exist" paragraph, the `identity`/`in_scope`/`out_of_scope`/
    `procedure` prose, and the `- **How to author** --` closing pointer (which becomes
    `- **How to generate** -- \`references/lanes/generation-lane.md\`.`).
  - **Do not touch** `author: christina` on the frontmatter line.
- **A-2** `git mv plugins/skills-kit/skills/md-domain/references/lanes/authoring-lane.md plugins/skills-kit/skills/md-domain/references/lanes/generation-lane.md`
  Then inside the renamed file: T1/T2/T3 in its lane table, T22/T23/T24/T25 in its
  prose, its `# The authoring lane` H1 becomes `# The generation lane`. Leave its
  `../authoring-patterns/content-authoring.md` and `../authoring-standards.md`
  relative links UNCHANGED (refusals R1/R2).
- **A-3** `plugins/skills-kit/skills/md-domain/references/lanes/audit-lane.md` -- T12
  in the "Producing a compliant artifact instead of judging one" pointer. Leave the
  `../authoring-standards.md` pointer on the line above unchanged.
- **A-4** `plugins/skills-kit/skills/md-domain/references/standards/skill-standards.md`
  -- T12 (twice, in the markdown link) and T22/T24 in the surrounding prose of the
  "The authoring lane ... applies these" passage and the "Order of application" intro.
  Leave every "author"-as-person and the `md-authoring and md-audit` history sentence
  unchanged.
- **A-5** `plugins/skills-kit/skills/md-domain/references/standards/claude-md-standards.md`
  -- T12 in the "lives in `../lanes/authoring-lane.md`" pointer; T22/T24 in the
  "the **authoring lane** applies it in the produce direction" sentence, the
  `### 6.3 The authoring anti-pattern` heading, the section-6 title
  `Authoring-direction notes`, and its TOC entry. Leave the
  `../authoring-patterns/` pointer and every author-as-person use unchanged.
- **A-6** `plugins/skills-kit/skills/md-domain/references/standards/project-doc-standards.md`
  -- T22/T24 in the "the **authoring lane** applies them in the opposite" sentence and
  in "The authoring lane fills that". M16 for the PD-10 heading and M1 wherever
  `generated_artifact` appears in its body; M2 in the finding table's left column.
  Leave the PD-* ids, `authored-doc criteria`, and `(readme, generated)` untouched.
- **A-7** `plugins/skills-kit/skills/md-domain/references/standards/references-standards.md`
  -- T22 in the "the **authoring lane** applies it when writing any doc" sentence.
- **A-8** `plugins/skills-kit/skills/md-domain/references/standards/coverage-standards.md`
  -- no verb edits (its only hit is the `references/authoring-standards.md` pointer,
  refusal R1). **Add the report-only non-licence sentence** required by decision 3 --
  see A-14.
- **A-9** `plugins/skills-kit/skills/md-domain/references/lanes/coverage-lane.md`
  -- T23 in "Unlike `audit` and `author`" and in "This verb NEVER runs as part of an
  `audit` or `author` invocation". Add the non-licence sentence per A-14.
  **Exclude the `.tmp.*` sibling from every pathspec.**
- **A-10** `plugins/skills-kit/skills/md-domain/references/cohesion-principles.md`
  -- T15 in the "the authoring lanes reached via `/md-domain author skill|...`"
  sentence and T22 in the same sentence. M1 for the `- id: generated_artifact` role
  and every `generated artifact` prose mention inside that role's `ccp_role` /
  `crp_role` / `adp_role` / `identification` / `audit_rules`; M2 for the
  `provenance_named` rule's parent id if spelled out. Leave
  `authoring-patterns/content-authoring.md`, `skill-authoring/CLAUDE.md` examples,
  `authored docs`, and `"authoring a .fbs"` untouched.
- **A-11** `plugins/skills-kit/skills/md-domain/workflow/coverage-detect.js`
  -- T23 in "of the verb being listed apart from audit and author" and T24 in
  "(the AUTHORING direction's list of what is worth writing up)".
  This file is HAND-AUTHORED, not generated -- edit it directly. Do not touch
  `authoritative source`.
- **A-12** `plugins/skills-kit/scripts/gen_workflow_js.py`
  -- T1 in the two occurrences of "routed to the md-domain author_skill lane" and
  "// author_skill lane by the main loop". **Then regenerate** (section D).
  Do NOT hand-edit `workflow/project-doc-remediate.js`.
- **A-13** `plugins/skills-kit/skills/md-domain/references/provenance/skill-authoring-decisions.md`
  -- amend `dec_20_audit_and_generation_vocabulary`. Its final paragraph currently
  begins "The RENAME ITSELF IS UNDECIDED and out of scope." and asserts that the verb
  token stays `author`, the lane ids stay `author_*`, and the role stays
  `generated_artifact`. **Replace that paragraph** with a record that the rename was
  RATIFIED on 2026-08-10 and executed: the verb token and lane ids became
  `generate` / `generate_*`, the bound procedure became `generation-lane.md`, the
  machine-emitted sense took the `machine_emitted*` qualifier across
  `cohesion-principles.md` and the code-review bundle, and the two things that did NOT
  move are `"kind": "md_audit"` (persisted consumer ledger state) and
  `references/audit-framework.{md,yaml}` (cross-plugin path API). Keep the KNOWN
  UNRESOLVED COLLISION paragraph but retitle it as RESOLVED and state the chosen term.
  Keep the four definitions and THE BOUNDARY GUARD paragraph verbatim.
  Also fix M1/M3/M4/M5/M6 where that record quotes the old bundle field names, and
  T1 where it quotes `author_*`.
- **A-14** The report-only non-licence (decision 3, must be WRITTEN not inferable).
  Add this sentence, verbatim, in TWO places -- near the top of
  `references/lanes/coverage-lane.md` (in the paragraph that frames coverage as the
  discovery phase) and in `references/standards/coverage-standards.md` beside the
  existing "Coverage is report-only" statement:

  > Coverage is the discovery phase OF generation, and being re-homed under generation
  > grants it no licence to write. It proposes a destination; it never creates or edits
  > one. The generation lane is the only surface that writes.

  If a sentence of equivalent force is already present at either site (the concurrent
  unit may have landed one), leave it and do not add a second. This is the one row
  where you must compare meaning rather than tokens; if unsure, leave the existing
  text and report it.
- **A-15** `plugins/skills-kit/README.md` -- T14, T27, and T22/T23 throughout
  ("skills-kit is authoring and auditing tooling", "pick a verb (`audit` or `author`)",
  "**author** guides writing them", "a rule the authoring guidance does not teach",
  "an authoring -> auditing -> review path", "it authors and audits the standards",
  "audit or author a SKILL.md").
- **A-16** `plugins/skills-kit/CLAUDE.md` -- T27 in the opening sentence, T22/T23 in
  `plugin_surface_overview` ("read in BOTH directions (detecting for audit, producing
  for authoring)", "Same doc for both verbs -- read detecting for an audit, producing
  for authoring" in `which_surface_for_which_task`), and in `invocation_paths`
  ("audit or authoring intent over project markdown"). Leave the
  `references/authoring-standards.md` and `references/authoring-patterns/` bullets
  and the `/md-audit` / `/md-authoring` history untouched.
- **A-17** `plugins/skills-kit/skills/md-domain/CLAUDE.md` -- verb-prose only; the
  coverage insight is already amended. Search `author_`, `author x`, `authoring lane`.
- **A-18** `tests/skills-kit/test_domain_members_resolve.py` -- T1/T2/T3 and T21 in
  `EXPECTED_LANES`; T5..T8 in `test_markdown_table_matches_the_lane_records` (both the
  `"author x references" in rows` assertion and the failure message
  `"the author x references row gained a lane id"`); T19; T20; T11 in the
  `assert record["procedure"] == ...` line; and the `record.get("verb") == "author"` /
  `record["verb"] != "author"` comparisons.
- **A-19** `tests/skills-kit/test_coverage_workflow_contract.py` -- the module
  docstring says "The coverage verb is a REPORT-ONLY third verb". Reword to "the
  discovery phase of generation" (B-row prose). **Do not change
  `test_generator_does_not_own_a_coverage_lane`** -- it asserts the string `coverage`
  is absent from `gen_workflow_js.py`, and packet A does not add one.
- **A-20** Golden corpus: **no re-record needed, and say so deliberately.** Verified:
  `tests/skills-kit/golden_corpus/` contains no `author_*` lane id, no `verb: author`,
  and no `generated_files`/`generated_axis`/`generated_artifact`. Its only `author`
  hits are the English words `authoring`, `authorial`, `hand-authored`, all in the
  human-writer sense (over-match list). Its `"generated"` hits are the bare boolean
  field (refusal R4). Record this no-op explicitly in the commit message, per the
  `contracts_preserved_verbatim_through_the_fold` discipline -- do not skip it silently.

- **A-21 (ADDED 2026-08-10 during execution -- the spec's own file list missed it).**
  `plugins/skills-kit/skills/update-documentation/SKILL.md` carries two LIVE,
  copy-pasteable T14/T15 command strings (`/md-domain author skill`,
  `/md-domain author claude-md`). Packet A's file list enumerated only `md-domain/**`
  plus the plugin's README and the two CLAUDE.md files, so a SIBLING skill in the same
  plugin naming md-domain's commands fell outside every row. Root cause worth carrying
  forward: the packet was scoped by DIRECTORY, while the token's blast radius is the set
  of files that NAME the verb -- which crosses skill boundaries inside a plugin. The
  `rg -n "/md-domain author"` sweep in E.5 is what caught it, which is precisely why that
  sweep is repo-wide rather than packet-scoped. Check `knowledge-encoding/SKILL.md` and
  `materialized-output/SKILL.md` the same way before declaring the packet done.

Packet A verification:

```
uv run --extra dev pytest tests/skills-kit/ -q
uv run python plugins/skills-kit/scripts/gen_workflow_js.py --check
```

### Packet B -- bootstrap (`bootstrap_lib/code_review/`)

Files: `plugins/bootstrap/bootstrap_lib/code_review/generated.py`,
`generated_paths.py`, `pipeline.py`;
`tests/bootstrap/code_review/test_generated.py`, `test_generated_paths.py`,
`test_pipeline.py`; `plugins/bootstrap/.claude-plugin/plugin.json`;
`plugins/bootstrap/pyproject.toml`.

- **B-1** `git mv` per M8 and M9; `git mv` the two test files per M13 and M14.
- **B-2** In the renamed modules and in `pipeline.py`: M10, M11, M12 (imports and
  docstring references), M7 (the `review_generated` parameter and locals through
  `assemble_bundle`), and M4/M5 for the keys written onto each entry.
  Leave `detect_signature`, the signature LABEL strings, and every detection regex
  alone (over-match list).
- **B-3** **THE DUAL-EMIT SHIM -- mandatory, this is the packet's whole risk.**
  `assemble_bundle` must write BOTH keys on the result dict for one release:

  ```python
  if machine_emitted_files:
      result["machine_emitted_files"] = machine_emitted_files
      # Compat alias for git-kit/p4-kit versions predating the rename. The two
      # kits are versioned independently of bootstrap, so a consumer running an
      # older prepare_review.py would otherwise read a missing key, drop the
      # "not reviewed" section, and silently lose files that are ALREADY excluded
      # from the diff chunks. Remove only per section H.2.
      result["generated_files"] = machine_emitted_files
  ```

  Per-entry keys carry both spellings the same way: write
  `machine_emitted_axis` / `machine_emitted_signature` AND keep `generated_axis` /
  `generated_signature` with identical values.
  Accept BOTH `review_machine_emitted` and a deprecated `review_generated` keyword on
  `assemble_bundle`; when both are passed and disagree, raise `TypeError` rather than
  guessing.
- **B-4** `tests/bootstrap/code_review/test_pipeline.py` -- rename assertions to the
  new keys AND add one test per key asserting the compat alias is present with an
  equal value, so the shim cannot be dropped by accident.
- **B-5** Version bump -- see section F.
- **B-6** `plugins/bootstrap/skills/bootstrap/references/` -- search
  `code_review.generated` and `generated_files` and update any hit. (None found at
  spec time; run the search anyway, the tree is shared.)

Packet B verification:

```
uv run --extra dev pytest tests/bootstrap/code_review/ -q
```

### Packet C -- git-kit AND p4-kit (one packet, they cannot be split)

They are ONE packet because `scripts/gen_code_review_skills.py` renders BOTH kits'
`SKILL.md` AND both `references/submit-gates.md` from one template, and
`tests/bootstrap/code_review/test_skill_drift.py` pins all four to byte-identity.
Editing one kit's SKILL.md alone is impossible.

Files:
- `scripts/gen_code_review_skills.py` (**the generator -- the only place the two
  SKILL.md files and the two submit-gates.md files may be edited**)
- `plugins/git-kit/scripts/prepare_review.py`
- `plugins/p4-kit/scripts/prepare_review.py`
- `plugins/git-kit/skills/git-code-review/references/md-domain-review.md` and
  `plugins/p4-kit/skills/p4-code-review/references/md-domain-review.md` (search
  `generated_files`, `author`; these are NOT generated, edit directly)
- rendered outputs (regenerated, never hand-edited): both `SKILL.md`, both
  `references/submit-gates.md`

Edits:
- **C-1** In `scripts/gen_code_review_skills.py`: M3, M4, M5, M6, M15, and change
  "review of generated output belongs on the generator" to "review of machine-emitted
  output belongs on the generator". Change "when a changed file was detected as
  machine-generated" to "when a changed file was detected as machine-emitted".
  Leave "the AUTHOR's call" (a person) untouched.
- **C-2** In both `prepare_review.py`: M6 (the `--review-generated` literal in
  `_parse_args`, the usage strings, and both docstrings), M7, and the bundle
  passthrough. The passthrough MUST be tolerant of both bootstrap spellings:

  ```python
  emitted = core.get("machine_emitted_files") or core.get("generated_files")
  if emitted:
      bundle["machine_emitted_files"] = emitted
  ```

  This is what makes packet C order-free against packet B.
- **C-3** **Regenerate** (section D). Do not hand-edit the four rendered files.
- **C-4** `tests/git-kit/test_prepare_review_git.py` and
  `tests/p4-kit/test_prepare_review.py` -- rename the flag literal, the kwarg, the
  bundle key assertions, and the test function names (`test_review_generated_flag`
  -> `test_review_machine_emitted_flag`, etc.).
- **C-5** Version bumps -- see section F (both fold into their unshipped versions).

Packet C verification:

```
uv run python scripts/gen_code_review_skills.py
uv run --extra dev pytest tests/bootstrap/code_review/test_skill_drift.py -q
uv run --extra dev pytest tests/git-kit/ tests/p4-kit/ -q
```

### Packet D -- prototypes

One token. `plugins/prototypes/skills/claude-explorer/SKILL.md`, the
`out_of_scope` entry `"skill authoring (use /md-domain author skill)"` -> T14, giving
`"skill generation (use /md-domain generate skill)"`.

Do NOT touch that file's `audit-framework.md` / `audit-framework.yaml` references
(decision 5) nor `plugins/prototypes/bootstrap.json`'s `$comment`.

Version bump required (0.3.0 IS on master) -- see section F.

Packet D verification:

```
uv run --extra dev pytest tests/repo-scripts/ -q
grep -rn "md-domain author" plugins/prototypes/    # must return nothing
```

### Packet E -- repo docs (non-shipping)

`docs/bootstrap/reference/case-studies/p4-kit.md` -- the table row citing
`generated_files` and `bootstrap_lib.code_review.generated.detect_generated`. Apply
M3, M10, M11. Also `docs/planning/md-domain-coverage-lane/*` still says "third verb";
that is a HISTORICAL planning record of a superseded decision -- **leave it**, and see
refusal R6.

No version bump (`docs/` does not ship). Parallel-safe with everything.

Packet E verification: `grep -rn "code_review.generated\b" docs/` returns nothing.

### Packet independence summary

| Packet | Touches | Parallel-safe with |
|---|---|---|
| A skills-kit | `plugins/skills-kit/**`, `tests/skills-kit/**` | B, C, D, E |
| B bootstrap | `plugins/bootstrap/bootstrap_lib/code_review/**`, `tests/bootstrap/code_review/test_{generated,generated_paths,pipeline}.py` | A, C, D, E |
| C git+p4 | `scripts/gen_code_review_skills.py`, `plugins/{git,p4}-kit/**`, `tests/{git,p4}-kit/**`, `tests/bootstrap/code_review/test_skill_drift.py` | A, B, D, E |
| D prototypes | `plugins/prototypes/skills/claude-explorer/SKILL.md` | A, B, C, E |
| E docs | `docs/bootstrap/reference/case-studies/p4-kit.md` | A, B, C, D |

All five are parallel-safe. B and C both live under `tests/bootstrap/code_review/`
but touch DISJOINT files there (B: `test_generated*.py`, `test_pipeline.py`;
C: `test_skill_drift.py`).

---

## C. Ordering and cross-plugin coupling

**There is exactly one cross-plugin coupling and it is a silent-failure risk.**

`bootstrap_lib.code_review.pipeline.assemble_bundle` (packet B, shipped by the
**bootstrap** plugin) produces the bundle key that
`plugins/{git,p4}-kit/scripts/prepare_review.py` (packet C, shipped by **git-kit** and
**p4-kit**) reads. The three plugins are versioned and cached INDEPENDENTLY, and
git-kit/p4-kit import `bootstrap_lib` at runtime through the shared-lib `.pth`, so a
consumer machine routinely runs a NEW bootstrap against an OLD git-kit.

The failure if the rename is landed naively: pipeline stops emitting
`generated_files`; the old `prepare_review.py` reads a missing key; the bundle loses
its `machine_emitted_files` list; the SKILL.md renders no
`## Machine-emitted artifacts (not reviewed)` section -- while those files remain
EXCLUDED from `diff_chunks` and `changed_files`. Result: files silently vanish from
the review with no honest line and no verdict. That is exactly the fake-gate failure
mode this task's CLAUDE.md forbids.

**Mitigation (mandatory, both halves):**
1. B-3 dual-emit: bootstrap writes new AND old keys.
2. C-2 tolerant read: the kits read new-key-or-old-key.

With both in place, **no ordering constraint exists.** B and C may land in either
order, in either publish, on any version skew. Removal of the shim is section H.2 and
is NOT part of this sweep.

**Drift tests that pin byte-identity between a generator and its output:**

| Drift test | Pins | Owning packet |
|---|---|---|
| `tests/skills-kit/test_workflow_js_drift.py` | `md-domain/workflow/*-remediate.js` == render of `plugins/skills-kit/scripts/gen_workflow_js.py` | A |
| `tests/bootstrap/code_review/test_skill_drift.py` | both `*-code-review/SKILL.md` AND both `references/submit-gates.md` == render of `scripts/gen_code_review_skills.py` | C |
| `tests/bootstrap/test_bootstrap_guard.py` | vendored `bootstrap_guard.py` copies == canonical | none (untouched) |

`test_workflow_js_drift.py` also asserts
`set(gen.remediate_targets()) == {"audit_skill", "audit_claude_md", "audit_project_doc", "audit_references"}`.
Those are AUDIT lane ids and are UNCHANGED -- do not edit that assertion.

---

## D. Generated files -- edit the generator, never the output

Hand-editing any file in this table is forbidden.

| Generated file | Generator to edit | Regeneration command |
|---|---|---|
| `plugins/skills-kit/skills/md-domain/workflow/skill-remediate.js` | `plugins/skills-kit/scripts/gen_workflow_js.py` | `uv run python plugins/skills-kit/scripts/gen_workflow_js.py` |
| `.../workflow/claude-md-remediate.js` | same | same |
| `.../workflow/project-doc-remediate.js` (carries T1 twice) | same | same |
| `.../workflow/references-remediate.js` | same | same |
| `plugins/git-kit/skills/git-code-review/SKILL.md` | `scripts/gen_code_review_skills.py` | `uv run python scripts/gen_code_review_skills.py` |
| `plugins/p4-kit/skills/p4-code-review/SKILL.md` | same | same |
| `plugins/git-kit/skills/git-code-review/references/submit-gates.md` | same | same |
| `plugins/p4-kit/skills/p4-code-review/references/submit-gates.md` | same | same |
| `.claude-plugin/marketplace.json` | `scripts/regen_marketplace.py` | `uv run python scripts/regen_marketplace.py` |
| repo-root `index.html` | awesome-kit `plugin-ecosystem` via `scripts/publish.py` | do NOT run by hand; `publish.py` owns it |

**Hand-authored, edit directly (do not confuse with the above):**
`workflow/coverage-detect.js`, `workflow/skill-detect.js`,
`workflow/claude-md-detect.js`, `workflow/project-doc-detect.js`,
`workflow/references-classify.js`.

---

## E. Verification plan

### E.1 Capture a BEFORE baseline first (do this before any edit)

"No NEW failures" must be provable, not asserted. From a clean checkout of the
pre-edit tree:

```
mkdir -p dev/tasks/md-domain-review-enablement/baseline
uv run --extra dev pytest tests/skills-kit/ tests/bootstrap/code_review/ \
    tests/git-kit/ tests/p4-kit/ tests/repo-scripts/ \
    -q -p no:randomly \
    > dev/tasks/md-domain-review-enablement/baseline/pytest-before.txt 2>&1 || true
uv run --extra dev pytest tests/skills-kit/ tests/bootstrap/code_review/ \
    tests/git-kit/ tests/p4-kit/ tests/repo-scripts/ \
    -q -p no:randomly --tb=no -rf \
    | grep '^FAILED' | sort \
    > dev/tasks/md-domain-review-enablement/baseline/failures-before.txt || true
```

The tree is SHARED with other sessions, so the baseline may already be red. That is
fine and is exactly why it is captured. Note the git SHA in the baseline file.

**`tests/p4-kit` and `tests/git-kit` gotcha:** their `conftest.py` sets
`_BOOTSTRAP_GUARD_VENV_REEXEC=1`. Never invoke a single test FILE from those dirs by
absolute path in a way that skips the package conftest -- a missing guard produces
**exit 0, no output, zero tests run: a false green**. Always pass the DIRECTORY.

### E.2 After the sweep

```
uv run --extra dev pytest tests/skills-kit/ tests/bootstrap/code_review/ \
    tests/git-kit/ tests/p4-kit/ tests/repo-scripts/ \
    -q -p no:randomly --tb=short -rf \
    | grep '^FAILED' | sort \
    > dev/tasks/md-domain-review-enablement/baseline/failures-after.txt || true
diff dev/tasks/md-domain-review-enablement/baseline/failures-before.txt \
     dev/tasks/md-domain-review-enablement/baseline/failures-after.txt
```

A clean `diff` (or only REMOVED lines) is the pass condition. Any ADDED line is a
regression this sweep caused.

Also confirm the collected test COUNT did not fall -- a drop with no failures means a
conftest or an import silently ate a package.

### E.3 Drift checks

```
uv run python plugins/skills-kit/scripts/gen_workflow_js.py --check
uv run --extra dev pytest tests/bootstrap/code_review/test_skill_drift.py \
    tests/skills-kit/test_workflow_js_drift.py -q
```

### E.4 The `merge_gate_convention` re-audit (REQUIRED)

Packet A edits standards docs (`project-doc-standards.md`, `claude-md-standards.md`,
`skill-standards.md`, `references-standards.md`) and `cohesion-principles.md`, which
triggers the gate in `plugins/skills-kit/CLAUDE.md` (`merge_gate_convention`). Run it
verbatim as that insight specifies, to ZERO FAILs:

```
for f in plugins/*/skills/*/SKILL.md \
         plugins/skills-kit/skills/md-domain/CLAUDE.md \
         plugins/skills-kit/skills_kit_lib/CLAUDE.md \
         plugins/skills-kit/CLAUDE.md \
         CLAUDE.md; do
  (cd plugins/skills-kit && uv run python -m skills_kit_lib.audit --config "../../$f")
done
```

Do not hardcode the SKILL.md count; the glob is the contract. Capture a BEFORE run of
this loop too, by the same reasoning as E.1.

### E.5 Residual-token sweeps (must all return NOTHING)

```
rg -n "author_skill|author_claude_md|author_project_doc" plugins/ tests/ scripts/ docs/
rg -n "verb: author|\"verb\": \"author\"" plugins/ tests/
rg -n "author x (skill|claude-md|project-doc|references)" plugins/ tests/
rg -n "authoring-lane|authoring_lane" plugins/ tests/ scripts/ docs/
rg -n "/md-domain author" plugins/ tests/ scripts/ docs/
rg -n "generated_files|generated_axis|generated_signature" plugins/ tests/ scripts/ docs/
rg -n "generated_artifact" plugins/ tests/ scripts/
rg -n "review-generated|review_generated|detect_generated" plugins/ tests/ scripts/ docs/
rg -n "code_review\.generated" plugins/ tests/ scripts/ docs/
```

**Two sweeps have EXPECTED survivors** -- they are the compat shim and are correct:
- `generated_files` / `generated_axis` / `generated_signature` survive inside
  `bootstrap_lib/code_review/pipeline.py` (the B-3 dual-emit block plus its comment),
  its tests, and the `core.get("generated_files")` fallback in both
  `prepare_review.py`. Nowhere else.
- `review_generated` survives only as the deprecated keyword alias in
  `assemble_bundle`'s signature, and as the kwarg both kits PASS at their
  `assemble_bundle` call site (the C-2 call-site half -- see H.2's ordering constraint).

**A third survivor class, found while executing (2026-08-10): the provenance records.**
`references/provenance/skill-authoring-decisions.md` and
`references/provenance/cohesion-principles-decisions.md` quote the OLD tokens on purpose --
A-13 explicitly requires the `dec_20` record to say what the names USED to be. A provenance
log that silently adopted the new spelling would destroy the very supersession it exists to
record. Expected survivors there: `generated_artifact` (as a `keywords:` search-routing entry
and inside "now `machine_emitted_artifact`" parentheticals), `generated_files` /
`generated_axis` / `generated_signature` / `--review-generated` in the same historical list,
and `(formerly \`authoring-lane.md\`)`. Do NOT convert these; the sweep's "must return
NOTHING" applies to LIVE declarations, never to a historical quotation in a provenance log.

Then a manual pass over `rg -n "\bauthor" plugins/skills-kit/` checking every hit
against the over-match list in A.3.

### E.6 Pre-commit gates

`scripts/pre-commit-version-check.sh` (which chains
`scripts/check_bootstrap_dependency.py`) and `scripts/check_pyproject_sync.py` run at
commit. Do not `--no-verify`.

---

## F. Version bumps

**Verified against `origin/master`'s `.claude-plugin/marketplace.json` and
`git log origin/master..dev` on 2026-08-10.** `origin/master..dev` holds four commits,
the oldest being `0f7eb1c skills-kit 0.44.0 / git-kit 0.13.0 / p4-kit 0.27.0`.

| Plugin | master | dev | Shipped? | Action |
|---|---|---|---|---|
| skills-kit | 0.43.1 | 0.44.0 | **no** | **Fold in. No bump.** |
| git-kit | 0.12.0 | 0.13.0 | **no** | **Fold in. No bump.** |
| p4-kit | 0.26.0 | 0.27.0 | **no** | **Fold in. No bump.** |
| bootstrap | 0.77.3 | 0.77.3 | **YES** | **Bump to 0.78.0** (minor: new bundle field + renamed public modules/function) |
| prototypes | 0.3.0 | 0.3.0 | **YES** | **Bump to 0.3.1** (patch: one command string) |
| awesome-kit | 0.26.1 | 0.26.1 | YES | untouched |

**The version-bump-in-place assumption HELD for all three named plugins.**

Mechanics:
- Move together, always: `plugins/<name>/.claude-plugin/plugin.json` `version`,
  `plugins/<name>/pyproject.toml` `version`, and the regenerated
  `.claude-plugin/marketplace.json`.
- Only bootstrap and prototypes need edits; skills-kit / git-kit / p4-kit already read
  0.44.0 / 0.13.0 / 0.27.0 in all three places -- **verify, change nothing.**
- After bumping bootstrap and prototypes:
  `uv run python scripts/regen_marketplace.py` -- never hand-edit marketplace entries.
- Do NOT publish. A publish is the user's call.

---

## G. The CV-8 boundary guard -- what this spec deliberately does NOT do

Decision 6 is honoured by construction:

- No rename in this spec touches `"code review"` as the name of git-kit's or p4-kit's
  skill, their skill directories, or their commands. A code review remains a
  separately-named SUBTYPE.
- No rename makes md-domain's document work and the kits' diff work share a word.
  md-domain AUDITS DOCUMENTS; the kits REVIEW DIFFS. CV-8 keeps both lexical anchors.
- `CV-8` itself, `coverage-standards.md`'s CV-* ids, and the `coverage-detect.js`
  "If you find yourself enumerating what is wrong with the code..." guard are
  UNTOUCHED.
- The `THE BOUNDARY GUARD` paragraph in `dec_20` is preserved verbatim by A-13.

**One candidate rename was considered and is REFUSED on these grounds** -- see R5.

---

## H. Separately-marked OPTIONAL items (never fold into the main sweep)

### H.1 (OPTIONAL) Backward-compatible migration for `"kind": "md_audit"`

Decision 4 forbids renaming the persisted ledger value. A safe migration is possible
and is specified here ONLY as a separate, independently-shippable item. **Do not
include it in the main sweep. Do not start it without the owner's explicit go.**

Design (read-old / write-new, in `plugins/bootstrap/bootstrap_lib/code_review/ledger.py`):
1. Introduce `MD_AUDIT_KINDS = ("md_document_audit", "md_audit")` -- new canonical
   first, legacy second.
2. Every READ path that currently compares `entry["kind"] == "md_audit"` compares
   membership in `MD_AUDIT_KINDS` instead. Collapse behaviour is then identical for
   old and new entries.
3. Every WRITE path emits `"md_document_audit"`.
4. Both SKILL.md comments (rendered from `scripts/gen_code_review_skills.py`) state
   that the legacy value is still honoured on read.
5. Tests: one asserting an on-disk ledger containing a legacy `md_audit` entry still
   collapses a matching finding; one asserting new writes use the new value.
6. Never remove the legacy read. There is no safe point at which every consumer
   ledger on every machine has aged out.

Cost/benefit note for the owner: the only defect the rename fixes is that the token
echoes the DELETED `/md-audit` skill. The prose fix for that is row D5 (already owned
by plan item `inherited-stale-md-audit-prose`), which costs nothing and carries no
persisted-state risk. **Recommendation: do not do H.1.**

### H.2 (OPTIONAL, LATER) Retire the dual-emit compat shim

Only after bootstrap >= 0.78.0 and git-kit/p4-kit carrying C-2 are both published AND
enough time has passed that no consumer is plausibly on an older kit. Remove the
`result["generated_files"]` alias, the per-entry alias keys, the
`review_generated` deprecated kwarg, and the `or core.get("generated_files")`
fallbacks; delete the alias tests. This is a separate change with its own version
bumps. It is NOT part of this sweep and has no deadline.

**ORDERING CONSTRAINT ADDED 2026-08-10 (C-2's second half).** Both kits' CALL INTO
`assemble_bundle` deliberately passes the OLD kwarg -- `review_generated=review_machine_emitted`
-- because a new kit routinely runs against a published bootstrap predating the rename,
which accepts only that spelling and raises `TypeError` on every review otherwise. So the
deprecated kwarg cannot be removed in one step: retiring it while the kits still pass it
turns the shim's removal into the very crash it exists to prevent. H.2 must be executed as
TWO published changes, in this order: (1) flip both kits' call sites to
`review_machine_emitted=` and publish them; only then (2) remove the deprecated kwarg from
`assemble_bundle`. The `core.get("generated_files")` read fallbacks and the per-entry alias
keys are independent of this ordering and may go in either step.

---

## I. Refusals -- flagged, deliberately not specified

| # | Refused | Why |
|---|---|---|
| R1 | Renaming `references/authoring-standards.md` | It is not md-domain's VERB. It documents how to write an ADDITIVE STANDARDS FILE -- "authoring" there is the generic English verb over a config artifact, not the dispatch verb. It is also referenced by `owner_doc` in `skills_kit_lib/schemas/standards.py` and asserted verbatim in `tests/skills-kit/test_schemas.py`, so a rename is a schema-metadata change for zero disambiguation gain. |
| R2 | Renaming `references/authoring-patterns/` and its six files | `plugins/skills-kit/CLAUDE.md` calls this "the verb-generic content-shape cluster" -- it is explicitly NOT the generation verb, it answers the orthogonal HOW-a-fact-is-shaped question and is read by the audit direction too. It is referenced by `owner_doc` in `skills_kit_lib/schemas/portable.py` (three entries) and by `knowledge-encoding/SKILL.md` and `update-documentation/SKILL.md` (cross-skill paths). Renaming it would make "generation-patterns" imply the cluster belongs to one verb, which is the opposite of its contract. |
| R3 | Renaming the `M_generated_missing_provenance` finding code | Rule ids are opaque handles preserved verbatim by standing decision (`md-domain/CLAUDE.md`), and it is an enum value in `project-doc-detect.js`'s result schema. |
| R4 | Renaming the bare `"generated"` boolean output field of `scripts/discover_project_doc.py` | It is pinned in `tests/skills-kit/golden_corpus/expected/project-doc-signals.json`, so the rename forces a golden re-record; the word never appears adjacent to the verb in that surface, so it creates no ambiguity. Residual inconsistency, flagged for the owner rather than silently swept. |
| R5 | Renaming md-domain's `audit` verb, or the code-review skills, toward a shared word | **REFUSED under decision 6.** The framework unifies audit as a KIND; the SUBJECT still discriminates. Any rename collapsing "document audit" and "code review" into one unqualified word erases CV-8's only lexical anchor and makes the defect-list failure mode a natural reading. Flagged, not specified. |
| R6 | Converting "third verb" in `docs/planning/md-domain-coverage-lane/*` and in `tests/skills-kit/test_coverage_workflow_contract.py`'s design rationale beyond its docstring | The planning docs are a HISTORICAL record of the decision that was superseded; rewriting them destroys the provenance that makes the supersession legible. A19 updates only the live test docstring. |
| R7 | Renaming `references/audit-framework.{md,yaml}`, despite "audit-framework" now being arguably too narrow (it hosts viewer-kinds) | **REFUSED under decision 5** -- consumed by literal path from awesome-kit and prototypes. Prose inside may change; the paths may not. |
| R8 | Renaming `"kind": "md_audit"` in the main sweep | **REFUSED under decision 4.** See H.1 for the optional safe migration. |
| R9 | Adding a `--review-generated` deprecated CLI alias alongside `--review-machine-emitted` | The repo took a clean break on aliases for the md-domain fold; a second precedent should be the owner's call, not an implementer's. Flagged: the flag IS user-typed, so this is a real (small) user-visible break. The internal WIRE key gets a shim (B-3) because silent data loss is a different severity from a flag that errors loudly. |
| R10 | Renaming `plugins/skills-kit/skills/md-domain/references/provenance/skill-authoring-decisions.md` | A provenance-log filename referenced from `standards-decisions.md`; not the verb, and moving it breaks the declared home for framework-vocabulary decisions. |

---

## J. Plugin-opinion razor

Per the root CLAUDE.md submit gate, the opinions this change adds or hardcodes:

1. **`generate` as the user-typed verb token** (replacing `author`). Not a new
   configurable seam. Razor test FAILS: no power-user scenario makes them uninstall or
   remediate -- the verb is discoverable from `/md-domain` bare and from
   `argument-hint`, and natural-language routing still reaches the lanes via
   `invocation_phrasings`. It is a one-time muscle-memory cost, self-explaining on the
   next bare invocation.
2. **`--review-machine-emitted` replacing `--review-generated`.** Razor test FAILS for
   the same reason, with a loud failure mode (an unknown flag errors; it does not
   silently mis-review). Flagged as R9 so the owner can overrule.
3. **The dual-emit compat shim (B-3).** Not a workflow opinion -- it is a correctness
   requirement forced by independent plugin versioning. No seam.

No new configurable opinion is introduced by this spec.

---

## K. Critical-infrastructure disclosure

- **Created:** this file only. It was written to
  `dev/tasks/md-domain-review-enablement/rename-spec.md`, which was then untracked scratch
  under a gitignored `dev/` and the only copy. Both of those facts have since changed and
  the sentence is kept only as provenance: `cc1a5d5d` promoted this document to
  `docs/reference/terminology/rename-spec.md`, where it is tracked. The task folder
  still exists under its original name; the tracked copy is authoritative.
- **Changed:** nothing. No file under `plugins/`, `tests/`, `scripts/`, or `docs/` was
  edited. No rename was applied. No generator was run.
- **Git:** no branch created, no branch switched, no `git add`, no commit, no push.
  The shared index was not touched. `git fetch origin` was run (read-only refs update).
- **Reads:** read-only throughout, plus `git show origin/master:.claude-plugin/marketplace.json`
  written to the session scratchpad (outside the repo).
- **Observed but not touched:** a foreign editor temp file
  `plugins/skills-kit/skills/md-domain/references/lanes/coverage-lane.md.tmp.8400.c328dfc53045`
  from a concurrent session.
