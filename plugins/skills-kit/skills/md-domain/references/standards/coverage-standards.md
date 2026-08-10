# Coverage Standards

What makes a code-derived fact EARN a place in an ambient CLAUDE.md.

Read by the `coverage_code_subtree` lane (`references/lanes/coverage-lane.md`),
which passes this document's path as `refs.criteria` to
`workflow/coverage-detect.js`. The workflow applies these criteria verbatim; it
does not paraphrase them and does not supply criteria of its own.

**Subject.** A code subtree plus the ambient CLAUDE.md chain above it -- not a
markdown file. Every other standards doc in this directory judges a document
that exists. This one judges what is ABSENT from one, which is why its criteria
read as admission tests rather than compliance rules.

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
    ambient CLAUDE.md, applied to a code subtree and its ancestor chain rather
    than to a markdown document.
  applies_to: code_subtree
  criteria:
    - id: absent-fact-earns-ambient-cost
      statement: >-
        A candidate fact must be durable, consequential, and not cheaply
        recoverable by reading the code it describes.
      severity: judgment
      enforcement: judgment
      keywords: [earns its place, ambient cost, durable, consequential, cheaply recoverable, value bar]
      example: >-
        ADMIT a two-place registration requirement that a compiler never
        enforces. REJECT a restatement of what a function's name already says,
        and REJECT a file inventory, which the reader can regenerate by listing
        the directory.

    - id: already-ambient-suppressed
      statement: >-
        A fact already carried by any CLAUDE.md in the subtree's ancestor chain
        is never a candidate, including at a trigger site closer to the code.
      severity: fail
      enforcement: judgment
      keywords: [already ambient, inherited, duplication, trigger site, restatement, ancestor chain]
      example: >-
        A rule stated in the repo-root CLAUDE.md is reaching every file beneath
        it. Proposing it again in a subdirectory is duplication the placement
        spine forbids, however close to the code that copy would sit.

    - id: no-cross-apply-placement
      statement: >-
        The destination must be a CLAUDE.md in an ancestor directory of every
        file the fact governs, and of no file it does not.
      severity: fail
      enforcement: judgment
      keywords: [placement, ancestor chain, no cross apply, sibling subtree, scope, destination]
      example: >-
        A seed variable belonging to one sandbox's tests must not be proposed
        for a sibling sandbox that never uses it. A sibling's CLAUDE.md never
        loads for this subtree, so such a placement reaches nobody it should and
        burdens everybody it should not.

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
        erases it and leaves a false instruction behind. A non-durable hazard
        qualifies only when it is severe AND no fix is in flight, and it is then
        reported as a severe deficiency, never as documentation content.

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
subtree without planning for it. `advanced` is the full treatment -- the shape
the generation method itself ran -- and is correspondingly expensive.

**Advanced is not "basic, but more".** Its invariant-discovery pass runs before
assessment and injects what it found as input; its verification pass re-checks
every surviving candidate against source after assessment. Both target
precision. The exhaustive read targets recall. A caller who wants only one of
those still gets both -- the dial is deliberately not two dials.

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
- **Code review.** Coverage never enumerates defects. A severe deficiency
  noticed in passing is reported under CV-5's carve-out as CLAUDE.md content,
  not as a defect list.

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
