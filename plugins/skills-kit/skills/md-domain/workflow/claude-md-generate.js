// md-domain generate verb -- CLAUDE.md GENERATION workflow, wave-ordered.
//
// WHY THIS LANE IS NOT A COPY OF coverage-detect.js, and this is the whole design.
// Coverage fans out over independent directories: every subject can run at once
// because no subject reads another subject's output. Generation cannot. A parent
// directory is COMPOSED from two inputs -- its own direct code AND every child
// directory's FINISHED CLAUDE.md -- so a directory at depth N cannot start until
// every directory beneath it has a written document
// (see ../references/lanes/generation-lane.md, "Parent composition").
//
// That is a topological dependency, and the two obvious primitives each express
// the wrong thing:
//   - parallel() is ONE barrier over ONE set. It cannot sequence a graph.
//   - pipeline() runs each item through stages with NO barrier between items,
//     which is precisely the property that breaks here.
// The expression that IS correct is a LOOP over waves, each wave a parallel()
// barrier. Wave K contains every directory whose deepest code-bearing descendant
// chain is K long; every member of wave K is independent OF EACH OTHER and
// dependent on wave K-1 having completed. So parallel() is the right primitive
// applied at the right granularity -- per wave, not per run.
//
// The failure this ordering exists to prevent is SILENT. A parent composed from
// unwritten children produces a confidently-worded document built from half its
// input, and nothing about the result looks wrong -- it is internally consistent.
// So the wave barrier is a correctness property, not a scheduling preference, and
// a future edit that "optimizes" it into a flat parallel() reintroduces exactly
// the defect the settled model was created to remove.
//
// MODEL PINNING. opus + high, matching the detect/classify pin rather than the
// remediate pin (skills-kit/CLAUDE.md, "Audit workflow lanes pin an explicit model
// AND effort"). Generation is judgment core, not application of an already-made
// decision: it places facts, words a hoist so it is true as stated at a new depth,
// and de-duplicates against an ambient chain. Remediation's sonnet+low pin is for
// applying edits the Q&A gate already decided; nothing here has been decided yet.
//
// args = {
//   subjects: [ { root: string,
//                 codeFiles: string[],
//                 reportPath: string|undefined,     // PREFERRED -- the agent reads it
//                 candidateCount: integer,          // REQUIRED with reportPath; see below
//                 candidates: [ ... ]|undefined,    // legacy inline form; see below
//                 ambientClaudeMdPaths: string[],   // HINT ONLY -- the agent derives its own
//                 skipNote: string|null } ],        // set => null branch, no document
//   finishedDocuments: string[]|undefined,           // dirs done by an EARLIER run
//
// TWO INPUTS ARE DERIVED AGENT-SIDE RATHER THAN TRUSTED FROM THE CALLER, and both
// for the same reason: the workflow script has no filesystem access, the agent
// does, so anything requiring a look at disk must be resolved on that side.
//   - reportPath over inline candidates -- inlining makes the ORCHESTRATING context
//     carry the whole report corpus as args (49KB for five directories), which does
//     not scale and pushes callers into trimming candidates to fit.
//   - ambientClaudeMdPaths is a HINT -- a caller list is a snapshot that goes stale
//     the moment an earlier wave writes an ancestor, which is precisely the document
//     the run must not duplicate.
// candidateCount is the EXCEPTION to that split, and deliberately so. The agent's own
// candidatesRead is self-attested and defeatable -- an agent that reads 14, expresses
// 12 and reports candidatesRead 12 passes every internal check -- so the denominator
// must come from a party that did not do the expressing. The caller resolved
// reportPath, so it can read the report and count its candidates array. Required
// whenever reportPath is set; derived from the array for the inline form.
//
// finishedDocuments makes the corpus walkable INCREMENTALLY. Composition input comes
// from writtenByRoot, which this run alone populates, so without it a parent can only
// be composed in the same run that writes its children -- and a parent dispatched
// alone takes the no-children branch and emits a composed-from-nothing document with
// nothing in the output to show it. Naming the already-finished directories restores
// the topology; the documents themselves are still read off disk by the agent.
//   refs: { standards: <abs path to references/standards/claude-md-standards.md>,
//           lane:      <abs path to references/lanes/generation-lane.md>,
//           placement: <abs path to references/cohesion-principles.md> }
//   houseStyle: <abs path to an exemplar CLAUDE.md>|undefined
// }
//
// Returns { perSubject, waves, waveRecords, totals }.
//
// EACH WAVE IS THREE STEPS, NOT ONE: compose (which PROPOSES hoists and writes
// none), verify (which settles each proposal against exactly the files it named),
// apply (which writes the survivors). The ordering is a dependency rather than a
// batching preference: a parent composes from its children's FINISHED documents,
// so deferring verification to the end of the corpus would leave nothing above
// the leaves writable at all. The phase runs INSIDE the wave loop for that
// reason, and the wave barrier below refuses a parent whose descendants are not
// yet RESOLVED -- a document with candidates still pending is not finished in the
// sense the barrier means.
//
// Why not write first and retract later: a retraction is a follow-up, and this
// corpus's measured behaviour is that follow-ups do not land. A wrong ambient
// rule at a parent stands over every descendant for as long as it stands. It
// also makes a broken phase INVISIBLE -- under write-then-verify a phase that
// never ran leaves documents that look exactly like a phase that ran and
// approved everything, whereas here it produces zero hoists and a recorded
// absence.

export const meta = {
  name: 'md-domain-claude-md-generate',
  description: 'Wave-ordered CLAUDE.md generation: each directory composed from its own direct code plus its children finished documents, deepest first',
  phases: [{ title: 'Generate', detail: 'one wave per depth level, deepest first' }],
}

// args may arrive as an object or as a JSON string depending on how the invoker
// passes it; normalize to an object. This binding is load-bearing: a bare `input`
// throws "input is not defined" on the first line and the lane dispatches zero
// agents while every text-level check still passes (skills-kit/CLAUDE.md, "A
// workflow lane is shipped only after ONE REAL DISPATCH").
let input = args
if (typeof input === 'string') {
  try { input = JSON.parse(input) } catch (_) { input = null }
}
if (!input) {
  throw new Error('claude-md-generate.js requires args = { subjects, refs }')
}

// Fail-closed on the standards seam, same posture as coverage-detect.js. Without
// the standards doc this lane would generate against REMEMBERED standards, which
// generation-lane.md names as an anti-pattern precisely because the result looks
// fine. Check for a non-empty STRING, not truthiness: `true` names no document.
const needRef = (key, why) => {
  const v = input.refs && input.refs[key]
  if (typeof v !== 'string' || v.trim() === '') {
    throw new Error(
      'claude-md-generate: refs.' + key + ' is not set. ' + why + ' ' +
      'Pass the absolute path, then re-run. See references/lanes/generation-lane.md.'
    )
  }
  return v
}
const standardsRef = needRef('standards',
  'Generating against remembered standards reintroduces the hand-maintained second copy the fold removed.')
const laneRef = needRef('lane',
  'The parent-composition contract and the bottom-up ordering rule live there, not in this file.')
const placementRef = needRef('placement',
  'Placement is a framework decision and is never re-derived from memory.')

const subjects = Array.isArray(input.subjects) ? input.subjects : []
if (!subjects.length) {
  throw new Error('claude-md-generate: no subjects. Nothing to generate.')
}

// A subject with NEITHER a reportPath NOR inline candidates is almost always a
// wiring mistake -- a caller that meant to pass reports and passed none. Left
// unchecked it silently produces a document written from code alone, which is
// indistinguishable in the output from an assessed directory that legitimately
// yielded nothing. Refuse, and name the subjects, rather than generate an
// un-assessed corpus that looks assessed.
//
// EXCEPT a composition-only subject, which has no coverage report BY DESIGN.
// A COVERAGE subject holds code files directly; a COMPOSITION subject is any
// directory that, or beneath which, code lives (discover_composition.py). A
// directory with no direct code is therefore never assessed and never has a
// report -- its whole input is its children's finished documents. Judging it by
// the report test would refuse a legitimate run and, worse, describe it as
// "written from code alone" when code alone is exactly what it does not have.
// The caller marks it with compositionOnly; discover_composition.py names the
// set as codeFreeCompositionSubjects.
const inputless = subjects.filter(
  (s) => !s.skipNote && !s.compositionOnly &&
    !s.reportPath && !(Array.isArray(s.candidates) && s.candidates.length)
)
const compositionOnly = subjects.filter((s) => !s.skipNote && s.compositionOnly)
if (inputless.length && inputless.length === subjects.length) {
  throw new Error(
    'claude-md-generate: no subject has a reportPath or inline candidates. Every ' +
    'directory would be written from its code alone, which is indistinguishable in ' +
    'the output from an assessed directory that yielded nothing. Pass reportPath per ' +
    'subject (preferred) or candidates, or set skipNote to take the null branch ' +
    'deliberately. Subjects: ' + inputless.map((s) => s.root).join(', ')
  )
}
if (inputless.length) {
  // s.root verbatim, NOT norm() -- norm is declared further down and would be in
  // the temporal dead zone here, throwing ReferenceError before any agent runs.
  log('NOTE: ' + inputless.length + ' of ' + subjects.length + ' subject(s) have no ' +
      'coverage input and will be written from code alone: ' +
      inputless.map((s) => String(s.root)).join(', '))
}
if (compositionOnly.length) {
  // The inverse of the note above, and it must not be collapsed into it: these
  // are written from their children's documents, having no direct code at all.
  log('NOTE: ' + compositionOnly.length + ' of ' + subjects.length + ' subject(s) hold no ' +
      'direct code and are composed from their children\'s documents alone: ' +
      compositionOnly.map((s) => String(s.root)).join(', '))
}

