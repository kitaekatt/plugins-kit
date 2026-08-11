// md-domain hierarchy verb -- DETECT workflow (the only phase; report-only).
//
// One lane over ONE subject: a claude_md_tree -- a named directory root, every
// CLAUDE.md governing files beneath it, and the persisted coverage reports
// targeting subtrees under it. It resolves PLACEMENT across that tree: one home
// per fact, duplicates collapsed, subtractions stated at each source, leaf
// dispositions re-judged, unplaceable facts declared.
//
// WHY IT IS NOT A COVERAGE RUN AND NOT AN AUDIT: coverage's subject is one
// subtree and its criteria judge ABSENT facts; the per-document audit lanes'
// criteria are per-file. Every criterion here is a RELATION between documents,
// or between a proposal and a document, and cannot be evaluated on any single
// file. It also consumes another lane's persisted output, which no audit lane
// does.
//
// ASSESSMENT CRITERIA live in references/standards/hierarchy-standards.md. The
// seam is filled through `refs.criteria`, never by embedding or paraphrasing the
// criteria in JavaScript. The guard below is fail-closed.
//
// NO REMEDIATE LANE, deliberately. The plan spans lanes with an ordering
// constraint (write the destination BEFORE subtracting the source -- a fact
// deleted from its only home before its replacement exists is a fact that exists
// nowhere, and nothing greps for an absence), it contains editorial rejections a
// user must be able to overrule per item, and its disposition re-judgments are
// judgment calls the user owns. So there is no hierarchy-remediate.js, the
// sonnet+low remediation pin does not apply, and scripts/gen_workflow_js.py is
// not involved.
//
// args = {
//   subject: { root: string,
//              leaves: string[],                 // enumerated by the lane, not the caller
//              claudeMdPaths: string[],          // documents in the tree
//              ambientAbove: string[],           // documents above the root
//              reports: [ { source, root, candidates, candidateCount, assessedNull } ],
//              inventory: [ { leaf, status, sources } ],   // status: report | assessed-null | written-doc | MISSING
//              unmatchedReports: [ { source, root } ],
//              candidateTotal: integer,
//              skipped, noisePruned, notes },
//     (all produced by scripts/discover_hierarchy.py, which is side-effect free
//      apart from reading the report files it was pointed at. Do NOT recompute
//      the leaf list here: the enumeration being independent of the caller is
//      the whole basis of the input-inventory criterion.)
//   refs: { criteria: <abs path to hierarchy-standards.md>,
//           placement: <abs path to references/cohesion-principles.md>,
//           claudeMdStandards: <abs path to references/standards/claude-md-standards.md> }
// }
//
// Returns { result, totals }. There is no `review` mode: review mode audits a
// CHANGE to a document, and this verb's subject is a tree.

export const meta = {
  name: 'md-domain-hierarchy-detect',
  description: 'Placement resolution over a CLAUDE.md tree: one home per fact, duplicates collapsed, subtractions and dispositions stated (report-only, no edits)',
  phases: [{ title: 'Hierarchy', detail: 'one resolution over one tree' }],
}

const subject = input.subject || {}
const inventory = Array.isArray(subject.inventory) ? subject.inventory : []
const reports = Array.isArray(subject.reports) ? subject.reports : []
const documents = Array.isArray(subject.claudeMdPaths) ? subject.claudeMdPaths : []
const unmatchedReports = Array.isArray(subject.unmatchedReports) ? subject.unmatchedReports : []

// The criteria guard. Without a criteria doc this lane has no basis for deciding
// where a fact belongs, and the failure mode of guessing is a plan that reads
// authoritative and hoists everything to the root. Refuse loudly instead.
//
// The check is for a non-empty STRING path, not merely truthiness: `true` is a
// truthy value that names no document.
const criteriaPath = input.refs && input.refs.criteria
if (typeof criteriaPath !== 'string' || criteriaPath.trim() === '') {
  throw new Error(
    'hierarchy-detect: refs.criteria is not set. The placement-resolution ' +
    'criteria were not wired into this call, and this lane will not improvise ' +
    'them -- an invented placement predicate produces a plan that looks ' +
    'authoritative while hoisting facts nothing justifies hoisting. Pass the ' +
    'absolute path to hierarchy-standards.md as refs.criteria, then re-run. ' +
    'See references/lanes/hierarchy-lane.md, "Step 3 -- Resolve".'
  )
}

