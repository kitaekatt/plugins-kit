# Coverage Standards

What makes a code-derived fact EARN a place in an ambient CLAUDE.md.

Read by the `coverage_code_subtree` lane (`references/lanes/coverage-lane.md`),
which passes this document's path as `refs.criteria` to
`workflow/coverage-detect.js`. The workflow applies these criteria verbatim; it
does not paraphrase them and does not supply criteria of its own.

**Subject.** ONE DIRECTORY'S OWN DIRECT code files, plus the ambient CLAUDE.md
chain above it -- not a markdown file, and **not a subtree**. Assessing D reads
the code files that sit directly in D and never descends into D's
subdirectories: each of those is its own subject, assessed on its own terms.
Every other standards doc in this directory judges a document that exists. This
one judges what is ABSENT from one, which is why its criteria read as admission
tests rather than compliance rules.

The chain still walks UPWARD without limit -- only the code-file set is
non-recursive. Ancestors are what make a fact already-ambient (CV-2), so
suppression would break if the chain stopped.

**Why not a subtree.** A recursive subject makes the same fact arrive once per
enclosing directory, so a parent's assessment duplicates every descendant's
findings and any de-duplication downstream compares facts against copies of
themselves. A parent gets its content instead by reading its children's finished
CLAUDE.md files -- see `references/lanes/generation-lane.md`, parent
composition. That input is what makes the non-recursive subject lossless rather
than merely narrower.

**Excluded directories.** A directory the project's VCS is configured to ignore
is not a subject and its CLAUDE.md is not an input: git -> `check-ignore
--no-index`; Perforce -> `p4 ignores`; neither -> nothing is excluded. This is
what keeps a task folder's or a scratch directory's CLAUDE.md -- a document
about a piece of work, not about code -- out of a parent's composition.

**Direction.** Coverage decides whether an absent fact should be present. It
never judges present content: that is CD-1..CD-6 in `claude-md-standards.md`
section 3, and re-deciding it here would both duplicate that criteria set and
invert the value filter (see CV-8).

## What good looks like

A candidate is a single code-derived fact, its destination CLAUDE.md, and the
evidence that put it there. A good candidate survives all eight criteria below.
A run that returns none is a normal, common result -- `COVERAGE-ASSESSED` is not
a failure state.

The two `fail` severities that suppress candidates (CV-2, CV-6, CV-8) exist
because the expensive error here is a false positive, not a miss. A run that
proposes bloat trains its reader to ignore the next run; a run that misses a
fact leaves the reader exactly where they already were.