// THE DENOMINATOR IS CALLER-SUPPLIED, AND IT IS FAIL-CLOSED FOR THE SAME REASON THE
// standards SEAM IS. Candidate accounting compares what the agent says it read against
// a count the agent did not produce; without that second party, "112 of 122 expressed"
// degrades to "112 of however many I chose to mention", which is exactly the shape the
// silent-drop defect took -- 2 candidates with no terminal disposition anywhere and
// every internal check green. An inline-candidates subject needs nothing from the
// caller: the array in hand IS the count.
const candidateCountOf = (s) => {
  if (Number.isInteger(s.candidateCount)) return s.candidateCount
  if (Array.isArray(s.candidates)) return s.candidates.length
  return null
}
const uncounted = subjects.filter(
  (s) => !s.skipNote && s.reportPath && !Number.isInteger(s.candidateCount)
)
if (uncounted.length) {
  throw new Error(
    'claude-md-generate: subject(s) with a reportPath and no candidateCount: ' +
    uncounted.map((s) => String(s.root)).join(', ') + '. candidateCount is the LENGTH ' +
    'OF THE candidates ARRAY in that report -- read the JSON at reportPath and pass ' +
    'its candidates.length as an integer on the subject. It is required because the ' +
    'agent\'s own candidatesRead cannot be checked against anything it did not also ' +
    'produce, so a candidate the agent never mentioned would leave no trace at all. ' +
    'See references/lanes/generation-lane.md.'
  )
}

const DOC_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  // Every field is REQUIRED. An optional disclosure field is a field a run can
  // satisfy on paper and omit in fact -- the same reasoning that makes `notes`
  // required in coverage-detect.js.
  required: [
    'root', 'written', 'writtenFalseReason', 'path', 'sections',
    'candidatesRead', 'candidateDispositions',
    'droppedCandidates', 'verifications',
    'hoists', 'candidateHoists', 'notProposed', 'notes',
  ],
  properties: {
    root: { type: 'string' },
    // false is a REAL result: the null branch of the done-condition, a directory
    // with no insight worth capturing at its scope. It must be recorded rather
    // than left implicit, or "every directory is done" is unfalsifiable.
    written: { type: 'boolean' },
    // WHICH false, and the two are not the same result. A JUDGED null branch is a
    // directory assessed and found to earn no ambient cost -- its verified hoists
    // still belong in a document, so the apply step creates one. An UNREADABLE
    // INPUT is a failed read: the directory was never assessed at all, and writing
    // it from hoists alone is what the inputless guard above refuses. Folding them
    // into one boolean is how a verified hoist got discarded silently.
    writtenFalseReason: {
      type: 'string',
      enum: ['null-branch', 'input-unreadable', 'n/a'],
    },
    path: { type: 'string' },
    sections: { type: 'array', items: { type: 'string' } },
    // HOW MANY CANDIDATES THIS RUN ACTUALLY READ. Required and integer so that 0 --
    // a legitimate answer for a directory with no report -- is distinguishable from
    // a field the run omitted. Checked against the caller's candidateCount.
    candidatesRead: { type: 'integer' },
    // ONE TERMINAL DISPOSITION PER CANDIDATE READ. The defect this exists for is
    // silent: of 122 fresh candidates one run expressed 112 and declined 8, and the
    // other 2 appeared in no field at all -- droppedCandidates and notProposed both
    // empty, every count internally consistent.
    //
    // The agent authors the written and deferred entries ONLY. The declined entries
    // are DERIVED IN CODE from droppedCandidates, because two agent-authored records
    // of one decline can disagree and this file already refuses a second statement
    // of a thing it can derive.
    //
    // factExcerpt is required and load-bearing rather than decorative. Coverage is
    // expressly non-idempotent, so an index into a REGENERATED report names a
    // different candidate; the excerpt cannot be verified here, but it makes the
    // record self-describing and index drift visible to a reader. It is a WORKAROUND
    // for the absent stable candidate id (P1 of the coverage-lane deficiencies plan,
    // unimplemented) -- not the intended end state.
    candidateDispositions: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['index', 'factExcerpt', 'disposition'],
        properties: {
          index: { type: 'integer' },
          factExcerpt: { type: 'string' },
          disposition: { type: 'string', enum: ['written', 'declined', 'deferred'] },
          section: { type: 'string' },
          reason: { type: 'string' },
        },
      },
    },
    // A candidate the run declined. Required with a reason so a dropped fact is
    // never silently absent -- the retired promotion machinery in older persisted
    // reports makes this the likeliest way a fact goes missing.
    droppedCandidates: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['index', 'fact', 'reason', 'reasonCode'],
        properties: {
          // 1-based position in the report's candidates array. This is what lets
          // the lane derive the declined dispositions from here rather than take a
          // second, disagreeable statement of the same decline.
          index: { type: 'integer' },
          fact: { type: 'string' },
          reason: { type: 'string' },
          // The free-text reason above stays: a code is comparable across runs and
          // a sentence is actionable, and neither replaces the other. The fifth
          // code exists because escalateToAncestor below maps to none of the other
          // four, and it was used three times in one live run.
          reasonCode: {
            type: 'string',
            enum: [
              'already-ambient',
              'not-evidenced-here',
              'superseded-by-broader-candidate',
              'below-local-value-bar',
              'destination-outside-this-directory',
            ],
          },
          // Set when the candidate names a destination outside this directory.
          // Those are the pre-settled-model reports, and the fact is at risk of
          // being lost entirely -- it can only re-enter at an ancestor by
          // hoisting, which requires some child to have written it down.
          escalateToAncestor: { type: 'string' },
        },
      },
    },
    // A claim is verified by EXECUTING, never by asserting. A previous generation
    // claimed a mirroring relationship an `ls` disproved and reported it verified.
    verifications: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['claim', 'command'],
        properties: {
          claim: { type: 'string' },
          command: { type: 'string' },
        },
      },
    },
    // OPTIONAL, deliberately -- most subjects will have none, and forcing an
    // empty array onto every subject is noise. A candidate fact hazard-durability
    // REJECTED from the document because it describes a defect's transient
    // state -- true only until the defect is fixed, so writing it as ambient
    // prose would fossilize a false instruction the moment the fix lands.
    // Recorded here instead of being dropped or rewritten as an invariant to
    // sneak it into the document -- that rewrite is the same scope-widening
    // mistake as writing the transient state directly. UNVERIFIED and never a
    // finding: see coverage-standards.md (hazard-durability) and
    // capability-boundaries.md ("The hand-off: CLAUDE-potential-defects.md").
    potentialDefects: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['anchor', 'observed', 'suspected', 'checked', 'whyNotAmbient'],
        properties: {
          // "path:line"
          anchor: { type: 'string' },
          // strictly factual -- cheap to state truly
          observed: { type: 'string' },
          // the inference; kept apart from observed
          suspected: { type: 'string' },
          // what was actually done; often "nothing beyond the read above"
          checked: { type: 'string' },
          // the hazard-durability verdict: why this is transient, not durable
          whyNotAmbient: { type: 'string' },
        },
      },
    },
    // Set only by a composition, and it now holds VERIFIED hoists ONLY. A hoist
    // must be worded so it is true as stated at the parent depth, and it obliges
    // the child copies to be removed. fromChildren MAY name a SINGLE child: the
    // repetition trigger is gone and wording is the whole test, so the field's
    // plural name is a plural of arity, not a threshold -- do not restore a
    // more-than-one rule from it.
    //
    // The composing run returns this EMPTY. It is populated by the apply step,
    // from the candidates the verification phase let through, and the lane
    // DERIVES each entry from its candidate rather than taking a second
    // statement of the same thing on trust.
    hoists: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['fact', 'fromChildren', 'wording'],
        properties: {
          fact: { type: 'string' },
          fromChildren: { type: 'array', items: { type: 'string' } },
          wording: { type: 'string' },
        },
      },
    },
    // PROPOSED, NOT WRITTEN. A composition records here every hoist it would
    // have made; the verification phase settles each one, and only survivors are
    // applied to the document. Nothing speculative reaches a document, because a
    // wrong ambient rule at a parent stands over every descendant until someone
    // retracts it -- and the retraction is exactly the follow-up this corpus
    // shows does not land.
    //
    // THIS IS NOT UPWARD NOMINATION, and a later reader will suspect that it is,
    // because the words "candidate" and "verification" belong to the rejected
    // design. Under that design a CHILD named a destination above itself and the
    // parent had to weigh a nomination that had crossed the child-parent
    // boundary. Here the PARENT proposes a hoist into ITS OWN document, from
    // documents it already holds, and the record lives in the parent's own
    // result. That placement is what makes the distinction MECHANICAL rather
    // than cultural: no child ever writes a candidate, so no candidate can cross
    // a boundary. Moving this array anywhere a child can write it reintroduces
    // the rejected design whatever the surrounding prose says.
    candidateHoists: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['id', 'fromClaim', 'fromChildren', 'wording', 'claimedOver', 'check'],
        properties: {
          id: { type: 'string' },
          // The CHILD CLAIM this derives from, by its identity -- never a
          // directory name alone. The hoisted wording is by construction not the
          // child's wording, so a directory reference cannot say which claim.
          fromClaim: { type: 'string' },
          // The child directory or directories whose documents stated it. ONE is
          // an ordinary, expected value.
          fromChildren: { type: 'array', items: { type: 'string' }, minItems: 1 },
          // The exact sentence proposed for THIS document, worded true at THIS
          // directory's depth.
          wording: { type: 'string' },
          // REQUIRED AND NON-EMPTY, and this is the field that bounds the whole
          // design. Verification reads exactly this set and nothing else, so a
          // candidate that cannot name the files its claim is about is not a
          // proposal but a guess, and it is refused at proposal time rather than
          // carried into the phase.
          claimedOver: { type: 'array', items: { type: 'string' }, minItems: 1 },
          check: {
            type: 'object',
            additionalProperties: false,
            required: ['kind', 'detail', 'expected'],
            properties: {
              // 'none' is the honest third value, not an escape: it records that
              // no admissible check exists, which resolves to hoist-unverifiable
              // and a REFUSAL. Omitting the candidate instead would make an
              // uncheckable claim indistinguishable from one nobody thought of.
              kind: { type: 'string', enum: ['mechanical', 'bounded-read', 'none'] },
              detail: { type: 'string' },
              expected: { type: 'string' },
            },
          },
        },
      },
    },
    // The composition's OWN judgment, with no phase involved: a child claim it
    // considered and left in the child. Required, because a composition that
    // proposes nothing and records no absence is indistinguishable from one with
    // nothing to propose -- and the second is a legitimate result while the
    // first is a failure that would otherwise score as a clean run.
    notProposed: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['fromClaim', 'reason'],
        properties: {
          fromClaim: { type: 'string' },
          reason: { type: 'string' },
        },
      },
    },
    notes: { type: 'array', items: { type: 'string' } },
  },
}

