# Human HTML standards

This document defines the `human-html` analysis, generation, and validation contract. Load it for one territory when md-domain routes either human HTML lane.
It is the single source of truth for page warrant, records, generated HTML, shared styling, and the host viewer integration.

Normative levels are **REQUIRED**, **CONDITIONALLY REQUIRED**, and **PROHIBITED**. A failed required or prohibited rule is a `FAIL` unless the rule assigns `INFO`.
The `human.html` and reference HTML files are machine-emitted artifacts.
Apply `references/standards/project-doc-standards.md` rule PD-10 only for their provenance role. Apply `references/standards/skill-standards.md` rules SR-1 through SR-4 to this reference document.

Contents: 1 artifact declaration (AD) -- 2 human coverage criteria (HC) --
3 decision record (DR) -- 4 page contract (PC) -- 5 browser-resolved access
(NF) -- 6 reference documents (RD) -- 7 style asset (SA) -- 8 size ceiling (SZ)
-- 9 script contracts (CK) -- 10 tree-scale order (TS) -- 11 host viewer
contract (HV) -- 12 proving corpora (PV).

## 1. Artifact declaration

### AD-1. Dedicated scalar lanes, two passes

- **Level:** REQUIRED
- **Rule:** Register one `human_html_directory` analysis lane and one
`human-html` generation lane. Keep `coverage_code_subtree.subject` scalar and
unchanged. The two lanes are two passes over the tree, run in order, with
separate prompts and no shared criteria:

1. **PLACEMENT.** The analysis lane decides `page` or `none` for every analyzed
   directory and persists that decision under DR-1, deepest first. The decision
   record is its report, so the lane stays `report_only`: it writes no HTML,
   removes no HTML, and applies no page-content criterion. Placement covers the
   whole tree before generation begins, because no directory's territory is
   known until every descendant decision exists.
2. **GENERATION.** The generation lane runs after placement is settled, deepest
   first, over the persisted decisions. It writes `human.html` for every `page`
   record and removes generated HTML for every `none` record. It applies no
   warrant criterion, takes each persisted decision as given, and never flips
   one.

Neither pass is a mode of the other, and neither carries the other's criteria.
- **Rationale:** A page's territory is a property of the whole tree, so a
directory cannot be generated against a scope its descendants have not yet
decided. The former fused unit -- analyze-then-generate per directory -- was
rejected on that ordering: it fixed a page's content before the placement below
it bounded that page's subject. One prompt carrying both sets of criteria was
rejected separately, because warrant reasoning then leaks into page content.
Splitting the passes does not split the lanes: the roster, the scalar subject
axis, `report_only`, and every registry expectation under AD-3 are unchanged, so
a list-valued second subject stays as invalid as it was.
- **Test:** The dispatch table, lane records, argument grammar, and registry
test all name both routes without changing the legacy route meanings. Running
placement alone leaves a decision record for every analyzed directory and no
HTML change. Running generation alone changes no directory's `decision` value.

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
- **Rule:** Route `analyze human-html <directory> [--tree] [--json]` to
`coverage_human_html_directory`. Route
`generate human-html <directory> [--coverage <path>] [--tree]
[--framework <path>]` to `generate_human_html`. With `--tree`, treat the named
directory as the repository root and use the tree driver. Require `--framework`
for tree generation. Keep `analyze <directory>` and `generate claude-md
<directory>` unchanged.
- **Rationale:** An explicit selector avoids guessing between two analysis subjects. Changing the legacy defaults was rejected because it alters established calls.
- **Test:** Each invocation selects one lane. The human analysis output is the
only accepted coverage input for human generation. Tree generation completes
tree placement before it starts a separate leaf-first generation pass.

### AD-3. Registry integrity update

- **Level:** CONDITIONALLY REQUIRED -- when the lane records are implemented
- **Rule:** The exact lane roster and dispatch-table expectations that the framework's
registry integrity check enforces must recognize the added lanes: the scalar axis (`artifact`
OR `subject`, never both), path resolution for every bound reference, the
three-phrasing minimum, a nonempty change driver, the producing-provenance
check, and the markdown table row for each human-html dispatch entry (`generate x
human-html`, and an `analyze` row whose `table_key` names the non-artifact
lane).
- **Rationale:** The registry integrity check must recognize the added lanes without weakening the invariants that made a list-valued subject invalid.
- **Test:** The registry-integrity test passes with both human-html lane records and fails
for a missing path, missing phrase, empty driver, or mixed axis.

## 2. Human coverage criteria

