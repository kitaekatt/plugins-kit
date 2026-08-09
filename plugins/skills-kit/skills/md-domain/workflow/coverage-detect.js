// md-domain coverage verb -- DETECT workflow (the only phase; report-only).
//
// Fan-out assessment, one lane per (code subtree, ambient CLAUDE.md chain). This
// is the first lane in the skill whose subject is CODE rather than a markdown
// file, which is why it is a third verb rather than a criterion inside
// audit_claude_md: the per-file lanes enumerate CLAUDE.md files, and no criterion
// can have a subject its lane cannot enumerate. The decisive case is a subtree
// with NO CLAUDE.md at all.
//
// WHAT THIS IS FOR: md-domain is not a code-review tool. This verb reads code
// only as a SOURCE OF INSIGHT for the CLAUDE.md that will be ambient for it. It
// does not hunt for defects -- that is the job of a code review conducted AGAINST
// the CLAUDE.md this verb helps produce. A run that returns a defect list has
// done the wrong work. The unit of output is always a fact about the code that
// belongs in a CLAUDE.md and is not ambient for the code it describes.
//
// ASSESSMENT CRITERIA live in references/standards/coverage-standards.md. The
// seam is filled through `refs.criteria`, never by embedding or paraphrasing the
// criteria in JavaScript. The guard below remains fail-closed so a miswired
// caller cannot improvise the hazard sweep two adversarial reviews rejected.
//
// NO REMEDIATE LANE, deliberately. Nothing is ever applied, so there is no
// coverage-remediate.js, the sonnet+low remediation pin does not apply, and
// scripts/gen_workflow_js.py is not involved. Report-only is what keeps a third
// verb cheap.
//
// args = {
//   subjects: [ { root: string,
//                 codeFiles: string[],
//                 ambientClaudeMdPaths: string[],   // root-most first; MAY be empty
//                 rootExclusion: string|null,
//                 skipped: [ { path: string, reason: string } ] } ],
//     (all produced by scripts/discover_coverage.py, which is side-effect free and
//     reads no file contents. Do NOT recompute the ambient chain here: the chain
//     INCLUDES a CLAUDE.md at the subtree root, and its upward walk stops at the
//     nearest .git so a nested repo never inherits the outer repo's chain. Both
//     are easy to get wrong by eye.)
//   ceiling: integer|undefined   // candidate cap PER SUBTREE; default below
//   depth: 'basic'|'advanced'    // resolved by the lane's intent gate
//   refs: { criteria: <abs path to the coverage standards doc>,
//           observationKinds: <abs path to references/standards/claude-md-standards.md>,
//           placement: <abs path to references/cohesion-principles.md>,
//           pluginRoot: <abs path to plugins/skills-kit> }
// }
//
// Returns { perSubject, totals }. There is no `review` mode: review mode audits a
// CHANGE to a document, and this verb's subject is a directory.

export const meta = {
  name: 'md-domain-coverage-detect',
  description: 'Fan-out coverage assessment: which facts about this code subtree belong in a CLAUDE.md and are not ambient for it (report-only, no edits)',
  phases: [{ title: 'Coverage', detail: 'one lane per code subtree' }],
}

// Candidate cap, applied PER SUBTREE (not per run). A per-run cap divided across
// subjects would give each subtree an arbitrary share that shrinks as the run
// widens, which is a worse answer than capping each subtree consistently. The
// aggregate is reported so a wide run cannot look complete when it is not.
// When the cap is hit the report SAYS so -- silent truncation in the verb that
// reports silent truncation would be its own joke, and the repo's own rule
// requires a capped run to announce the cap.
const DEFAULT_CEILING = 25

const ceiling = Number.isInteger(input.ceiling) ? input.ceiling : DEFAULT_CEILING