// ---------------------------------------------------------------------------
// Input index. Every candidate carried by every loaded report, keyed by the
// stable id discover_hierarchy.py assigned it. This is what makes the
// accounting check below possible: without a stable identity, a candidate that
// was silently dropped is indistinguishable from one that was merged.
// ---------------------------------------------------------------------------

const candidateById = new Map()
for (const report of reports) {
  for (const candidate of report.candidates || []) {
    candidateById.set(candidate._id, {
      id: candidate._id,
      leaf: report.root,
      source: report.source,
      fact: candidate.fact,
      proposedDestination: candidate.destination || null,
      // Caller-supplied depth judgment, carried through when present. The lane
      // does NOT require it: the depth judgment belongs to the lane whose
      // subject contains the parent, which is this one.
      scope: candidate.scope || null,
      siblingOverlap: candidate.sibling_overlap || null,
      anchors: candidate.anchors || [],
      tier: candidate.tier || null,
    })
  }
}

const norm = (p) => String(p == null ? '' : p).replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase()

// ---------------------------------------------------------------------------
// STRUCTURAL REFUSAL, evaluated BEFORE any agent dispatch.
//
// The false pass this lane exists to prevent has a precise shape: a resolution
// handed 10 of 18 leaf reports treating the other 8 as empty candidate sets, and
// then reporting the chain as coherent. Absence of a report is absence of
// evidence, not evidence of absence. So the affirmative verdicts are not
// something an agent may return -- they are COMPUTED from the inventory, and
// when the inventory does not support one, this lane never reaches the agent at
// all. Deciding it here, mechanically, is what keeps the refusal from depending
// on an agent having read the lane doc.
// ---------------------------------------------------------------------------

const missingLeaves = inventory.filter((row) => row.status === 'MISSING')
const hasNoInput = reports.length === 0 && documents.length === 0
const hasNoLeaves = inventory.length === 0

const preflightBlockers = []
if (missingLeaves.length) {
  preflightBlockers.push(
    `${missingLeaves.length} enumerated leaf/leaves map to no candidate report, ` +
    'no explicit assessed-null, and no written document. Absence of a report is ' +
    'absence of evidence, not evidence of absence, so no affirmative verdict is ' +
    'emittable (input-inventory-complete).'
  )
}
if (unmatchedReports.length) {
  preflightBlockers.push(
    `${unmatchedReports.length} report(s) name a root that matches no enumerated ` +
    'leaf. The reports and the tree disagree about what exists, so the inventory ' +
    'cannot be trusted to be complete.'
  )
}
if (hasNoInput) {
  preflightBlockers.push(
    'the tree carries no CLAUDE.md and no candidate report was supplied -- there ' +
    'is nothing to resolve. Reporting a clean chain here would be an affirmative ' +
    'verdict over zero input.'
  )
}
if (hasNoLeaves) {
  preflightBlockers.push(
    'no leaf was enumerated under this root -- the tree holds no code directory, ' +
    'so the inventory the verdict is computed from is empty.'
  )
}

const incomplete = (blockers, extra) => ({
  root: subject.root,
  verdict: 'INPUTS-INCOMPLETE',
  inventory,
  unmatchedReports,
  destinations: [],
  subtractions: [],
  liftOuts: [],
  dispositions: [],
  unplaceable: [],
  rejections: [],
  documentInventory: (extra && extra.documentInventory) || [],
  unaccountedCandidates: (extra && extra.unaccountedCandidates) || [],
  notes: blockers,
})

phase('Hierarchy')

