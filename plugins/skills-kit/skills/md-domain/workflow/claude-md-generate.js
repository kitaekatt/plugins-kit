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
//                 candidates: [ ... ]|undefined,    // legacy inline form; see below
//                 ambientClaudeMdPaths: string[],   // HINT ONLY -- the agent derives its own
//                 skipNote: string|null } ],        // set => null branch, no document
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
//   refs: { standards: <abs path to references/standards/claude-md-standards.md>,
//           lane:      <abs path to references/lanes/generation-lane.md>,
//           placement: <abs path to references/cohesion-principles.md> }
//   houseStyle: <abs path to an exemplar CLAUDE.md>|undefined
// }
//
// Returns { perSubject, waves, totals }.

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
const inputless = subjects.filter(
  (s) => !s.skipNote && !s.reportPath && !(Array.isArray(s.candidates) && s.candidates.length)
)
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

const DOC_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  // Every field is REQUIRED. An optional disclosure field is a field a run can
  // satisfy on paper and omit in fact -- the same reasoning that makes `notes`
  // required in coverage-detect.js.
  required: ['root', 'written', 'path', 'sections', 'droppedCandidates', 'verifications', 'hoists', 'notes'],
  properties: {
    root: { type: 'string' },
    // false is a REAL result: the null branch of the done-condition, a directory
    // with no insight worth capturing at its scope. It must be recorded rather
    // than left implicit, or "every directory is done" is unfalsifiable.
    written: { type: 'boolean' },
    path: { type: 'string' },
    sections: { type: 'array', items: { type: 'string' } },
    // A candidate the run declined. Required with a reason so a dropped fact is
    // never silently absent -- the retired promotion machinery in older persisted
    // reports makes this the likeliest way a fact goes missing.
    droppedCandidates: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['fact', 'reason'],
        properties: {
          fact: { type: 'string' },
          reason: { type: 'string' },
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
    // Set only by a composition. A hoist must be worded so it is true as stated
    // at the parent depth, and it obliges the child copies to be removed.
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
    notes: { type: 'array', items: { type: 'string' } },
  },
}

// ---------------------------------------------------------------------------
// Wave computation. Derived HERE from the subject set rather than taken from the
// caller, for the same reason discover_hierarchy.py enumerates its own leaves: a
// caller-supplied ordering cannot notice what the caller already forgot, and the
// ordering is the one thing whose violation is invisible in the output.
// ---------------------------------------------------------------------------
const norm = (p) => String(p).replace(/\\/g, '/').replace(/\/+$/, '')
const roots = subjects.map((s) => norm(s.root))

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
const waves = []
for (const r of roots) {
  const w = waveOf(r)
  if (!waves[w]) waves[w] = []
  waves[w].push(r)
}

const lanePrompt = (s, root, writtenChildren) => {
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
  const chain = s.ambientClaudeMdPaths || []
  const hintClause = chain.length
    ? '\n\nThe caller believes the chain to be the list below. Treat it as a HINT ONLY, ' +
      'and prefer what you actually find on disk -- a caller list can be a stale ' +
      'snapshot taken before an ancestor was written:\n' +
      chain.map((p) => '  - ' + p).join('\n')
    : ''

  const chainClause =
    'DERIVE YOUR OWN AMBIENT CHAIN FIRST, BEFORE ANYTHING ELSE. Walk UP from this ' +
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

  const compositionClause = writtenChildren.length
    ? '\nCOMPOSITION -- THIS DIRECTORY HAS CHILDREN, AND THEIR DOCUMENTS ARE YOUR SECOND INPUT.\n' +
      'Read every one of these finished CLAUDE.md files in full:\n' +
      writtenChildren.map((p) => '  - ' + p + '/CLAUDE.md').join('\n') +
      '\n\nThis is not optional enrichment. A composition that skips it produces a document ' +
      'containing only this directory thin layer of direct code, which is strictly worse ' +
      'than the recursive subject it replaced.\n\n' +
      'HOISTING is where de-duplication happens, and it happens HERE because this is the ' +
      'only place the documents being compared have actually been read. A fact appearing ' +
      'in more than one child moves up to this directory.\n\n' +
      'REPETITION TRIGGERS A HOIST; WORDING LICENSES IT. These are two tests and they come ' +
      'apart in both directions. A fact stated by 2 of 20 children, hoisted verbatim, ' +
      'becomes ambient for 18 directories it does not govern. So a hoisted fact must be ' +
      'WORDED so it is true as stated of everything below this directory -- usually by ' +
      'naming its subjects explicitly. Scope lives in the sentence; there is no separate ' +
      'scoping mechanism. When no such wording exists short of a list of exceptions, the ' +
      'fact DOES NOT HOIST -- it stays in the children, and you say so in notes.\n\n' +
      'Report every hoist you make in the hoists field, naming which children stated it ' +
      'and the exact wording you used. A hoist obliges the child copies to be removed, ' +
      'which is a separate run per child -- you do NOT edit the child documents.'
    : '\nThis directory has no in-scope children, so there is no composition step and no hoisting.'

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
      'If the file is missing or unreadable, SAY SO IN NOTES and set written false -- ' +
      'do not improvise candidates from the code, which would silently substitute an ' +
      'un-assessed directory for an assessed one.'
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
    'ARTIFACT TYPE. This is a CODE-DIRECTORY CLAUDE.md -- a review-notes file, the BRANCH ' +
    'in step 1 of ' + laneRef + '. It carries NO claude_md: YAML block and the schema ' +
    'validator is NEVER run on it. Follow the code-directory section of ' + standardsRef +
    ' in the PRODUCING direction: the documented shapes, the high-value observation kinds, ' +
    'symbol anchors in preference to line numbers, no machine-specific absolute paths, and ' +
    'the value gate applied to every entry.\n\n' +
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
    'invisible from inside any one file.\n\n' +
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
    'the writing; nothing else checks a carried-forward rule as RESTATED.\n' +
    '  - Verify by EXECUTING, not by asserting. A previous run claimed every module here ' +
    'has a matching C++ file; an ls disproved it, and the writer had reported it verified. ' +
    'Record every claim you checked, with the command, in verifications. A claim you ' +
    'cannot verify is DROPPED or stated with its limit -- never hedged into the prose.\n\n' +
    'THE BOUNDARY YOU MUST NOT CROSS. md-domain INFORMS code review; it does not perform ' +
    'it. Reading the code is in scope ONLY as a source of insight for the document that ' +
    'will be ambient for it. Do NOT identify code defects, do NOT propose fixes, and do ' +
    'NOT edit any file other than the one CLAUDE.md you are writing. A run that returns a ' +
    'defect list has done the wrong work.\n\n' +
    'DOCUMENTING A HAZARD CAN FOSSILIZE A BUG. Before writing a hazard into ambient prose, ' +
    'ask whether the honest remedy is a code fix or a loud failure. If you judge so, say ' +
    'it in notes -- not in the document, and do not fix it yourself.\n\n' +
    'THE NULL BRANCH IS A REAL RESULT. If nothing in this directory earns ambient cost at ' +
    'this scope level, write NO file, set written false, and say why in notes. That is an ' +
    'admissible outcome, not a failure -- but it must be RECORDED, never left implicit.\n\n' +
    'ASCII only. Write exactly one file and touch nothing else. Do not stage, commit, or ' +
    'create or switch any git branch.\n\n' +
    'Return the structured object.'
}

