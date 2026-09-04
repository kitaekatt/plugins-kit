# Human HTML standards

This document defines the `human-html` analysis, generation, and validation contract. Load it for one directory when md-domain routes either human HTML lane.
It is the single source of truth for page warrant, records, generated HTML, shared styling, and the host viewer integration.

Normative levels are **REQUIRED**, **CONDITIONALLY REQUIRED**, and **PROHIBITED**. A failed required or prohibited rule is a `FAIL` unless the rule assigns `INFO`.
The `human.html` and reference HTML files are machine-emitted artifacts.
Apply `references/standards/project-doc-standards.md` rule PD-10 only for their provenance role. Apply `references/standards/skill-standards.md` rules SR-1 through SR-4 to this reference document.

Contents: 1 artifact declaration (AD) -- 2 human coverage criteria (HC) --
3 decision record (DR) -- 4 page contract (PC) -- 5 browser-resolved access
(NF) -- 6 reference documents (RD) -- 7 style asset (SA) -- 8 size signal (SZ)
-- 9 script contracts (CK) -- 10 tree-scale order (TS) -- 11 host viewer
contract (HV) -- 12 proving corpora (PV).

## 1. Artifact declaration

### AD-1. Dedicated scalar lanes

- **Level:** REQUIRED
- **Rule:** Register one `human_html_directory` analysis lane and one
`human-html` generation lane. Keep `coverage_code_subtree.subject` scalar and
unchanged. The analysis lane stays report-only. The generation lane runs for every
analyzed directory, deepest first, persists `page` and `none` records alike,
and generates or removes HTML to match the decision. The unit of execution is
analyze-then-generate per directory.
- **Rationale:** Dedicated records preserve one axis value per lane. A list-valued second subject was rejected because the registry test requires a scalar subject.
- **Test:** The dispatch table, lane records, argument grammar, and registry
test all name both routes without changing the legacy route meanings.

The lane records have this target shape:

```yaml
- id: coverage_human_html_directory
  verb: analyze
  subject: human_html_directory
  standards: references/standards/human-html-standards.md
  procedure: references/lanes/coverage-lane.md
  discover_script: scripts/discover_human_html.py
  verdicts: [PAGE-WARRANTED, NO-PAGE]
  report_only: true
  invocation_phrasings:
    - "analyze this directory for human html"
    - "decide whether this directory needs a human page"
    - "assess the human browsing experience here"
    - "find what an orientation page needs to explain"
  change_driver: >-
    Changes when the human coverage criteria, decision-record contract, or
    bottom-up analysis procedure changes.
- id: generate_human_html
  verb: generate
  artifact: human-html
  standards: references/standards/human-html-standards.md
  procedure: references/lanes/generation-lane.md
  contract_script: scripts/human_html_check.py
  verdicts: [COMPLIANT, NON-COMPLIANT]
  input_provenance: coverage
  regeneration: replace-generated
  invocation_phrasings:
    - "generate human html from this analysis"
    - "regenerate this directory's human page"
    - "write the warranted human pages"
    - "refresh the human html tree"
  change_driver: >-
    Changes when the page, record, style, navigation, or regeneration
    contract changes.
```

### AD-2. Explicit grammar

- **Level:** REQUIRED
- **Rule:** Route `analyze human-html <directory> [--json]` to
`coverage_human_html_directory`. Route
`generate human-html <directory> [--coverage <path>]` to
`generate_human_html`. Keep `analyze <directory>` and
`generate claude-md <directory>` unchanged.
- **Rationale:** An explicit selector avoids guessing between two analysis subjects. Changing the legacy defaults was rejected because it alters established calls.
- **Test:** Each invocation selects one lane. The human analysis output is the
only accepted coverage input for human generation.

### AD-3. Registry integrity update