if (preflightBlockers.length) {
  log(
    `Hierarchy: INPUTS-INCOMPLETE over ${subject.root} -- ` +
    `${missingLeaves.length}/${inventory.length} leaf/leaves MISSING, ` +
    `${unmatchedReports.length} unmatched report(s), ${reports.length} report(s) ` +
    `and ${documents.length} document(s) loaded. No verdict emitted, and no ` +
    'assessment was dispatched.'
  )
  return { result: incomplete(preflightBlockers), totals: preflightTotals() }
}

// ---------------------------------------------------------------------------
// Result schema.
// ---------------------------------------------------------------------------

const RESOLUTION_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  // Every list is REQUIRED, including the ones that are usually empty. An
  // optional list lets a run that dropped an input validate while saying
  // nothing about the drop -- which is the exact shape of the failure this lane
  // is built to refuse.
  required: [
    'destinations', 'rejections', 'unplaceable', 'liftOuts',
    'dispositions', 'documentInventory', 'notes',
  ],
  properties: {
    destinations: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['destination', 'facts'],
        properties: {
          destination: { type: 'string' },
          facts: {
            type: 'array',
            items: {
              type: 'object',
              additionalProperties: false,
              required: ['fact', 'sources', 'why'],
              properties: {
                fact: { type: 'string' },
                // The candidate ids folded into this entry. minItems 1: an
                // entry with no source is a fact the resolution invented, and
                // this lane discovers nothing.
                sources: { type: 'array', minItems: 1, items: { type: 'string' } },
                why: { type: 'string' },
                // merge-preserves-precision: a reporter's recorded constraint
                // survives the merge, so it needs somewhere to land.
                constraints: { type: 'array', items: { type: 'string' } },
                anchors: { type: 'array', items: { type: 'string' } },
              },
            },
          },
        },
      },
    },
    rejections: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['candidateId', 'reason'],
        properties: {
          candidateId: { type: 'string' },
          reason: { type: 'string' },
          // Where the work goes instead, when the candidate is really another
          // lane's job (a correction of a stale claim is CD-lane work).
          routedTo: { type: 'string' },
        },
      },
    },
    unplaceable: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        // `reason` is required: unplaceable-declared is fail-severity on the
        // REASON, not merely on the label. No `destination` property exists at
        // all, so an unplaceable fact cannot be quietly assigned to the root.
        required: ['candidateId', 'fact', 'reason'],
        properties: {
          candidateId: { type: 'string' },
          fact: { type: 'string' },
          reason: { type: 'string' },
        },
      },
    },
    liftOuts: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['document', 'fact', 'destination', 'reason'],
        properties: {
          document: { type: 'string' },
          section: { type: 'string' },
          fact: { type: 'string' },
          destination: { type: 'string' },
          reason: { type: 'string' },
        },
      },
    },
    dispositions: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['leaf', 'before', 'after'],
        properties: {
          leaf: { type: 'string' },
          before: { type: 'string', enum: ['WARRANTED', 'NOT-WARRANTED'] },
          after: { type: 'string', enum: ['WARRANTED', 'NOT-WARRANTED'] },
          note: { type: 'string' },
        },
      },
    },
    documentInventory: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['document', 'status'],
        properties: {
          document: { type: 'string' },
          status: { type: 'string', enum: ['EXTRACTED', 'UNEXTRACTED'] },
          reason: { type: 'string' },
        },
      },
    },
    notes: { type: 'array', items: { type: 'string' } },
  },
}

// ---------------------------------------------------------------------------
// The prompt.
// ---------------------------------------------------------------------------

const inventoryLines = inventory
  .map((row) => `  [${row.status}] ${row.leaf}${row.sources && row.sources.length ? `  <- ${row.sources.join(', ')}` : ''}`)
  .join('\n')

const candidateLines = [...candidateById.values()]
  .map((c) => {
    const bits = [
      `  id: ${c.id}`,
      `    leaf: ${c.leaf}`,
      `    fact: ${c.fact}`,
      `    proposed destination: ${c.proposedDestination || '(none stated)'}`,
    ]
    if (c.scope) bits.push(`    reporter scope judgment: ${c.scope}`)
    if (c.siblingOverlap) bits.push(`    reporter sibling overlap: ${c.siblingOverlap}`)
    if (c.tier) bits.push(`    tier: ${c.tier}`)
    if ((c.anchors || []).length) bits.push(`    anchors: ${c.anchors.join(', ')}`)
    return bits.join('\n')
  })
  .join('\n')