const SUBJECT_FINDINGS_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  // `notes` is REQUIRED, not optional: it is where a ceiling hit reports how
  // many candidates were set aside, and an optional field lets a truncated run
  // validate while saying nothing about the truncation.
  required: ['root', 'candidates', 'verdict', 'ceilingReached', 'notes'],
  properties: {
    root: { type: 'string' },
    verdict: { type: 'string', enum: ['GAPS-FOUND', 'COVERAGE-ASSESSED'] },
    ceilingReached: { type: 'boolean' },
    assessedFileCount: { type: 'integer' },
    candidates: {
      type: 'array',
      // Enforced here as well as asked for in the prompt. Without maxItems a
      // schema-valid response can exceed the cap while the log reports the run
      // as capped.
      maxItems: ceiling,
      items: {
        type: 'object',
        additionalProperties: false,
        // `tier` and `anchors` are REQUIRED for the same reason `notes` is: a
        // criterion the schema cannot carry is a criterion the run can satisfy
        // on paper and omit in fact. CV-4 requires the tier to be REPORTED, and
        // with additionalProperties:false there was nowhere to report it; CV-7
        // is fail-severity and its anchor was optional.
        required: ['fact', 'destination', 'why', 'tier', 'anchors'],
        properties: {
          // The fact as it would read in a CLAUDE.md -- not a description of a
          // defect, and not a code location on its own.
          fact: { type: 'string' },
          // The CLAUDE.md the placement algorithm selects: ambient for the code
          // it describes, never wherever is convenient.
          destination: { type: 'string' },
          why: { type: 'string' },
          // CV-4. FINDING-CONVERTIBLE means a reviewer could catch a violation:
          // quotable imperative + unambiguous test + locatable at file and line.
          // CONTEXT-ONLY is admissible and common -- it just must not be
          // reported as though it were convertible.
          tier: { type: 'string', enum: ['FINDING-CONVERTIBLE', 'CONTEXT-ONLY'] },
          // Set only for the severe-deficiency carve-out, so a reader can tell a
          // documentation gap from the rare case where the code is defective.
          severeDeficiency: { type: 'boolean' },
          // CV-7's evidence floor. minItems:1 because an empty array satisfies a
          // bare `required` while citing nothing.
          anchors: { type: 'array', minItems: 1, items: { type: 'string' } },
        },
      },
    },
    notes: { type: 'array', items: { type: 'string' } },
  },
}

const subjects = Array.isArray(input.subjects) ? input.subjects : []

// The criteria guard. Without a criteria doc this lane has no basis for deciding what
// earns ambient cost, and the failure mode of guessing is the rejected hazard
// sweep -- which looks like it works. Refuse loudly instead.
//
// The check is for a non-empty STRING path, not merely truthiness: `true` is a
// truthy value that names no document, and letting it through would reach agent
// fan-out with nothing to apply.
const criteriaPath = input.refs && input.refs.criteria
if (typeof criteriaPath !== 'string' || criteriaPath.trim() === '') {
  throw new Error(
    'coverage-detect: refs.criteria is not set. The coverage assessment criteria ' +
    'were not wired into this call, and this lane will not improvise them -- an invented ' +
    'predicate reproduces the hazard sweep that two adversarial reviews rejected. ' +
    'Pass the absolute path to coverage-standards.md as refs.criteria, then re-run. ' +
    'See references/lanes/coverage-lane.md, "Step 3 -- Assess".'
  )
}

const depth = input.depth
if (depth !== 'basic' && depth !== 'advanced') {
  throw new Error(
    `coverage-detect: input.depth must be 'basic' or 'advanced'; resolve it at ` +
    'the coverage lane intent gate before dispatch.'
  )
}

