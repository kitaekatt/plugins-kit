# The producing lane (author and generate)

The ONE producing procedure, shared by TWO verbs and parameterized by artifact.
It reads the SAME standards docs the audit lane reads -- in the PRODUCING
direction (make it compliant) rather than the DETECTING direction (find the
violation).

**Two verbs, one procedure, told apart by INPUT PROVENANCE.**

- **AUTHOR** takes content the USER supplies -- a conversation, a pile of notes,
  an audit remediation -- and makes it meet the standards. Its claims cannot be
  re-checked against anything, because there is no source to re-read. It crosses
  `skill`, `claude-md` and `project-doc`.
- **GENERATE** takes COVERAGE produced by an `analyze` run and writes it up, so
  each claim carries the `file:line` evidence it came from and a later run can
  re-derive it. It crosses `claude-md` ALONE: nothing analyzes a codebase and
  emits skill or project-doc candidates, so there is no coverage to generate from.

Both then run the same five steps below, against the same standards doc for the
artifact. Provenance is not a stylistic label -- it is what decides how much
REGENERATION is allowed to keep.

**REGENERATION is `generate` over a document that already exists.** EXISTENCE
DECIDES and nothing else does: no flag to pass, no label to choose per run. What
a regeneration may KEEP is governed by the retention contract in
`../standards/claude-md-standards.md` section 6.4 -- verified content kept with
refreshed anchors, MARKED content kept verbatim, unverified content REPORTED
rather than deleted. Over a document carrying no markings at all the run proposes
them and writes nothing.

An earlier revision of this paragraph said the preservation half of regeneration
"is already carried by the summarize-and-reference rule and its loss-free-deletion
guard in Step 4". That was too weak to carry the weight: those rules govern
whether a RESTATEMENT loses value, not whether an unverifiable fact survives a
rewrite at all. Section 6.4 is the authority now; Step 4's guard still applies on
top of it.

**Authoring has no retention question.** There is no source to verify against and
no coverage to compare with, so an author run over an existing document is an
ordinary edit held to the standards.

**Single-invocation by default.** One run of this procedure writes ONE document,
and generating N documents is N runs of it. There is no pre-image, no
detect/remediate split, and no review mode -- those belong to the audit lane and
answer a question generation does not ask.

**The one exception is TREE-SCALE `claude-md` generation**, which binds
`workflow/claude-md-generate.js`. It exists because at tree scale the runs are
not independent: parent composition below makes a directory's document an INPUT
to its parent's, so the N runs carry a topological order whose violation is
silent. The lane does not change this procedure -- each agent it dispatches
performs exactly the five steps below -- it supplies the two things a caller
otherwise has to get right by hand every time:

- **The wave order**, derived from the subject set rather than taken on trust, so
  a parent cannot start before its descendants have finished. `parallel()` alone
  is one barrier over one set and `pipeline()` has no barrier between items;
  neither expresses a dependency graph. A LOOP over waves, each wave a
  `parallel()` barrier, does.
- **The model pin** (opus + high). Without it a generation run inherits whatever
  model the session happens to be on, which for judgment-heavy work -- placement,
  wording a hoist so it is true as stated at a new depth, de-duplicating against
  the chain -- is silently under- or over-powered.

An earlier revision of this paragraph stated that the verb "has no fan-out
machinery and gains none: no Workflow lanes ... New generation machinery would be
new scope." That was the correct call while the only known consumer was a
single-document request; it was overturned deliberately once tree-scale
generation became a real workload. Single-document generation is unaffected and
still needs no lane.

Load this together with exactly one standards doc, selected by the dispatch
table:

| Lane id | Standards doc | Validation |
|---|---|---|
| `author_skill` | `../standards/skill-standards.md` | `python -m skills_kit_lib.audit <path>` |
| `author_claude_md` | `../standards/claude-md-standards.md` | `python -m skills_kit_lib.audit <path>` |
| `author_project_doc` | `../standards/project-doc-standards.md` | no mechanical validator -- self-check against the standards doc |
| `generate_claude_md` | `../standards/claude-md-standards.md` | `python -m skills_kit_lib.audit <path>` |
| `author_references` | -- | no lane; see below |

**There is no `author_references` lane.** Cross-references are an emergent
property of the other three artifacts, not an authored one. A request to
"fix my broken references" is remediation on the `audit_references` lane; a
request to "add a reference" is authoring whichever artifact carries it. Route
there rather than improvising.