// The verification phase's result, one per composition that proposed anything.
// Three dispositions, and they are the PHASE's output over the proposed set. The
// fourth disposition in the model -- not-proposed -- is the composition's own
// judgment and never reaches this schema, which is why it lives on DOC_SCHEMA
// instead. Keeping the two apart is what lets a run tell "proposed and refuted"
// from "never proposed": both produce zero hoists and they are different results.
const VERIFY_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['root', 'dispositions', 'notes'],
  properties: {
    root: { type: 'string' },
    dispositions: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['id', 'disposition', 'reason', 'filesRead'],
        properties: {
          id: { type: 'string' },
          disposition: {
            type: 'string',
            enum: ['hoist-verified', 'hoist-rejected', 'hoist-unverifiable'],
          },
          reason: { type: 'string' },
          // What the check ACTUALLY read, which is checked against claimedOver
          // below. Declaring it turns the read bound from prompt text into a
          // mechanical property: a verification that read outside the claim has
          // discovered a mis-scoped candidate, and the lane can say so without
          // trusting the agent to notice. It is also the provenance edge a
          // verified hoist adds -- the parent's claim can go false when these
          // files change while every child document stays byte-identical.
          filesRead: { type: 'array', items: { type: 'string' } },
        },
      },
    },
    notes: { type: 'array', items: { type: 'string' } },
  },
}

// The apply step. Separate from composition because the composing run never wrote
// a hoist, so the survivors are an ADDITION to a document written without any --
// or, when the composition took the JUDGED null branch, the whole content of a
// document created here. Either way it is never a filter over a file that already
// holds speculative sentences, so there is nothing to retract, which is the
// property the whole ordering buys.
const APPLY_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  // created and sections are TOP LEVEL, not per applied item. created is what tells
  // the lane a document now exists for a subject whose composition returned written
  // false -- without it the run reports a null branch over a file on disk, and the
  // parent never offers it as composition input. sections is here because a created
  // document's compose step returned none.
  required: ['root', 'path', 'created', 'sections', 'applied', 'notes'],
  properties: {
    root: { type: 'string' },
    path: { type: 'string' },
    created: { type: 'boolean' },
    sections: { type: 'array', items: { type: 'string' } },
    // OPTIONAL, same shape as DOC_SCHEMA's. The create path had no surface for
    // this before -- a null-branch subject's potentialDefects come from the
    // composition step (lanePrompt), which normally writes the sidecar itself,
    // but the field is mirrored here so an apply/create turn can report one too
    // if it is the step that ends up with a directory to write into.
    potentialDefects: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['anchor', 'observed', 'suspected', 'checked', 'whyNotAmbient'],
        properties: {
          anchor: { type: 'string' },
          observed: { type: 'string' },
          suspected: { type: 'string' },
          checked: { type: 'string' },
          whyNotAmbient: { type: 'string' },
        },
      },
    },
    applied: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['id', 'section'],
        properties: {
          id: { type: 'string' },
          section: { type: 'string' },
        },
      },
    },
    notes: { type: 'array', items: { type: 'string' } },
  },
}

// ---------------------------------------------------------------------------
// Wave computation. Derived HERE from the subject set rather than taken from the
// caller: a caller-supplied ordering cannot notice what the caller already
// forgot, and the ordering is the one thing whose violation is invisible in the
// output.
// ---------------------------------------------------------------------------
const norm = (p) => String(p).replace(/\\/g, '/').replace(/\/+$/, '')
const subjectRoots = subjects.map((s) => norm(s.root))

// Directories whose CLAUDE.md was written by an EARLIER run and which are not being
// regenerated now. Without this the lane can only compose a parent in the same run
// that writes its children, because writtenByRoot is populated from THIS run's
// results alone -- so an incremental parent wave silently takes the no-children
// branch and emits a composed-from-nothing document, the exact failure the wave
// barrier exists to prevent. Declaring them here makes them full participants in
// the topology (descendant computation, the ordering guard, composition input)
// while never being dispatched.
//
// They are TOPOLOGY ONLY. Their content still reaches the parent by the same route
// as any other child -- the agent reads the document off disk -- so this adds no
// caller-supplied content and no second source of truth.
const finishedDocuments = Array.isArray(input.finishedDocuments)
  ? input.finishedDocuments.map(norm)
  : []
const finishedSet = new Set(finishedDocuments)
const overlap = subjectRoots.filter((r) => finishedSet.has(r))
if (overlap.length) {
  throw new Error(
    'claude-md-generate: ' + overlap.join(', ') + ' appear(s) in BOTH subjects and ' +
    'finishedDocuments. A directory is either being generated now or already ' +
    'finished, and treating one as both would let a parent compose from a document ' +
    'this run is concurrently rewriting.'
  )
}

// The full topology: everything being generated now, plus everything already done.
const roots = subjectRoots.concat(finishedDocuments)

// EVERY descendant, at any depth. Used for the wave number only: a directory must
// come after everything beneath it, however deep.
const descendantsOf = (p) => roots.filter((q) => q !== p && q.startsWith(p + '/'))

// DIRECT children only -- a descendant with no other subject strictly between it
// and p. This distinction is load-bearing and easy to miss: composition reads its
// CHILDREN's documents, not its grandchildren's. Handing a parent the whole
// subtree's documents would re-ingest facts a child has ALREADY hoisted and
// reworded, so the parent would see a fact "repeated" across a child and that
// child's own child and hoist it a second time -- manufacturing the repetition
// out of its own output rather than observing it.
const directChildrenOf = (p) => {
  const desc = descendantsOf(p)
  return desc.filter((q) => !desc.some((r) => r !== q && q.startsWith(r + '/')))
}

const waveCache = new Map()
const waveOf = (p) => {
  if (waveCache.has(p)) return waveCache.get(p)
  const kids = descendantsOf(p)
  const w = kids.length ? 1 + Math.max(...kids.map(waveOf)) : 0
  waveCache.set(p, w)
  return w
}

const byRoot = new Map(subjects.map((s) => [norm(s.root), s]))
// Waves are built from the SUBJECT roots only -- a finished directory shapes the
// wave NUMBERS (it is a descendant of its ancestors) but is never dispatched.
const waves = []
for (const r of subjectRoots) {
  const w = waveOf(r)
  if (!waves[w]) waves[w] = []
  waves[w].push(r)
}

// The ambient chain is DERIVED BY THE AGENT, not trusted from the caller.
// A caller-supplied list is a snapshot, and it goes stale the moment an earlier
// wave writes an ancestor document -- which is exactly when it matters most,
// because that ancestor is the one thing this run must not duplicate. Observed
// live in 0.49.0: a run was handed only the repo root while its real parent
// existed on disk, and the fact that the child noticed and corrected it is the
// only reason the output was not a C-1 duplication.
//
// The agent has filesystem access and the workflow script does not, so the
// derivation belongs on that side. Any caller-supplied list is a HINT.
//
// DECLARED HERE RATHER THAN INSIDE lanePrompt because the CREATE variant of the
// apply step needs the identical text: a document created from verified hoists
// alone has an ambient chain like any other, and without this clause C-1 is
// unenforced on exactly the documents that path newly produces.
const chainClauseFor = (s) => {
  const chain = s.ambientClaudeMdPaths || []
  const hintClause = chain.length
    ? '\n\nThe caller believes the chain to be the list below. Treat it as a HINT ONLY, ' +
      'and prefer what you actually find on disk -- a caller list can be a stale ' +
      'snapshot taken before an ancestor was written:\n' +
      chain.map((p) => '  - ' + p).join('\n')
    : ''
  return 'DERIVE YOUR OWN AMBIENT CHAIN FIRST, BEFORE ANYTHING ELSE. Walk UP from this ' +
    'directory to the repository root, collecting every CLAUDE.md you find on the way ' +
    '(stop at the directory containing .git). Those, root-most first, are the documents ' +
    'ambient for this code. Do not assume the set; look.\n\n' +
    'Read every one. Do NOT restate a fact an ancestor already carries -- that is a C-1 ' +
    'parent-child duplication failure. When an ancestor states a fact and this directory ' +
    'has only a local DELTA on it, write the delta alone and point at the ancestor.\n\n' +
    'BUT: an ambient claim that is FALSE suppresses nothing -- de-duplicating against a ' +
    'false claim de-duplicates against nothing. When an ambient claim contradicts what ' +
    'you observe here, say so in notes and write the fact anyway.\n\n' +
    'If what you find differs from the caller hint below, SAY SO IN NOTES. That ' +
    'disagreement is a caller defect worth surfacing, not a detail to absorb quietly.' +
    hintClause
}

// Also shared with the CREATE variant, and for the same reason: a created document
// is a code-directory CLAUDE.md exactly as a composed one is, and a step that does
// not say so writes the wrong artifact in the right place.
const artifactTypeClause =
  'ARTIFACT TYPE. This is a CODE-DIRECTORY CLAUDE.md -- a review-notes file, the BRANCH ' +
  'in step 1 of ' + laneRef + '. It carries NO claude_md: YAML block and the schema ' +
  'validator is NEVER run on it. Follow the code-directory section of ' + standardsRef +
  ' in the PRODUCING direction: the documented shapes, the high-value observation kinds, ' +
  'symbol anchors in preference to line numbers, no machine-specific absolute paths, and ' +
  'the value gate applied to every entry.'