const documentLines = documents.map((d) => `  - ${d}`).join('\n')
const aboveLines = (subject.ambientAbove || []).map((d) => `  - ${d}`).join('\n')

const prompt = `Resolve the PLACEMENT of every fact across one CLAUDE.md tree.

Tree root: ${subject.root}

Input inventory (${inventory.length} leaf/leaves; already verified complete):
${inventoryLines}

CLAUDE.md files IN the tree (${documents.length}):
${documentLines || '  (none)'}

CLAUDE.md files ABOVE the root, ambient for the whole tree (${(subject.ambientAbove || []).length}):
${aboveLines || '  (none)'}

Candidate facts from the persisted reports (${candidateById.size}):
${candidateLines || '  (none)'}

WHAT YOU ARE DOING -- read this before anything else.

You are NOT discovering facts. Facts enter from the reports above and from the
documents listed above, and from nowhere else. Do not read source code to find
new facts; that is the coverage lane's job and it has already run. You may open
a source file ONLY to check an anchor a candidate already carries, and even that
is not required of you here.

You are NOT auditing content. The fidelity and value of what a document already
says belong to CD-1..CD-6 in ${input.refs.claudeMdStandards || 'the claude-md standards doc'}.
If a candidate is really a correction of a stale claim, REJECT it with
routedTo naming that lane -- do not absorb the work.

You are deciding, for each fact, exactly ONE home, and stating everything that
decision implies.

CRITERIA. Apply the criteria in ${input.refs.criteria} verbatim. That document,
not your sense of what is tidy, decides where a fact lives. The placement spine
it defers to is ${input.refs.placement || 'references/cohesion-principles.md'}; do not re-derive it.

READ THE EXISTING DOCUMENTS, for three purposes and no others:
  1. SUPPRESSION -- a candidate already carried by a written fact that resolves
     is not a placement, it is a rejection.
  2. LIFT-OUT -- a written fact whose depth test now fails (its violators work
     outside the document's directory) is a liftOuts entry naming the document,
     the fact, its new destination, and why.
  3. PRECEDENT -- where the tree already places a class of fact by an observable
     convention, that convention outranks the criteria's no-precedent default.
Record EVERY document you were asked to read in documentInventory. A document
you could not parse, or chose not to read, is UNEXTRACTED with a reason -- and
it bars the affirmative verdict, which is the honest outcome. Do not mark a
document EXTRACTED to make the verdict emittable.

DUPLICATE COLLAPSE. Sibling subtrees cannot see each other's CLAUDE.md, so the
same fact arriving from several leaves is CORRECT reporting, not noise. Collapse
those into ONE entry whose sources list every contributing candidate id. When
the reporters' phrasings differ, take the NARROWER verified statement -- merging
two phrasings is exactly where a statement true as cited becomes false as
restated -- and carry any precision constraint a reporter recorded into
constraints.

ACCOUNT FOR EVERY CANDIDATE, EXACTLY ONCE. Every id listed above must appear in
exactly one of: a destination entry's sources, rejections, or unplaceable. A
candidate in none of the three has been silently dropped, and this run will be
reported as INPUTS-INCOMPLETE if that happens.

UNPLACEABLE. A fact for which no destination is an ancestor of every file it
governs and of no file it does not -- typically because its trigger is an edit
in a SIBLING subtree -- goes in unplaceable with the reason. Do not force it to
the root and do not drop it. Declaring the condition is the whole job here;
resolving it is out of scope.

DISPOSITIONS. For every leaf that had candidates, state whether it warranted its
own CLAUDE.md BEFORE this resolution and whether it does AFTER. A flip may only
run WARRANTED -> NOT-WARRANTED: subtraction only removes content from a leaf, so
a leaf that did not warrant a document before cannot warrant one after. Where a
verdict survives on one remaining fact, say so in note.

DO NOT emit a verdict. The verdict is computed from the inventories, not
returned by you. Do not emit COMPLIANT or NON-COMPLIANT under any circumstance:
those belong to the document lanes and answer a different question.

HONESTY. The reports you were handed are samples, not inventories, and they are
non-idempotent by their own contract. Your resolution inherits both. Do not
imply the merged fact set is the tree's fact inventory.

Return the structured object.`