**There is no `generate_skill` or `generate_project_doc` lane either**, for the
reason given above: no analysis emits coverage for those artifacts. A user who
says "generate a skill" is asking for `author_skill`. Take the intent, name the
lane you are taking, and proceed -- do not refuse on the wording, and do not
invent a coverage input to justify the token.

## Procedure: produce an md artifact (author, generate, or regenerate)

**Goal.** Produce an artifact that is schema-valid where a schema exists,
compliant with its standards doc, correctly placed, and shaped in the file's
native form.

**Precondition.** A fact, convention, insight, contract, or document body needs a
home; the artifact type has been named (or can be resolved in step 1); and the
VERB is settled, because the verb names the input. For `author` the input is
whatever the user supplied. For `generate` the input is a COVERAGE REPORT -- if
there is none, run `analyze` first and say so. Never proceed on an empty input
and present the result as generated: a document with no coverage behind it is an
authored document, and calling it generated claims a re-checkability it does not
have.

### The regeneration gate -- run this BEFORE writing anything

Fires when the verb is `generate` AND the target document already exists.

1. **Read the existing document and split it into units** -- records inside the
   `claude_md:` block, and marked or unmarked prose sections outside it.
2. **Classify every unit** VERIFIED / RETAINED / UNVERIFIED per
   `../standards/claude-md-standards.md` section 6.4. VERIFIED means this run's
   coverage re-derives it from current code; RETAINED means it carries a
   retention marking; UNVERIFIED is everything else.
3. **If the document carries NO retention marking of either form, STOP AND
   PROPOSE.** Report each unit with its classification and name the UNVERIFIED
   units you propose marking `retain`. Write nothing at all this run. This is the
   normal state of every hand-written CLAUDE.md, and honouring the table without
   this gate would delete all of it.
4. **Otherwise proceed**, carrying RETAINED units through VERBATIM (they are not
   re-worded, re-ordered into a different record, or "improved"), refreshing
   VERIFIED units' anchors, and reporting UNVERIFIED units for the user to rule
   on rather than dropping them silently.

The gate is DETECTED, not configured -- there is no flag to skip or force it. A
document this lane generated cannot reach step 3, because every unverifiable unit
it kept was marked when it was written.

**The fact may arrive pre-derived.** This lane is agnostic about where content
came from: a conversation with the user, an audit remediation, or a batch handed
over by another lane. The case with a defined shape is a **coverage report** --
its candidates carry `fact`, `why`, `anchors` (`file:line` evidence),
`destination`, and a `tier`. Take them as follows:

- `destination` is a pre-resolved placement (step 2), so the placement framework
  is not re-invoked for it.
- `anchors` are the evidence the written content cites; carry them through rather
  than re-deriving citations.
- `tier` (FINDING-CONVERTIBLE / CONTEXT-ONLY) is a reader signal that travels
  with the fact. It does not decide whether to write the fact up, and it is not a
  standards input -- the artifact's own standards doc governs the output either
  way.

One run of this procedure writes ONE document, so a report spanning several
destinations is one run per destination, taking that destination's candidates
together. `coverage-lane.md` ("Handing the report to generation") is the caller
side of the same seam.

**A coverage report is not the whole input for a directory that has children.**
Its candidates come from that directory's own direct code, which is the entire
subject of a coverage run (`coverage-lane.md`, "Subject and unit"). The second
input is the children's own CLAUDE.md files, and reading them is part of this
procedure, not of the report -- see "Parent composition" below.

### Parent composition -- the second input, and the order it forces

A `claude-md` run whose directory contains child directories is a COMPOSITION,
and it has TWO inputs:

1. **The directory's own direct code.** Non-recursive: the code files sitting
   directly in it, never its descendants' files. This is what a coverage report
   for the directory carries.
2. **Every child directory's CLAUDE.md, already written.** Reading the child
   documents is the second input, not an optional enrichment. A composition that
   skips it produces a document whose only content is the parent's own thin layer
   of direct code, which is strictly worse than what a recursive subject would
   have produced.

The second input is what makes the non-recursive subject lossless rather than
merely narrower (`../standards/coverage-standards.md:22-28`). Neither input
substitutes for the other, and a child document that has not been written yet is
not an input -- it is a missing prerequisite, which is what the ordering rule
below is about.