const lanePrompt = (s, root, writtenChildren) => {
  const chainClause = chainClauseFor(s)

  const compositionClause = writtenChildren.length
    ? '\nCOMPOSITION -- THIS DIRECTORY HAS CHILDREN, AND THEIR DOCUMENTS ARE YOUR SECOND INPUT.\n' +
      'Read every one of these finished CLAUDE.md files in full:\n' +
      writtenChildren.map((p) => '  - ' + p + '/CLAUDE.md').join('\n') +
      '\n\nThis is not optional enrichment. A composition that skips it produces a document ' +
      'containing only this directory thin layer of direct code, which is strictly worse ' +
      'than the recursive subject it replaced.\n\n' +
      'HOISTING is where de-duplication happens, and it happens HERE because this is the ' +
      'only place the documents being compared have actually been read. A fact appearing ' +
      'in ANY of these children is a hoist candidate for this directory.\n\n' +
      'WORDING LICENSES A HOIST, AND IT IS THE ONLY TEST. There is no separate repetition ' +
      'trigger: one child stating a fact is enough to consider it, and you may HYPOTHESIZE ' +
      'from the documents you hold that a fact reported by one child also governs its ' +
      'siblings. But the wording test is unchanged and it is now carrying the whole load. ' +
      'A fact stated by 2 of 20 children, hoisted verbatim, becomes ambient for 18 ' +
      'directories it does not govern. So a hoisted fact must be WORDED so it is true as ' +
      'stated of everything below this directory -- usually by naming its subjects ' +
      'explicitly. Scope lives in the sentence; there is no separate scoping mechanism. ' +
      'When no such wording exists short of a list of exceptions, the fact DOES NOT HOIST ' +
      '-- it stays in the children, and you say so in notes.\n\n' +
      'YOU DO NOT MAKE A HOIST HERE. YOU PROPOSE ONE. Write this document from your own ' +
      'direct code and the undisputed content of these children, and leave every hoist OUT ' +
      'of it: the hoists field must come back EMPTY from this run. Each hoist you would ' +
      'have made goes into candidateHoists instead, and a separate verification step ' +
      'settles it before one word of it is written. A speculative ambient rule at this ' +
      'depth stands over every descendant until someone retracts it, and retractions do ' +
      'not land -- so nothing unverified is written at all.\n\n' +
      'EACH CANDIDATE CARRIES FIVE THINGS AND ALL OF THEM ARE REQUIRED:\n' +
      '  - id: stable within this run, so a verdict can name it.\n' +
      '  - fromClaim: the child claim it derives from, by its identity. A directory name ' +
      'alone will not do -- your wording is by construction not the child wording, so the ' +
      'directory cannot say which claim you mean.\n' +
      '  - fromChildren: the child directory or directories that stated it. ONE is an ' +
      'ordinary value here, not an anomaly to apologize for.\n' +
      '  - wording: the exact sentence proposed for THIS document, already worded true at ' +
      'THIS depth. Not the child sentence.\n' +
      '  - claimedOver: the specific repository-relative FILES the wording claims to hold ' +
      'of. Non-empty. If you cannot name them, you do not have a proposal, you have a ' +
      'guess -- record it in notProposed instead.\n' +
      '  - check: how to settle it, defined next.\n\n' +
      'A CHECK NAMES ITS FILE SET IN ADVANCE, and there are exactly two admissible kinds ' +
      'plus one honest refusal:\n' +
      '  - kind mechanical: a read-only command, run FROM THE REPOSITORY ROOT with ' +
      'repository-relative paths, over exactly the files in claimedOver, plus the expected ' +
      'result stated in expected as a PREDICATE over its output ("every listed file ' +
      'matches"), never as a remembered count. Its search space must not include the ' +
      'generated CLAUDE.md corpus, which grows as this run writes.\n' +
      '  - kind bounded-read: open exactly the files in claimedOver, no discovery and no ' +
      'widening, and answer whether the wording is true as stated of them. detail says ' +
      'what to look for; expected says what a true answer looks like.\n' +
      '  - kind none: no admissible check exists. That resolves to hoist-unverifiable and ' +
      'the candidate is REFUSED -- the fact stays where the child put it. Record it anyway. ' +
      'A refusal that is counted is a result; a candidate you quietly drop is not.\n\n' +
      'THE READ BOUND, WHICH IS THE ONE THING A PLAUSIBLE HYPOTHESIS WILL TEMPT YOU TO ' +
      'VIOLATE ON YOUR OWN INITIATIVE. Your inputs are your own direct code files and the ' +
      'child DOCUMENTS listed above. Do NOT open a child source file to decide what the ' +
      'child should have said. RE-EVALUATING a directory -- opening its code to see what ' +
      'facts emerge -- is unbounded, its cost compounds with every level above it, and it ' +
      'is forbidden at composition. VERIFYING one claim -- checking one stated sentence ' +
      'against one stated set of named files -- is bounded by the claim, and it is the ' +
      'only source reading a hoist ever causes. It happens in the verification step, over ' +
      'claimedOver, not here.\n\n' +
      'REPORT WHAT YOU CONSIDERED AND DID NOT PROPOSE, in notProposed, one entry per child ' +
      'claim you weighed and left in the child, with the reason -- almost always that no ' +
      'wording is true at this depth short of a list of exceptions. Without this a ' +
      'composition that proposed nothing is indistinguishable from one that had nothing to ' +
      'propose, and only the second is a legitimate result.\n\n' +
      'A hoist obliges the child copies to be removed, which is a separate run per child ' +
      '-- you do NOT edit the child documents.'
    : '\nThis directory has no in-scope children, so there is no composition step and no ' +
      'hoisting: return hoists, candidateHoists and notProposed all empty.'

  // Candidates arrive one of two ways, and reportPath is STRONGLY preferred at
  // any scale beyond a handful of directories.
  //
  // Workflow scripts have no filesystem access, so inlining candidates forces the
  // ORCHESTRATING context to carry every subject's full report as args -- measured
  // at 49KB for five directories, which does not survive a 43-directory corpus and
  // makes the caller trim candidates to fit, silently degrading the documents.
  // Agents DO have filesystem access, so handing over a PATH moves the cost from
  // O(corpus) in the caller to O(1). Same reasoning as the ambient chain above:
  // derive at the side that can actually look.
  const candidates = s.candidates || []
  const candidateClause = s.reportPath
    ? '\nCOVERAGE CANDIDATES -- READ THEM YOURSELF from:\n  ' + s.reportPath + '\n\n' +
      'It is JSON with a "candidates" array; each entry carries "fact", "why", ' +
      '"anchors" (file:line evidence), "destination" and "tier". Read the file before ' +
      'writing anything. Carry the anchors through rather than re-deriving citations.\n' +
      'If the file is missing or unreadable, SAY SO IN NOTES, set written false AND set ' +
      'writtenFalseReason to input-unreadable -- do not improvise candidates from the ' +
      'code, which would silently substitute an un-assessed directory for an assessed ' +
      'one. That value is not interchangeable with null-branch: it says this directory ' +
      'was never assessed, so nothing may be written for it from any other source.'
    : candidates.length
      ? '\nCOVERAGE CANDIDATES for this directory (' + candidates.length + '), inlined by ' +
        'the caller. Pre-derived facts with evidence anchors; carry the anchors through ' +
        'rather than re-deriving citations.\n\n' +
        JSON.stringify(candidates, null, 2)
      : '\nThis directory has NO coverage candidates. Assess it from its own direct code ' +
        'alone, and be readier than usual to take the null branch.'

  return 'Write ONE code-directory CLAUDE.md, for exactly this directory.\n\n' +
    'Directory: ' + root + '\n' +
    'Its own direct code files, NON-RECURSIVE (' + (s.codeFiles || []).length + '):\n' +
    (s.codeFiles || []).map((p) => '  - ' + p).join('\n') + '\n\n' +
    artifactTypeClause + '\n\n' +
    'Placement within the document defers to ' + placementRef + '. The placement of the ' +
    'DOCUMENT itself is already resolved: it is this directory own CLAUDE.md.\n' +
    chainClause + compositionClause + candidateClause + '\n\n' +
    'THE QUALITY BAR. A CLAUDE.md that exists but repeats its parent is a FAILURE, not a ' +
    'pass. Two properties are required and they are the hard part:\n' +
    '  1. RIGHT DEPTH. A fact lives at the shallowest directory where it is true of ' +
    'everything below it, and no shallower. State facts only about code you actually read.\n' +
    '  2. DE-DUPLICATED. A fact appears ONCE in the chain.\n' +
    'The value gate is the third: an orientation sentence naming what the directory ' +
    'contains earns nothing. What earns a section is a cross-boundary hazard, a ' +
    'silent-failure mode, a magic constant with a non-obvious contract, a coupling ' +
    'invisible from inside any one file, or a deliberate oddity whose apparent ' +
    'wrongness invites correction -- code that looks wrong on purpose, documented ' +
    'with its reason, an explicit do-not-simplify, and what to do instead, because ' +
    'the code shows the oddity without showing the intent.\n\n' +
    'EVERY CANDIDATE GETS EXACTLY ONE TERMINAL DISPOSITION, AND THE ACCOUNTING IS CHECKED.\n' +
    'NUMBER THE CANDIDATES AS YOU READ THEM, from 1, in the order they appear in the ' +
    'candidates array. Report how many you read in candidatesRead -- the count of what you ' +
    'actually read, not the count of what you used. A run that expressed 112 of 122 and ' +
    'declined 8 left 2 with no terminal disposition anywhere, in no field at all, and every ' +
    'number it reported was internally consistent. That is the failure this closes.\n' +
    'Where each disposition goes, and this split is not a style choice:\n' +
    '  - WRITTEN or DEFERRED go in candidateDispositions, one entry per candidate, with ' +
    'index, factExcerpt (the candidate fact verbatim, first 80 characters or so), and ' +
    'disposition. A written entry names the section of THIS document that took it. A ' +
    'deferred entry carries a non-empty reason.\n' +
    '  - A DECLINE goes in droppedCandidates ONLY -- with index, fact, reasonCode and a ' +
    'free-text reason. Do NOT also list it in candidateDispositions. The lane derives the ' +
    'declined entries from droppedCandidates, and two records of one decline can disagree.\n' +
    'The five reasonCode values, and choose the one that is true rather than the one that ' +
    'is convenient: already-ambient (an ancestor carries it), not-evidenced-here (the ' +
    'anchors do not support it against this code), superseded-by-broader-candidate (another ' +
    'candidate you wrote states it more generally), below-local-value-bar (true, evidenced, ' +
    'and not worth ambient cost here), destination-outside-this-directory (see the next ' +
    'paragraph -- set escalateToAncestor as well).\n' +
    'factExcerpt is required because coverage is not idempotent: an index alone names a ' +
    'different candidate the moment the report is regenerated, so the excerpt is what makes ' +
    'the record readable later.\n\n' +
    'SAY WHICH FALSE IT IS. When written is false, writtenFalseReason is null-branch (you ' +
    'assessed this directory and nothing earned ambient cost) or input-unreadable (you ' +
    'could not read the report, so you assessed nothing). When written is true it is n/a. ' +
    'The two false values are different results and are treated differently downstream.\n\n' +
    'A CANDIDATE NAMING A DESTINATION OUTSIDE THIS DIRECTORY. Older persisted reports were ' +
    'produced under a retired model in which an assessment could nominate an ancestor. Do ' +
    'NOT write such a fact here as though it governed this directory. But do NOT drop it ' +
    'silently either: record it in droppedCandidates with escalateToAncestor set to the ' +
    'named destination. Under the current model such a fact can only re-enter at an ' +
    'ancestor by HOISTING from a child document, so a fact no child writes down is LOST -- ' +
    'the disclosure is what makes that recoverable.\n\n' +
    'TWO VERIFICATION RULES THAT HAVE BEEN VIOLATED BEFORE.\n' +
    '  - Any claim imposing a PROHIBITION (never, do not, must not) must be checked ' +
    'against the code in THIS directory, not merely against the candidate that proposed ' +
    'it. Generation has previously admitted a claim correctly and over-generalized it in ' +
    'the writing; nothing else checks a carried-forward rule as RESTATED. Known ' +
    'failure shape: a rule carried from a source document (for example, PORTING.md or ' +
    'a design note) is restated with stronger force or scope. The source does not ' +
    'license that stronger claim. Verify the exact restatement independently against ' +
    'the code; if unsupported, weaken it to a claim supported by both source and code. ' +
    'Cite the source beside the rule.\n' +
    '  - Verify by EXECUTING, not by asserting. A previous run claimed every module here ' +
    'has a matching C++ file; an ls disproved it, and the writer had reported it verified. ' +
    'Record every claim you checked, with the command, in verifications. A claim you ' +
    'cannot verify is DROPPED or stated with its limit -- never hedged into the prose.\n\n' +
    'A SEVERE HAZARD REJECTED BY hazard-durability IS NOT DISCARDED -- IT GOES TO ' +
    'potentialDefects. hazard-durability (' + standardsRef + ') rejects a hazard fact from ' +
    'this document when it describes a defect transient state -- true only until someone ' +
    'fixes it, so writing it as ambient prose would fossilize a false instruction the moment ' +
    'the fix lands. When that is what you are looking at, AND ONLY when it is severe, record ' +
    'it in potentialDefects instead of dropping it or rewriting it as an invariant to get it ' +
    'into the document -- that rewrite is the same scope-widening mistake as writing the ' +
    'transient state directly.\n' +
    'potentialDefects entries are UNVERIFIED POSSIBILITIES, never findings. Do NOT spend ' +
    'effort verifying one, and do NOT go looking for defects: only what you encounter while ' +
    'reading THIS directory own direct code for the purpose above is eligible, the same ' +
    'eligibility rule as a coverage candidate. Keep observed strictly factual -- what the ' +
    'code does, cheap to state truly -- and put every inference in suspected, which is where ' +
    'a confident falsehood would otherwise enter. checked records what you actually did about ' +
    'it, which is very often "nothing beyond the read above" -- do not claim a check you did ' +
    'not run. whyNotAmbient is the one-sentence hazard-durability verdict: why this is a ' +
    'transient defect state rather than a durable invariant.\n' +
    'IF potentialDefects IS NON-EMPTY, WRITE THE SIDECAR FILE YOURSELF, in this same turn: ' +
    root + '/CLAUDE-potential-defects.md. This is independent of whether you also write a ' +
    'CLAUDE.md this turn -- write it even on the null branch. Check first whether that file ' +
    'already exists; if it does, do NOT overwrite it and do NOT merge into it -- leave it ' +
    'untouched and say so in notes. Otherwise write it as: a short prose header stating these ' +
    'are unverified possible defects noticed while documenting this directory, that this is ' +
    'NOT a findings list and NOT a code review, and that verification and removal belong to ' +
    'the code-audit capability described in ../capability-boundaries.md -- then one YAML ' +
    'block with root key potential_defects, _schema_version "1", status unverified, and ' +
    'entries: one object per potentialDefects item plus a stable id (pd-1, pd-2, ...). If you ' +
    'also write a CLAUDE.md this turn, it may carry a ONE-LINE pointer to the sidecar file and ' +
    'NO entry content -- the sidecar is never a composition input, so a defect claim can never ' +
    'hoist upward into ambient guidance.\n\n' +
    'THE BOUNDARY YOU MUST NOT CROSS. md-domain INFORMS code review; it does not perform ' +
    'it. Reading the code is in scope ONLY as a source of insight for the document that ' +
    'will be ambient for it. Do NOT identify code defects, do NOT propose fixes, and do NOT ' +
    'edit any file other than the one CLAUDE.md you are writing, EXCEPT the ' +
    'CLAUDE-potential-defects.md sidecar above -- that file is the one sanctioned release ' +
    'valve for a severe hazard that hazard-durability rejected, and writing an entry into it is ' +
    'not identifying a defect for review, it is recording an unverified possibility for a ' +
    'capability that does not exist yet. A run that returns a defect LIST, or that edits any ' +
    'other file, has done the wrong work.\n\n' +
    'DOCUMENTING A HAZARD CAN FOSSILIZE A BUG. Before writing a hazard into ambient prose, ' +
    'ask whether the honest remedy is a code fix or a loud failure. If you judge so, say ' +
    'it in notes -- not in the document, and do not fix it yourself.\n\n' +
    'THE NULL BRANCH IS A REAL RESULT. If nothing in this directory earns ambient cost at ' +
    'this scope level, write NO file, set written false, set writtenFalseReason to ' +
    'null-branch, and say why in notes. That is an admissible outcome, not a failure -- but ' +
    'it must be RECORDED, never left implicit. Note that a null branch does NOT discard ' +
    'your hoist candidates: a verified hoist still belongs at this depth, and the apply ' +
    'step creates a document for it. Propose them as you would otherwise.\n\n' +
    'ASCII only. Write exactly one file and touch nothing else. Do not stage, commit, or ' +
    'create or switch any git branch.\n\n' +
    'Return the structured object.'
}