The analysis subject is one TERRITORY: a directory plus every descendant
directory, EXCEPT the subtrees owned by a nearer descendant page. Ownership
stops at the next page down. With pages at `A`, `A/B/C`, and `A/D`, page `A`'s
territory is `A` and `A/B`; `A/B/C` and `A/D` own their own subtrees. A
territory is therefore a property of the whole tree, not of one directory, and
it is computed from the persisted placement decisions rather than inferred by
the analyzing or generating agent.

Evidence can come from code, project guidance, documentation, data, assets, configuration, and finished descendant records anywhere inside the territory.
Project guidance is evidence, not the source model. The analysis applies HC criteria, not the CV criteria in `references/standards/coverage-standards.md`.

The unbounded-subtree subject this replaced, and the overlap argument that retired it, are recorded in `references/provenance/standards-decisions.md`.

### HC-1. Checkable page warrant

- **Level:** REQUIRED
- **Rule:** Judge warrant over the TERRITORY the candidate directory would own:
itself plus every descendant not already owned by a nearer descendant page.
Start at a plain directory listing with default file previews
(any file browser, or the host viewer) and no human page.
Use only listed files, normal links, and default previews. Do not use search,
a terminal, hidden `.databench/` data, or prior notes. Do not browse into, or
answer from, a subtree owned by a nearer descendant page. For each applicable
question, identify one direct and coherent answer in displayed material within
the territory:

1. What is this directory for?
2. Why does it have this shape?
3. What will hurt the reader?
4. Where does the reader go next?

Record a reason for each question that does not apply. If all applicable
answers exist, choose `none`. If an answer is materially absent or fragmented,
apply HC-2. Choose `page` only when HC-2 admits at least one gap. A `none`
decision is a normal result.
- **Rationale:** A repeatable browsing exercise makes page warrant observable. Intuition alone was rejected because it cannot distinguish a useful page from decoration. The exercise is bounded at the ownership edge so a page and its descendant page never answer the same question over the same files; an unbounded browse was rejected because the deepest material would then re-warrant a page at every level above it. The bound is why placement runs deepest first: the descendant decisions that draw the edge must already exist.
- **Test:** The analysis report names the territory it judged -- the owned
directories and the excluded ones -- and records each answer or gap, each
inapplicable reason, the HC-2 result, and the final `page` or `none` decision.

### HC-2. Orientation-first admission

- **Level:** REQUIRED
- **Rule:** Admit an HC-1 gap for placement only when the missing orientation
causes reacquisition work. The admission establishes page warrant. It does not
specify page content or produce a content-unit payload. Exclude gaps whose only
possible answer is a file inventory, source paraphrase, unsupported inference,
or duplicated project guidance.
- **Rationale:** Orientation earns page space. A source tour was rejected because the host viewer already exposes files.
- **Test:** The report names each qualifying HC-1 question and states why the
gap causes reacquisition work. It emits no claim, evidence, or proposed page
content.

### HC-3. Reader order

- **Level:** REQUIRED
- **Rule:** Write first for an owner returning after context loss. Add only the
extra context that a newcomer needs to use the same orientation path.
- **Rationale:** The returning owner defines the durable reading task. A newcomer-only tutorial was rejected because it obscures reacquisition cues.
- **Test:** The qualifying HC-1 gap and the identity line are stated for an owner
  returning after context loss, and any added context is only what a newcomer
  needs to follow the same orientation path.

### HC-4. Repository research

- **Level:** REQUIRED
- **Rule:** Research the actual TERRITORY before deciding: the candidate
directory and every descendant directory it would own. Treat every
repository input type as eligible evidence. Do not derive the page from a
project-guidance file or from filename patterns alone. Do not research into a
subtree owned by a nearer descendant page; read that subtree only through the
descendant's finished decision record and identity line.
- **Rationale:** The page explains the territory as it exists. Guidance-only generation was rejected because guidance can omit or lag important structure. Researching past the ownership edge was rejected because it reproduces a descendant page's material, and because the words it adds are spent against the same SZ-1 ceiling the descendant already spent them against.
- **Test:** The report cites inspected evidence from inside the territory, cites
each excluded subtree only through its record, and separates observed facts from
inference.

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

### DR-2. Territory source stamp

