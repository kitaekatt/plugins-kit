// md-domain coverage verb -- DETECT workflow (the only phase; report-only).
//
// Fan-out assessment, one lane per BATCH of (code directory, ambient CLAUDE.md
// chain) subjects. This is the first lane in the skill whose subject is CODE
// rather than a markdown file, which is why it has its own procedure rather than
// being a criterion inside audit_claude_md: the per-file lanes enumerate
// CLAUDE.md files, and no criterion can have a subject its lane cannot
// enumerate. The decisive case is a directory with NO CLAUDE.md at all.
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
// scripts/gen_workflow_js.py is not involved. Report-only is what keeps a
// separate procedure cheap, and it is a property of this entry point rather than
// of the verb being listed apart from audit and generate.
//
// ---------------------------------------------------------------------------
// BATCHING, AND WHAT IT DOES AND DOES NOT GUARANTEE
// ---------------------------------------------------------------------------
//
// One agent per subject was deliberate CONTEXT ISOLATION: it is what stopped one
// directory's code from bleeding into another directory's candidate facts. It is
// also where the run's fixed cost lives -- every agent re-reads the same criteria
// documents (~180 KB), which on a small directory is most of what it spends.
// Batching amortizes that read across the subjects in a batch.
//
// Read this next part literally, because an earlier revision of this file
// overclaimed it and an adversarial review was right to reject it. Batching does
// NOT preserve isolation. It BOUNDS contamination, by four mechanisms, of which
// only the last three are enforcement:
//
//   1. Sequential turns with a SCOPED reset in the brief. Prompt-level: asked
//      for, not enforced, and it cannot be. Treated as hygiene, not a guarantee.
//   2. IDENTITY BY SCRIPT-ISSUED KEY. Every requested subject is issued a
//      `subjectKey` here and the agent must echo it. Results are matched BY KEY,
//      never by position. This closes the misattribution hole: a batch that
//      returns A and C for a request of A,B,C now marks B not-assessed and files
//      C's findings under C -- where positional zipping filed C's candidates
//      under B's directory, manufacturing exactly the contamination the design
//      exists to prevent, using the root overwrite that was meant as a safeguard.
//   3. ANCHOR MEMBERSHIP AGAINST A FILE LIST. Every candidate anchor must name a
//      file that is IN that subject's own codeFiles list and must carry a line
//      number. Not "under the root" -- a path-prefix test let an empty string, a
//      nonexistent file, a foreign file, and a same-named directory in another
//      module all through. Membership in a concrete list has no such surface.
//   4. DESTINATION DERIVED, NOT ACCEPTED. `destination` is overwritten from the
//      subject's own identity, because generation groups by that field and the
//      schema could only ask for it.
//
// WHAT REMAINS UNCLOSED, and it cannot be closed by string work: a fact REASONED
// from subject A's code but ANCHORED to a real, in-list file of subject B passes
// every check above. Anchors prove a file was named, never that a claim was
// derived from it. The honest statement is: batching preserves the cost saving
// and bounds contamination to that residual case; it does not eliminate it. See
// `batchSize` for what that means in practice.
//
// TRUST DIFFERS BY INPUT MODE, and every record says which it got:
//   - inline mode -- `root` and `codeFiles` are TRUSTED INPUT. Identity and
//     anchor membership are checked against data the agent never supplied.
//     Records are stamped provenance 'harness-verified'.
//   - subjectsFile mode -- this lane has no filesystem, so `root` and
//     `codeFiles` are ATTESTED BY THE AGENT (echoed from the record it read).
//     The key still binds a result to a line THIS SCRIPT requested, and anchors
//     are still checked against the echoed list -- which catches the incidental
//     mislabel, because an agent that assessed A while labelling it B echoes B's
//     file list and anchors A's files. It does NOT catch a self-consistent
//     fabrication. Records are stamped provenance 'agent-attested', the mode is
//     on the log line, and a caller needing verified provenance must use inline
//     mode or re-verify the report against the subjects file afterwards -- the
//     rule is in coverage-lane.md, "Verifying an agent-attested run".
//
// args = {
//   subjects: [ { root: string,
//                 codeFiles: string[],
//                 ambientClaudeMdPaths: string[],   // root-most first; MAY be empty
//                 rootExclusion: string|null,
//                 skipped: [ { path: string, reason: string } ],
//                 unknownExtensions: { [ext: string]: number } } ],
//     (all produced by scripts/discover_coverage.py, which is side-effect free and
//     reads no file contents. Do NOT recompute the ambient chain here: the chain
//     INCLUDES a CLAUDE.md at the directory root, and its upward walk stops at the
//     nearest PROJECT marker (.git/.hg/.svn/.p4config.txt) so a nested project
//     never inherits the outer project's chain. Both
//     are easy to get wrong by eye.)
//   subjectsFile: string|undefined   // ABSOLUTE path to a JSONL file, ONE subject
//     object per line, each line the same shape as an inline `subjects[]` entry.
//     This is the wide-corpus input mode. A workflow script has no filesystem, so
//     inline subjects must travel through the ORCHESTRATOR'S context to reach it
//     -- ~2.3 KB per subject, which over a four-figure corpus is megabytes of
//     payload routed through the one context that must stay lean. With
//     subjectsFile the script holds only the path and a line range per batch, and
//     the AGENTS read their own slice (agents do have filesystem tools). JSONL
//     rather than a JSON array so a slice is a LINE RANGE: an agent extracts
//     exactly its own subjects and never reads the whole file. The price of the
//     mode is the weaker provenance described above.
//   subjectCount: integer|undefined  // REQUIRED with subjectsFile: the number of
//     lines in it. The script cannot count them itself, and guessing would either
//     truncate the run or dispatch agents at empty ranges. An INACCURATE count is
//     survivable rather than silent: too high and the surplus keys come back
//     not-assessed, too low and the tail is never requested -- both visible in
//     the requested-vs-completed split on the log line.
//   batchSize: integer|undefined     // subjects per agent; default below
//   ceiling: integer|undefined   // candidate cap PER SUBTREE; default below
//   depth: 'basic'|'advanced'    // resolved by the lane's intent gate
//   refs: { criteria: <abs path to the coverage standards doc>,
//           observationKinds: <abs path to references/standards/claude-md-standards.md>,
//           placement: <abs path to references/cohesion-principles.md>,
//           pluginRoot: <abs path to plugins/skills-kit> }
// }
//
// PRECEDENCE: inline `subjects[]` WINS over `subjectsFile` when both are
// supplied, loudly (a run note and a log clause; never silently). Two reasons:
// an inline array is data the caller literally handed this lane, and ignoring it
// in favour of a file it also mentioned would be the more surprising of the two
// choices; and inline is the mode with the stronger provenance. The mode is
// SELECTED FIRST and only the selected mode is validated -- an ignored
// subjectsFile must not be able to fail a run it takes no part in.
//
// Returns { perSubject, totals }. There is no `review` mode: review mode audits a
// CHANGE to a document, and this verb's subject is a directory.

export const meta = {
  name: 'md-domain-coverage-detect',
  description: 'Fan-out coverage assessment: which facts about this code directory belong in a CLAUDE.md and are not ambient for it (report-only, no edits)',
  phases: [{ title: 'Coverage', detail: 'one lane per batch of code directories' }],
}

// Candidate cap, applied PER SUBTREE (not per run). A per-run cap divided across
// subjects would give each directory an arbitrary share that shrinks as the run
// widens, which is a worse answer than capping each directory consistently. The
// aggregate is reported so a wide run cannot look complete when it is not.
// When the cap is hit the report SAYS so -- silent truncation in the verb that
// reports silent truncation would be its own joke, and the repo's own rule
// requires a capped run to announce the cap.
const DEFAULT_CEILING = 25

// Subjects per agent. The fixed per-agent cost is the criteria read (~180 KB,
// ~45K tokens); the variable cost is the directory's own code. At 1 the fixed
// cost is paid once per subject; at 8 it is paid once per eight.
//
// The default is 8 and NOT higher, and the reason is the residual risk named at
// the top of this file rather than the arithmetic: the checks bound
// contamination to facts reasoned from one subject and anchored to another
// subject's real files, and that residual grows with the number of subjects one
// context holds at once. 8 is the largest batch this lane claims a defensible
// story for. Raising it trades a shrinking cost saving (the fixed share is
// already down to roughly a fourteenth of a basic subject at 8) against a
// growing unverifiable one. Lower it -- to 1, which is the pre-batching
// behaviour -- for any run whose candidates will be promoted without human
// review.
const DEFAULT_BATCH_SIZE = 8

// args may arrive as an object or as a JSON string depending on how the
// invoker passes it; normalize to an object.
let input = args
if (typeof input === 'string') {
  try { input = JSON.parse(input) } catch (_) { input = null }
}
if (!input) {
  throw new Error('coverage-detect.js requires args = { subjects | subjectsFile, depth, refs }')
}