```yaml
standards_set:
  _schema_version: "1"
  identity: >-
    Admission criteria deciding which code-derived facts earn a place in an
    ambient CLAUDE.md, applied to one directory's own direct code files plus its
    ancestor chain, rather than to a markdown document.
  applies_to: code_subtree
  criteria:
    - id: absent-fact-earns-ambient-cost
      statement: >-
        A candidate fact must be durable and consequential. It is not
        "cheaply recoverable" when a straightforward reading of the local
        files where the mistake would be made does not reveal it -- for
        example, a local obligation whose counterpart lives elsewhere, an
        invariant the code obeys without stating, or the rationale for a
        deliberate oddity whose apparent wrongness invites correction.
      severity: judgment
      enforcement: judgment
      keywords: [earns its place, ambient cost, durable, consequential, cheaply recoverable, local view, negative knowledge, intentional oddity, value bar]
      example: >-
        ADMIT a two-place registration requirement that a compiler never
        enforces. ADMIT a deliberate oddity documented with its reason and
        what to do instead -- the Shape-A "deliberate hack/workaround (and
        don't simplify)" observation kind in claude-md-standards.md section
        3.2. Its apparent wrongness invites correction, while the compliant
        code shows the oddity but not its intent. REJECT a restatement of
        what a function's name
        already says, and REJECT a file inventory, which the reader can
        regenerate by listing the directory. Apply 'cheaply recoverable' to the FACT, never to the
        area it sits in: a docstring that states an invariant for the cases it
        handles does not make the case it omits recoverable. When source
        commentary covers part of a topic, name the specific proposition being
        suppressed and check that the commentary states THAT proposition -- a
        suppression whose justification is 'this area is documented' is
        mis-scoped.
        A FACT ANCHORED TO A SINGLE SITE IS CHECKED AT THAT SITE. Before
        admitting a fact whose subject is one file -- often one function or one
        line -- read the site and state whether commentary there already
        carries the proposition. If it does, the fact is recoverable and is
        REJECTED. If it does not but a comment there would reach the reader who
        would make the mistake, ADMIT the fact, worded to name its own better
        home: "Suggest moving this to a comment on <filename>, as it applies
        only to that file." The reader gets the fact and the relocation
        suggestion in one line, rather than the fact being discarded with no
        trace it was ever found -- nothing downstream of this criterion can
        write the comment itself, and a report carries only surviving
        candidates.
        The governing question is whether a comment there REACHES THE READER
        WHO WOULD MAKE THE MISTAKE; everything below is that one question
        applied to the cases seen so far, and it is the question you answer, not
        a checklist you match. When a comment there would NOT reach that
        reader, admit the fact plainly, with no relocation suggestion. Known
        cases where a comment fails to reach: the fact's other end is in
        another file; it must reach an author writing NEW code rather than
        reading the existing code; it holds across the directory; the file
        cannot be edited; or the reader who would make the mistake never opens
        the file at all because they INVOKE this code or CONSUME ITS OUTPUT
        rather than read it -- a caller relying on an exit code, a schema, a
        file format, or a printed result is not reading the site that
        documents it, so a comment there reaches nobody.
        Name which case holds and why a comment fails to reach, or admit with
        the relocation suggestion. Judge by that, never by how important the
        fact is; a very important fact about one line still gets the
        relocation suggestion. If no case holds and you cannot articulate a new
        one in those terms, admit the fact with the suggestion that it move to
        a comment.

    - id: already-ambient-suppressed
      statement: >-
        A fact already carried by any CLAUDE.md in this directory's ancestor
        chain is never a candidate, including at a trigger site closer to the
        code.
      severity: fail
      enforcement: judgment
      keywords: [already ambient, inherited, duplication, trigger site, restatement, ancestor chain]
      example: >-
        A rule stated in the repo-root CLAUDE.md is reaching every file beneath
        it. Proposing it again in a subdirectory is duplication the placement
        spine forbids, however close to the code that copy would sit.

    - id: fact-scoped-to-this-directory
      statement: >-
        A candidate must be a fact about the assessed directory's own direct
        code. Its destination is that directory, always; an assessment never
        proposes a fact for anywhere else.
      severity: fail
      enforcement: judgment
      keywords: [scope, this directory only, destination is the subject, no nomination, no promotion, no hoisting from below, sibling subtree]
      example: >-
        REJECT a fact whose subject is a file in a subdirectory, a sibling, or a
        parent -- each of those is assessed on its own terms and would receive
        the fact from its own run. An assessment that read only this directory
        has no basis to place anything anywhere else: it cannot see whether the
        fact holds of code it never opened. A fact that genuinely governs a
        wider area reaches that area by HOISTING, which happens at the parent
        when the parent observes the fact in a child's document -- never by
        nomination from below.

    - id: candidate-tier-classified
      statement: >-
        Every surviving candidate is classified finding-convertible or
        context-only, and the classification is reported.
      severity: judgment
      enforcement: judgment
      keywords: [finding convertible, context only, tier, quotable, unambiguous violation, consumer usable]
      example: >-
        FINDING-CONVERTIBLE requires all three: an imperative a reviewer can
        quote verbatim, a violation test that is unambiguous rather than
        discretionary, and a violation locatable at a file and line.
        CONTEXT-ONLY is a real and admissible outcome -- orientation and
        architecture facts earn ambient space without being convertible -- but
        it is never reported as though it were convertible.

    - id: hazard-durability
      statement: >-
        An observed hazard earns ambient prose only if it is durable -- still
        true after any reasonable fix -- or severe and not being fixed.
      severity: judgment
      enforcement: judgment
      keywords: [hazard, fossilize, durable invariant, defect, severity, transient state]
      example: >-
        ADMIT the invariant a defect reveals ("callers must X because Y is not
        checked anywhere"). REJECT the defect's transient state as prose: a fix
        erases it and leaves a false instruction behind.
        THE REJECTED OBSERVATION IS NOT DISCARDED. A non-durable hazard that is
        severe goes to the directory's CLAUDE-potential-defects.md as an
        UNVERIFIED possible defect -- never into the document, and never
        rewritten as an invariant to get it in. That rewrite is a scope
        widening, the same operation that turns a true observation into a false
        ambient claim. The overflow file exists so this rejection stops
        destroying information; it is a release valve, not an output, and
        admission to it requires this criterion to have rejected the fact
        first. Contract, format, and the consuming capability:
        ../capability-boundaries.md.

    - id: loud-failure-excluded
      statement: >-
        A constraint that is documented, fails loudly at runtime, and is
        test-enforced is never a candidate.
      severity: fail
      enforcement: judgment
      keywords: [loud failure, test enforced, already documented, silent failure, near miss, fixed cap]
      example: >-
        A fixed compile-time cap whose loader errors on overflow and whose test
        asserts the bound is fully handled. Reporting it means the criterion
        keyed on the SHAPE of the construct rather than on whether its failure
        is silent -- the exact inversion that makes a hazard sweep useless.

    - id: evidence-floor
      statement: >-
        Every candidate cites a file and line observed in source; a convention
        needs two or more observed instances or one authoritative source.
      severity: fail
      enforcement: judgment
      keywords: [evidence, file line cite, ground truth, speculation, convention, observed]
      example: >-
        Code outranks comments, guides, and rationale. Names, layout, and
        repeated patterns are discovery signals that start an investigation,
        never findings on their own. A fact that cannot be anchored to observed
        source is dropped, not hedged.
        A FACT WHOSE COUNTERPART LIVES OUTSIDE THE SUBJECT IS ANCHORED AT ITS
        LOCAL END. CV-1 deliberately admits such a fact -- a local obligation
        whose other end is elsewhere -- while every anchor must name a file in
        the subject's own direct list. Both hold at once: anchor the LOCAL half,
        the site where the obligation is incurred or where the reader would make
        the mistake, and name the outside end in the fact's prose rather than in
        an anchor. There is no field for an outside citation and none is being
        added. The consequence for verification is the part that bites: such a
        fact cannot be contradicted by the subject's own files alone, so a
        refuter reading only those files has not falsified it and it STANDS --
        including when the only line it can cite is the candidate's own anchor,
        whose apparent contradiction an outside definition would resolve.

    - id: present-content-not-re-audited
      statement: >-
        Coverage judges absent facts only; it never evaluates the quality,
        accuracy, or value of content already present.
      severity: fail
      enforcement: judgment
      keywords: [absent only, do not re-audit, value filter, dense prose, CD criteria, scope boundary]
      example: >-
        A dense hazard section is the content the value filter exists to
        PROTECT. Flagging it as low-value inverts the filter. Fidelity and value
        of present content belong to CD-1..CD-6 in claude-md-standards.md
        section 3.
```