// Step 2 of the wave. Settle the candidates ONE composition proposed, against
// exactly the files those candidates named.
//
// Granularity note, because the batching argument in the design reads as though
// it should apply here: within a single wave every subject is a disjoint subtree
// of every other, so their claimedOver sets do not overlap and a wave-wide batch
// would save no reads. Batching ACROSS waves is what would pay, and it is exactly
// what the dependency forbids -- a shallower wave composes from documents this
// wave has not finished resolving yet.
const verifyPrompt = (r) => {
  // A null-branch subject has NO document, and telling this step that one exists is
  // a false premise it cannot detect -- it was told exactly that in a live run. The
  // candidates are settled against their own claimedOver either way, so the branch
  // costs nothing and removes an invitation to go looking for a missing file.
  const documentClause = r.written
    ? 'Its document, already written: ' + (r.path || (r.root + '/CLAUDE.md')) + '\n\n' +
      'A composition of this directory proposed the candidates below. NONE of them is in ' +
      'the document, and none may be put there by you. Your entire job is to return one ' +
      'disposition per candidate id.\n\n'
    : 'NO DOCUMENT EXISTS FOR THIS DIRECTORY YET. Its composition took the null branch -- ' +
      'nothing in its own direct code earned ambient cost -- so there is no CLAUDE.md here ' +
      'to read, and its absence is expected rather than a fault to investigate. That ' +
      'changes nothing about your job: each candidate is settled against the files named ' +
      'in its own claimedOver, and never against the document. Your entire job is to ' +
      'return one disposition per candidate id.\n\n'

  return 'Settle proposed CLAUDE.md hoist candidates. Decide them; write nothing.\n\n' +
    'Directory: ' + r.root + '\n' +
    documentClause +
    JSON.stringify(r.candidateHoists, null, 2) + '\n\n' +
    'HOW TO SETTLE EACH ONE, by its check.kind:\n' +
    '  - mechanical: run the command in check.detail as a READ-ONLY command, from the ' +
    'REPOSITORY ROOT, with repository-relative paths. Compare its output to the predicate ' +
    'in check.expected. If the command would write, move, or delete anything, do not run ' +
    'it -- that is hoist-unverifiable, with the reason.\n' +
    '  - bounded-read: read exactly the files in that candidate claimedOver and answer ' +
    'whether its wording is true AS STATED of them.\n' +
    '  - none: hoist-unverifiable. Do not go looking for a check the proposer could not ' +
    'find. A refused candidate is a recorded result, and the fact stays where the child ' +
    'put it.\n\n' +
    'THE READ BOUND IS THE POINT OF THIS STEP, AND IT IS NOT NEGOTIABLE BY CURIOSITY. ' +
    'Read only the files a candidate names in its own claimedOver. You are VERIFYING one ' +
    'stated sentence against one stated file set -- you are NOT re-evaluating any ' +
    'directory, and opening a directory code to see what facts emerge is forbidden here ' +
    'exactly as it is at composition. If settling a candidate would require a file its ' +
    'claimedOver does not name, you have discovered that the candidate is MIS-SCOPED: ' +
    'return hoist-rejected saying so. That is a rejection, not a licence to widen the ' +
    'read, and widening it is caught -- filesRead is checked against claimedOver.\n\n' +
    'Record filesRead as exactly the files you opened or the command touched. It is ' +
    'checked, and it becomes the provenance edge for a verified claim: these files can ' +
    'change while every child document stays byte-identical, and the claim goes false ' +
    'with nothing else moving.\n\n' +
    'Give every disposition a reason a later reader can act on. For hoist-rejected, say ' +
    'what the check actually returned, not that it failed.\n\n' +
    'A REJECTION RATE IS EVIDENCE THIS STEP IS WORKING. Verifying every candidate is the ' +
    'shape a rubber stamp takes. Do not reach for a verdict that keeps the proposal ' +
    'alive.\n\n' +
    'Return one disposition per candidate id above -- all of them, none invented. Do NOT ' +
    'edit any file, do not stage or commit, and do not create or switch any git branch.\n\n' +
    'Return the structured object.'
}