const ceiling = Number.isInteger(input.ceiling) ? input.ceiling : DEFAULT_CEILING
const batchSize = Number.isInteger(input.batchSize) && input.batchSize > 0
  ? input.batchSize
  : DEFAULT_BATCH_SIZE

const SUBJECT_FINDINGS_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  // `notes` is REQUIRED, not optional: it is where a ceiling hit reports how
  // many candidates were set aside, and an optional field lets a truncated run
  // validate while saying nothing about the truncation.
  //
  // `subjectKey` is REQUIRED and is the identity spine. It is issued by this
  // script, not chosen by the agent, and results are matched by it. Position is
  // never used: a batch that silently omits its middle subject would otherwise
  // shift every later result one slot, which does not lose data -- it MISFILES
  // it, and the root overwrite meant as a safeguard is what would do the
  // misfiling.
  //
  // `codeFiles` is REQUIRED as an ECHO. In inline mode it is compared against
  // the trusted list; in subjectsFile mode it is the only file list this lane
  // can see, and anchors are checked against it. It is STRIPPED from the
  // returned record after validation -- the workflow result travels back through
  // the orchestrator, and re-inflating it there would hand back exactly the
  // payload subjectsFile mode exists to remove.
  //
  // The trailing counts are transcription, not judgment: they let the
  // discovery-failure refusal and the uncovered tally stay mechanical decisions
  // made HERE, in a mode where this lane cannot read the subject record itself.
  required: ['subjectKey', 'root', 'candidates', 'verdict', 'ceilingReached', 'notes', 'codeFiles', 'assessedFileCount', 'unknownExtensionCount', 'ambientChainCount'],
  properties: {
    subjectKey: { type: 'string' },
    root: { type: 'string' },
    verdict: { type: 'string', enum: ['GAPS-FOUND', 'COVERAGE-ASSESSED'] },
    ceilingReached: { type: 'boolean' },
    // Echoed verbatim from the subject record.
    codeFiles: { type: 'array', items: { type: 'string' } },
    // Transcribed from the subject record: codeFiles.length. Cross-checked
    // against the echoed array, so a disagreement between the two is visible.
    assessedFileCount: { type: 'integer' },
    // Transcribed from the subject record: the number of KEYS in
    // unknownExtensions. Feeds the discovery-failure refusal.
    unknownExtensionCount: { type: 'integer' },
    // Transcribed from the subject record: ambientClaudeMdPaths.length. Feeds
    // the uncovered tally, and ONLY when the input is unavailable -- see
    // chainSizeForRoot below.
    ambientChainCount: { type: 'integer' },
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
          // ALWAYS the assessed directory. Degenerate by design: an
          // assessment reads only its own directory's direct code, so it has no
          // basis to place a fact anywhere else. Kept as a field so reports
          // written before this model stay loadable, and DERIVED below rather
          // than believed -- generation groups by this field, so a wrong value
          // silently re-homes a fact into another directory's document.
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
          // bare `required` while citing nothing. minLength:1 on the ITEM
          // because an array holding one empty string satisfies minItems while
          // citing nothing either -- which is how a prefix-based containment
          // check passed 200 empty anchors in one batch.
          anchors: { type: 'array', minItems: 1, items: { type: 'string', minLength: 1 } },
          // `scope` ('LEAF-ONLY' | 'PROMOTE -> <dir>') and `sibling_overlap`
          // were REMOVED from this schema deliberately, and the removal is the
          // enforcement -- with additionalProperties:false above, an assessment
          // now CANNOT emit them. They were the promotion machinery: a candidate
          // nominating a destination above itself, which an assessment cannot
          // justify from a directory it never opened. A fact reaches a wider
          // area by HOISTING at the parent, which compares child documents it
          // has actually read.
          //
          // This is an OUTPUT schema, so it constrains only new emissions --
          // reports persisted before this model keep both fields on disk and
          // still load. Do not re-add them here to "stay compatible": that
          // conflates what a run may produce with what a loader may read, and it
          // reopens the exact nomination path fact-scoped-to-this-directory
          // forbids.
        },
      },
    },
    notes: { type: 'array', items: { type: 'string' } },
  },
}

// The batch envelope. It is an ARRAY OF THE PER-SUBJECT OBJECT and nothing else:
// there is deliberately no batch-level candidate list, no shared notes array,
// and no summary field. A batch-level container is the one shape that would let
// a fact be reported without naming the directory it came from.
//
// There is NO minItems, deliberately. An empty array is legal and meaningful --
// it is what a batch that could assess nothing has to be able to say -- and
// requiring at least one entry made the wholly-skipped batch inexpressible, so
// the not-assessed path below was unreachable through the very schema that
// guards it.
// The REFUTATION stage's output. One record per candidate, matched back by the
// index the prompt issued -- never by position in the returned array, for the
// same reason reconcileBatch matches subjects by key.
//
// `quote` is the load-bearing field and it is REQUIRED. A verifier that applies
// a rule it cannot quote from the criteria document has invented that rule, and
// an invented rule is not a hypothetical failure mode here: a corpus-scale run
// enforced one ("evidence outside the directory fails even when the fact is
// true") that the criteria do not contain and that CV-1's own ADMIT example
// contradicts, and it produced more wrong rejections than any property of the
// lane. Carrying the quote is what makes that detectable from the record
// afterwards rather than only by re-reading a brief nobody kept.
const VERIFY_FINDINGS_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['verdicts'],
  properties: {
    verdicts: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['index', 'truth', 'counterexample', 'filesRead', 'filesInDir'],
        properties: {
          index: {
            type: 'integer',
            description: 'The candidate index exactly as the prompt issued it.',
          },
          truth: {
            type: 'string',
            enum: ['STANDS', 'FALSIFIED'],
            description:
              'FALSIFIED only when a file in this directory contradicts the fact ' +
              'as written. Not a judgment about whether the fact is worth carrying.',
          },
          counterexample: {
            type: 'string',
            description:
              'file:line that falsifies the fact, or empty when truth is STANDS. ' +
              'A FALSIFIED verdict without one is discarded as unsupported.',
          },
          narrowing: {
            type: 'string',
            description:
              'Optional. When one over-reaching clause is the only thing that ' +
              'fails, the restatement that would stand. Carried to the caller, ' +
              'never auto-applied.',
          },
          quote: {
            type: 'string',
            description:
              'Verbatim phrase from the criteria document backing any criterion ' +
              'invoked. Empty is correct for a pure falsification.',
          },
          filesRead: { type: 'integer' },
          filesInDir: { type: 'integer' },
        },
      },
    },
  },
}