- **Level:** CONDITIONALLY REQUIRED -- when the lane records are implemented
- **Rule:** Extend the exact lane roster and dispatch-table expectations in `tests/skills-kit/test_domain_members_resolve.py`.
Generalize its single-lane generate, analyze, and regeneration assertions to select the applicable lane by id. Preserve
the scalar axis check, path resolution, three-phrasing minimum, nonempty change driver, and producing-provenance check.
Extend `test_markdown_table_matches_the_lane_records` with the dispatch-table
row `generate x human-html` and an analyze row whose `table_key` names the
non-artifact lane.
- **Rationale:** The test must recognize the added lanes without weakening the invariants that made a list-valued subject invalid.
- **Test:** The registry-integrity test passes with both new records and fails
for a missing path, missing phrase, empty driver, or mixed axis.

## 2. Human coverage criteria

The analysis subject is one directory and its source subtree. Evidence can come from code, project guidance, documentation, data, assets, configuration, and finished descendant records.
Project guidance is evidence, not the source model. The analysis applies HC criteria, not the CV criteria in `references/standards/coverage-standards.md`.

### HC-1. Checkable page warrant

- **Level:** REQUIRED
- **Rule:** Start at a plain directory listing with default file previews
(any file browser, or the host viewer) and no human page.
Use only listed files, normal links, and default previews. Do not use search,
a terminal, hidden `.databench/` data, or prior notes. For each applicable
question, identify one direct and coherent answer in displayed material:

1. What is this directory for?
2. Why does it have this shape?
3. What will hurt the reader?
4. Where does the reader go next?

Record a reason for each question that does not apply. If all applicable
answers exist, choose `none`. If an answer is materially absent or fragmented,
apply HC-2. Choose `page` only when HC-2 admits at least one unit. A `none`
decision is a normal result.
- **Rationale:** A repeatable browsing exercise makes page warrant observable. Intuition alone was rejected because it cannot distinguish a useful page from decoration.
- **Test:** The analysis report records each answer or gap, each inapplicable
reason, the HC-2 result, and the final `page` or `none` decision.

### HC-2. Orientation-first admission

- **Level:** REQUIRED
- **Rule:** Admit a content unit only when it answers one HC-1 question and
reduces reacquisition work. It must state why the fact matters and cite
repository evidence. Give priority to purpose and boundaries, structural reasons,
non-obvious hazards, and concrete entry points. Exclude file inventories,
source paraphrases, facts already clear in plain browsing, unsupported
inference, and duplicated project guidance.
- **Rationale:** Orientation earns page space. A source tour was rejected because the host viewer already exposes files.
- **Test:** For every admitted unit, the report gives `question`, `claim`,
`why`, `evidence`, and `plain_browsing_gap`. An omitted field rejects the unit.

### HC-3. Reader order

- **Level:** REQUIRED
- **Rule:** Write first for an owner returning after context loss. Add only the
extra context that a newcomer needs to use the same orientation path.
- **Rationale:** The returning owner defines the durable reading task. A newcomer-only tutorial was rejected because it obscures reacquisition cues.
- **Test:** Each admitted unit helps the returning owner, or adds a necessary
definition beside a unit that does.

### HC-4. Repository research

- **Level:** REQUIRED
- **Rule:** Research the actual directory subtree before deciding. Treat every
repository input type as eligible evidence. Do not derive the page from a
project-guidance file or from filename patterns alone.
- **Rationale:** The page explains the repository as it exists. Guidance-only generation was rejected because guidance can omit or lag important structure.
- **Test:** The report cites inspected evidence from the subtree and separates
observed facts from inference.

## 3. Decision record

### DR-1. One record per directory

- **Level:** REQUIRED
- **Rule:** Store one JSON-compatible YAML 1.2 record at
`.databench/human/<relative-directory>/decision.yaml`, written in JSON syntax
so a stdlib JSON parser reads it. Use
`.databench/human/decision.yaml` for repository root. Normalize child paths to
POSIX separators. Use `.` as the root value inside the record.
- **Rationale:** One record keeps decision, identity, instructions, and references atomic. Separate files were rejected because their shared change cadence permits drift.
- **Test:** The record parses as both YAML 1.2 and JSON, maps to exactly one
repository directory, and contains these fields:

```json
{
  "schema_version": 1,
  "directory": "src/example",
  "decision": "page",
  "source_sha": "0123456789abcdef0123456789abcdef01234567",
  "dirty": false,
  "identity": "The subsystem that validates example inputs.",
  "instructions": "",
  "references": [
    {"slug": "protocol", "title": "Protocol map", "file": "human.protocol.html"}
  ]
}
```

The fields have these contracts:

- `schema_version` is integer `1`.
- `directory` is `.` or a normalized repository-relative directory path.
- `decision` is `page` or `none`.
- `source_sha` is a full lowercase 40-hex commit id under DR-2.
- `dirty` is a boolean under DR-2.
- `identity` is one nonempty line for `page`. It can be empty for `none`.
- `instructions` is a string. An empty string is valid.
- `references` is an array of unique `slug`, `title`, and `file` mappings. It
  is empty for `none`. It reserves schema growth for reference data.

### DR-2. Subtree source stamp

- **Level:** REQUIRED
- **Rule:** Set `source_sha` to the newest commit at or before `HEAD`. That
commit must have changed a tracked analysis input in the directory subtree. Exclude
`.databench/`, `human.html`, and `human.<slug>.html` from the input set. When an
analysis input has uncommitted content, persist the record with `dirty: true`
and report `INFO DIRTY`; do not block.
- **Rationale:** A subtree source stamp limits staleness to affected branches. `HEAD` was rejected because an unrelated commit stales every record.
- **Test:** Recompute the last-touch commit over the same excluded path set. A
different commit yields `STALE`. Dirty tracked or untracked analysis input
sets `dirty: true` and reports `INFO DIRTY`, because no commit identifies the
judged content.

### DR-3. Schema ownership

- **Level:** REQUIRED
- **Rule:** Define field validation and path mapping in
`skills_kit_lib/schemas/human_html.py`. Expose its stable consumer interface
from `skills_kit_lib.human_html`. Generators, lane scripts, and the host viewer consume that interface.
- **Rationale:** Package ownership gives producers and consumers one schema. Duplicate schema code in the host viewer was rejected because it can drift.
- **Test:** One package implementation validates records for both md-domain and
the host viewer.

### DR-4. Instructions survive regeneration

- **Level:** REQUIRED
- **Rule:** The generator rewrites every record field except `instructions`.
A human or an agent edits `instructions` directly, and every regeneration
reads it as input and writes it back unchanged.
- **Rationale:** The record mixes generated fields with the one human-managed field. Clobbering it was rejected because instructions are the only steering channel for a page nobody hand-edits.
- **Test:** Regenerating a directory whose record carries nonempty
`instructions` leaves that field byte-identical.

## 4. Page contract

### PC-1. Page identity and metadata

- **Level:** REQUIRED
- **Rule:** Generate `human.html` in a directory whose record says `page`.
Include `<!doctype html>`, `<html lang="en">`, UTF-8 `charset`, a responsive
`viewport`, and `<meta name="color-scheme" content="dark">`. Place this JSON marker within the first 20 lines:

```html
<!-- human-html: {"generated_by":"md-domain","source_sha":"<40-lower-hex>","directory":"<relative-path>","kind":"page"} -->
```
- **Rationale:** Stable metadata makes a page portable and attributable. An unmarked generated page was rejected under PD-10.
- **Test:** The marker values match the decision record and the generated file's
directory.

### PC-2. Navigation spine

- **Level:** REQUIRED
- **Rule:** Add one navigation region marked `data-human-html-chrome="nav"`.
Link up to the nearest ancestor whose fresh record says `page`. Link down once to every nearest descendant whose fresh
record says `page`. Traverse through `none` directories and stop a branch at its first page. Repository root has no
up link. Use each target record's identity as its link description. Omit the
descendant section when no down link exists.
- **Rationale:** Nearest-page links keep the tree navigable without exposing no-page gaps. Direct-child links were rejected because they strand skipped directories.
- **Test:** Discovery computes the expected up and down targets. The check script
compares that set with the navigation links.