// Step 3 of the wave. The survivors, and only the survivors, enter the document.
// It is never a filter over a file that already contains speculative sentences --
// which is why no retraction path exists to fail. It takes one of two shapes: an
// ADDITION to a document written without any hoist in it, or, when the composition
// took the judged null branch, the CREATION of the document those hoists now
// justify. The second shape exists because the survivors of a null-branch subject
// were previously discarded in silence.
const applyPrompt = (r, verified) => {
  return 'Add VERIFIED hoists to an existing CLAUDE.md. Add exactly these and nothing ' +
    'else.\n\n' +
    'Directory: ' + r.root + '\n' +
    'Document to edit: ' + (r.path || (r.root + '/CLAUDE.md')) + '\n\n' +
    'Each entry below was proposed by this directory own composition and then checked ' +
    'against the files it named. The wording is SETTLED: write each sentence as given. ' +
    'Rewording it here would put an unverified claim into the document under a verified ' +
    'claim disposition, which is the one outcome this whole ordering exists to prevent. ' +
    'If a wording cannot be placed as written, leave it out and say so in notes.\n\n' +
    JSON.stringify(verified, null, 2) + '\n\n' +
    'Placement within the document defers to ' + placementRef + ', and the surface form ' +
    'to the code-directory section of ' + standardsRef + '. Put each sentence in the ' +
    'section where it belongs, creating one if none fits, and report which section took ' +
    'it.\n\n' +
    'Do NOT add anything else, do NOT re-word existing content, and do NOT touch the ' +
    'child documents -- removing the child copies a hoist obliges is a separate run per ' +
    'child.\n\n' +
    'ASCII only. Edit exactly one file. Do not stage, commit, or create or switch any git ' +
    'branch.\n\n' +
    'Set created false -- this document already existed. Report the sections you touched ' +
    'in sections.\n\n' +
    'Return the structured object.'
}

// The CREATE variant, for a subject whose composition took the JUDGED null branch
// and whose candidates then survived verification. It is a separate builder rather
// than a flag on the one above because it needs two clauses that step never had:
// the ambient chain (without it a created document restates whatever an ancestor
// already carries, and C-1 goes unenforced on exactly the documents this path newly
// produces) and the artifact type (without it the step writes some other kind of
// markdown in a code directory). Both are shared verbatim with the compose prompt.
//
// It is NOT reachable for an input-unreadable subject. That distinction is enforced
// at the apply filter, not here -- a prompt cannot refuse a dispatch it received.
const createPrompt = (s, r, verified) => {
  return 'CREATE a code-directory CLAUDE.md holding exactly these VERIFIED hoists, and ' +
    'nothing else.\n\n' +
    'Directory: ' + r.root + '\n' +
    'Document to create: ' + (r.path || (r.root + '/CLAUDE.md')) + '\n\n' +
    'THIS DIRECTORY HAS NO DOCUMENT YET, AND THAT IS THE EXPECTED STATE. Its composition ' +
    'assessed its own direct code and found nothing that earned ambient cost -- a real ' +
    'result, not a failure. What it DID find is the hoists below: facts drawn from its ' +
    'children documents, worded true at this depth, and each one then checked against the ' +
    'files it named. A verified hoist belongs at this depth whether or not the directory ' +
    'own code had anything to say, so the document exists to carry them.\n\n' +
    'Each entry below is SETTLED: write each sentence as given. Rewording it here would ' +
    'put an unverified claim into the document under a verified claim disposition, which ' +
    'is the one outcome this whole ordering exists to prevent. If a wording cannot be ' +
    'placed as written, leave it out and say so in notes.\n\n' +
    JSON.stringify(verified, null, 2) + '\n\n' +
    artifactTypeClause + '\n\n' +
    chainClauseFor(s) + '\n\n' +
    'Placement within the document defers to ' + placementRef + ', and the surface form ' +
    'to the code-directory section of ' + standardsRef + '. Create the sections these ' +
    'sentences need and no others, and report which section took each sentence.\n\n' +
    'ADD NOTHING BEYOND THE SENTENCES ABOVE. Do not re-assess this directory own code, do ' +
    'not add an orientation paragraph describing what the directory contains (it earns ' +
    'nothing), and do NOT touch the child documents -- removing the child copies a hoist ' +
    'obliges is a separate run per child.\n\n' +
    'ASCII only. Write exactly one file. Do not stage, commit, or create or switch any git ' +
    'branch.\n\n' +
    'Set created true and report the sections you created in sections.\n\n' +
    'Return the structured object.'
}

// ---------------------------------------------------------------------------
// Dispatch, wave by wave, deepest first. The loop IS the topological order, and
// each wave is now COMPOSE -> VERIFY -> APPLY rather than a single step.
// ---------------------------------------------------------------------------
phase('Generate')

const perSubject = []
// Wrote a document. Only these are offered to a parent as composition input.
const writtenByRoot = new Set()
// RESOLVED, which is a stronger condition than the "processed" this set used to
// hold. A directory is resolved when it has been composed AND every candidate
// hoist it proposed has a terminal disposition AND the survivors have been
// applied -- written OR null-branch OR skipped, in each case with nothing left
// pending. The ordering guard checks THIS, because a document whose candidates
// are still unresolved is not finished in the sense the barrier means: a parent
// composing from it would be reading a document that is about to gain sentences.
const resolvedRoots = new Set()
// One record per wave, and its ABSENCE is a failure rather than a silent pass.
// A corpus in which nothing hoisted is the expected output of both a correct run
// over children sharing nothing hoistable AND a run whose verification step never
// executed; these numbers are what tell those two apart.
const waveRecords = []
// Subjects whose verified hoists had no document to land in. Collected across waves
// so the end-of-run summary names them, since a per-wave log line scrolls away.
const createFailures = []

// A directory finished by an earlier run satisfies both: it is resolved, and it
// has a document to offer its parent.
for (const r of finishedDocuments) {
  resolvedRoots.add(r)
  writtenByRoot.add(r)
}
if (finishedDocuments.length) {
  log('Composing against ' + finishedDocuments.length + ' document(s) written by an ' +
      'earlier run; those directories are NOT regenerated. A stale one silently ' +
      'corrupts its parent -- the model trusts a child document as an input.')
}