const lanePrompt = (s) => {
  const chain = s.ambientClaudeMdPaths || []
  const chainClause = chain.length
    ? `The CLAUDE.md files AMBIENT for this subtree, root-most first:\n${chain.map((p) => `  - ${p}`).join('\n')}\n\nRead every one. A fact already carried by an ambient claim that RESOLVES is NOT a candidate -- that suppression is applied HERE, at assessment time, because establishing it requires reading the ambient document and usually the source it anchors to.`
    : `This subtree has NO ambient CLAUDE.md. Nothing loads for this code at all. That is not an error and not a skip -- it is the strongest form of the finding this verb exists to surface.`

  const exclusionClause = (s.skipped || []).length
    ? `\nAlready excluded structurally (do not assess, and mention in notes):\n${s.skipped.map((k) => `  - [${k.reason}] ${k.path}`).join('\n')}`
    : ''

  const rootClause = s.rootExclusion
    ? `\nNOTE: this root is itself ${s.rootExclusion}. It is being assessed because the user named it explicitly; say so in notes.`
    : ''

  const depthClause = depth === 'advanced'
    ? `ANALYSIS DEPTH: advanced. Read every source file completely. First run an
invariant-discovery pass and carry those invariants into assessment. After
assessment, run a verification pass over every surviving candidate against the
source. At this depth COVERAGE-ASSESSED means verified absent.`
    : `ANALYSIS DEPTH: basic. Use a bounded, sampled read and one assessment pass.
At this depth COVERAGE-ASSESSED means not found within budget.`

  return `Assess the CLAUDE.md COVERAGE of one code subtree.

Subtree: ${s.root}
Code files (${(s.codeFiles || []).length}):
${(s.codeFiles || []).map((p) => `  - ${p}`).join('\n')}

${chainClause}${exclusionClause}${rootClause}

${depthClause}

WHAT YOU ARE LOOKING FOR -- read this before anything else.

md-domain is not a code-review tool. You are NOT reviewing this code. You are not
looking for bugs, and you must not return a defect list. You are answering ONE
question: which facts about this code belong in a CLAUDE.md that will be AMBIENT
for it, and are not there today?

Finding defects is the job of a code review conducted AGAINST the CLAUDE.md this
verb helps produce. If you find yourself enumerating what is wrong with the code,
you have answered the wrong question.

THE SEVERE-DEFICIENCY CARVE-OUT. If you incidentally establish that the code is
defective, report it ONLY when severe, mark it severeDeficiency: true, and still
express it as CLAUDE.md content. The bar is deliberately high, for two reasons
that survive: documenting a hazard can FOSSILIZE a bug whose right answer was a
code change ("this silently truncates at 65536" enshrines the truncation as
behaviour to preserve); and a stated invariant the code contradicts is a
contradiction to surface, not to write down twice. When in doubt, do not report
it -- a missed deficiency is recoverable by a code review, a fossilized one is not.

CRITERIA. Apply the criteria in ${input.refs.criteria} verbatim. That document,
not your judgment about what seems important, decides what earns ambient cost.
The observation kinds it builds on are in ${input.refs.observationKinds || 'the claude-md standards doc'}
(the AUTHORING direction's list of what is worth writing up).

DESTINATION. Every candidate names the CLAUDE.md the placement algorithm in
${input.refs.placement || 'references/cohesion-principles.md'} selects -- ambient for the code it
describes. Ambient budget is PER FILE: putting a fact in a file that already
loads somewhere else does not make it reach this code. Do not default to the
nearest CLAUDE.md.

TIER (CV-4). Classify every surviving candidate as FINDING-CONVERTIBLE or
CONTEXT-ONLY. FINDING-CONVERTIBLE requires ALL THREE: an imperative a reviewer
can quote verbatim, a violation test that is unambiguous rather than
discretionary, and a violation locatable at a file and line. CONTEXT-ONLY is a
real and admissible outcome -- orientation and architecture facts earn ambient
space without being convertible. What is forbidden is reporting a CONTEXT-ONLY
fact as though it were convertible; when the three do not all hold, say
CONTEXT-ONLY.

EVIDENCE (CV-7). Every candidate cites at least one file and line you OBSERVED
in source, in anchors. A convention needs two or more observed instances or one
authoritative source. Code outranks comments, guides, and rationale. Names,
layout, and repeated patterns start an investigation; they are not evidence on
their own. A fact you cannot anchor to observed source is DROPPED, not hedged
and not reported with a guess at a location.

CEILING. Report at most ${ceiling} candidates FOR THIS SUBTREE. If you would have exceeded it, set
ceilingReached: true and say in notes how many you set aside. Never truncate
silently.

HONESTY. Your result is a SAMPLE, not an inventory -- two thorough reviewers over
one corpus found largely different facts. Do not imply exhaustiveness.

VERDICT. GAPS-FOUND if at least one candidate survives; COVERAGE-ASSESSED if the
subtree was assessed and none did. NEVER emit COMPLIANT or NON-COMPLIANT: those
belong to the document lanes and answer a different question. A CLAUDE.md can be
COMPLIANT while its subtree is GAPS-FOUND at the same moment.

Return the structured object.`
}