### PC-3. Announce message

- **Level:** REQUIRED
- **Rule:** Include this viewer-agnostic announce snippet:

```js
if (window.parent !== window) {
  window.parent.postMessage({
    type: "human-html:announce",
    version: 1,
    directory: ".",
    file: "human.html",
    kind: "page",
    source_sha: "<40-lower-hex>"
  }, "*");
}
```

Use the normalized record directory. Use the generated file's basename. The
host viewer validates the message under HV-5.
- **Rationale:** A small viewer-agnostic message lets an enclosing tree follow browser navigation. A viewer-specific API was rejected because standalone pages lack it.
- **Test:** The script sends once after document parsing, sends nothing without a
parent, and matches the page marker.

### PC-4. Shared inline style

- **Level:** REQUIRED
- **Rule:** Inline the exact SA-1 asset bytes in one
`<style data-human-html-style>` element. Do not link a stylesheet.
- **Rationale:** Inline style keeps one-file portability. Copied or linked CSS was rejected because it creates a runtime dependency.
- **Test:** The checker compares the style element with the packaged asset.

### PC-5. Free generated body

- **Level:** REQUIRED
- **Rule:** Outside required chrome, the generator controls structure and
layout. A page built from scratch is valid. Template support is deferred and
never required.
- **Rationale:** Evidence shapes the page. A mandatory template was rejected because repository directories do not share one information shape.
- **Test:** Validation checks the contract, not a body template.

### PC-6. Unsafe or nonportable content

- **Level:** PROHIBITED
- **Rule:** Do not emit `fetch`, `XMLHttpRequest`, an absolute URL, or an
absolute path. Do not emit an external-origin asset, non-ASCII content, or hand-written HTML content.
- **Rationale:** The page must survive every target environment. Network APIs, fixed locations, external dependencies, and mixed authorship were rejected as nonportable.
- **Test:** CK-1 checks generated pages and reference pages for every prohibited
form.

## 5. Browser-resolved cross-file access

### NF-1. No-fetch portability rule

- **Level:** REQUIRED
- **Rule:** Every cross-file read uses a relative URL that the browser resolves
from the HTML file to a repository file. Allowed carriers are `a[href]`, `iframe[src]`,
`script[src]`, and `img[src]`. Same-document fragments are valid. Scripted
network reads are prohibited.
- **Rationale:** Browser-resolved relative URLs let one file work from a file manager, a static host, and the host viewer frame. Fetch-based loading was rejected because file pages block it.
- **Test:** Every `href` and `src` is relative, resolves from its containing
file, and stays inside the repository. No script contains `fetch` or `XMLHttpRequest`.

## 6. Reference documents

### RD-1. Sibling reference names

- **Level:** CONDITIONALLY REQUIRED -- when a page needs a separate reference
- **Rule:** Name each reference `human.<slug>.html` beside `human.html`. A slug
matches `[a-z0-9]+(?:-[a-z0-9]+)*` and is unique in that directory. List the
reference in `decision.yaml` and link it from `human.html`.
- **Rationale:** Dot-separated topic names group machine output without hiding the relation. `human-<topic>.html` was rejected because it weakens that namespace.
- **Test:** The record, main-page link, sibling file, slug, and title agree.

### RD-2. Reference chrome and return path

- **Level:** CONDITIONALLY REQUIRED -- for every reference HTML file
- **Rule:** Apply PC-1, PC-3, PC-4, PC-5, PC-6, and NF-1. Set marker and announce
`kind` to `reference`. Add `"reference":"<slug>"` to the marker and `reference: "<slug>"` to the message. Replace
the page tree spine with one relative backlink to `human.html` in the same
navigation region.
- **Rationale:** Shared chrome preserves identity and portability. A reference without a one-hop return was rejected because it becomes a navigation dead end.
- **Test:** The reference passes the page checks and its backlink resolves to
the sibling main page.

### RD-3. Deferred data-backed references