// ASCENDING, and the direction is the whole correctness property. waveOf()
// returns 0 for a directory with NO code-bearing descendants, so wave 0 is the
// DEEPEST set and each successive wave is shallower. Iterating from
// waves.length-1 downward therefore runs parents FIRST -- the exact defect this
// lane exists to prevent, and it does not announce itself: the parent still has
// its own direct code and its own candidates, so it emits a confident,
// internally-consistent document composed from children that do not exist yet,
// and its hoists array is empty because it had nothing to compare.
// (Shipped inverted in 0.48.0; caught only by a real dispatch, never by reading.)
for (let w = 0; w < waves.length; w++) {
  const wave = waves[w] || []
  if (!wave.length) continue

  // Structural guard: prove the ordering rather than trusting the loop. Every
  // descendant of every directory in this wave must ALREADY be RESOLVED -- not
  // merely composed, but composed with every candidate hoist dispositioned and
  // every survivor applied. A future edit that flips the direction again, or that
  // moves verification out of the wave to batch it at the end, fails here instead
  // of silently producing parents composed from documents still due to change.
  for (const root of wave) {
    const unresolved = descendantsOf(root).filter((d) => !resolvedRoots.has(d))
    if (unresolved.length) {
      throw new Error(
        'claude-md-generate: wave ordering violated. ' + root + ' is being composed ' +
        'before its descendant(s) are resolved: ' + unresolved.join(', ') + '. A parent ' +
        'composed from unwritten -- or from not-yet-resolved -- children produces an ' +
        'internally-consistent document built from half its input, so this refuses ' +
        'rather than proceeding.'
      )
    }
  }

  log('Wave ' + w + ': ' + wave.length + ' directory/ies (deepest first; every directory ' +
      'below this wave now has a finished document or a recorded null branch)')

  const results = await parallel(wave.map((root) => () => {
    const s = byRoot.get(root)

    // A subject the caller marked as skipped never reaches an agent. Deciding it
    // here, mechanically, keeps a skip from being reported as a written document.
    //
    // It closes its candidate accounting at ZERO -- nothing was read, so nothing
    // needs a disposition -- and it is EXEMPT from the candidateCount cross-check
    // below, because a skipped subject can legitimately carry a non-zero
    // candidateCount: the report exists, the caller chose not to spend it.
    // writtenFalseReason is null-branch because no document is produced and the
    // reason is not a failed read; nothing downstream can create one for it, since
    // a skip proposes no candidates and so is never an apply target.
    if (s.skipNote) {
      return Promise.resolve({
        root, written: false, writtenFalseReason: 'null-branch', path: '', sections: [],
        candidatesRead: 0, candidateDispositions: [], droppedCandidates: [],
        verifications: [], hoists: [], candidateHoists: [], notProposed: [],
        potentialDefects: [],
        notes: ['skipped by caller: ' + s.skipNote],
      })
    }

    // Only children that ACTUALLY produced a document are offered as composition
    // input. A child that took the null branch has no document to read, and
    // naming a non-existent path would send the agent looking for a file that
    // is absent for a legitimate reason.
    const writtenChildren = directChildrenOf(root).filter((c) => writtenByRoot.has(c))

    return agent(lanePrompt(s, root, writtenChildren), {
      label: 'generate:' + root.split('/').pop(),
      phase: 'Generate',
      model: 'opus',
      effort: 'high',
      schema: DOC_SCHEMA,
    }).then((r) => ({ ...r, root }))
  }))

  // ---- Step 1 done: COMPOSE (propose). ----
  const composed = results.filter(Boolean).map((r) => ({ ...r, root: norm(r.root) }))

  // A composition that WROTE a hoist has done the one thing the ordering removes,
  // and it has already done it -- the sentence is in the file. Refuse rather than
  // absorb it: tolerating it silently degrades the run to hoist-on-plausibility
  // while every count still looks healthy, which is precisely the shape of
  // failure this design was chosen over.
  const speculative = composed.filter((r) => (r.hoists || []).length)
  if (speculative.length) {
    throw new Error(
      'claude-md-generate: ' + speculative.map((r) => r.root).join(', ') + ' returned a ' +
      'WRITTEN hoist from the composition step. A composition proposes into ' +
      'candidateHoists and writes none; hoists is populated only by the apply step, ' +
      'from candidates the verification step let through. The document(s) named now ' +
      'contain an unverified ambient claim and must be regenerated.'
    )
  }

  // ---- Step 2: VERIFY. ----
  const proposers = composed.filter((r) => (r.candidateHoists || []).length)
  const verdictByRoot = new Map()
  if (proposers.length) {
    const verdicts = await parallel(proposers.map((r) => () =>
      agent(verifyPrompt(r), {
        label: 'verify-hoists:' + r.root.split('/').pop(),
        phase: 'Generate',
        model: 'opus',
        effort: 'high',
        schema: VERIFY_SCHEMA,
      }).then((v) => ({ ...v, root: r.root }))
    ))
    for (const v of verdicts.filter(Boolean)) verdictByRoot.set(norm(v.root), v)
  }

  // Resolution accounting, and every branch of it is a REFUSAL rather than a
  // count. "Candidates proposed, no dispositions returned" is a failed run in the
  // scoring model, not a wave that hoisted nothing, so it must not be reachable
  // by omission here either.
  const dispositionsByRoot = new Map()
  for (const r of proposers) {
    const v = verdictByRoot.get(r.root)
    if (!v || !Array.isArray(v.dispositions)) {
      throw new Error(
        'claude-md-generate: ' + r.root + ' proposed ' + r.candidateHoists.length +
        ' candidate hoist(s) and the verification step returned no dispositions. That ' +
        'is a failed run, not a wave that hoisted nothing -- the two are different ' +
        'results and are never folded together.'
      )
    }
    const byId = new Map(v.dispositions.map((d) => [d.id, d]))
    const missing = r.candidateHoists.filter((c) => !byId.has(c.id)).map((c) => c.id)
    if (missing.length) {
      throw new Error(
        'claude-md-generate: ' + r.root + ' has candidate(s) with no disposition: ' +
        missing.join(', ') + '. Every proposed candidate gets exactly one of ' +
        'hoist-verified, hoist-rejected, hoist-unverifiable.'
      )
    }
    const invented = v.dispositions.filter((d) => !r.candidateHoists.some((c) => c.id === d.id))
    if (invented.length) {
      throw new Error(
        'claude-md-generate: ' + r.root + ' returned disposition(s) for candidate id(s) ' +
        'that were never proposed: ' + invented.map((d) => d.id).join(', ') + '.'
      )
    }

    // THE READ BOUND, ENFORCED RATHER THAN REQUESTED. Prompt text asking an agent
    // not to widen its read is exactly the kind of instruction a plausible
    // hypothesis talks it out of, and the widening leaves no trace in the
    // document. filesRead against claimedOver turns it into a mechanical
    // property: a check that had to look outside the claim did not verify the
    // claim, it discovered the candidate was mis-scoped, and a mis-scope is a
    // rejection. Downgrading rather than throwing is deliberate -- the run's own
    // answer is preserved verbatim in the reason, and one over-eager check does
    // not cost the corpus.
    for (const d of v.dispositions) {
      const c = r.candidateHoists.find((x) => x.id === d.id)
      const claimed = new Set((c.claimedOver || []).map(norm))
      const escaped = (d.filesRead || []).map(norm).filter((f) => !claimed.has(f))
      if (escaped.length && d.disposition === 'hoist-verified') {
        d.disposition = 'hoist-rejected'
        d.reason =
          'MIS-SCOPED (enforced by the lane, not reported by the check): verification ' +
          'read file(s) the candidate did not claim over -- ' + escaped.join(', ') + '. ' +
          'A claim can only be verified over the files it named in advance. The check ' +
          'reported: ' + String(d.reason)
      }
    }
    dispositionsByRoot.set(r.root, v.dispositions)
  }

  // ---- Step 3: APPLY the survivors, and nothing else. ----
  const applyTargets = proposers
    .map((r) => ({
      r,
      verified: r.candidateHoists.filter((c) =>
        (dispositionsByRoot.get(r.root) || []).some(
          (d) => d.id === c.id && d.disposition === 'hoist-verified')),
    }))
    // SURVIVORS ARE THE PREDICATE; the document's existence is not. A composition
    // that took the JUDGED null branch still proposed candidates, and a candidate
    // that survived verification is a fact established at THIS depth -- discarding
    // it because the directory's own code had nothing to say loses a verified fact
    // in silence, which is what this filter used to do. An INPUT-UNREADABLE subject
    // is the opposite case and stays excluded: it was never assessed, so writing a
    // document for it is the thing the inputless guard refuses. godot/extensions --
    // nulled with zero survivors -- is the control confirming that t.verified.length
    // remains the core of the predicate.
    .filter((t) => t.verified.length &&
      (t.r.written || t.r.writtenFalseReason === 'null-branch'))
  const appliedByRoot = new Map()
  if (applyTargets.length) {
    const applied = await parallel(applyTargets.map((t) => () =>
      agent(
        t.r.written
          ? applyPrompt(t.r, t.verified)
          : createPrompt(byRoot.get(t.r.root) || {}, t.r, t.verified),
        {
          label: (t.r.written ? 'apply-hoists:' : 'create-from-hoists:') +
            t.r.root.split('/').pop(),
          phase: 'Generate',
          model: 'opus',
          effort: 'high',
          schema: APPLY_SCHEMA,
        }).then((a) => ({ ...a, root: t.r.root }))
    ))
    for (const a of applied.filter(Boolean)) appliedByRoot.set(norm(a.root), a)
  }

  // REPLACES THE DIAGNOSTIC THIS FIX RETIRES. unappliedNote at the end of the run
  // is what CAUGHT the silent discard, and it stops firing once the create path
  // works. Without a successor, a future break in that path reverts to exactly the
  // same silent loss with nothing in the output to show it. A note, not a throw:
  // the rest of the corpus is unaffected and a half-written run helps nobody.
  for (const t of applyTargets) {
    if (t.r.written) continue
    const a = appliedByRoot.get(t.r.root)
    if (!a || a.created !== true) {
      const failure = t.r.root + ' (' + t.verified.length + ' verified hoist(s))'
      createFailures.push(failure)
      log('LOSS: ' + failure + ' took the null branch, its hoists were verified, and the ' +
          'apply step did NOT report creating a document. Those settled sentences exist ' +
          'nowhere. This is the failure the create path was added to close -- treat a ' +
          'recurrence as that path breaking, not as an odd result.')
    }
  }

  // The barrier above is the dependency. Record what this wave produced BEFORE
  // the next (shallower) wave starts, because that wave composes from it -- and
  // a wave is not recorded until all three of its steps are done, which is what
  // makes resolvedRoots stronger than the old processed set.
  let proposed = 0, verified = 0, rejected = 0, unverifiable = 0, notProposed = 0
  for (const r of composed) {
    const dispositions = dispositionsByRoot.get(r.root) || []
    const applied = appliedByRoot.get(r.root)
    const appliedIds = new Set(((applied && applied.applied) || []).map((a) => a.id))

    // hoists is DERIVED from the candidates the phase passed AND the apply step
    // actually placed, never taken as a second statement of the same thing. A
    // verified candidate the apply step left out is not a hoist, and saying so
    // here is what keeps every hoists entry corresponding to a sentence in the
    // document.
    const hoists = (r.candidateHoists || [])
      .filter((c) => appliedIds.has(c.id))
      .map((c) => ({ fact: c.fromClaim, fromChildren: c.fromChildren, wording: c.wording }))

    proposed += (r.candidateHoists || []).length
    notProposed += (r.notProposed || []).length
    for (const d of dispositions) {
      if (d.disposition === 'hoist-verified') verified++
      else if (d.disposition === 'hoist-rejected') rejected++
      else unverifiable++
    }

    // CANDIDATE ACCOUNTING. The declined dispositions are DERIVED from
    // droppedCandidates rather than authored beside it, for the same reason hoists
    // is derived above: two agent-authored records of one decline can disagree, and
    // nothing could then say which is true. The merged set is what the checks below
    // and every later reader see.
    const declined = (r.droppedCandidates || []).map((d) => ({
      index: d.index,
      factExcerpt: String(d.fact || '').slice(0, 80),
      disposition: 'declined',
      reason: String(d.reasonCode || 'unspecified') + ': ' + String(d.reason || ''),
    }))
    const allDispositions = (r.candidateDispositions || []).concat(declined)

    const created = !!(applied && applied.created === true)
    const record = { ...r, hoists, hoistDispositions: dispositions, created,
                     candidateDispositions: allDispositions }
    if (created) {
      // The composition returned path '' and sections [] because it wrote nothing.
      // A created document exists, so the record must say where it is and what is
      // in it -- otherwise perSubject reports a null branch over a file on disk and
      // every downstream count reads that report rather than the disk.
      record.path = applied.path || (r.root + '/CLAUDE.md')
      record.sections = applied.sections || []
    }
    if (applied && applied.notes && applied.notes.length) {
      record.notes = (r.notes || []).concat(applied.notes.map((n) => 'apply: ' + n))
    }

    // THE COMPLETENESS CHECK DOWNGRADES; IT DOES NOT THROW. By the time it runs,
    // compose has already written every document in this wave to disk, so a throw
    // aborts the run and leaves a half-written corpus with no result object at all
    // -- trading a 1.6% accounting loss for a 100% one. It follows the read-bound
    // precedent above (downgrade, say why in-line) rather than the verify-side
    // throws, which fire before anything has been written. The contract it enforces
    // is therefore: every candidate has a disposition, OR the run NAMES the subjects
    // that failed to provide one. Both halves are falsifiable.
    //
    // The conditional requirements live here rather than in DOC_SCHEMA because the
    // schema cannot express them -- a section that must exist in a sibling array, a
    // reason required only for one enum value.
    const subj = byRoot.get(r.root) || {}
    const count = subj.skipNote ? null : candidateCountOf(subj)
    const read = r.candidatesRead
    const shortfalls = []
    const indices = allDispositions.map((d) => d.index)
    if (new Set(indices).size !== indices.length) {
      shortfalls.push('duplicate candidate index/indices')
    }
    if (count !== null && indices.some((i) => !Number.isInteger(i) || i < 1 || i > count)) {
      shortfalls.push('index outside 1..' + count)
    }
    if (allDispositions.length !== read) {
      shortfalls.push(allDispositions.length + ' disposition(s) for ' + read +
                      ' candidate(s) read')
    }
    if (count !== null && read !== count) {
      shortfalls.push('read ' + read + ' of ' + count + ' candidate(s) in the report')
    }
    const sectionSet = new Set(record.sections || [])
    for (const d of allDispositions) {
      if (d.disposition === 'written' && !sectionSet.has(d.section)) {
        shortfalls.push('written candidate ' + d.index + ' names section ' +
                        JSON.stringify(d.section) + ', which this document does not have')
      }
      if (d.disposition === 'deferred' && !String(d.reason || '').trim()) {
        shortfalls.push('deferred candidate ' + d.index + ' carries no reason')
      }
    }
    for (const d of r.droppedCandidates || []) {
      if (allDispositions.filter((x) => x.index === d.index).length !== 1) {
        shortfalls.push('dropped candidate ' + d.index +
                        ' does not appear exactly once in the dispositions')
      }
    }
    // The two false values must stay distinguishable in both directions, or the
    // apply filter above is deciding on a field nobody maintained.
    const wfr = r.writtenFalseReason
    if (r.written && wfr !== 'n/a') {
      shortfalls.push('written true with writtenFalseReason ' + String(wfr))
    }
    if (!r.written && wfr === 'n/a') {
      shortfalls.push('written false with writtenFalseReason n/a -- say which false it is')
    }
    if (shortfalls.length) {
      record.incomplete = true
      record.incompleteReasons = shortfalls
      log('INCOMPLETE ACCOUNTING: ' + r.root + ' -- ' + shortfalls.join('; ') +
          '. The run continues; the documents already written are unaffected.')
    }

    perSubject.push(record)
    resolvedRoots.add(r.root)
    // A CREATED document counts exactly as a written one here. Without this the
    // parent never reads it: writtenByRoot is what offers a child's document as
    // composition input, so a document created from verified hoists would exist on
    // disk and be invisible to every ancestor.
    if (r.written || created) writtenByRoot.add(r.root)
  }

  waveRecords.push({
    wave: w, subjects: wave.length, composed: composed.length,
    phaseRan: true, proposed, verified, rejected, unverifiable, notProposed,
  })
  log('Wave ' + w + ' resolved: ' + proposed + ' candidate hoist(s) proposed, ' +
      verified + ' verified, ' + rejected + ' rejected, ' + unverifiable +
      ' unverifiable; ' + notProposed + ' child claim(s) considered and not proposed.')
}