// Resolution is this verb's judgment core, so the tier is pinned rather than
// inherited. This lane has exactly ONE subject by construction, which is the
// case where an inline single-subject shortcut would silently run at whatever
// model the session happens to be on. Going through the workflow regardless is
// what keeps the only case on-pin.
const raw = await agent(prompt, {
  label: `hierarchy:${String(subject.root).split(/[\\/]/).pop()}`,
  phase: 'Hierarchy',
  model: 'opus',
  effort: 'high',
  schema: RESOLUTION_SCHEMA,
})

// ---------------------------------------------------------------------------
// Post-checks. Everything below is DERIVED or ENFORCED, never taken on trust:
// the schema can require a list, but it cannot express "every input candidate
// appears exactly once" or "an unplaceable fact carries no destination".
// ---------------------------------------------------------------------------

const notes = Array.isArray(raw.notes) ? [...raw.notes] : []
const destinations = Array.isArray(raw.destinations) ? raw.destinations : []
const rejections = Array.isArray(raw.rejections) ? raw.rejections : []
const liftOuts = Array.isArray(raw.liftOuts) ? raw.liftOuts : []
const documentInventory = Array.isArray(raw.documentInventory) ? raw.documentInventory : []

// unplaceable-declared is fail-severity. `destination` is not a declared
// property, so a schema-valid response cannot carry one -- but a missing or
// blank reason still has to be caught, because "UNPLACEABLE" with no reason is
// indistinguishable from a silent drop with a label on it.
const unplaceable = []
const unplaceableWithoutReason = []
for (const item of Array.isArray(raw.unplaceable) ? raw.unplaceable : []) {
  if (!item || typeof item.reason !== 'string' || item.reason.trim() === '') {
    unplaceableWithoutReason.push(item && item.candidateId)
    unplaceable.push({ ...item, reason: 'REASON MISSING -- the declaration is incomplete' })
    continue
  }
  unplaceable.push(item)
}
if (unplaceableWithoutReason.length) {
  notes.push(
    `${unplaceableWithoutReason.length} unplaceable item(s) carried no reason ` +
    `(${unplaceableWithoutReason.join(', ')}); an unreasoned UNPLACEABLE is a ` +
    'silent drop with a label on it.'
  )
}

// Input accounting. Exactly once, across all three sinks.
const seen = new Map()
const bump = (id, where) => {
  if (!seen.has(id)) seen.set(id, [])
  seen.get(id).push(where)
}
for (const group of destinations) {
  for (const fact of group.facts || []) {
    for (const id of fact.sources || []) bump(id, `destination:${group.destination}`)
  }
}
for (const r of rejections) bump(r.candidateId, 'rejection')
for (const u of unplaceable) bump(u.candidateId, 'unplaceable')

const unaccountedCandidates = [...candidateById.keys()].filter((id) => !seen.has(id))
const doubleCounted = [...seen.entries()]
  .filter(([id, where]) => candidateById.has(id) && where.length > 1)
  .map(([id, where]) => `${id} (${where.join(' + ')})`)
const unknownIds = [...seen.keys()].filter((id) => !candidateById.has(id))