## Two RETIRED carriage fields on a candidate

A candidate record may still carry `scope` (`LEAF-ONLY` or `PROMOTE -> <dir>`)
and `sibling_overlap` (a sibling document stating the fact, and whether it
reaches this directory's author). **Neither is produced any more, and neither is
a criterion.** They are read-only compatibility surface: reports written before
this model exists carry them, and a loader must not choke on them.

They were the promotion machinery -- an assessment nominating a destination
above itself. `fact-scoped-to-this-directory` now forbids exactly that, for the
reason the fields themselves half-admitted: judging whether a fact belongs here
or at a parent means reading the parent or a sibling, which is outside the
subject. The judgment was never licensed; it was merely given somewhere to sit.

A fact reaches a wider area by HOISTING instead -- the parent observes the fact
in a child's document and lifts it, rewording it so it is true as stated at its
new depth. That happens during parent composition
(`references/lanes/generation-lane.md`), where the documents being compared have
actually been read.

**Do not emit either field, and do not reintroduce an equivalent.** A
`destination` pointing anywhere but the subject directory is a criterion
violation, not a hint.

## Analysis depth: basic and advanced

Depth is a caller-supplied parameter, not a per-run judgment. It changes how
much source is read AND how many analysis passes run -- one dial, two coherent
operating points.

| | basic | advanced |
|---|---|---|
| Calibrated for | a Claude Code power user | "give me the full experience" |
| Read | bounded budget, sampled | every source file, completely |
| Passes | assess | discover invariants -> assess -> verify |
| `COVERAGE-ASSESSED` means | not found within budget | verified absent |

`basic` is not a degraded mode. It is the level a Claude Code power user should
expect from a routine invocation: bounded, repeatable, and worth running on a
directory without planning for it. `advanced` is the full treatment -- the shape
the generation method itself ran -- and is correspondingly expensive.

**Advanced is not "basic, but more".** Its invariant-discovery pass runs before
assessment and injects what it found as input; its verification pass re-checks
every surviving candidate against source after assessment. Both target
precision. The exhaustive read targets recall. A caller who wants only one of
those still gets both -- the dial is deliberately not two dials.

**"Verified absent" names the REFUTATION STAGE, not the assessing agent's own
verification pass.** The distinction is the whole content of the claim, so it
must not be collapsed. An agent's pass over its own candidates, in the context
that produced them, is a self-check: it does not hunt counterexamples to the
pattern it just abstracted, and a lane that stopped there would be asserting
verification it never performed. What earns the word is a separate stage that
runs after the assessment returns, in FRESH context, and tries to falsify each
surviving candidate against the subject's exhaustive direct-file list.

Two consequences a caller must be able to act on:

- **The stage can be absent.** It runs at advanced depth and can be switched
  off. When it does not run, `COVERAGE-ASSESSED` means "not found within
  budget" no matter what depth was requested, and the run says so on its own
  summary line rather than leaving the caller to infer it.
- **It deletes only for FALSITY, and only with a counterexample.** It does not
  re-judge whether a fact earns its ambient cost; those criteria are applied
  once, at assessment. A stage that re-judges value does not raise precision,
  it manufactures rejections -- measured on a real corpus, refutation pointed
  at admission judgment overturned at 76% and 67% when its verdicts were
  re-checked against these criteria, while refutation pointed at truth held.
  A verdict that cannot name the file and line that contradicts the fact is
  discarded, and the candidate survives.

**The verdict carries the mode.** Because `COVERAGE-ASSESSED` means materially
different things in the two modes, the report states which mode produced it. A
verdict printed without its mode is ambiguous and must not be emitted.

### Selecting the depth: ASK, do not assume

An explicit flag wins and runs silently. **When the invocation expresses no
depth, the lane's intent gate ASKS via AskUserQuestion rather than defaulting.**

This is a deliberate exception to the usual preference for a sensible default,
and it is worth naming because it costs real UX -- a prompt on a run the user
thought they had already specified. It is justified by the asymmetry between the
two levels: silently choosing wrong in the `advanced` direction opts a user into
an extreme, expensive experience they never asked for, and silently choosing
wrong in the `basic` direction returns a bounded sample to someone who wanted
the exhaustive treatment and may read `COVERAGE-ASSESSED` as "verified absent".
Neither error is cheap, and neither is visible to the user at the moment it
happens. One prompt removes both.

The prompt names the two levels by their calibration, not by their mechanics:
what a power user should expect, versus the full experience.

**The prompt is a fallback, not the mechanism.** It is correct only while no
durable experience posture has been captured for the user. Once one exists, it
answers this question and coverage runs at the corresponding depth silently --
a user who chose power-user defaults has already said which level they want,
and asking again reads as not having listened. The precedence is: explicit flag,
then captured posture, then this prompt. See
`docs/reference/first-run-experience.md` in the plugins-kit repo for the
pattern; that posture is bootstrap-managed and does not exist yet, so today the
prompt always applies.

**Where asking is impossible, disclose.** In a non-interactive dispatch the gate
cannot prompt, so it takes `basic` and discloses it in keyword form on one line,
listing only the keys that fell to a default:

```
defaults: depth=basic
```

No rationale, no explanation of what `basic` means -- the flag name carries it.
An explicitly passed flag drops off the line entirely.

Disclosure is the fallback for the interactive case, not a substitute for it: a
disclosed default is correctable only after the expensive run has already
happened, which is precisely what the prompt exists to prevent.

## What is not in this doc

- **Whether present content is any good.** CD-1..CD-6 in
  `claude-md-standards.md` section 3 own fidelity and value of existing
  sections. CV-8 forbids restating them.
- **Which CLAUDE.md a fact belongs in, mechanically.** The placement algorithm
  is `references/cohesion-principles.md`. CV-3 constrains the ANSWER; it does
  not re-derive the algorithm.
- **The observation kinds a fact is described in terms of.** Shapes A-D in
  `claude-md-standards.md` are the SSOT vocabulary; coverage references them.
- **How candidates are discovered.** `scripts/discover_coverage.py` resolves
  subjects and structural exclusions and is deliberately criteria-free.
- **What to do about a candidate.** Coverage is report-only. It proposes a
  destination; it never writes one, and there is no remediation phase.
  Coverage is the discovery phase OF generation, and being re-homed under
  generation grants it no licence to write. It proposes a destination; it
  never creates or edits one. The generation lane is the only surface that
  writes.
- **Code review, and code audit.** Coverage never enumerates defects and never
  goes looking for them. A severe deficiency noticed in passing -- while reading
  this directory's own direct code for the purpose above, never by a hunting
  pass -- is recorded as an UNVERIFIED possible defect in the directory's
  CLAUDE-potential-defects.md, under `hazard-durability`. Coverage does not
  verify it, decide whether it is worth fixing, or retire it once fixed; those
  belong to a code-audit capability that does not exist yet. Who owns what, and
  why the boundary is drawn at the subject rather than the topic:
  ../capability-boundaries.md.

## Configuring these criteria

Every criterion above carries a stable id and is disable-able or tunable
through the standard mechanism in `references/configuring-standards.md`. A
project that holds a different position on an opinion here changes it there
rather than forking this document.

**Named deviation: `applies_to: code_subtree` is not a file-type primitive.**
`references/authoring-standards.md` says a standards file governs exactly one
file-type primitive, and registers four (`skill_md`, `claude_md`,
`reference_doc`, `plain_md`) in `references/audit-framework.yaml`. Coverage's
subject is a directory plus its ancestor chain -- a composition, not a file
type -- so it fits none of them, and forcing it into one would misdescribe it.
The go-live registration therefore adds `code_subtree` under the framework's
compositions, while leaving the primitive roster unchanged. The workflow is
unaffected: `coverage-detect.js` requires only a readable document at
`refs.criteria` and never inspects `applies_to`.

The one opinion worth naming explicitly, because a competent team genuinely
disagrees with it: `already-ambient-suppressed` refuses a second placement at a
trigger site even when visibility near the code would help. That is a
deliberate default, not an oversight -- copies consume ambient budget and drift
apart -- and a project that prefers trigger-site visibility disables the
criterion by id.
