# Capability boundaries -- what md-domain does, and what it deliberately leaves to others

md-domain reads source code. Coverage opens a directory's files to discover
facts worth making ambient; generation reads them again to write the document.
A pass that reads code constantly comes within one sentence of noticing that
the code is WRONG -- and noticing is not this domain's job.

This document names the capabilities that own the jobs md-domain declines, so
that declining them is a routing decision with a destination rather than a
refusal. Two of those capabilities exist; one does not exist yet and is
described here so that its absence is not mistaken for a gap in md-domain.

## The three questions, and who answers each

| Capability | Subject | Question it answers | Output | Status |
|---|---|---|---|---|
| **md-domain** | a markdown artifact, or one directory's own direct code | is this document right, and what should it say? | a verdict, or a written document | this plugin |
| **code review** | a CHANGE (a diff, a branch, a changelist) | is what changed correct? | issues raised against the change | `git-kit`, `p4-kit` |
| **code audit** | a CODEBASE, at rest | what is wrong in here? | a defect inventory | DOES NOT EXIST |

The axis that separates them is the SUBJECT, not the topic. All three may end
up looking at the same file. They differ in what they are accountable for
having covered: a document, a change, or a body of code.

## md-domain

**Owns:** the content and correctness of project markdown. Whether a fact earns
ambient cost, where it belongs, whether a document satisfies its contract, and
what a directory's CLAUDE.md should say.

**Reads code for exactly one purpose:** as a source of insight for the document
that will be ambient for it. That purpose is the whole licence, and it is what
`present-content-not-re-audited` and the admission criteria in
`standards/coverage-standards.md` are scoped to.

**Does not:** decide whether code is correct, enumerate defects, or verify that
a reported defect is real.

## Code review

**Owns:** the correctness of a CHANGE. Its accountability is bounded by the
diff -- it must cover what changed, and it is not answerable for what did not.

**Delivered by** `git-kit:git-code-review` and `p4-kit:p4-code-review`.

**The relationship to md-domain is one-directional and is the reason this
domain exists in its present form:** md-domain produces the ambient documents
that inform a review; the review raises the issues. A CLAUDE.md that makes a
reviewer able to catch something is md-domain succeeding. A CLAUDE.md that
raises the issue itself is md-domain doing the reviewer's job with none of the
reviewer's accountability -- it has no diff, no submit gate, and no reader
expecting a finding.

## Code audit

**Does not exist. It should.** This section is a specification for a capability
to be built, not a description of one that ships.

**Would own:** the state of a codebase at rest. Where code review asks "is this
change correct", an audit asks "what is wrong in this body of code" -- a
question with no diff to bound it, so its accountability is coverage of an area
rather than coverage of a change.

**Would be responsible for, at minimum:**

- Finding defects in existing code that no recent change introduced, and that
  therefore no review was ever accountable for.
- **Verifying reported possible defects.** md-domain records observations it
  cannot and does not verify (see the hand-off below). Establishing whether
  they are real is the audit's job, not the reporter's.
- **Closing entries that no longer hold.** A possible-defect record describes a
  transient state by construction. Nothing in md-domain can know when a defect
  was fixed; the capability that verifies is the one positioned to retire the
  record.
- Deciding what is worth acting on. An observation being true does not make it
  worth fixing, and that triage belongs with the capability that owns the
  codebase's health.

**It should consume the defect overflow, and discovery needs no wiring.** An
audit covering an area reads that area's CLAUDE.md files as a matter of course,
and each CLAUDE.md carrying overflow holds a one-line pointer to its
`CLAUDE-potential-defects.md`. So the queue is found by the reading the audit
was going to do anyway -- no registry, no scan for a magic filename, and no
coupling between the two capabilities beyond a path in a document. This is the
reason the pointer is a required part of the design rather than a courtesy:
without it the file is discoverable only by a tool that already knows to look
for it, which is precisely the coupling the split avoids.

Until it exists, the records described below accumulate unread. That is the
intended failure mode -- an unread queue is recoverable, whereas a fact
destroyed at the moment it was noticed is not.

## The hand-off: `CLAUDE-potential-defects.md`

The one place md-domain emits anything defect-shaped, and it is a RELEASE VALVE
rather than an output.

**STATUS: the criteria route here, the lane does not write the file yet.**
`standards/coverage-standards.md` carries the admission rule
(`hazard-durability`) and the boundary statement, so a coverage run is directed
to this destination. The generation lane's write step is not implemented, so
until it is, a run has nowhere to put what the criteria tell it to record --
treat an absent CLAUDE-potential-defects.md as "not implemented", never as "no
defects observed".

A candidate fact is sometimes rejected from a CLAUDE.md by `hazard-durability`
-- it describes a defect's transient state, which written as ambient prose
would fossilize into a false instruction the moment the defect is fixed. That
rejection is correct and it destroys information. The valve is where the
rejected observation goes instead.

Its properties are deliberate, and each one keeps this from becoming a code
audit by accretion:

- **Admission is a residue of a rejection.** An entry exists only because a
  named criterion rejected the fact from the CLAUDE.md. There is no
  defect-hunting pass; only what was encountered while reading the directory's
  own direct code is eligible.
- **Entries are UNVERIFIED, and say so.** They are possible defects. Verifying
  them is the consuming audit's responsibility, and paying to verify findings
  that may never be acted on is the cost this split exists to avoid.
- **`observed` is separated from `suspected`.** The observation is cheap to
  state truly; the inference is where confident falsehood enters. Separating
  them keeps an unverified entry honest rather than merely fast.
- **It is REFERENCED, never ambient.** A CLAUDE.md may carry a one-line pointer
  and no entry content. The file is not a composition input, so a defect claim
  cannot hoist upward and become ambient guidance.
- **No file when there are no entries.** An empty one implies a clean bill of
  health that nothing established.

## The test, when it is not obvious

Ask what the pass is ACCOUNTABLE FOR HAVING COVERED.

- A document -> md-domain.
- A change -> code review.
- An area of code -> code audit.

An observation about code encountered while documenting a directory is not an
audit finding, because nothing about the pass that found it covered the area it
came from. Recording it as a possible defect states exactly that much and no
more. Writing it up as a finding claims a coverage the pass never had.