**A hoist is PROPOSED, then verified, then written -- never written on
speculation.** Composing a directory records each candidate hoist as a
proposal: the wording, the child document it came from, and the specific files
the wording claims to hold of. A verification phase then checks each candidate
against exactly those named files; only a candidate that survives is written
into the document. This bounds the check to the claim it verifies -- reading a
stated file set once, answering one question -- and it is not a license to
re-read a child's source wholesale: composition's two inputs above are still
the only things a hoist may be based on, and verification reads only the files
a candidate itself names, never a child's directory at large.

**Which child documents count.** A directory the project's VCS is configured to
ignore is not a subject and its CLAUDE.md is not an input: git ->
`check-ignore --no-index`; Perforce -> `p4 ignores`; neither -> nothing is
excluded (`../standards/coverage-standards.md:30-34`). This is what keeps a task
folder's or a scratch directory's CLAUDE.md -- a document about a piece of WORK,
not about code -- out of the composition.

**Hoisting is where de-duplication happens.** A fact found in a child's document --
whether repeated across children or stated by only one -- moves to their common
ancestor when the wording test below licenses it. That movement is discovered HERE,
at the parent, because this is the only place the documents being compared have
actually been read. It is never nominated from below: an assessment that read only
its own directory cannot know whether the fact holds of code it never opened, and
`fact-scoped-to-this-directory` forbids it proposing a destination anywhere else
(`../standards/coverage-standards.md:103-119`). Depth itself is
`shallowest_true_depth` -- the shallowest directory where the fact is true of
everything below it, and no shallower -- in `../cohesion-principles.md`,
under `principles_applied_to_placement`.

**WORDING is the only test; there is no separate repetition trigger.** A fact
found in a single child's document may hoist, provided it passes the test below.
The repetition trigger that used to gate a hoist first was dropped, by owner
decision on 2026-08-12, because this document already conceded the gap it left
open: "a fact true of every child that only one child noticed never triggers at
all" -- and that concession is the evidence the decision acts on, not a new
observation. The failure direction the old trigger guarded against is still real
and still governs the test: a fact stated by 2 of 20 children and hoisted
verbatim becomes ambient for 18 directories it does not govern. So a hoisted
fact must be WORDED so it is true as stated of everything below its new home,
usually by naming its subjects explicitly ("Tools and stack-traces both ...").
Scope lives in the sentence; there is no separate scoping mechanism. When no
such wording exists short of a list of exceptions, the fact does not hoist --
it stays in the children.

**A hoisted fact leaves duplication behind.** Once the parent carries it, each
child's copy is a near-verbatim restatement of an ancestor instruction that
already loads ambient, which is a C-1 finding
(`../standards/claude-md-standards.md:84-94`), and the sibling copies are the C-2
case the hoist answers (`:96-104`). Removing them is part of the hoist, not a
later tidy-up -- and because one run writes one document, it is one further run of
this procedure per child document.

**Order is strictly BOTTOM-UP, and it is a hard dependency.** Regenerating D
COMMITS to regenerating every descendant of D first. Two consequences, both of
which must be stated to a caller rather than left implied:

- **A root regeneration is a whole-corpus operation.** Every directory beneath the
  root is in scope, in depth order. There is no cheap "just refresh the root": a
  root composed from unrefreshed children is a root composed from the previous
  corpus.
- **A stale child document silently corrupts its parent.** The parent composes
  what the child SAYS, not what the child's code does. A child left unregenerated
  contributes facts that no longer hold, suppresses hoists whose repetition it no
  longer shows, and triggers hoists for facts its code no longer carries -- and
  the parent it produces is internally consistent, so nothing about the result
  looks wrong.

**The propose-verify-write phase runs per wave, not once at the end of the
corpus.** Because a grandparent composes from its children's finished documents,
deferring verification to the end of the whole run would leave nothing above the
leaves writable -- every composition above the first would be waiting on
documents that are not yet finished. So each depth level runs compose, then
verify, then write for that wave alone, and only then does the next (shallower)
wave begin.

**A judged null branch does NOT discard its verified hoists, and the write step
creates the document they need.** The null branch is an answer about a
directory's OWN DIRECT CODE -- nothing there earned ambient cost. It says nothing
about the facts that directory's children established, and a hoist that survived
verification is established at this depth whatever the local code had to say. So
when a composition returns no document and its candidates survive, the write step
CREATES one holding exactly those sentences; it derives its own ambient chain and
follows the code-directory standards like any other. The directory then offers
that document to its parent as composition input, exactly as a composed one does.