- **Level:** REQUIRED
- **Rule:** Set `source_sha` to the newest commit at or before `HEAD`. That
commit must have changed a tracked analysis input inside the directory's
TERRITORY. Exclude `.databench/`, `human.html`, and `human.<slug>.html` from the
input set, and exclude in addition every descendant subtree owned by a nearer
descendant page. When an analysis input inside the territory has uncommitted
content, persist the record with `dirty: true` and report `INFO DIRTY`; do not
block.
- **Rationale:** A territory source stamp limits staleness to the branches the page is responsible for. `HEAD` was rejected because an unrelated commit stales every record. A whole-subtree stamp was rejected for the same reason one level down: it restamps an ancestor whenever a descendant page's own material changes, so a page is marked stale, and regenerated, over files it must not mention. The excluded-subtree edits still invalidate a page -- the descendant page they belong to.
- **Test:** Recompute the last-touch commit over the same excluded path set,
including each owned-out subtree. A different commit yields `STALE`. An edit
confined to a descendant page's territory leaves the ancestor's `source_sha`
unchanged. Dirty tracked or untracked analysis input inside the territory
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
- **Rule:** Placement preserves `instructions` byte-for-byte whenever it writes
the record. A human or an agent edits `instructions` directly. Generation reads
the record as input and may write only the `references` field, because it alone
knows which reference pages it emitted; placement does not write `references`.
- **Rationale:** The record mixes generated fields with the one human-managed field. Clobbering it was rejected because instructions are the only steering channel for a page nobody hand-edits.
- **Test:** Reanalyzing a directory whose record carries nonempty
`instructions` leaves that field byte-identical. Regeneration leaves every
record field unchanged except `references`, which matches the reference pages
it emitted.

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
up link. Put all links in one `ul`, with one link in each `li`. Each link has a
`span.hh-nav-label` followed by a `span.hh-nav-identity`. Use `Repository root`
as the root label. Otherwise use the target directory's final path segment.
Use the target record's complete identity as the identity text. Omit the
descendant section when no down link exists.
- **Rationale:** Nearest-page links keep the tree navigable without exposing no-page gaps. Direct-child links were rejected because they strand skipped directories.
- **Test:** Discovery computes the expected up and down targets. The check script
compares that set with the navigation links, then checks the list, label, and
identity structure.

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

The same snippet also relays ORDINARY FILE LINKS to the host, and only when
framed. A click on a relative link that is not itself a human page is prevented
and posted as:

```js
{ type: "human-html:navigate", version: 1, path: "<repository-relative path>" }
```

The path is resolved LEXICALLY against the record directory, so the page never
learns the host's URL shape and NF-1 still holds. A link to another human page
is left alone -- it navigates in the frame and announces itself, which is the
existing spine. Absolute paths, anchors, other schemes, and modified or
non-primary clicks are all left alone.

This exists because a framed page resolves its relative links against the raw
route, so a plain link to `router.c` served bytes into the frame instead of the
host's rendered viewer. The host cannot fix this itself: the frame is sandboxed
without `allow-same-origin` (HV-4), so it can see nothing but `postMessage`. A
page opened directly from a file manager has no parent, sends nothing, and its
links work exactly as before.
- **Rationale:** A small viewer-agnostic message lets an enclosing tree follow browser navigation. A viewer-specific API was rejected because standalone pages lack it. The relay reuses that channel rather than adding a second one.
- **Test:** The script sends once after document parsing, sends nothing without a
parent, matches the page marker, and posts a lexically-resolved path for a
non-human relative link while leaving human-page links to the announce spine.

### PC-4. Shared inline style

- **Level:** REQUIRED
- **Rule:** Inline the exact SA-1 asset bytes in one
`<style data-human-html-style>` element. Do not link a stylesheet.
- **Rationale:** Inline style keeps one-file portability. Copied or linked CSS was rejected because it creates a runtime dependency.
- **Test:** The checker compares the style element with the packaged asset.

### PC-5. Free generated body

- **Level:** REQUIRED
- **Rule:** Read `../human-html-presentation.md` before writing the page.
Outside required chrome, the generator controls the evidence-shaped hierarchy,
section order, and semantic body structure within that reference. The generator
does not control the palette, fonts, theme, width, scripts, or external assets.
A page built from scratch is valid. Template support is deferred and never
required.
- **Rationale:** Evidence shapes the page. A mandatory template was rejected because repository directories do not share one information shape.
- **Test:** Validation checks the contract, not a body template.

### PC-6. Unsafe or nonportable content

- **Level:** PROHIBITED
- **Rule:** Do not emit `fetch`, `XMLHttpRequest`, an absolute URL, or an
absolute path. Do not emit an external-origin asset, non-ASCII content, or hand-written HTML content.
- **Rationale:** The page must survive every target environment. Network APIs, fixed locations, external dependencies, and mixed authorship were rejected as nonportable.
- **Test:** CK-1 checks generated pages and reference pages for every prohibited
form.

### PC-7. Flat job-named sections

