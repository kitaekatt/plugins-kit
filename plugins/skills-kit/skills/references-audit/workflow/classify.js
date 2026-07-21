// references-audit — CLASSIFY workflow (after-scan, before-Q&A phase).
//
// The scan itself stays a single references_audit.py run (fast, stdlib-only,
// whole-corpus — there is nothing to parallelize there, and splitting it would
// fragment the skill pool). The agent work is what fans out: classify each
// scanner finding into the A-K taxonomy and, for FIX findings, compute the exact
// before/after edit. ONE lane per file-with-findings (so two lanes never edit the
// same file, and FIX before/after text is computed against that file's real
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
//
// Dispositions are the ratified four-disposition contract: FIX (auto-applied) /
// SERIOUS (summarized at top) / IMPROVE (count + one-liners, opt-in) / SILENT
// (not surfaced); K_unclassified -> SPECIAL. The taxonomy default bucket is a
// starting point only; the lane assigns the final disposition instance-level
// against explicit predicates (see lanePrompt).

export const meta = {
  name: 'references-audit-classify',
  description: 'Fan-out reference-finding classification: assign A-K taxonomy + a FIX/SERIOUS/IMPROVE/SILENT disposition + compute FIX before/after, one lane per file (no edits)',
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
          bucket: { type: 'string', enum: ['FIX', 'SERIOUS', 'IMPROVE', 'SILENT', 'SPECIAL'], description: 'per-finding disposition assigned instance-level by the classifier' },
          before: { type: 'string', description: 'exact current line text for a FIX edit that is a before/after replacement; empty for instruction-type FIX and for SERIOUS/IMPROVE/SILENT/SPECIAL' },
          after: { type: 'string', description: 'proposed replacement text for a FIX before/after edit; empty otherwise' },
          rationale: { type: 'string', description: 'why this category + disposition fits; for instruction-type FIX the recipe; for SERIOUS the one-line top-of-report summary; for IMPROVE the one-line pitch; for SILENT why it is not surfaced' },
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
3. For EACH finding, assign exactly one taxonomy category (A-K) AND one of the four dispositions -- FIX / SERIOUS / IMPROVE / SILENT (K_unclassified -> SPECIAL). The taxonomy default bucket is a STARTING POINT only; decide the disposition instance-level against the predicates below.

   Classifier prod (read this FIRST -- it overrides your default caution): You are biased toward conservatism; the user's time and attention are the scarce resources; source control and CL review are the safety net. If the edit very likely improves the doc, apply it.

   Master razor: FIX = anything decidable by VERIFIED FACTS (does the ref resolve against the scanner's skill pool?) plus DOCUMENTED PROJECT CONVENTIONS (the example:/proposed: escape prefixes, code-fence masking, ASCII-only). Reserve IMPROVE for where no fact and no convention decides. The bar for FIX is: would a reasonable owner, seeing this diff in CL review, accept it without discussion? "Very likely improving" clears it.

   FIX (auto-applied; lands in a reviewable CL) -- the mechanical, decidable categories:
     - A_renamed with a KNOWN 1:1 mapping (find/replace old-name -> new-name).
     - C_merged (rewrite the slash form to the new dispatch form in prose; demote to a backticked literal inside a dispatch-alias table).
     - E_compound_adjective (reword to drop the punctuation slash).
     - F_cli_flag / G_xml_template (fence the command / example -- the scanner masks fenced regions).
     - I_illustrative (add the example: escape prefix) when the sentence is clearly meta-descriptive.
     - J_forward_looking (add the proposed: escape prefix) when the prose explicitly cues "planned"/"future".
     - B_retired ONLY in the purely-incidental sub-case: a broken clause whose removal loses nothing (falsified-content deletion under the loss-free guard).
   SERIOUS (surface at the TOP, summarized, NEVER auto-fixed, never buried):
     - An ERROR hard-dep (a Skill-tool \`skill: "..."\` invocation) to a skill that is GENUINELY GONE with no replacement and no mechanical escape -- a live runtime-crash path with NO surviving mechanism. The real finding is the unguarded invocation, not the doc drift.
   IMPROVE (count + one-liners; opt-in) -- structural or judgment calls where no fact/convention decides the fix:
     - A_renamed with an UNKNOWN mapping (offer the best-guess new name as a one-liner).
     - B_retired non-incidental (which sub-case -- delete the section, demote to backtick, or add to the references-audit-allow-stale frontmatter -- protects surrounding true content; loss-free guard).
     - D_scope_violating (deleting a cross-scope reference may drop true comparison content).
     - H_harness_transcript (recommend the --ignore-dir wrapper flag ONCE for the whole batch -- a config decision that edits the invocation wrapper, not the scanned files).
     - I_illustrative / J_forward_looking when the cue is ambiguous (it could be a real, currently-broken instruction rather than an example/plan).
     - name-mismatch when inbound references disagree on which name is canonical (renaming would break real refs).
     - shadowing that looks ACCIDENTAL (one-line "user skill X shadows project skill X -- intended?").
   SILENT (do NOT surface; no hedging):
     - shadowing confirmed to be an intentional personal override (an accepted structural pattern).
     - a do-nothing conclusion, or a finding the scanner already silenced via a references-audit-allow-stale entry.
   SPECIAL = K_unclassified only (escape hatch).

   Rule-level defaults (before taxonomy refines them): hard-dep -> FIX when a mechanical re-point/prefix resolves it, else SERIOUS; soft-ref -> FIX (decidable against the verified skill pool + escape conventions), refined per taxonomy; name-mismatch -> FIX (align frontmatter and directory, after verifying which side inbound refs use); shadowed -> IMPROVE (surface once, opt-in), SILENT when intentional.

   Ambiguity rulings: (1) Your own verified reading (the ref does/does not resolve against the pool; the surrounding prose is/ is not meta-descriptive) DISCHARGES any "confirm with author" hedge -- do not leak a decided FIX back into IMPROVE. (2) A validator/detection artifact that also happens to be a genuine convention fix (e.g. a real broken ref, not just a false positive) is FIX, not SILENT. (3) Dedup/mechanical fixes never wait on a larger structural relocation -- FIX the mechanical part now; the relocation stays a separate IMPROVE.
4. For every FIX finding that is a before/after replacement, compute the EXACT before-text (the current line, verbatim) and the after-text (per the category's default remediation). For instruction-type FIX (fence a command, add an escape prefix, delete an incidental clause) leave before/after empty and put the recipe in \`rationale\`. For SERIOUS put the one-line top-of-report summary in \`rationale\`; for IMPROVE the one-line pitch; for SILENT why it is not surfaced. Leave before/after empty for SERIOUS/IMPROVE/SILENT/SPECIAL.
5. Do NOT reclassify what the taxonomy has already settled; your job is the category match + the disposition + (for FIX) the precise edit, not second-guessing the taxonomy.

Idempotency matters: classify deterministically from the detection signals and the predicates above. Return the structured object (preserve each finding's severity/line/ref).`
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
    if (fnd.bucket === 'FIX') acc.fix++
    else if (fnd.bucket === 'SERIOUS') acc.serious++
    else if (fnd.bucket === 'IMPROVE') acc.improve++
    else if (fnd.bucket === 'SILENT') acc.silent++
    else if (fnd.bucket === 'SPECIAL') acc.special++
    if (fnd.severity === 'ERROR') acc.errors++
  }
  return acc
}, { fix: 0, serious: 0, improve: 0, silent: 0, special: 0, errors: 0 })

log(`Classified findings in ${results.length}/${input.files.length} files — ${totals.errors} ERROR; dispositions SERIOUS=${totals.serious} FIX=${totals.fix} IMPROVE=${totals.improve} SPECIAL=${totals.special} (SILENT=${totals.silent} omitted)`)

return { perFile: results, totals }