**Which false it is must be recorded, because the two are treated differently.**
A JUDGED null branch is the case above. An UNREADABLE INPUT -- the coverage report
missing or unopenable -- means the directory was never assessed at all, and no
document is created for it from any source; that is the same refusal the
no-coverage-input guard makes. A run that records only "not written" cannot tell
them apart, and the first one's verified hoists were previously lost in silence.

**Every candidate gets exactly one terminal disposition, and the count comes from
the caller.** A run states how many candidates it read and gives each one a
disposition -- written, deferred, or (recorded once, in the dropped set with a
reason code) declined. The denominator is the length of the report's candidates
array, supplied by the caller that resolved the report path, because a count the
same run produces cannot check that run's own omissions. When the accounting does
not close, the run marks those subjects incomplete and NAMES them rather than
aborting: the documents are already on disk by then, so the contract is "every
candidate has a disposition, or the run says whose does not".

**A severe hazard `hazard-durability` rejects is written to
`CLAUDE-potential-defects.md`, never into the CLAUDE.md.** The write happens in
the same turn that would otherwise drop the observation, and only when that
subject returns at least one entry -- no file is written when there are none,
because an empty one would read as a clean bill of health nothing established.
The sidecar is NEVER a composition input: a parent's second input is its
children's CLAUDE.md files only, so a defect claim recorded there can never
hoist upward and become ambient guidance for anyone above it. A CLAUDE.md may
carry a one-line pointer to its sidecar and no entry content. Full contract --
the properties, the file shape, and the capability that consumes it --
../capability-boundaries.md.

**Where this sits in the spine.** Parent composition is a case WITHIN steps 1-5,
not a replacement for them. Step 1 still confirms the artifact is a `claude-md`.
Step 2's placement question is already answered -- the document is this
directory's own CLAUDE.md -- but where content sits inside it still defers to
`../cohesion-principles.md`. Step 3 applies `../standards/claude-md-standards.md`
in the producing direction to the merged content, hoisted material included. Step
4 is where a hoist that collapses several child statements into one is governed by
summarize-and-reference and the loss-free-deletion guard. Step 5 validates the one
document this run wrote.

### Step 1 -- Confirm the artifact

Confirm which of the four artifacts this is, and that the content belongs there.
The four boundaries:

- **skill** -- a SKILL.md: a contract for a capability the agent invokes. If the
  content is really project-convention knowledge, it belongs in a co-located
  CLAUDE.md; if it is reference body text, it belongs in a skill's `references/`.
- **claude-md** -- a CLAUDE.md carrying a `claude_md:` block (scope, insights,
  optional conventions / glossary). **BRANCH:** if the target sits INSIDE a
  directory of code / YAML / CSV -- a per-directory review-notes file rather than
  a project-root or docs CLAUDE.md -- it is a **code-directory CLAUDE.md**. It
  carries review intelligence, NOT a `claude_md:` schema block. Follow the
  code-directory section of `../standards/claude-md-standards.md` (the shapes,
  the high-value observation kinds, the anchoring and path discipline, the value
  gate); steps 3-5 below apply only in their code-directory form, and the schema
  validator is NOT run on it.
- **project-doc** -- a standalone project document outside the skill tree and
  outside the CLAUDE.md hierarchy (a design record, a reference doc under
  `docs/` or `.claude/docs/`, a README).
- **references** -- not generated; see above.

Before generating a NEW skill, apply the two-step **skill_packaging_razor** from
`../cohesion-principles.md`: step 1, does the content EXECUTE a process or pull
dynamic information, or is it static reference text (static text is never a
skill)? Step 2, fold into an existing domain if BOTH the vocabulary-reachability
test and the CCP cadence test pass. Do not default a technique to standalone.

Expected: a confirmed artifact type -- or a handoff to the lane that owns the
content.

### Step 2 -- Resolve placement