- **Level:** REQUIRED
- **Rule:** Every group the page presents in its contents is a top-level
section: one `h2` under the single `h1`, named for the job that group does for
the reader rather than for a directory, a file type, or an HC-1 question. Do not
nest sections, and do not condition nesting on territory size, owned-directory
count, section count, or any other trigger. This narrows PC-5's generator
control over section hierarchy; PC-5 continues to govern the rest of the body.
- **Rationale:** Grouping by job already caps section count, so a nesting rule
has nothing to fire on. A nesting-by-territory-size trigger was written and then
tested on territories of 12 and 31 owned directories, and it fired on NEITHER:
the 31-directory territory produced FEWER sections (5) than the 12-directory one
(8), because a larger territory groups harder instead of listing longer. The
trigger's size limb decided nothing either time, and its section-count limb was
not crisply falsifiable, so both were removed rather than retuned. A flat list
that is genuinely unscannable earns a rule when one is observed, not before.
- **Test:** Outside chrome the page has exactly one `h1`, its content groups are
all `h2`, and no `h3` or deeper heading subdivides a group.

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
font stack and the monospace font stack. It sets no maximum width on `main` or
prose, so a page always fills its viewport. It owns a small spacing scale and
gap-based section rhythm; the PC-2 navigation list with its label and identity
levels; `dl` evidence; underlined links at rest and the visible focus outline;
the one-`h1`, `h2`-section hierarchy; bounded scrolling for code blocks and
tables; and table styling for multi-attribute comparison. Seed it from the host
viewer's established dark theme. Keep every concrete value in the asset. The
asset is ASCII. Expose the asset through
`skills_kit_lib.human_html` for the generator and the host viewer.
- **Rationale:** One packaged asset prevents visual drift. Values copied into this document or the host viewer were rejected as duplicate sources of truth.
- **Test:** A built wheel contains the asset. Both consumers read the same
package resource, and every page contains those exact bytes.

## 8. Size ceiling

### SZ-1. Hard visible-word ceiling

- **Level:** REQUIRED
- **Rule:** Report the visible-word count for every page. The default ceiling is
900 visible words for every main page and every reference page. Configure the
flat ceiling with the positive integer `thresholds.human_html_max_words`. This
ceiling does not vary with territory size, with depth, or between the repository
root and any other page. A record's `instructions` does not raise it. Parse HTML
text and exclude `script`, `style`, `template`, and every subtree marked
`data-human-html-chrome`. Count tokens that match
`[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*`.
- **Rationale:** This rule REVERSES the position it replaces -- that size is a
signal and never a failure -- and the reversal is the point, not a side effect,
so it is recorded here rather than swapped in quietly. Two things forced it.
First, budgets keyed on root-vs-other gave a page owning forty directories the
same allowance as a page owning only itself, and the observed result was
enumeration: the large page reproduced the file tree instead of routing to it.
A ceiling that does not grow with the territory makes enumeration physically
impossible, so the large page must group and route, which is the behavior
wanted. Second, an advisory budget changed no output, because a signal nothing
enforces is a signal nothing obeys. Per-page overrides were removed with the
keying, since an override reintroduces exactly the territory-proportional growth
the ceiling exists to stop. 900 is a TUNED STARTING NUMBER, not a settled
constant, and it sits DELIBERATELY BELOW the range the trials produced. Pages
rejected as trivia ran near 900 words; pages whose shape was accepted ran 1,215
to 1,698, and those enumerated -- the length was the enumeration, which is what
this ceiling exists to stop. Pages written to the ceiling afterwards ran 866 and
898 words over territories of 31 and 12 owned directories. The number is
expected to move once generated corpora give better evidence. Line and byte limits were rejected because HTML
formatting distorts them.
- **Test:** Report the observed word count and the ceiling for every page. A
count above the ceiling is a `FAIL`.

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
- navigation that is not one list, lacks one link per item, or lacks the exact
  short label and target identity required by PC-2.
- a missing or invalid decision record.
- `human.html` or a reference file when `decision` is `none`.
- no `human.html` when `decision` is `page`.
- a reference-list, filename, or backlink mismatch; a consumption mismatch
  only once RD-3 is implemented.
- a visible-word count above the SZ-1 ceiling.

The script reports `INFO` for:

- `STALE` when DR-2 recomputes a different `source_sha`, including stale-child
  propagation under TS-2 and placement drift under TS-3.
- `DIRTY` when the record carries `dirty: true`.
- **Rationale:** One stdlib checker enforces portable output without provisioning a runtime. Treating staleness as failure was rejected because it is a manual-action signal: the page is correct for the content it was written against. Size moved the other way when SZ-1 became a ceiling -- an over-long page is wrong now, not merely aging -- so the word count is a `FAIL` and staleness is not.
- **Test:** Fixtures cover each `FAIL`, each `INFO`, clean `page`, clean `none`,
root mapping, and reference HTML.