// ---------------------------------------------------------------------------
// Dispatch, wave by wave, deepest first. The loop IS the topological order.
// ---------------------------------------------------------------------------
phase('Generate')

const perSubject = []
// Wrote a document. Only these are offered to a parent as composition input.
const writtenByRoot = new Set()
// Reached a decision at all -- written OR null-branch OR skipped. This is what
// the ordering guard checks, because a null-branch child is legitimately absent
// and must still count as "its wave completed".
const processedRoots = new Set()

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
  // descendant of every directory in this wave must ALREADY have been processed.
  // A future edit that flips the direction again fails here instead of silently
  // producing composed-from-nothing parents.
  for (const root of wave) {
    const unprocessed = descendantsOf(root).filter((d) => !processedRoots.has(d))
    if (unprocessed.length) {
      throw new Error(
        'claude-md-generate: wave ordering violated. ' + root + ' is being composed ' +
        'before its descendant(s): ' + unprocessed.join(', ') + '. A parent composed ' +
        'from unwritten children produces an internally-consistent document built ' +
        'from half its input, so this refuses rather than proceeding.'
      )
    }
  }

  log('Wave ' + w + ': ' + wave.length + ' directory/ies (deepest first; every directory ' +
      'below this wave now has a finished document or a recorded null branch)')

  const results = await parallel(wave.map((root) => () => {
    const s = byRoot.get(root)

    // A subject the caller marked as skipped never reaches an agent. Deciding it
    // here, mechanically, keeps a skip from being reported as a written document.
    if (s.skipNote) {
      return Promise.resolve({
        root, written: false, path: '', sections: [], droppedCandidates: [],
        verifications: [], hoists: [], notes: ['skipped by caller: ' + s.skipNote],
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

  // The barrier above is the dependency. Record what this wave produced BEFORE
  // the next (shallower) wave starts, because that wave composes from it.
  for (const r of results.filter(Boolean)) {
    perSubject.push(r)
    processedRoots.add(norm(r.root))
    if (r.written) writtenByRoot.add(norm(r.root))
  }
}

// ---------------------------------------------------------------------------
// Totals. Derived, never taken on trust.
// ---------------------------------------------------------------------------
const totals = perSubject.reduce((acc, r) => {
  if (r.written) acc.written++
  else acc.nullBranch++
  acc.sections += (r.sections || []).length
  acc.hoists += (r.hoists || []).length
  acc.dropped += (r.droppedCandidates || []).length
  // The at-risk class: a fact declined here that can only re-enter above by a
  // hoist some child must first have written down. Counted separately because
  // folding it into `dropped` is how it would go quiet.
  acc.escalated += (r.droppedCandidates || []).filter((d) => d.escalateToAncestor).length
  // A document with zero recorded verifications is not proof of anything, but it
  // is the shape a report-not-artifact run takes, so it is surfaced.
  if (r.written && !(r.verifications || []).length) acc.unverified++
  return acc
}, { written: 0, nullBranch: 0, sections: 0, hoists: 0, dropped: 0, escalated: 0, unverified: 0 })

const escalatedNote = totals.escalated
  ? ', ' + totals.escalated + ' candidate(s) named an ancestor destination and were NOT ' +
    'written -- under the settled model they re-enter only by a hoist from a child ' +
    'document, so a fact no child wrote down is LOST; review these explicitly'
  : ''
const unverifiedNote = totals.unverified
  ? ', ' + totals.unverified + ' document(s) recorded NO executed verification'
  : ''
const nullNote = totals.nullBranch
  ? ', ' + totals.nullBranch + ' directory/ies took the null branch (no document, recorded)'
  : ''

log('Generate: ' + totals.written + ' document(s) written across ' + waves.length +
    ' wave(s), ' + totals.sections + ' section(s), ' + totals.hoists + ' hoist(s)' +
    nullNote + escalatedNote + unverifiedNote +
    '. Verify against the ARTIFACT, not this report: a lane result describes what each ' +
    'run intended.')

return { perSubject, waves, totals }