Read `../cohesion-principles.md` and apply the placement algorithm (CCP change
cadence -> CRP reader set -> ADP load order -> frequency) plus
`placement_follows_trigger_shape` (a TASK-shaped trigger, a verb the session
performs, points at a skill; a LOCATION-shaped trigger, knowledge scoped to a
directory, points at that directory's CLAUDE.md and never graduates to a skill).
Do not re-derive placement from memory.

Per artifact this answers:

- **skill** -- which skill owns this, and does it belong in the SKILL.md body or
  in a `references/` doc (the CRP L2 -> L3 test).
- **claude-md** -- WHICH CLAUDE.md in the load graph (root / subsystem /
  directory / `.local`).
- **project-doc** -- whether a project doc is the right home at all, or the
  content should graduate to a skill, fold into a directory CLAUDE.md, or move
  into an existing skill's `references/`. Project references are the escape-hatch
  and nursery for still-emerging content, not a permanent home. When a project
  doc IS correct, this step also fixes where it sits and which CLAUDE.md or
  SKILL.md will cite it, so it is not born an orphan.

**PRE-RESOLVED PLACEMENT.** When a placement decision arrives already resolved --
an audit remediation naming the destination, a coverage candidate's
`destination` field, an orchestrator directive -- follow it. Do not re-invoke the
placement framework for it; invoke it only for content without a resolved
placement.

Expected: one target file, justified by the placement algorithm (or accepted as
pre-resolved).

### Step 3 -- Apply the artifact's standards doc in the producing direction

Read the bound standards doc and write to it. Each rule is stated as
Rule / Why / Test / Severity; the generation direction is "satisfy the Rule",
the audit direction is "run the Test". Same document, one SSOT, no second
hand-maintained copy.

- **skill** -- pick the type, then satisfy that type's contract: the required
  floor blocks, the conditionally-required blocks whose criteria fire, and none
  of the prohibited ones. Frontmatter with an activation trigger; a description
  under 160 characters in directive form with an exclusion clause; the YAML
  contract block under the type's root key. Schemas are floors -- add
  load-bearing structured keys beyond the floor freely, but do not declare a
  "recommended" pattern (only required, conditionally required, prohibited).
- **claude-md** -- write or extend the `claude_md:` block: `scope.covers` plus
  `scope.excludes` (the exclusion clause is load-bearing and required), then
  records as `insights` and/or `conventions`. An insight record carries
  `id` / `keywords` (>= 3) / `summary` / `detail` / `origin` / `added`; a
  convention record carries `rule` / `keywords` / `why`. The floor is >= 1 record
  across the insights + conventions UNION, so a conventions-only CLAUDE.md is
  valid; an empty block is not. For a **code-directory** file, follow the
  code-directory standards instead: one of the documented shapes, high-value
  observation kinds only, symbol anchors in preference to line numbers, no
  machine-specific absolute paths, and the value gate applied to every entry.
- **project-doc** -- satisfy the PD rules directly: a single reading task (CRP);
  forward-only load-graph edges; cross-references one hop deep, never chained; no
  back-references into CLAUDE.md sections by name (an orientation mention is
  fine, a content dependency is not); pointers to skill-owned content rather than
  restatement (the owning skill's `references/` stays SSOT); and an inbound
  citation from the CLAUDE.md or SKILL.md that should load it, so it is
  discoverable. For a **README**, the named role applies: the agent-facing copy
  (CLAUDE.md / skill graph) is the SSOT and the README is the derived human
  brief -- never strand a command, convention, or schema there that is not also
  reachable through the agent load graph. For a **generated artifact**, the
  single applicable rule is provenance: name the generator or the generation
  record.

Expected: a body satisfying every rule in the bound standards doc.

### Step 4 -- Shape the content

Apply `../authoring-patterns/content-authoring.md` -- the three content-form
surfaces (YAML frontmatter / markdown text / embedded YAML) and the framework for
choosing between them. Default to structured YAML for LLM-facing content; use
prose only when the content is naturally narrative (an identity sentence, an
orientation paragraph) or when hierarchy carries no meaning. Structure asserts
what prose cannot: a list of records asserts every entry is genuinely of that
kind.

Deeper shape references in the same cluster, loaded on demand:
`three-surfaces.md`, `area-ownership.md`, `area-config.md`,
`actions-pattern.md`, `query-tool-pattern.md`.

Match the surrounding file's existing format and SSOT: extend an existing record
rather than duplicating one. Summarize-and-reference -- a fact lives in one place
and is referenced from elsewhere; an inline reminder is acceptable only when the
referenced fact fits about a dozen tokens, and the loss-free-deletion guard
applies before collapsing any duplicate.

Expected: records in the file's native shape, no duplication.

### Step 5 -- Validate

- **skill and claude-md** -- run the mechanical validator via the plugin venv:

  ```
  (cd ${CLAUDE_PLUGIN_ROOT} && <venvPython> -m skills_kit_lib.audit <path>)
  ```

  Resolve every FAIL (missing `scope.excludes`, keywords under 3, a missing
  required block, a forbidden key indicating mixed-type drift) before considering
  the work done. Zero FAILs is well-formed. Do NOT run it on a code-directory
  CLAUDE.md -- it carries no `claude_md:` block by design.
- **project-doc** -- no mechanical validator exists. Self-check against the
  standards doc's Test column: does every outbound file path resolve, is there at
  least one inbound citation, is every cross-reference one hop, does the body
  serve one reading task.
- **All artifacts** -- when the work was substantial, the honest verification is
  the audit lane on the same artifact (`/md-domain audit <artifact> <path>`).
  Generation and auditing read the same standards doc, so a clean audit is the
  proof the generation landed.

Expected: 0 FAILs on the target, or a stated reason a JUDGMENT row is accepted.

## Gotchas

- Re-deriving placement from memory instead of reading the placement spine.
  Which file a fact belongs in is a framework decision, not a guess. (Exception:
  a placement already resolved upstream is followed, not re-derived.)
- Omitting `scope.excludes` on a `claude_md:` block. The exclusion clause is what
  stops adjacent areas from drifting into the file's ownership; the schema
  requires it.
- Keyword clusters under 3 entries on a record. The schema floor is >= 3, for
  chat-term routing.
- A root CLAUDE.md without a `claude_md:` block. Root files SHOULD carry one; add
  it when generation touches a root file that lacks it. (The audit side treats
  absence on a pre-existing root file as INFO -- adding the block is the
  generation path's job.)
- Treating a code-directory review-notes file like a schema-block CLAUDE.md, or
  vice-versa. Review-notes files carry gotchas / review checks / boundary claims,
  not a `claude_md:` block, and the schema validator is never run on them.
- Line-only anchors in a code-directory file. Line numbers rot fast; prefer a
  symbol anchor and drop the number unless the gotcha is sub-function.
- Taking a VCS-ignored child directory's CLAUDE.md as a composition input. A task
  folder's document is about a piece of work, not about code beneath the parent;
  ask the VCS rather than the directory's name.
- Generating a project doc with no inbound citation. A doc nothing points at never
  loads. Add the pointer from the owning CLAUDE.md or SKILL.md in the same pass.
- Restating skill-owned content in a project doc. When a skill exists for the
  topic, its `references/` is SSOT and the doc collapses to a pointer.
- Declaring a "recommended" pattern in a SKILL.md. Only required, conditionally
  required, prohibited.
- Shipping a verbatim command in a SKILL.md without running it first. Every
  copy-pasteable command is verified against a real environment before shipping
  (see `../skill-domain/example-verification.md`).

## Anti-patterns

- **Same fact in two CLAUDE.mds.** It seems safer -- putting the fact in both the
  root and the subsystem file guarantees the reader sees it. It is wrong: two
  copies drift independently and CCP/SSOT breaks. The placement algorithm yields
  exactly one home; if sibling scopes also need the fact, HOIST it to the common
  parent -- still one copy, reworded so it is true as stated at its new depth, and
  removed from the children.
- **Composing a parent without reading its children's documents.** The parent's
  own direct code is only half its input. A document built from that half alone is
  not a lean parent, it is a parent missing everything its subtree established.
- **Regenerating a parent before its descendants.** It looks like the cheap way to
  refresh a root and it produces a confidently-worded document composed from the
  previous corpus. Bottom-up is a dependency, not a preference.
- **Hoisting on repetition alone.** Two children out of twenty is repetition; it is
  not a licence. If the fact cannot be worded so it holds of everything below its
  new home, it stays where it is.
- **Creating a skill for static reference text.** Step 1 of the packaging razor
  rules it out: if the content does not execute a process or fetch dynamic
  information, it is a reference doc inside an existing home, not a skill.
- **Generating against remembered standards.** The standards doc is the SSOT and
  it is one read away. Writing from memory reintroduces the hand-maintained
  second copy the fold removed.

## Cross-references

- Where a fact lives (the placement spine, incl. the packaging razor) --
  `../cohesion-principles.md`.
- How a fact is shaped -- `../authoring-patterns/content-authoring.md`.
- Skill-artifact deep references (vocabulary, worked examples, domain layering,
  sub-domain schema, script reference) -- `../skill-domain/`.
- Discovering the facts a directory's own direct code implies -- `coverage-lane.md`.
- Judging an existing artifact instead of producing one -- `audit-lane.md`.