// ---------------------------------------------------------------------------
// Totals. Derived, never taken on trust.
// ---------------------------------------------------------------------------
const totals = perSubject.reduce((acc, r) => {
  // A DOCUMENT EXISTS EITHER WAY. r.written is the composition's answer about its
  // own direct code; a created document is one the apply step wrote from verified
  // hoists after that answer was false. Counting the second as a null branch is how
  // a run reported "0 document(s) written ... 3 VERIFIED hoist(s)" with three
  // documents on disk. created is counted separately as well, because the two are
  // different provenances and a reader should be able to see how many of each.
  const hasDocument = r.written || r.created
  if (hasDocument) acc.written++
  else acc.nullBranch++
  if (r.created) acc.created++
  if (r.incomplete) acc.incomplete++
  // r.sections is the apply step's list for a created document (see the record
  // assembly above) and the composition's for a written one; compose returns none
  // for a null branch, so reading it unconditionally undercounts by exactly the
  // sections the create path produced.
  acc.sections += (r.sections || []).length
  // VERIFIED hoists only, because hoists now holds nothing else. The proposed
  // set is counted beside it rather than inside it: "proposed and refuted" and
  // "never proposed" are different results and neither is a hoist, so a single
  // number over them would answer no question anyone asks.
  acc.hoists += (r.hoists || []).length
  acc.proposed += (r.candidateHoists || []).length
  acc.notProposed += (r.notProposed || []).length
  for (const d of r.hoistDispositions || []) {
    if (d.disposition === 'hoist-verified') acc.verified++
    else if (d.disposition === 'hoist-rejected') acc.rejected++
    else acc.unverifiable++
  }
  acc.dropped += (r.droppedCandidates || []).length
  // The at-risk class: a fact declined here that can only re-enter above by a
  // hoist some child must first have written down. Counted separately because
  // folding it into `dropped` is how it would go quiet.
  acc.escalated += (r.droppedCandidates || []).filter((d) => d.escalateToAncestor).length
  // potentialDefects is the hazard-durability release valve (capability-boundaries.md,
  // "The hand-off"). defectFiles counts subjects, not files-actually-created-by-this-run:
  // a subject with a non-empty array ends this run with a CLAUDE-potential-defects.md in
  // its directory either way, whether freshly written or already present and left alone
  // per the prompt's leave-pre-existing-file-untouched instruction (the pre-existing case
  // is surfaced in that subject's notes, not distinguished here).
  const defectCount = (r.potentialDefects || []).length
  acc.potentialDefects += defectCount
  if (defectCount) acc.defectFiles++
  // A document with zero recorded verifications is not proof of anything, but it
  // is the shape a report-not-artifact run takes, so it is surfaced. A CREATED
  // document is in scope here too -- it is a document, and exempting it would let
  // the create path be the one way to reach a corpus with no recorded checks.
  if (hasDocument && !(r.verifications || []).length) acc.unverified++
  return acc
}, {
  written: 0, created: 0, nullBranch: 0, sections: 0, hoists: 0, dropped: 0,
  escalated: 0, incomplete: 0,
  unverified: 0, proposed: 0, verified: 0, rejected: 0, unverifiable: 0, notProposed: 0,
  potentialDefects: 0, defectFiles: 0,
})

// A verified candidate that never became a hoist means the apply step dropped one
// after the phase passed it. Surfaced rather than reconciled silently, because it
// is the one way a settled sentence can still fail to reach a document.
const unappliedNote = totals.verified > totals.hoists
  ? ', ' + (totals.verified - totals.hoists) + ' verified candidate(s) were NOT applied ' +
    'to a document -- review these, a settled wording that never landed is a loss'
  : ''

// The candidate accounting that did not close. LOUD and NAMED, because the whole
// point of the downgrade is that the run finishes: a count alone would say a
// disposition is missing without saying whose, and the alternative to naming them
// here is the silence this replaced.
const incompleteSubjects = perSubject.filter((r) => r.incomplete)
const incompleteNote = incompleteSubjects.length
  ? ', ' + incompleteSubjects.length + ' subject(s) did NOT close their candidate ' +
    'accounting and are marked incomplete -- ' +
    incompleteSubjects.map((r) => r.root + ' [' + (r.incompleteReasons || []).join('; ') + ']')
      .join(' | ') +
    '. Every candidate is meant to carry exactly one terminal disposition; for these ' +
    'subjects that is unproven, so treat their reports as partial'
  : ''

// The successor to unappliedNote for the create path specifically; see the in-wave
// check that fills this.
const createFailureNote = createFailures.length
  ? ', ' + createFailures.length + ' null-branch subject(s) had VERIFIED hoists and no ' +
    'document was created for them: ' + createFailures.join(', ') +
    ' -- those settled sentences exist nowhere'
  : ''

// Proposed but zero-rate outcomes read very differently and must not be inferred
// from the hoist count alone. A phase that rejects nothing is the shape a rubber
// stamp takes; a wave with no record at all is a failed run whatever its
// documents look like, which is why waveRecords is returned rather than summed
// away here.
const candidateNote = totals.proposed
  ? ', ' + totals.proposed + ' candidate hoist(s) proposed -> ' + totals.verified +
    ' verified / ' + totals.rejected + ' rejected / ' + totals.unverifiable +
    ' unverifiable' +
    (totals.rejected + totals.unverifiable === 0
      ? ' (NOTHING refused: read that as a rubber stamp until a sample is checked by hand)'
      : '')
  : ', NO candidate hoists were proposed' +
    (totals.notProposed
      ? ' (' + totals.notProposed + ' child claim(s) considered and left in the child)'
      : ' AND no child claim was recorded as considered -- a composition that proposes ' +
        'nothing and records no absence is indistinguishable from one with nothing to ' +
        'propose, and only the second is a legitimate result')

const escalatedNote = totals.escalated
  ? ', ' + totals.escalated + ' candidate(s) named an ancestor destination and were NOT ' +
    'written -- under the settled model they re-enter only by a hoist from a child ' +
    'document, so a fact no child wrote down is LOST; review these explicitly'
  : ''
const unverifiedNote = totals.unverified
  ? ', ' + totals.unverified + ' document(s) recorded NO executed verification'
  : ''
const potentialDefectsNote = totals.potentialDefects
  ? ', ' + totals.potentialDefects + ' potential defect(s) recorded across ' +
    totals.defectFiles + ' CLAUDE-potential-defects.md file(s) -- UNVERIFIED possibilities, ' +
    'never findings; verification belongs to the code-audit capability described in ' +
    'capability-boundaries.md'
  : ''
const nullNote = totals.nullBranch
  ? ', ' + totals.nullBranch + ' directory/ies took the null branch (no document, recorded)'
  : ''

const createdNote = totals.created
  ? ' (' + totals.created + ' of them CREATED by the apply step for a directory whose own ' +
    'code earned nothing, from verified hoists alone)'
  : ''

log('Generate: ' + totals.written + ' document(s) written across ' + waves.length +
    ' wave(s)' + createdNote + ', ' + totals.sections + ' section(s), ' + totals.hoists +
    ' VERIFIED hoist(s)' + candidateNote + unappliedNote + createFailureNote +
    incompleteNote + nullNote + escalatedNote + unverifiedNote + potentialDefectsNote +
    '. Verify against the ARTIFACT, not this report: a lane result describes what each ' +
    'run intended.')

// waveRecords is part of the contract, not a diagnostic. Scoring a run needs to
// tell "the phase ran and nothing qualified" from "the phase never ran", and the
// documents look identical in both cases -- the record is the only thing that
// separates them, and its ABSENCE is a failure rather than a silent pass.
return { perSubject, waves, waveRecords, totals }