const BATCH_FINDINGS_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['subjects'],
  properties: {
    subjects: { type: 'array', items: SUBJECT_FINDINGS_SCHEMA },
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

// ---------------------------------------------------------------------------
// Input mode resolution. SELECT the mode first, then validate ONLY the selected
// mode -- an ignored subjectsFile must not be able to fail a run it takes no
// part in, which is what happened while the path check ran ahead of precedence.
// ---------------------------------------------------------------------------

const runNotes = []
const rawSubjectsFile = typeof input.subjectsFile === 'string' && input.subjectsFile.trim() !== ''
  ? input.subjectsFile.trim()
  : null

let subjectsFile = null
let subjectCount = 0

if (subjects.length) {
  if (rawSubjectsFile) {
    // Loudly, never silently -- see PRECEDENCE in the header.
    runNotes.push(
      `both inline subjects[] (${subjects.length}) and subjectsFile ("${rawSubjectsFile}") were ` +
      'supplied; the inline subjects WIN, and the file was neither read nor ' +
      'validated. Pass exactly one input mode.'
    )
  }
  subjectCount = subjects.length
} else if (rawSubjectsFile) {
  // An ABSOLUTE path, and this is checked rather than assumed. The agents that
  // read it are separate processes whose working directory this lane does not
  // control, so a relative path names a different file for each of them -- or no
  // file at all, which would read as an empty batch rather than as an error.
  if (!/^([\\/]|[A-Za-z]:[\\/]|\\\\)/.test(rawSubjectsFile)) {
    throw new Error(
      `coverage-detect: subjectsFile must be an ABSOLUTE path; got "${rawSubjectsFile}". ` +
      'The agents that read it do not share this lane working directory, so a ' +
      'relative path resolves differently for each of them. See ' +
      'references/lanes/coverage-lane.md, "Step 2 -- Discover".'
    )
  }
  if (!Number.isInteger(input.subjectCount) || input.subjectCount < 1) {
    throw new Error(
      'coverage-detect: subjectsFile requires args.subjectCount (a positive ' +
      'integer -- the number of lines in the file). This lane has no filesystem ' +
      'and cannot count them; without the count it would either truncate the run ' +
      'or dispatch agents at empty line ranges.'
    )
  }
  subjectsFile = rawSubjectsFile
  subjectCount = input.subjectCount
} else {
  throw new Error(
    'coverage-detect: no subjects. Pass either args.subjects (inline, for small ' +
    'runs) or args.subjectsFile (an ABSOLUTE path to a JSONL file, one subject ' +
    'per line) together with args.subjectCount.'
  )
}

const provenance = subjectsFile ? 'agent-attested' : 'harness-verified'

// ---------------------------------------------------------------------------
// Path canonicalization. Pure string work: this lane has no filesystem, so
// nothing here touches disk, and "canonical" means "comparable", not "real".
// ---------------------------------------------------------------------------

const nfc = (s) => { try { return String(s).normalize('NFC') } catch (_) { return String(s) } }

// A path is treated as Windows-shaped when it carries a drive letter, a UNC
// prefix, or a backslash separator. Case-folding applies only then, and only
// when EITHER side of a comparison is Windows-shaped: folding everywhere would
// merge two files on a case-sensitive filesystem that differ only in case, which
// is a real shape in POSIX trees.
const windowsShaped = (p) =>
  /^[A-Za-z]:[\\/]/.test(p) || /^\\\\/.test(p) || String(p).indexOf('\\') !== -1

const canonicalSegments = (p) => {
  const out = []
  const parts = nfc(p).replace(/\\/g, '/').split('/')
  for (let i = 0; i < parts.length; i++) {
    const seg = parts[i]
    if (seg === '') { if (i === 0) out.push('') ; continue }
    if (seg === '.') continue
    if (seg === '..') {
      const last = out.length ? out[out.length - 1] : null
      if (last !== null && last !== '' && last !== '..') { out.pop(); continue }
      out.push('..')
      continue
    }
    out.push(seg)
  }
  return out
}

const canonicalPath = (p, fold) => {
  const joined = canonicalSegments(p).join('/')
  return fold ? joined.toLowerCase() : joined
}

// Split "file:line" or "file:line:col" into its parts. A trailing line number is
// REQUIRED: CV-7 asks for a file AND a line, and an anchor with no line number is
// a guess at a location wearing a citation's clothes.
//
// Peeled from the END, one trailing number at a time, rather than matched in one
// pass. A single greedy pattern with an optional column group binds the LAST
// number to the line and swallows the real line into the filename, so
// "f.cpp:12:4" parsed as the file "f.cpp:12" at line 4 and was then rejected as
// naming no file in the list. Peeling also keeps a drive letter safe, because
// "C:" is never a trailing digit group.
const TRAILING_NUMBER = /^(.*):(\d+)$/
const splitAnchor = (raw) => {
  const first = TRAILING_NUMBER.exec(String(raw).trim())
  if (!first) return null
  const second = TRAILING_NUMBER.exec(first[1])
  // Two trailing numbers means file:line:col; one means file:line.
  const file = (second ? second[1] : first[1]).trim()
  const line = Number(second ? second[2] : first[2])
  if (!file || !Number.isInteger(line) || line < 1) return null
  return { file, line }
}

// MEMBERSHIP, not containment. The anchor must name a file that is IN this
// subject's own code-file list. A path-prefix test -- "does the anchor sit under
// the root" -- was the previous rule, and it admitted an empty string, a file
// that does not exist, a foreign file that happened to share a directory name,
// and any bare filename whatsoever. A concrete list has none of those surfaces.
//
// An anchor may be spelled relatively (the agent quoting the path it opened
// rather than the absolute one). A relative spelling is accepted only when it is
// a trailing path-SEGMENT suffix of EXACTLY ONE entry: exactly-one stops
// "Private/file.cpp" resolving against a second module's identically-named file,
// and the segment boundary stops "f.py" matching "conf.py".
const anchorRejectionReason = (raw, codeFiles) => {
  const parsed = splitAnchor(raw)
  if (!parsed) return 'no line number'
  const fold = windowsShaped(parsed.file) || codeFiles.some(windowsShaped)
  const a = canonicalPath(parsed.file, fold)
  if (!a) return 'empty path'
  const list = codeFiles.map((f) => canonicalPath(f, fold))
  if (list.indexOf(a) !== -1) return null
  const suffixHits = list.filter((f) => f.endsWith('/' + a))
  if (suffixHits.length === 1) return null
  if (suffixHits.length > 1) return 'ambiguous relative path'
  return 'names no file in this subject own code-file list'
}

// ---------------------------------------------------------------------------
// Prompt construction.
// ---------------------------------------------------------------------------

const FENCE = '\n\n----------------------------------------------------------------\n\n'

// The per-subject block for an INLINE batch: the subject record, rendered.
const subjectBlock = (s, key, ordinal, total) => {
  const chain = s.ambientClaudeMdPaths || []
  const chainClause = chain.length
    ? `The CLAUDE.md files AMBIENT for this directory, root-most first:\n${chain.map((p) => `  - ${p}`).join('\n')}\n\nRead every one. A fact already carried by an ambient claim that RESOLVES is NOT a candidate -- that suppression is applied HERE, at assessment time, because establishing it requires reading the ambient document and usually the source it anchors to.`
    : `This directory has NO ambient CLAUDE.md. Nothing loads for this code at all. That is not an error and not a skip -- it is the strongest form of the finding this verb exists to surface.`

  const exclusionClause = (s.skipped || []).length
    ? `\nAlready excluded structurally (do not assess, and mention in notes):\n${s.skipped.map((k) => `  - [${k.reason}] ${k.path}`).join('\n')}`
    : ''

  const rootClause = s.rootExclusion
    ? `\nNOTE: this root is itself ${s.rootExclusion}. It is being assessed because the user named it explicitly; say so in notes.`
    : ''

  return `SUBJECT ${ordinal} OF ${total}
subjectKey: ${key}

Directory: ${s.root}
Code files (${(s.codeFiles || []).length}):
${(s.codeFiles || []).map((p) => `  - ${p}`).join('\n')}

${chainClause}${exclusionClause}${rootClause}

In this subject's result object set:
  subjectKey: ${key}
  root: ${s.root}
  codeFiles: the list above, verbatim
  assessedFileCount: ${(s.codeFiles || []).length}
  unknownExtensionCount: ${Object.keys(s.unknownExtensions || {}).length}
  ambientChainCount: ${chain.length}`
}

// The subject material for a subjectsFile batch: a LINE RANGE, not the subjects.
// The whole point of this mode is that the subject payloads never travel through
// the orchestrator, so they must not travel through this prompt either.
const sliceBlock = (start, end) => {
  const n = end - start + 1
  return `YOUR SUBJECTS ARE IN A FILE, AND YOU READ ONLY YOUR OWN SLICE.

File: ${subjectsFile}
Format: JSONL -- one subject record per line, 1-based line numbers.
Your slice: lines ${start} to ${end} inclusive (${n} subject${n === 1 ? '' : 's'}).

Extract EXACTLY those lines, for example with:
  sed -n '${start},${end}p' "${subjectsFile}"
or by reading the file with offset ${start} and limit ${n}.

DO NOT read the whole file. It holds ${subjectCount} subject records; reading it
entire would cost more than the assessment you were dispatched to do, and would
put ${subjectCount - n} other directories' code inventories into your context --
which is precisely the contamination the slice exists to prevent.

Each line is a JSON object with: root, codeFiles (the direct code files to
assess -- NON-RECURSIVE), ambientClaudeMdPaths (root-most first, MAY be empty),
rootExclusion, skipped, unknownExtensions.

THE subjectKey FOR A RECORD IS THE LETTER L FOLLOWED BY ITS 1-BASED LINE NUMBER.
The record on line ${start} has subjectKey L${start}, the next L${start + 1}, and
so on through L${end}. Set it on every result object. Results are matched by that
key and never by their position in your array, so returning fewer results, or
returning them out of order, is safe -- but a key that is missing, wrong, or
repeated loses that subject.

Process the lines in FILE ORDER.

OMIT rather than invent. If a line in your slice is blank, does not parse as
JSON, or does not exist because the file is shorter than the range you were
given, return NO result object for it. Do not synthesize a record, do not guess
its root, and do not move another subject's result into its place. This lane
marks an unreturned key NOT-ASSESSED, which is the honest outcome; a fabricated
one would be reported as a clean pass over a directory nobody read.

Read every path in ambientClaudeMdPaths for the subject you are on. A fact
already carried by an ambient claim that RESOLVES is NOT a candidate. An EMPTY
ambientClaudeMdPaths is not an error and not a skip -- it is the strongest form
of the finding this verb exists to surface.

Anything listed in that subject's skipped array is excluded structurally: do not
assess it, and mention it in that subject's notes. If rootExclusion is set, say
so in notes -- the directory is being assessed because the user named it.

Per subject, copy into that subject's result object, from the record you read:
  root: its root, verbatim
  codeFiles: its codeFiles array, verbatim
  assessedFileCount: the length of its codeFiles
  unknownExtensionCount: the number of keys in its unknownExtensions
  ambientChainCount: the length of its ambientClaudeMdPaths
Copy them; do not estimate them and do not tidy them. This lane checks your
evidence anchors against the codeFiles you echo, and derives mechanical decisions
from those counts.

If a subject has ZERO codeFiles and a NON-EMPTY unknownExtensions, that directory
was never read -- nothing in it matched a known code, doc, or asset type. Do not
assess it: return its echoed fields and an empty candidates list. That is enough
for this lane to mark it a discovery failure rather than verified-absent.`
}

const depthClause = depth === 'advanced'
  ? `ANALYSIS DEPTH: advanced. Read every source file completely. First run an
invariant-discovery pass and carry those invariants into assessment. After
assessment, run a verification pass over every surviving candidate against the
source and drop what you falsify.

Your verification pass is a SELF-CHECK, and it is NOT what makes this depth
verified. You are judging candidates you just wrote, in the context that wrote
them, which is measurably the weakest place to catch an over-reaching claim.
After you return, this lane runs a separate refutation stage in FRESH context
that tries to falsify every candidate you emit. COVERAGE-ASSESSED means verified
absent only downstream of THAT stage. Do not weaken a claim to survive it: state
the fact you actually believe, at the scope the source actually supports.`
  : `ANALYSIS DEPTH: basic. Use a bounded, sampled read and one assessment pass.
At this depth COVERAGE-ASSESSED means not found within budget.`

// The batch brief. Everything that is IDENTICAL for every subject is stated once
// -- that is the whole economy of batching -- and the per-subject material is
// fenced so the two can never be confused for each other.
const batchPrompt = (batch) => {
  const total = batch.keys.length
  const subjectMaterial = batch.kind === 'inline'
    ? batch.items.map((s, i) => subjectBlock(s, batch.keys[i], i + 1, total)).join(FENCE)
    : sliceBlock(batch.start, batch.end)

  return `Assess the CLAUDE.md COVERAGE of ${total} DIRECTORY/IES, ONE AT A TIME.

HOW TO WORK THROUGH THIS BATCH -- read this before anything else.

You have been given ${total} independent subjects. They are independent in the
strong sense: each one is a separate assessment whose only inputs are its own
directory's own direct code files and its own ambient CLAUDE.md chain. They share
this brief and they share the criteria documents named below. They share NOTHING
ELSE.

Work strictly SEQUENTIALLY. Finish subject 1 completely -- open its files, apply
the criteria, settle its candidates, write its result object -- before opening
anything belonging to subject 2.

Between subjects, RESET. The reset is scoped, and the scope matters in both
directions:
  - KEEP the criteria documents. They are the same for every subject; re-reading
    them per subject is the cost this batch exists to avoid.
  - DISCARD everything else from the previous subject: its source files, its
    ambient documents, its candidate facts, and any pattern you inferred from
    them. A fact you learned from subject K's files is NOT evidence about subject
    K+1, however similar the two directories look, and similarity is exactly when
    this goes wrong -- sibling directories in one codebase are the case where a
    borrowed fact is most plausible and most likely to be false.

Do not compare subjects with each other. Do not report that two subjects share a
convention. Do not carry a candidate forward "because it applies here too". If a
fact holds of two directories, each directory's own assessment must establish it
from its own files, or it is not established.

IDENTITY. Every subject carries a subjectKey issued by the harness. Echo it on
that subject's result object. Results are matched BY KEY and never by position,
so returning fewer results than there are subjects is safe and returning them out
of order is safe -- but a wrong or missing key loses that subject.

RETURN ONE RESULT OBJECT PER SUBJECT YOU ACTUALLY ASSESSED. If you could not
assess one, OMIT it. Never pad the array, never invent a subject, and never file
one subject's findings under another subject's key.

ANCHORS ARE CHECKED AGAINST YOUR OWN FILE LIST. Every anchor you cite must name a
file that appears in that subject's codeFiles, and must carry a line number
("path/to/file.py:42"). Anything else -- a file in a sibling directory, a parent,
a subdirectory, a file not in the list, or a path with no line number -- is
rejected by the harness after you return, and the candidate carrying it is
DROPPED and counted as an isolation violation. Cite the path you actually opened,
in the spelling the list uses, rather than a tidied or shortened version.

${depthClause}

WHAT YOU ARE LOOKING FOR.

md-domain is not a code-review tool. You are NOT reviewing this code. You are not
looking for bugs, and you must not return a defect list. You are answering ONE
question, once per subject: which facts about this code belong in a CLAUDE.md
that will be AMBIENT for it, and are not there today?

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
(the GENERATION direction's list of what is worth writing up).
Read those documents ONCE, now, before you open subject 1, and keep them for the
whole batch. Do not re-read them between subjects, and do not compress them into
a summary of your own -- the criteria are what they say, not what you remember of
them.

SCOPE, AND IT IS THE HARDEST RULE HERE. Every candidate must be a fact about
ITS OWN SUBJECT DIRECTORY'S OWN DIRECT code, and its destination is ALWAYS that
directory. You are not choosing a placement. Set destination to that subject's
directory, verbatim, on every candidate.

REJECT a fact whose subject is a file in a subdirectory, a sibling, or a parent
-- and, in this batch, a file belonging to any OTHER subject you were given.
Each of those is assessed on its own terms and receives the fact from its own
run. You read only that directory, so you cannot know whether such a fact holds
of code you never opened -- and a fact placed on that basis burdens every reader
it does not apply to.

Do NOT propose that a fact belongs "higher up", and do not hedge toward it in
the "why" field. A fact that genuinely governs a wider area reaches it by HOISTING, which
happens later, at the parent, by comparing the finished CLAUDE.md files of
several children. That is not your job and you do not have the inputs for it.

TIER (CV-4). Classify every surviving candidate as FINDING-CONVERTIBLE or
CONTEXT-ONLY. FINDING-CONVERTIBLE requires ALL THREE: an imperative a reviewer
can quote verbatim, a violation test that is unambiguous rather than
discretionary, and a violation locatable at a file and line. CONTEXT-ONLY is a
real and admissible outcome -- orientation and architecture facts earn ambient
space without being convertible. What is forbidden is reporting a CONTEXT-ONLY
fact as though it were convertible; when the three do not all hold, say
CONTEXT-ONLY.

EVIDENCE (CV-7). Every candidate cites at least one file and line you OBSERVED
in source, in anchors, and that file is one of that subject's own codeFiles. A
convention needs two or more observed instances or one authoritative source.
Code outranks comments, guides, and rationale. Names, layout, and repeated
patterns start an investigation; they are not evidence on their own. A fact you
cannot anchor to observed source is DROPPED, not hedged and not reported with a
guess at a location.

CEILING. Report at most ${ceiling} candidates PER SUBJECT -- the cap is per
directory, not per batch, so a batch of ${total} may return up to ${ceiling}
candidates for each of them. If a subject would have exceeded it, set
ceilingReached: true on that subject and say in its notes how many were set
aside. Never truncate silently.

HONESTY. Your result is a SAMPLE, not an inventory -- two thorough reviewers over
one corpus found largely different facts. Do not imply exhaustiveness.

VERDICT, per subject. GAPS-FOUND if at least one candidate survives;
COVERAGE-ASSESSED if the directory was assessed and none did. NEVER emit
COMPLIANT or NON-COMPLIANT: those belong to the document lanes and answer a
different question. A CLAUDE.md can be COMPLIANT while its directory is
GAPS-FOUND at the same moment.
${FENCE}${subjectMaterial}${FENCE}Return the structured object: { subjects: [ ... ] }, one entry per subject you
assessed, each carrying its own subjectKey.`
}

// ---------------------------------------------------------------------------
// Batch construction.
// ---------------------------------------------------------------------------

// Structural refusal (never let a discovery failure read as a clean pass).
// `codeFiles` empty AND `unknownExtensions` non-empty means the directory was
// never READ -- nothing in it matched a known code, doc, or asset type -- not
// that it was verified clean. Letting that reach the agent risks a
// COVERAGE-ASSESSED verdict ("verified absent") over a directory nobody looked
// at, so this subject never gets an agent dispatch at all: it is decided here,
// mechanically, before any tokens are spent, and it is filtered out BEFORE the
// batches are cut so it cannot even occupy a slot in one.
//
// In subjectsFile mode this lane cannot see the fields, so the same rule is
// applied in the reducer to the ECHOED record instead -- the decision is still
// made here, on fields the agent copied rather than judged.
const hasUnknownExtensions = (s) =>
  s.unknownExtensions && Object.keys(s.unknownExtensions).length > 0
const hasNoCodeFiles = (s) => !Array.isArray(s.codeFiles) || s.codeFiles.length === 0

const discoveryFailureNote = (entries) =>
  `discovery failure: 0 recognized code files but unrecognized extensions ` +
  `present (${entries}) -- CODE_DATA_EXT does not cover them, so this ` +
  `directory was never read. This is NOT COVERAGE-ASSESSED.`

const discoveryFailure = (s) => {
  const entries = Object.entries(s.unknownExtensions)
    .map(([ext, count]) => `${ext || '(no extension)'}: ${count}`)
    .join(', ')
  return {
    root: s.root,
    candidates: [],
    verdict: 'DISCOVERY-FAILED',
    status: 'NOT-ASSESSED',
    provenance: 'harness-verified',
    ceilingReached: false,
    assessedFileCount: 0,
    unknownExtensionCount: Object.keys(s.unknownExtensions).length,
    ambientChainCount: (s.ambientClaudeMdPaths || []).length,
    notes: [discoveryFailureNote(entries)],
  }
}

const preDecided = []
const batches = []

if (subjectsFile) {
  for (let start = 1; start <= subjectCount; start += batchSize) {
    const end = Math.min(start + batchSize - 1, subjectCount)
    const keys = []
    for (let n = start; n <= end; n++) keys.push(`L${n}`)
    batches.push({ kind: 'file', start, end, keys })
  }
} else {
  const dispatchable = []
  for (const s of subjects) {
    if (hasNoCodeFiles(s) && hasUnknownExtensions(s)) preDecided.push(discoveryFailure(s))
    else dispatchable.push(s)
  }
  for (let i = 0; i < dispatchable.length; i += batchSize) {
    const items = dispatchable.slice(i, i + batchSize)
    batches.push({ kind: 'inline', items, keys: items.map((_, j) => `S${i + j + 1}`) })
  }
}

const batchLabel = (b, i) => {
  if (b.kind === 'file') return `coverage:batch${i + 1}:lines${b.start}-${b.end}`
  const leaf = String(b.items[0].root).split(/[\\/]/).pop()
  return b.items.length === 1 ? `coverage:${leaf}` : `coverage:${leaf}+${b.items.length - 1}`
}

// A subject the batch was asked for and did not return. It is NOT an assessment
// result and must never be tallied as one: "the agent skipped it" and "the
// directory was assessed and found clean" are the two things this lane exists to
// keep apart.
const batchIncomplete = (b, i) => ({
  subjectKey: b.keys[i],
  root: b.kind === 'inline'
    ? String(b.items[i].root)
    : `${subjectsFile}#${b.keys[i]}`,
  candidates: [],
  verdict: 'BATCH-INCOMPLETE',
  status: 'NOT-ASSESSED',
  provenance,
  ceilingReached: false,
  assessedFileCount: 0,
  unknownExtensionCount: 0,
  ambientChainCount: 0,
  notes: [
    `the batch agent returned no result object for subjectKey ${b.keys[i]}; it ` +
    'was NOT assessed. This is neither GAPS-FOUND nor COVERAGE-ASSESSED, and a ' +
    'consumer must not read its empty candidate list as assessed-null. Re-run ' +
    'this subject, on its own or at a smaller batchSize.',
  ],
})

// The refutation brief. Its scope is deliberately NARROW: is the fact TRUE as
// written? It does not re-judge admission value, and that boundary is the whole
// design, not a simplification.
//
// It is drawn from measurement rather than taste. An improvised gate over one
// corpus ran four checks; two tested truth (universal quantifiers, ordering) and
// two re-judged admission (evidence location, already-stated-at-site). Re-judged
// blind against the criteria alone, the truth checks largely HELD -- 33% and 50%
// overturned -- while the value checks COLLAPSED, at 76% and 67%. Refutation is a
// posture that finds what it is pointed at; pointed at truth it corrects the
// record, pointed at value it manufactures rejections. So this stage may delete a
// candidate only for being false, and every other criterion stays exactly where
// the criteria document put it: with the assessment, judged once.
const verifyPrompt = (rec, cands) => `Try to REFUTE each proposed fact below. Report only what you can show.

You are a fresh reviewer. Another agent proposed these facts about ONE directory
after reading it. You have not seen its reasoning and you are not being asked to
agree or disagree with its judgment.

## The ONE question you are answering

Is each fact TRUE AS WRITTEN of this directory?

That is the whole of your task. You are NOT deciding whether a fact is worth
recording, whether it belongs in an ambient document, whether a comment at the
site would serve better, or whether the evidence is interesting. Those judgments
were made against the criteria and are not yours to revisit. A fact you find
useless but true is STANDS. A fact you find valuable but false is FALSIFIED.

## Directory

${rec.root}

## Its direct code files -- ${(rec.codeFiles || []).length} of them, and this list is exhaustive

${(rec.codeFiles || []).map((f) => `- ${f}`).join('\n')}

Read these files. Not a sample of them, and never a subdirectory: a fact about
this directory is falsified or not by these files alone.

## What falsifies a fact

A fact is FALSIFIED when a file in the list above CONTRADICTS it as written.
Give the file:line. A FALSIFIED verdict with no counterexample is discarded, so
if you cannot point at the contradiction, the claim STANDS.

Three shapes account for nearly every real falsification, so check them first:

1. UNIVERSAL QUANTIFIERS. "every", "all", "always", "never", "each", "only",
   "no ... does". One file that behaves otherwise makes the fact false. Check
   every file in the list -- this is the class that a self-check misses most,
   because the agent that abstracted a pattern does not go hunting its
   exceptions.
2. ORDERING AND PRECEDENCE. "first", "before any other", "runs first", "at the
   top". Open the file and read the actual order rather than the intent.
3. EXCLUSIVITY. "the only X that", "nothing else". One sibling doing the same
   thing refutes it.

A fact that is true but IMPRECISE is not falsified. Judge the proposition, not
the prose.

## When one clause is the only problem

Most falsified facts are a real observation carrying one over-reaching clause.
When that is the case, put the restatement that WOULD stand in \`narrowing\`. It
is handed to the caller, never applied automatically -- a fact rewritten by its
verifier has been proposed by nobody.

## Do not invent criteria

The criteria document is at:

${criteriaPath}

It is the ONLY source of admission rules, and it is given to you here so the
requirement below is checkable rather than rhetorical: if you invoke any rule
from it, READ IT and quote the rule verbatim in \`quote\`. If you cannot quote
it, you have invented it -- drop it and judge on truth alone. A pure
falsification needs no quote and empty is correct there.

You are not being asked to re-run the admission judgment against that document.
Read it only to check a rule you were about to invoke.

## The candidates

${cands.map((c, i) => `### index ${i}

FACT: ${c.fact}

ANCHORS: ${(Array.isArray(c.anchors) ? c.anchors : []).join(', ')}`).join('\n\n')}

## Return

One verdict per candidate, carrying the index EXACTLY as issued above. Report
\`filesRead\` (how many of the listed files you actually opened) and
\`filesInDir\` (${(rec.codeFiles || []).length}). A universal claim judged
without reading every file has not been checked, and the two counts are how that
is visible afterwards.`

// Match the batch's returned array back onto the subjects that were REQUESTED,
// BY KEY. Never by position: a batch that omits its middle subject would
// otherwise shift every later result one slot, and the inline root overwrite --
// the thing documented as a safeguard -- would then stamp the wrong directory
// onto real findings. Losing a subject is recoverable; misfiling one is the
// contamination this whole design exists to prevent.
let extraReturned = 0
let identityUnmatched = 0
const reconcileBatch = (b, r) => {
  const returned = r && Array.isArray(r.subjects) ? r.subjects : []
  const wanted = new Set(b.keys)
  const byKey = new Map()
  for (const rec of returned) {
    const k = rec && typeof rec.subjectKey === 'string' ? rec.subjectKey : null
    if (!k || !wanted.has(k) || byKey.has(k)) {
      // Unkeyed, unrequested, or a duplicate key: discarded, never guessed at.
      extraReturned++
      if (k && !wanted.has(k)) identityUnmatched++
      continue
    }
    byKey.set(k, rec)
  }
  return b.keys.map((k, i) => {
    const rec = byKey.get(k)
    if (!rec) return batchIncomplete(b, i)
    // Inline: identity and the file list are TRUSTED INPUT, and the key is what
    // makes "which input" unambiguous. subjectsFile: both are attested by the
    // agent, and the record says so through `provenance`.
    const trusted = b.kind === 'inline' ? b.items[i] : null
    const root = trusted ? String(trusted.root) : String(rec.root || '')
    const codeFiles = trusted
      ? (trusted.codeFiles || [])
      : (Array.isArray(rec.codeFiles) ? rec.codeFiles : [])
    return { ...rec, subjectKey: k, root, codeFiles, provenance, status: 'ASSESSED' }
  })
}

phase('Coverage')
const batchResults = await parallel(batches.map((b, i) => () =>
  // Detection is this verb's judgment core, so the tier is pinned rather than
  // inherited. This matters more here than in the document lanes: a coverage run
  // may have exactly ONE subject, and the audit lane's single-subject shortcut
  // runs inline at whatever model the session happens to be on. Going through
  // the workflow regardless of count is what keeps the common case on-pin.
  agent(batchPrompt(b), {
    label: batchLabel(b, i),
    phase: 'Coverage',
    model: 'opus',
    effort: 'high',
    schema: BATCH_FINDINGS_SCHEMA,
  }).then((r) => reconcileBatch(b, r))
))

const perSubject = preDecided.concat(...batchResults.filter(Boolean))

// The verdict is DERIVED, never taken on trust. The schema can constrain the
// verdict to two values but cannot express "GAPS-FOUND iff candidates is
// non-empty", so a schema-valid response can carry GAPS-FOUND with zero
// candidates (or the reverse) and contradict the decision rules the lane doc
// states. Recomputing it here makes the rule true by construction. The same
// argument covers `destination`, which generation groups by.
let isolationViolations = 0
let destinationCorrected = 0
let fileListDisagreements = 0
// The echoed code-file list is stripped from every returned record below, but
// the refutation stage needs it: it is the exhaustive read set a universal claim
// has to be checked against. Kept here, keyed by subject, so the reducer's own
// contract (nothing travels back to the orchestrator that subjectsFile mode
// exists to keep out) is unchanged.
const codeFilesByKey = new Map()
const results = perSubject.filter(Boolean).map((r) => {
  if (r && r.subjectKey) codeFilesByKey.set(r.subjectKey, Array.isArray(r.codeFiles) ? r.codeFiles : [])
  const incoming = r.candidates || []
  // A DISCOVERY-FAILED or BATCH-INCOMPLETE subject never produced an assessment,
  // so there is nothing to derive or re-derive: it is passed through unchanged,
  // and it must NOT fall into the candidates.length ? GAPS-FOUND :
  // COVERAGE-ASSESSED derivation below -- that would turn "never read" or "never
  // returned" into "verified absent" right here.
  if (r.verdict === 'DISCOVERY-FAILED' || r.verdict === 'BATCH-INCOMPLETE') {
    return { ...r, candidates: incoming, depth }
  }

  const notes = Array.isArray(r.notes) ? [...r.notes] : []
  const codeFiles = Array.isArray(r.codeFiles) ? r.codeFiles : []

  // Transcription cross-check. The count and the list are two statements about
  // one fact, so a disagreement means one of them is wrong and neither can be
  // preferred silently.
  if (r.assessedFileCount !== codeFiles.length) {
    fileListDisagreements++
    notes.push(
      `transcription disagreement: assessedFileCount ${r.assessedFileCount} but ` +
      `${codeFiles.length} codeFiles echoed; the echoed list is what anchors were ` +
      'checked against.'
    )
  }

  // The discovery-failure refusal. In inline mode these subjects were filtered
  // out before dispatch, so this fires in subjectsFile mode -- or on an inline
  // subject whose transcription contradicts its own record, which is worth
  // catching either way.
  if (r.assessedFileCount === 0 && r.unknownExtensionCount > 0) {
    notes.push(discoveryFailureNote(`${r.unknownExtensionCount} unrecognized extension(s)`))
    return {
      ...r, candidates: [], verdict: 'DISCOVERY-FAILED', status: 'NOT-ASSESSED',
      depth, notes, codeFiles: undefined,
    }
  }

  // Anchor membership. Runs BEFORE the verdict derivation, so a subject whose
  // every candidate was rejected reads as COVERAGE-ASSESSED with the drop named
  // in its notes rather than as GAPS-FOUND over evidence that was thrown away.
  const leaked = []
  const candidates = incoming.filter((c) => {
    const anchors = Array.isArray(c.anchors) ? c.anchors : []
    if (!anchors.length) { leaked.push('(no anchors)'); return false }
    const bad = []
    for (const a of anchors) {
      const why = anchorRejectionReason(a, codeFiles)
      if (why) bad.push(`${JSON.stringify(String(a))} [${why}]`)
    }
    if (!bad.length) return true
    for (const b of bad) leaked.push(b)
    return false
  })
  const dropped = incoming.length - candidates.length
  if (dropped) {
    isolationViolations += dropped
    notes.push(
      `isolation: ${dropped} candidate(s) DROPPED -- their evidence anchors do not ` +
      `name a file in this subject own code-file list, with a line number ` +
      `(${leaked.slice(0, 5).join(', ')}` +
      `${leaked.length > 5 ? `, +${leaked.length - 5} more` : ''}). A fact that ` +
      'cannot be anchored inside the assessed directory is a CV-3 violation and, ' +
      'in a batch, the signature of cross-subject contamination.'
    )
  }

  // `destination` is DERIVED. The schema could ask for it and additionalProperties
  // could not check it, and generation groups by this field -- so a wrong value
  // re-homes a fact into a document that never earned it.
  let correctedHere = 0
  const placed = candidates.map((c) => {
    if (String(c.destination || '') === String(r.root)) return c
    correctedHere++
    return { ...c, destination: r.root }
  })
  if (correctedHere) {
    destinationCorrected += correctedHere
    notes.push(
      `destination corrected on ${correctedHere} candidate(s): a directory other ` +
      'than the assessed one was named. destination is degenerate by construction ' +
      'and is derived from the subject, never accepted from the assessment.'
    )
  }

  const derived = placed.length ? 'GAPS-FOUND' : 'COVERAGE-ASSESSED'
  if (r.verdict && r.verdict !== derived) {
    notes.push(
      `verdict corrected: lane returned ${r.verdict} with ${placed.length} candidate(s)`
    )
  }
  // A ceiling hit must never be silent, even if the lane forgot to say so.
  if (r.ceilingReached && !notes.some((n) => /ceiling|set aside|capped/i.test(n))) {
    notes.push(`candidate ceiling of ${ceiling} reached; results are capped, not complete`)
  }
  // The echoed code-file list is DROPPED from the returned record. It has done
  // its job (anchor membership), and the workflow result travels back through
  // the orchestrator -- carrying it there would re-inflate exactly the payload
  // subjectsFile mode exists to keep out of that context.
  return { ...r, candidates: placed, verdict: derived, depth, notes, codeFiles: undefined }
})

// ---- The REFUTATION stage. ----
//
// This is the difference between a lane that GENERATES and a lane that
// generates and VERIFIES. Everything above enforces FORM -- subject identity,
// anchor membership, destination, the verdict rule. Until this stage existed,
// every SEMANTIC property, the truth of the fact included, was enforced only by
// the proposing agent judging its own output in its own context, while the depth
// table told the caller the result was "verified absent".
//
// It runs in FRESH context, one dispatch per subject, and it may do exactly one
// thing to the record: delete a candidate that a named file:line contradicts.
// A measured note on why it is not per-candidate: a subject's candidates share
// one read of one directory, so per-subject amortizes that read across them,
// and per-candidate would pay it again for each.
let verified = 0
let falsified = 0
// A subject the stage never answered for and a single candidate missing from an
// otherwise-complete verdict set are DIFFERENT failures, and one counter over
// both hides which one happened. The first is an infrastructure failure over a
// whole directory; the second is what OUTPUT TRUNCATION looks like -- on the
// measured run the one unanswered candidate was the LAST index of the LONGEST
// candidate list, which is a shape a judgment does not produce. Reading them
// apart is what lets an operator tell "the dispatch failed" from "the answer
// was cut off", so they are counted apart.
let verifySubjectsUnreturned = 0
let verifyCandidatesUnanswered = 0
let verifyUnsupported = 0
let verifyPartialReads = 0
// Counted apart from verifyPartialReads, which tallies partial-read rows in
// BOTH truth directions. A partial read is not symmetric: unread files can only
// ADD counterexamples, so they can never rescue a fact a read file already
// contradicted, but they can easily hold the counterexample that would have
// killed one that was allowed to stand. So a partial FALSIFIED is sound and a
// partial STANDS is the exposure, and only the second number tells a reader how
// much of the run's "upheld" column was never actually checked.
let verifyPartialStands = 0
const verifyEnabled = depth === 'advanced' && input.verify !== false
const verifyTargets = verifyEnabled
  ? results.filter((r) => r.status === 'ASSESSED' && (r.candidates || []).length)
  : []

const verifyByKey = new Map()
if (verifyTargets.length) {
  const verdictSets = await parallel(verifyTargets.map((r) => () =>
    agent(verifyPrompt({ root: r.root, codeFiles: codeFilesByKey.get(r.subjectKey) || [] }, r.candidates), {
      label: `verify ${r.root}`,
      phase: 'Coverage',
      // Pinned for the same reason detection is: refuting a universal claim
      // means reading every file in a directory and noticing the one that does
      // not conform, which is exactly where a cheaper tier was measured to fail.
      model: 'opus',
      effort: 'high',
      schema: VERIFY_FINDINGS_SCHEMA,
    }).then((v) => [r.subjectKey, v])
  ))
  for (const pair of verdictSets) {
    if (!pair) continue
    verifyByKey.set(pair[0], pair[1])
  }
}

// Apply the verdicts. A subject the stage did not answer for KEEPS its
// candidates and says so: an unreturned verification is missing evidence, and
// silently dropping candidates on it would let an infrastructure failure read
// as a clean directory -- the exact confusion between "nobody checked" and
// "nothing found" that DISCOVERY-FAILED exists to prevent upstream.
const verifiedResults = !verifyEnabled ? results : results.map((r) => {
  if (r.status !== 'ASSESSED' || !(r.candidates || []).length) return r
  const notes = Array.isArray(r.notes) ? [...r.notes] : []
  const v = verifyByKey.get(r.subjectKey)
  const rows = v && Array.isArray(v.verdicts) ? v.verdicts : null
  if (!rows) {
    verifySubjectsUnreturned += r.candidates.length
    notes.push(
      'verification UNRETURNED for this subject: its candidates are unverified ' +
      'and were kept. At this depth COVERAGE-ASSESSED means verified absent, ' +
      'which this subject has not earned -- treat its candidates as depth basic.'
    )
    // Stamp every CANDIDATE too, not just the subject. The sibling path below
    // (a single candidate with no row) already does this, and a consumer reads
    // candidate records, not subject flags. Leaving the key ABSENT here was
    // worse than a wrong value: `c.verified === false` read `undefined` and
    // silently gave the wrong answer, while `!c.verified` happened to work by
    // accident -- a contract only the laxer of two idiomatic checks satisfies
    // is not a contract. No `readComplete`, for the same reason as the sibling
    // path: no row was returned, so there is no read to report on.
    return {
      ...r,
      notes,
      verified: false,
      candidates: r.candidates.map((c) => ({ ...c, verified: false })),
    }
  }

  const byIndex = new Map()
  for (const row of rows) {
    if (!row || typeof row.index !== 'number') continue
    if (!byIndex.has(row.index)) byIndex.set(row.index, row)
  }

  // A shortfall is a fact about the ROW, so it is computed once here and read
  // in both directions below rather than re-derived at each use.
  const shortfallOf = (row) => (
    row && typeof row.filesRead === 'number' && typeof row.filesInDir === 'number' &&
    row.filesRead < row.filesInDir
  ) ? { filesRead: row.filesRead, filesInDir: row.filesInDir } : null

  const killed = []
  // The STRUCTURAL record of every deletion. The prose note below is a reading
  // convenience and truncates; this does not. A kill is a decision the run made
  // about a fact, and report-only means nothing is written to the CODEBASE --
  // it was never a licence for the report to forget what it decided. Without
  // this array a subject with eight kills named three of them and discarded the
  // counterexample the other five were killed on, so the rejection was less
  // accountable than the deletion the schema already guards ("a rejection is as
  // accountable as a deletion", ../references/lanes/coverage-lane.md).
  const falsifiedRecords = []
  let unansweredHere = 0
  let partialStandsHere = 0
  const kept = r.candidates.map((c, i) => {
    const row = byIndex.get(i)
    if (!row) {
      verifyCandidatesUnanswered++
      unansweredHere++
      // No `readComplete` here on purpose: the field reports what a RETURNED
      // row said about its own read, and there is no row. `verified: false`
      // already says this candidate was not checked at all, which is the
      // stronger statement; adding readComplete would imply a read happened.
      return { ...c, verified: false }
    }
    const ce = String(row.counterexample || '').trim()
    if (row.truth === 'FALSIFIED' && !ce) {
      // A FALSIFIED verdict that cannot point at the contradiction is not
      // evidence, and deleting a candidate on it would be the same
      // unaccountable rejection this stage exists to replace.
      verifyUnsupported++
      notes.push(
        `verification returned FALSIFIED with no counterexample for candidate ` +
        `${i + 1}; the verdict was DISCARDED and the candidate kept.`
      )
      return { ...c, verified: false }
    }
    if (row.truth === 'FALSIFIED') {
      // A partial read is NOT a reason to withhold a kill. Unread files can
      // only add counterexamples, never withdraw the one that was found, so
      // reading the rest of the directory cannot rescue this fact. The
      // asymmetry is the whole reason the STANDS branch below behaves
      // differently on the same shortfall.
      killed.push(`"${String(c.fact).slice(0, 60)}..." [${ce}]`)
      falsifiedRecords.push({
        index: i,
        fact: String(c.fact),
        anchors: Array.isArray(c.anchors) ? [...c.anchors] : [],
        tier: c.tier,
        counterexample: ce,
        // Empty is CORRECT for a pure falsification -- the criteria document is
        // quoted only when a criterion was invoked, and a contradiction needs
        // none. Carried verbatim rather than defaulted, so a judge that DID
        // invoke a rule is still readable off the record.
        quote: String(row.quote || ''),
      })
      return null
    }
    // A candidate that STANDS on a partial read must not claim `verified: true`.
    // The refuter did not open every file in the directory, so the file it did
    // not open is exactly where a counterexample to a universal claim would
    // sit. The subject-level note and verifyPartialReads already said this
    // about the SUBJECT, but a consumer reads CANDIDATES, and a record stamped
    // verified is indistinguishable there from a fully-checked one.
    const short = shortfallOf(row)
    const out = { ...c, verified: !short, readComplete: !short }
    if (short) {
      partialStandsHere++
      verifyPartialStands++
      out.filesRead = short.filesRead
      out.filesInDir = short.filesInDir
    }
    if (String(row.narrowing || '').trim()) out.narrowing = String(row.narrowing).trim()
    return out
  })

  const surviving = kept.filter(Boolean)
  falsified += r.candidates.length - surviving.length
  verified += surviving.filter((c) => c.verified).length

  const partial = rows.filter((row) => shortfallOf(row))
  if (partial.length) {
    verifyPartialReads += partial.length
    notes.push(
      `verification read ${partial.length} candidate(s) against fewer files than ` +
      'the directory holds; a universal claim judged that way was not fully checked.'
    )
  }
  if (partialStandsHere) {
    notes.push(
      `of those, ${partialStandsHere} STOOD on the partial read and were therefore ` +
      'NOT stamped verified (readComplete: false on the candidate, with the read ' +
      'figures). A partial read is safe in the FALSIFIED direction and unsafe in ' +
      'the STANDS direction: an unread file can only add a counterexample, so it ' +
      'cannot rescue a fact already contradicted, but it can hold the one that ' +
      'would have killed a fact that was allowed to stand.'
    )
  }
  if (unansweredHere) {
    notes.push(
      `verification returned no verdict row for ${unansweredHere} of this subject ` +
      'candidate(s) while answering the rest; they were KEPT, verified false. This ' +
      'is not the same failure as an unreturned subject -- a verdict set that is ' +
      'complete except at its tail is what OUTPUT TRUNCATION looks like, so a ' +
      'recurrence points at the response budget rather than at the judge.'
    )
  }
  if (killed.length) {
    // The prose stays short and the full record rides on `falsified` below, so
    // this note is a summary rather than the evidence.
    notes.push(
      `verification FALSIFIED ${killed.length} candidate(s) against source ` +
      `(${killed.slice(0, 3).join('; ')}${killed.length > 3 ? `; +${killed.length - 3} more` : ''}). ` +
      'Every deletion is recorded in full in this record falsified array.'
    )
  }

  // Re-derive with the SAME expression the reducer used, so "GAPS-FOUND iff
  // candidates" stays true by construction after a deletion rather than being
  // asserted twice and able to disagree with itself.
  const derived = surviving.length ? 'GAPS-FOUND' : 'COVERAGE-ASSESSED'
  return { ...r, candidates: surviving, verdict: derived, notes, falsified: falsifiedRecords }
})

// A root appearing twice means two result objects claim one directory. Counted
// rather than resolved: this lane cannot tell which of the two is real.
const rootSeen = new Map()
for (const r of verifiedResults) {
  const k = String(r.root)
  rootSeen.set(k, (rootSeen.get(k) || 0) + 1)
}
let duplicateRoots = 0
for (const n of rootSeen.values()) if (n > 1) duplicateRoots += n - 1

// The ambient chain is an INPUT fact, not something the lane reports back, so
// the uncovered tally is computed from the subjects rather than the results --
// reading it off the agent's object would count every directory as uncovered.
// In subjectsFile mode there IS no input here to read, and only then does the
// echoed count stand in for it.
const chainSizeByRoot = new Map(
  subjects.map((s) => [String(s.root), (s.ambientClaudeMdPaths || []).length])
)
const chainSizeForRoot = (r) =>
  chainSizeByRoot.has(String(r.root))
    ? chainSizeByRoot.get(String(r.root))
    : (subjectsFile ? r.ambientChainCount : 0)

const totals = verifiedResults.reduce((acc, r) => {
  acc.candidates += (r.candidates || []).length
  acc.severe += (r.candidates || []).filter((c) => c.severeDeficiency).length
  // CV-4 requires the classification to be REPORTED. The per-candidate `tier`
  // reaches the rendered report through the lane's candidate template; this
  // tally makes it reach the RUN SUMMARY too, so a reader who never opens the
  // candidate list still sees the split. Counted by exact enum value rather
  // than by "not FINDING-CONVERTIBLE", so a future third tier shows up as a
  // discrepancy against acc.candidates instead of being silently folded into
  // CONTEXT-ONLY.
  acc.findingConvertible += (r.candidates || []).filter((c) => c.tier === 'FINDING-CONVERTIBLE').length
  acc.contextOnly += (r.candidates || []).filter((c) => c.tier === 'CONTEXT-ONLY').length
  // CV-7's evidence floor is schema-enforced (anchors required, minItems 1,
  // minLength 1) AND membership-enforced above, so this is a carriage check
  // rather than an adjudication.
  acc.unanchored += (r.candidates || []).filter((c) => !(c.anchors || []).length).length
  if (r.verdict === 'GAPS-FOUND') acc.gapsFound++
  if (r.verdict === 'COVERAGE-ASSESSED') acc.assessed++
  // Counted apart from both verdicts, deliberately: DISCOVERY-FAILED is
  // neither "gaps found" nor "assessed clean" -- it means the directory could
  // not be classified at all, and folding it into assessed would be exactly
  // the fake pass this refusal exists to prevent. BATCH-INCOMPLETE is counted
  // apart for the same reason: requested, not returned, not assessed.
  if (r.verdict === 'DISCOVERY-FAILED') acc.discoveryFailed++
  if (r.verdict === 'BATCH-INCOMPLETE') acc.batchIncomplete++
  if (r.status === 'ASSESSED') acc.completed++
  if (r.ceilingReached) acc.ceilingReached++
  // Counted apart from the verdicts, and ONLY over subjects actually assessed:
  // a directory nothing covers is the finding this verb exists for, but a
  // directory nobody read is not evidence that nothing covers it.
  if (r.status === 'ASSESSED' && !chainSizeForRoot(r)) acc.uncovered++
  return acc
}, {
  candidates: 0,
  severe: 0,
  findingConvertible: 0,
  contextOnly: 0,
  unanchored: 0,
  gapsFound: 0,
  assessed: 0,
  discoveryFailed: 0,
  batchIncomplete: 0,
  completed: 0,
  ceilingReached: 0,
  uncovered: 0,
})
totals.requested = subjectCount
totals.notAssessed = verifiedResults.length - totals.completed
totals.isolationViolations = isolationViolations
totals.destinationCorrected = destinationCorrected
totals.fileListDisagreements = fileListDisagreements
totals.duplicateRoots = duplicateRoots
totals.extraReturned = extraReturned
totals.identityUnmatched = identityUnmatched
totals.provenance = provenance
// The refutation stage's own accounting. `verifyRan` is what downstream must
// read before believing "verified absent": a run where it is false produced
// candidates nothing independent ever checked, whatever the depth says.
totals.verifyRan = verifyEnabled
totals.verified = verified
totals.falsified = falsified
// `verified` counts only candidates that carry `verified: true`, so a candidate
// that stood on a partial read is NOT in it -- it survives, and the run does not
// claim to have checked it.
totals.verifySubjectsUnreturned = verifySubjectsUnreturned
totals.verifyCandidatesUnanswered = verifyCandidatesUnanswered
totals.verifyUnsupported = verifyUnsupported
totals.verifyPartialReads = verifyPartialReads
totals.verifyPartialStands = verifyPartialStands

// The ceiling is per directory, so a wide run's aggregate is subjects x ceiling.
// Stating the aggregate keeps a capped multi-directory run from reading as
// complete just because no single directory looks truncated.
const ceilingNote = totals.ceilingReached
  ? `, ${totals.ceilingReached}/${verifiedResults.length} directory/ies hit the per-directory ceiling of ${ceiling} (those results are capped, not complete)`
  : ''
const severeNote = totals.severe ? `, ${totals.severe} severe-deficiency` : ''
// CV-4: the tier split rides on the summary line so the classification is
// reported even when only the log is read. CV-7: an unanchored candidate cannot
// pass the schema, so the clause is silent unless one somehow does.
const tierNote = totals.candidates
  ? ` (${totals.findingConvertible} FINDING-CONVERTIBLE, ${totals.contextOnly} CONTEXT-ONLY)`
  : ''
const evidenceNote = totals.unanchored
  ? `, ${totals.unanchored} candidate(s) arrived with NO anchor -- CV-7 evidence floor breached`
  : ''
const discoveryFailedNote = totals.discoveryFailed
  ? `, ${totals.discoveryFailed} directory/ies DISCOVERY-FAILED -- unrecognized ` +
    `extensions with zero recognized code files, never read, not COVERAGE-ASSESSED`
  : ''
const uncoveredNote = totals.uncovered
  ? `, ${totals.uncovered} assessed directory/ies with NO ambient CLAUDE.md at all`
  : ''
const isolationNote = totals.isolationViolations
  ? `, ${totals.isolationViolations} candidate(s) DROPPED for anchors that name no file in their own subject code-file list -- cross-subject contamination; re-run those subjects at batchSize 1`
  : ''
const incompleteNote = totals.batchIncomplete
  ? `, ${totals.batchIncomplete} subject(s) BATCH-INCOMPLETE -- requested but not returned, NOT assessed`
  : ''
const destinationNote = totals.destinationCorrected
  ? `, ${totals.destinationCorrected} candidate destination(s) corrected to their own subject`
  : ''
const disagreementNote = totals.fileListDisagreements
  ? `, ${totals.fileListDisagreements} subject(s) disagreed with their own file count`
  : ''
const duplicateNote = totals.duplicateRoots
  ? `, ${totals.duplicateRoots} duplicate root(s) returned`
  : ''
const extraNote = totals.extraReturned
  ? `, ${totals.extraReturned} unrequested or unkeyed result object(s) discarded`
  : ''
// The refutation stage rides on the summary line for the same reason the tier
// split does: a reader who sees only the log must be able to tell a verified run
// from an unverified one, and must never have to infer it from the depth.
const verifyNote = !verifyEnabled
  ? (depth === 'advanced'
    ? ', verification DISABLED for this run -- COVERAGE-ASSESSED here means not found within budget, NOT verified absent'
    : '')
  : `, verification: ${totals.falsified} candidate(s) FALSIFIED against source, ${totals.verified} upheld` +
    (totals.verifySubjectsUnreturned ? `, ${totals.verifySubjectsUnreturned} UNVERIFIED in subjects the stage never answered for (kept, treat as depth basic)` : '') +
    (totals.verifyCandidatesUnanswered ? `, ${totals.verifyCandidatesUnanswered} candidate(s) missing a verdict row from an otherwise-answered subject (kept, verified false -- the signature of output truncation, not of a judgment)` : '') +
    (totals.verifyUnsupported ? `, ${totals.verifyUnsupported} unsupported FALSIFIED verdict(s) discarded` : '') +
    (totals.verifyPartialReads ? `, ${totals.verifyPartialReads} judged against fewer files than the directory holds` : '') +
    (totals.verifyPartialStands ? `, of which ${totals.verifyPartialStands} STOOD on that partial read and are NOT counted as verified` : '')
const modeNote = subjectsFile ? `subjectsFile=${subjectsFile}` : 'inline'
const provenanceWarning = provenance === 'agent-attested'
  ? ' PROVENANCE: roots and code-file lists are AGENT-ATTESTED, not verified against the subjects file -- verify before promoting (coverage-lane.md, "Verifying an agent-attested run").'
  : ''
const runNoteClause = runNotes.length ? ` NOTE: ${runNotes.join(' ')}` : ''

log(`Coverage (depth=${depth}, ${modeNote}, provenance=${provenance}, ${batches.length} batch(es) of up to ${batchSize}): ${totals.completed} of ${totals.requested} requested directory/ies COMPLETED (${totals.notAssessed} NOT assessed): ${totals.gapsFound} GAPS-FOUND, ${totals.assessed} COVERAGE-ASSESSED, ${totals.candidates} candidate(s)${tierNote}${severeNote}${evidenceNote}${uncoveredNote}${discoveryFailedNote}${incompleteNote}${isolationNote}${destinationNote}${disagreementNote}${duplicateNote}${extraNote}${verifyNote}${ceilingNote}. Advisory and non-idempotent: re-runs may differ, and nothing is applied.${provenanceWarning}${runNoteClause}`)

return { perSubject: verifiedResults, totals, ceiling, depth, batchSize, batches: batches.length, subjectsFile, provenance, notes: runNotes }