- **Level:** CONDITIONALLY REQUIRED -- when data-backed references are added
- **Rule:** Keep pure YAML data under the matching `.databench/human/` record
directory. Generate a `.js` twin and load it through relative `script[src]`.
Use the HTML page as the presentation template. Treat data that no generated
page consumes as a defect unless its schema entry declares it non-display.
Defer the data schema, filenames, and template protocol until this phase is
implemented.
- **Rationale:** A generated script twin preserves NF-1 while keeping YAML authoritative. Runtime YAML fetch and unused data were rejected.
- **Test:** The implementation phase adds schema and consumption checks before
it accepts data-backed references.

## 7. Style asset

### SA-1. Package-owned dark style

- **Level:** REQUIRED
- **Rule:** Ship `skills_kit_lib/assets/human-html.css` as package data. The
asset defines dark-only values for background, text, headings, muted text,
surface, border, link, link hover, and accent. It also defines the body
font stack and the monospace font stack; it sets no page width, so a page
fills its viewport. Seed it from the host viewer's established dark theme.
Keep every concrete value in the asset. The asset is ASCII. Expose the asset through
`skills_kit_lib.human_html` for the generator and the host viewer.
- **Rationale:** One packaged asset prevents visual drift. Values copied into this document or the host viewer were rejected as duplicate sources of truth.
- **Test:** A built wheel contains the asset. Both consumers read the same
package resource, and every page contains those exact bytes.

## 8. Size signal

### SZ-1. Visible-word budgets

- **Level:** REQUIRED
- **Rule:** Report the visible-word count for every page. The default budget
is 1,200 words for the repository-root page and 600 words for every other
main page and each reference page; a record's `instructions` may override the
budget for its page. Parse HTML text and
exclude `script`, `style`, `template`, and every subtree marked
`data-human-html-chrome`. Count tokens that match
`[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*`.
- **Rationale:** The budgets target six minutes at root and three minutes elsewhere at 200 words per minute. Line and byte limits were rejected because HTML formatting distorts them.
- **Test:** Report observed words and the applicable budget as `INFO` when the
count exceeds it. Size alone never produces `FAIL`.

## 9. Script contracts

### CK-1. `human_html_check.py`

- **Level:** REQUIRED
- **Rule:** Implement `scripts/human_html_check.py` with `skills_kit_lib.human_html` and Python standard-library imports
only. Keep that package interface free of non-stdlib imports. Accept a repository root and an optional directory.
Support a machine-readable JSON result. Exit nonzero only when at least one `FAIL` exists.

The script reports `FAIL` for:

- an `href` or `src` that is not relative or escapes the repository.
- an `href` or `src` that does not resolve from its file or names a missing fragment.
- `fetch`, `XMLHttpRequest`, a URL scheme, or a protocol-relative URL.
- in an `href`, `src`, or script string literal: a leading slash, a drive or
  UNC path, a hostname, or an external-origin asset.
- non-ASCII bytes.
- a missing, malformed, duplicate, or record-inconsistent generated marker.
- missing or invalid required metadata, navigation, announce code, or inline
  style.
- a missing or invalid decision record.
- `human.html` or a reference file when `decision` is `none`.
- no `human.html` when `decision` is `page`.
- a reference-list, filename, or backlink mismatch; a consumption mismatch
  only once RD-3 is implemented.

The script reports `INFO` for:

- `STALE` when DR-2 recomputes a different `source_sha`, including stale-child
  propagation under TS-2.
- a visible-word count above SZ-1.
- `DIRTY` when the record carries `dirty: true`.
- **Rationale:** One stdlib checker enforces portable output without provisioning a runtime. Treating staleness or size as failure was rejected because both are manual-action signals.
- **Test:** Fixtures cover each `FAIL`, each `INFO`, clean `page`, clean `none`,
root mapping, and reference HTML.

### CK-2. `discover_human_html.py`

