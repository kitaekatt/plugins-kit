// references-audit — CLASSIFY workflow (after-scan, before-Q&A phase).
//
// The scan itself stays a single references_audit.py run (fast, stdlib-only,
// whole-corpus — there is nothing to parallelize there, and splitting it would
// fragment the skill pool). The agent work is what fans out: classify each
// scanner finding into the A-K taxonomy and, for AUTO findings, compute the exact
// before/after edit. ONE lane per file-with-findings (so two lanes never edit the
// same file, and AUTO before/after text is computed against that file's real
// content). Pure classification — NO file is modified here (remediation is a
// separate after-Q&A pass).
//
// Invoked by the references-audit SKILL.md only when 2+ files carry findings (the
// multi-file threshold that equalizes the Workflow tool's per-run overhead). A
// single file's findings are classified inline in the main loop.
//
// args = {
//   files: [ { file: string,
//              findings: [ { severity: "ERROR"|"WARNING"|"INFO",
//                            line: integer|null, ref: string } ] } ],
//   refs:  { taxonomyDoc: <abs path to references/finding-taxonomy.md> }
// }

export const meta = {
  name: 'references-audit-classify',
  description: 'Fan-out reference-finding classification: assign A-K taxonomy + bucket + compute AUTO before/after, one lane per file (no edits)',
  phases: [{ title: 'Classify', detail: 'one lane per file with findings' }],
}

const FILE_CLASSIFIED_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    file: { type: 'string' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          severity: { type: 'string', enum: ['ERROR', 'WARNING', 'INFO'] },
          line: { type: ['integer', 'null'] },
          ref: { type: 'string' },
          category: { type: 'string', description: 'taxonomy id A-K' },
          bucket: { type: 'string', enum: ['AUTO', 'DISCUSS', 'SPECIAL'] },
          before: { type: 'string', description: 'exact current line text for an AUTO edit; empty for DISCUSS/SPECIAL' },
          after: { type: 'string', description: 'proposed replacement text for an AUTO edit; empty for DISCUSS/SPECIAL' },
          rationale: { type: 'string', description: 'why this category fits / what the user must decide' },
        },
        required: ['severity', 'line', 'ref', 'category', 'bucket', 'before', 'after', 'rationale'],
      },
    },
  },
  required: ['file', 'findings'],
}

let input = args
if (typeof input === 'string') {
  try { input = JSON.parse(input) } catch (_) { input = null }
}
if (!input || !Array.isArray(input.files) || input.files.length === 0) {
  throw new Error('classify.js requires args.files = [{file, findings}]')
}
const refs = input.refs || {}

function lanePrompt(f) {
  const taxonomyClause = refs.taxonomyDoc
    ? `Read the taxonomy reference at ${refs.taxonomyDoc} for the full A-K detection signals, default remediations, and the background-agent brief template.`
    : `Use the A-K taxonomy from the references-audit contract (taxonomy doc path was not provided).`

  return `You are ONE lane of a references audit. Classify every finding for exactly one file. This is CLASSIFICATION ONLY — do not modify any file.

Target file: ${f.file}

Scanner findings for this file:
${f.findings.map((x, i) => `${i + 1}. [${x.severity}] line ${x.line == null ? '?' : x.line}: broken ref "${x.ref}"`).join('\n')}

Steps:
1. ${taxonomyClause}
2. Read the target file (at least around each cited line) so you can see the real surrounding text.
3. For EACH finding, assign exactly one taxonomy category and its bucket:
     - AUTO  = mechanical, unambiguous: A_renamed (1:1 rename), C_merged (slash->dispatch form), E_compound_adjective (reword to drop the slash), F_cli_flag (fence the command), G_xml_template (fence the example), I_illustrative (add example: prefix), J_forward_looking (add proposed: prefix).
     - DISCUSS = needs a user decision: B_retired (which sub-case), D_scope_violating, H_harness_transcript (ignore-dir), and any AUTO category whose mapping is unknown (e.g. A_renamed with no clear new name).
     - SPECIAL = K_unclassified.
4. For every AUTO finding, compute the EXACT before-text (the current line, verbatim) and the after-text (per the category's default remediation). For DISCUSS/SPECIAL leave before/after empty and put the decision the user must make in \`rationale\`.
5. Do NOT reclassify what the taxonomy has already settled; your job is the category match + (for AUTO) the precise edit, not second-guessing the taxonomy.

Idempotency matters: classify deterministically from the detection signals. Return the structured object (preserve each finding's severity/line/ref).`
}

phase('Classify')
const perFile = await parallel(input.files.map((f) => () =>
  // Default lane tier: opus at high effort. Classification is the audit's
  // judgment core — it warrants the judge tier, explicitly pinned.
  agent(lanePrompt(f), {
    label: `classify:${f.file.split(/[\\/]/).pop()}`,
    phase: 'Classify',
    model: 'opus',
    effort: 'high',
    schema: FILE_CLASSIFIED_SCHEMA,
  }).then((r) => ({ ...r, file: f.file }))
))

const results = perFile.filter(Boolean)
const totals = results.reduce((acc, r) => {
  for (const fnd of r.findings) {
    if (fnd.bucket === 'AUTO') acc.auto++
    else if (fnd.bucket === 'DISCUSS') acc.discuss++
    else if (fnd.bucket === 'SPECIAL') acc.special++
    if (fnd.severity === 'ERROR') acc.errors++
  }
  return acc
}, { auto: 0, discuss: 0, special: 0, errors: 0 })

log(`Classified findings in ${results.length}/${input.files.length} files — ${totals.errors} ERROR; buckets AUTO=${totals.auto} DISCUSS=${totals.discuss} SPECIAL=${totals.special}`)

return { perFile: results, totals }