if (unaccountedCandidates.length) {
  notes.push(
    `${unaccountedCandidates.length} input candidate(s) appear in no destination, ` +
    `no rejection and no unplaceable declaration: ${unaccountedCandidates.join(', ')}. ` +
    'A silently dropped input is the failure this lane refuses.'
  )
}
if (doubleCounted.length) {
  notes.push(
    `${doubleCounted.length} candidate(s) were accounted for more than once -- ` +
    `one-home-per-fact is violated: ${doubleCounted.join('; ')}`
  )
}
if (unknownIds.length) {
  notes.push(
    `${unknownIds.length} referenced candidate id(s) are not in the loaded ` +
    `reports: ${unknownIds.join(', ')}`
  )
}

const unextracted = documentInventory.filter((d) => d.status === 'UNEXTRACTED')
const unlistedDocuments = documents.filter(
  (d) => !documentInventory.some((row) => norm(row.document) === norm(d))
)
if (unlistedDocuments.length) {
  notes.push(
    `${unlistedDocuments.length} document(s) in the tree appear in no extraction ` +
    `inventory row: ${unlistedDocuments.join(', ')}. A document nobody accounted ` +
    'for is treated as UNEXTRACTED.'
  )
}

// Subtractions are DERIVED, not reported. For every merged fact, each source
// candidate whose own proposed destination is not the resolved destination
// implies a removal at that source -- and the safe execution order is
// write-destination-before-subtract-source. Deriving this makes "the subtraction
// list is emitted per source" true by construction rather than by the agent
// having remembered.
const subtractions = []
for (const group of destinations) {
  for (const fact of group.facts || []) {
    for (const id of fact.sources || []) {
      const candidate = candidateById.get(id)
      if (!candidate) continue
      if (norm(candidate.proposedDestination) === norm(group.destination)) continue
      subtractions.push({
        source: candidate.leaf,
        sourceReport: candidate.source,
        candidateId: id,
        fact: candidate.fact,
        from: candidate.proposedDestination,
        to: group.destination,
        order: 'write-destination-before-subtract-source',
      })
    }
  }
}

// Per-leaf arithmetic behind the disposition re-judgment. `after` is the count
// of a leaf's candidates that still land at that leaf; everything else moved,
// was rejected, or is unplaceable.
const removedByLeaf = new Map()
const countRemoval = (leaf) => removedByLeaf.set(norm(leaf), (removedByLeaf.get(norm(leaf)) || 0) + 1)
for (const s of subtractions) countRemoval(s.source)
for (const r of rejections) {
  const c = candidateById.get(r.candidateId)
  if (c) countRemoval(c.leaf)
}
for (const u of unplaceable) {
  const c = candidateById.get(u.candidateId)
  if (c) countRemoval(c.leaf)
}

const dispositions = (Array.isArray(raw.dispositions) ? raw.dispositions : []).map((d) => {
  const report = reports.find((r) => norm(r.root) === norm(d.leaf))
  const before = report ? report.candidateCount : null
  const removed = removedByLeaf.get(norm(d.leaf)) || 0
  const after = before === null ? null : Math.max(0, before - removed)
  const row = { ...d, candidatesBefore: before, candidatesAfter: after }
  // disposition-re-judged: downward-only. This is arithmetic, not preference --
  // subtraction only REMOVES content from a leaf, so an upward flip would
  // assert facts the resolution never added.
  if (d.before === 'NOT-WARRANTED' && d.after === 'WARRANTED') {
    row.after = 'NOT-WARRANTED'
    row.note = `${d.note ? d.note + ' ' : ''}[corrected: an upward disposition flip is not derivable from subtraction]`
  }
  if (after === 0 && row.after === 'WARRANTED') {
    row.after = 'NOT-WARRANTED'
    row.note = `${row.note ? row.note + ' ' : ''}[corrected: no candidate remains at this leaf]`
  }
  return row
})

// ---------------------------------------------------------------------------
// The verdict is COMPUTED from the inventories, never returned by the agent.
// ---------------------------------------------------------------------------