- **Level:** REQUIRED
- **Rule:** Implement `scripts/discover_human_html.py` with `skills_kit_lib.human_html` and Python standard-library imports only. Make no writes. Accept a repository root and an
optional directory. Walk non-ignored repository directories. Exclude VCS
metadata, `.databench/`, and directories that contain only generated output.
Emit JSON records in deepest-first order. For each directory, report its normalized path, current DR-2 commit, dirty-input state,
record status, decision, and identity. Also report page and reference files,
nearest page ancestor, nearest page descendants, and stale-child state.
- **Rationale:** Shared discovery keeps navigation and ordering deterministic. Separate generator and checker scans were rejected because they can disagree.
- **Test:** Fixtures cover root, nested pages, skipped `none` directories, stale
records, dirty input, missing records, and multiple descendant branches.

## 10. Tree-scale order

### TS-1. Bottom-up execution

- **Level:** REQUIRED
- **Rule:** Analyze and generate deepest directories first. A parent reads each
finished child decision and each page child's identity line. No-page
directories stay in traversal but do not become navigation targets.
- **Rationale:** Parent navigation and orientation depend on child decisions. Top-down generation was rejected because it guesses unfinished child state.
- **Test:** Discovery order places every descendant before its ancestors, and a
parent uses only finished child records.

### TS-2. Stale-child propagation

- **Level:** REQUIRED
- **Rule:** A stale or missing child record makes every dependent ancestor
`STALE`. Do not finalize or regenerate the parent until the child is fresh.
This execution gate does not change CK-1 staleness from `INFO` to `FAIL`.
- **Rationale:** A parent trusts child identity and decision data. Continuing past stale child data was rejected because it silently corrupts the navigation spine.
- **Test:** Changing a descendant input marks that descendant and each dependent
ancestor stale, then blocks parent generation until bottom-up refresh finishes.

## 11. The host viewer contract

The host viewer consumes only the following surfaces.

| Rule | Level | Host contract | Rationale |
| --- | --- | --- | --- |
| HV-1 | REQUIRED | Depend on the installed `skills-kit` package and import `skills_kit_lib.human_html`. | The package is the integration boundary. Copied schema or style code was rejected because it creates a second owner. |
| HV-2 | REQUIRED | Read records through the package schema and path mapping from DR-3. | Schema-mediated reads keep page decisions and identity lines consistent. Parsing in the host viewer was rejected. |
| HV-3 | REQUIRED | Import the SA-1 package asset for matching chrome in the host viewer. | Importing the asset preserves one visual source. A copied palette was rejected. |
| HV-4 | REQUIRED | Serve repository files through a raw-file route on a separate origin, or use a sandbox that allows scripts without same-origin access. Never execute repository-authored HTML on the application origin. | Repository-authored HTML is an XSS boundary. Trusting it on the application origin was rejected. |
| HV-5 | REQUIRED | Set the frame URL to the raw page URL. Let the browser resolve links without rewriting. Accept `human-html:announce` only from that frame's `contentWindow`. Validate version, kind, normalized directory, relative file, and source SHA against the record before following it. | URL-backed navigation preserves browser semantics. `srcdoc` rewriting and unvalidated messages were rejected. |
| HV-6 | REQUIRED | Keep the instructions affordance present for every directory record. Disable it for empty `instructions`. Enable it for text. | A stable control preserves discoverability. Hiding the control for empty instructions was rejected. |

## 12. Proving corpora and success

### PV-1. Three-corpus proof

- **Level:** REQUIRED
- **Rule:** Prove the system on three repositories of different shapes: a game
engine, a game, and a configuration repo. The owner browses each generated
corpus from root through its page spine and decides when the design is proven.
- **Rationale:** Different repository shapes expose false template and hierarchy assumptions. A single showcase corpus was rejected as insufficient evidence.
- **Test:** For all three corpora, every directory has a fresh decision. Every `page` decision has compliant output,
and every `none` decision has no page. All navigation works by file URL and the host viewer URL. The host viewer tree
follows announcements. The instructions control reflects empty and nonempty values. The owner declares the design proven.