### CK-2. `discover_human_html.py`

- **Level:** REQUIRED
- **Rule:** Implement `scripts/discover_human_html.py` with `skills_kit_lib.human_html` and Python standard-library imports only. Make no writes. Accept a repository root and an
optional directory. Walk non-ignored repository directories. Exclude VCS
metadata, `.databench/`, and directories that contain only generated output.
Emit JSON records in deepest-first order. For each directory, report its normalized path, current DR-2 commit, dirty-input state,
record status, decision, and identity. Also report page and reference files,
nearest page ancestor, nearest page descendants, and stale-child state. For each
`page` directory, report its computed TERRITORY: the owned directories and the
excluded subtrees that a nearer descendant page owns.
- **Rationale:** Shared discovery keeps navigation and ordering deterministic. Separate generator and checker scans were rejected because they can disagree. Territory is reported here rather than recomputed per consumer for the same reason, and because DR-2's stamp, TS-2's gate, and the generation brief must all bound themselves identically or a page is stamped over one scope and written over another.
- **Test:** Fixtures cover root, nested pages, skipped `none` directories, stale
records, dirty input, missing records, and multiple descendant branches. A
fixture with pages at `A`, `A/B/C`, and `A/D` reports `A` owning `A` and `A/B`,
and excluding `A/B/C` and `A/D`.

## 10. Tree-scale order

### TS-1. Bottom-up execution

- **Level:** REQUIRED
- **Rule:** Run each AD-1 pass deepest directories first: placement across the
whole tree, then generation. A parent reads each
finished child decision and each page child's identity line. No-page
directories stay in traversal but do not become navigation targets.
- **Rationale:** Parent navigation and orientation depend on child decisions. Top-down generation was rejected because it guesses unfinished child state.
- **Test:** Discovery order places every descendant before its ancestors, and a
parent uses only finished child records.

### TS-2. Stale-child propagation

- **Level:** REQUIRED
- **Rule:** A page depends on exactly two sets of records: the records of the
directories inside its TERRITORY, and the record of each nearest descendant page
it links to under PC-2. A stale or missing record in that set makes the page
`STALE`, and the page is not finalized or regenerated until those records are
fresh. A stale record deeper than a nearest descendant page does not gate this
page; it gates the descendant page that owns it, and reaches further up only
through TS-3. This execution gate does not change CK-1 staleness from `INFO` to
`FAIL`.
- **Rationale:** A page trusts the material it owns and the identity lines it links, so continuing past stale data in that set was rejected: it silently corrupts the navigation spine. Gating on ALL descendants was rejected, because ownership stops at the next page down while the old gate did not: one broken page deep in a branch froze every ancestor above it, including pages whose own material and links were fresh, and the deeper the tree the more of the spine one failure held hostage.
- **Test:** Changing an input inside a page's territory marks that page stale and
blocks its generation until refresh. Changing an input under a nearer descendant
page marks that descendant stale and leaves its ancestors generable.

### TS-3. Placement-drift invalidation

- **Level:** REQUIRED
- **Rule:** When a directory's `decision` flips between `page` and `none` while
its content is otherwise unchanged, mark stale every page whose territory or
PC-2 navigation the flip alters -- at minimum the nearest ancestor page. A
`page` that becomes `none` returns its subtree to the nearest ancestor's
territory; a `none` that becomes `page` removes that subtree from it. Report the
invalidation through the same `INFO STALE` channel as DR-2 staleness, and gate
the affected pages under TS-2 until they are regenerated.
- **Rationale:** Placement drift changes what a page owns without changing any byte its source stamp covers: DR-2 excludes `.databench/`, so rewriting a child's `decision.yaml` moves no ancestor `source_sha`, and TS-2 fires only on a missing, invalid, or stale record. Without this rule the drift surfaced only after the fact, as a `FAIL navigation-mismatch` from `human_html_check.py` against an already-written page. A scheduling signal was chosen over that failure because the correct response to placement drift is to rerun the ancestor in the ordinary bottom-up pass, and a `FAIL` reports a page as broken when nothing about it is wrong except its age. The rule sits beside TS-2 because the two are the same gate over different causes: TS-2 tracks content drift, TS-3 tracks placement drift.
- **Test:** Flipping a descendant's decision with no content change marks the
nearest ancestor page stale while its `source_sha` is unchanged. Regenerating
that ancestor clears the staleness and the navigation mismatch together.

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