phase('Coverage')
const perSubject = await parallel(subjects.map((s) => () =>
  // Detection is this verb's judgment core, so the tier is pinned rather than
  // inherited. This matters more here than in the document lanes: a coverage run
  // normally has exactly ONE subject, and the audit lane's single-subject
  // shortcut runs inline at whatever model the session happens to be on. Going
  // through the workflow regardless of count is what keeps the common case on-pin.
  agent(lanePrompt(s), {
    label: `coverage:${String(s.root).split(/[\\/]/).pop()}`,
    phase: 'Coverage',
    model: 'opus',
    effort: 'high',
    schema: SUBJECT_FINDINGS_SCHEMA,
  }).then((r) => ({ ...r, root: s.root }))
))

// The verdict is DERIVED, never taken on trust. The schema can constrain the
// verdict to two values but cannot express "GAPS-FOUND iff candidates is
// non-empty", so a schema-valid response can carry GAPS-FOUND with zero
// candidates (or the reverse) and contradict the decision rules the lane doc
// states. Recomputing it here makes the rule true by construction.
const results = perSubject.filter(Boolean).map((r) => {
  const candidates = r.candidates || []
  const derived = candidates.length ? 'GAPS-FOUND' : 'COVERAGE-ASSESSED'
  const notes = Array.isArray(r.notes) ? [...r.notes] : []
  if (r.verdict && r.verdict !== derived) {
    notes.push(
      `verdict corrected: lane returned ${r.verdict} with ${candidates.length} candidate(s)`
    )
  }
  // A ceiling hit must never be silent, even if the lane forgot to say so.
  if (r.ceilingReached && !notes.some((n) => /ceiling|set aside|capped/i.test(n))) {
    notes.push(`candidate ceiling of ${ceiling} reached; results are capped, not complete`)
  }
  return { ...r, candidates, verdict: derived, depth, notes }
})

// The ambient chain is an INPUT fact, not something the lane reports back, so
// the uncovered tally is computed from the subjects rather than the results --
// reading it off the agent's object would count every subtree as uncovered.
const chainSizeByRoot = new Map(
  subjects.map((s) => [String(s.root), (s.ambientClaudeMdPaths || []).length])
)

const totals = results.reduce((acc, r) => {
  acc.candidates += (r.candidates || []).length
  acc.severe += (r.candidates || []).filter((c) => c.severeDeficiency).length
  if (r.verdict === 'GAPS-FOUND') acc.gapsFound++
  if (r.verdict === 'COVERAGE-ASSESSED') acc.assessed++
  if (r.ceilingReached) acc.ceilingReached++
  // Counted apart from the verdicts: a subtree nothing covers is the finding
  // this verb exists for, and folding it into gapsFound would hide it.
  if (!chainSizeByRoot.get(String(r.root))) acc.uncovered++
  return acc
}, { candidates: 0, severe: 0, gapsFound: 0, assessed: 0, ceilingReached: 0, uncovered: 0 })

// The ceiling is per subtree, so a wide run's aggregate is subjects x ceiling.
// Stating the aggregate keeps a capped multi-subtree run from reading as
// complete just because no single subtree looks truncated.
const ceilingNote = totals.ceilingReached
  ? `, ${totals.ceilingReached}/${results.length} subtree(s) hit the per-subtree ceiling of ${ceiling} (those results are capped, not complete)`
  : ''
const severeNote = totals.severe ? `, ${totals.severe} severe-deficiency` : ''
const uncoveredNote = totals.uncovered
  ? `, ${totals.uncovered} subtree(s) with NO ambient CLAUDE.md at all`
  : ''

log(`Coverage (depth=${depth}): assessed ${results.length}/${subjects.length} subtree(s): ${totals.gapsFound} GAPS-FOUND, ${totals.assessed} COVERAGE-ASSESSED, ${totals.candidates} candidate(s)${severeNote}${uncoveredNote}${ceilingNote}. Advisory and non-idempotent: re-runs may differ, and nothing is applied.`)

return { perSubject: results, totals, ceiling, depth }