const postBlockers = []
if (unextracted.length) {
  postBlockers.push(
    `${unextracted.length} document(s) reported UNEXTRACTED ` +
    `(${unextracted.map((d) => d.document).join(', ')}) -- a chain cannot be ` +
    'declared coherent against a document nobody read.'
  )
}
if (unlistedDocuments.length) {
  postBlockers.push(
    `${unlistedDocuments.length} document(s) were never accounted for in the ` +
    'extraction inventory, which has the same standing as UNEXTRACTED.'
  )
}
if (unaccountedCandidates.length) {
  postBlockers.push(
    `${unaccountedCandidates.length} input candidate(s) went unaccounted for.`
  )
}
if (doubleCounted.length) {
  postBlockers.push(`${doubleCounted.length} candidate(s) were placed more than once.`)
}
if (unplaceableWithoutReason.length) {
  postBlockers.push(
    `${unplaceableWithoutReason.length} unplaceable declaration(s) carry no reason.`
  )
}

const planIsEmpty =
  destinations.every((g) => (g.facts || []).length === 0) &&
  subtractions.length === 0 &&
  liftOuts.length === 0 &&
  unplaceable.length === 0

const verdict = postBlockers.length
  ? 'INPUTS-INCOMPLETE'
  : (planIsEmpty ? 'CHAIN-COHERENT' : 'RESOLUTION-PROPOSED')

const result = {
  root: subject.root,
  verdict,
  inventory,
  unmatchedReports,
  documentInventory,
  destinations,
  subtractions,
  liftOuts,
  dispositions,
  unplaceable,
  rejections,
  unaccountedCandidates,
  notes: postBlockers.length ? [...postBlockers, ...notes] : notes,
}

function preflightTotals() {
  return {
    leaves: inventory.length,
    missingLeaves: missingLeaves.length,
    reports: reports.length,
    documents: documents.length,
    candidatesIn: candidateById.size,
    mergedFacts: 0,
    subtractions: 0,
    liftOuts: 0,
    unplaceable: 0,
    rejections: 0,
    unaccounted: 0,
    unextractedDocuments: 0,
    inputsIncomplete: 1,
    chainCoherent: 0,
    resolutionProposed: 0,
  }
}

const totals = {
  leaves: inventory.length,
  missingLeaves: missingLeaves.length,
  reports: reports.length,
  documents: documents.length,
  candidatesIn: candidateById.size,
  mergedFacts: destinations.reduce((n, g) => n + (g.facts || []).length, 0),
  subtractions: subtractions.length,
  liftOuts: liftOuts.length,
  unplaceable: unplaceable.length,
  rejections: rejections.length,
  unaccounted: unaccountedCandidates.length,
  unextractedDocuments: unextracted.length + unlistedDocuments.length,
  // Counted apart from the two affirmative verdicts, deliberately: an
  // incomplete-input run is neither a coherent chain nor a proposed resolution,
  // and folding it into either is exactly the fake pass this lane refuses.
  inputsIncomplete: verdict === 'INPUTS-INCOMPLETE' ? 1 : 0,
  chainCoherent: verdict === 'CHAIN-COHERENT' ? 1 : 0,
  resolutionProposed: verdict === 'RESOLUTION-PROPOSED' ? 1 : 0,
}

const unplaceableNote = totals.unplaceable
  ? `, ${totals.unplaceable} UNPLACEABLE (declared, not resolved)`
  : ''
const unaccountedNote = totals.unaccounted
  ? `, ${totals.unaccounted} candidate(s) UNACCOUNTED FOR -- inputs silently dropped`
  : ''
const unextractedNote = totals.unextractedDocuments
  ? `, ${totals.unextractedDocuments} document(s) UNEXTRACTED -- not read, not coherent`
  : ''

log(`Hierarchy: ${verdict} over ${subject.root}: ${totals.candidatesIn} candidate(s) from ${totals.reports} report(s) across ${totals.leaves} leaf/leaves -> ${totals.mergedFacts} merged fact(s) at ${destinations.length} destination(s), ${totals.subtractions} subtraction(s), ${totals.liftOuts} lift-out(s), ${totals.rejections} rejection(s)${unplaceableNote}${unaccountedNote}${unextractedNote}. Report-only: nothing is applied, and the plan is a sample of samples -- re-runs may differ.`)

return { result, totals }
