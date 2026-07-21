// references-audit — REMEDIATE workflow (after-Q&A phase).
//
// Fan-out remediation, one lane per file, applying the decided reference fixes
// the main loop gathered during the Q&A gate (interactive IMPROVE/SPECIAL) or the
// FIX bucket (auto-applied, no decision needed). Runs AFTER classification + the
// user decision step — never folded into classification (detection and
// remediation stay separate so re-running the scan reproduces the same findings).
//
// One lane per FILE (not per finding) so two lanes never edit the same file
// concurrently; within a lane, edits are applied in order. No worktree isolation:
// lanes touch disjoint files, so they cannot conflict.
//
// Invoked by the references-audit SKILL.md only when remediation work spans 2+
// files (the multi-file threshold that equalizes Workflow-tool overhead). A
// single file's edits are applied inline in the main loop.
//
// args = {
//   perFile: [ {
//     file: string,
//     edits: [ {
//       category: string, bucket: "FIX"|"SERIOUS"|"IMPROVE"|"SILENT"|"SPECIAL",
//       line: integer|null,
//       before: string,                // exact current text (before/after FIX)
//       after: string,                 // replacement text (before/after FIX)
//       instruction: string,           // human-readable edit description (instruction-type FIX / IMPROVE / SPECIAL)
//       decision: "apply"|"skip"|string  // user/inferred decision; free-text = refined instruction
//     } ]
//   } ]
// }

export const meta = {
  name: 'references-audit-remediate',
  description: 'Fan-out reference-fix remediation: apply the decided edits, one lane per file (after-Q&A phase)',
  phases: [{ title: 'Remediate', detail: 'one lane per file' }],
}

const FILE_RESULT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    file: { type: 'string' },
    applied: { type: 'integer' },
    skipped: { type: 'integer' },
    failed: { type: 'integer' },
    actions: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          category: { type: 'string' },
          status: { type: 'string', enum: ['applied', 'skipped', 'failed'] },
          note: { type: 'string' },
        },
        required: ['category', 'status', 'note'],
      },
    },
  },
  required: ['file', 'applied', 'skipped', 'failed', 'actions'],
}

let input = args
if (typeof input === 'string') {
  try { input = JSON.parse(input) } catch (_) { input = null }
}
if (!input || !Array.isArray(input.perFile) || input.perFile.length === 0) {
  throw new Error('remediate.js requires args.perFile = [{file, edits}]')
}

// Drop files whose every edit is a skip — nothing to do, no lane needed.
const actionable = input.perFile.filter(
  (f) => Array.isArray(f.edits) && f.edits.some((e) => e.decision !== 'skip')
)

function lanePrompt(f) {
  return `You are ONE lane of a references remediation pass. Apply the decided reference fixes to exactly one file. Make ONLY the edits listed; do not re-scan or fix anything not listed here.

Target file: ${f.file}

Edits (apply in order):
${f.edits.map((e, i) => `${i + 1}. [${e.bucket} / category ${e.category}${e.line != null ? ` @ line ${e.line}` : ''}]
   ${e.before ? `before: ${JSON.stringify(e.before)}\n   after:  ${JSON.stringify(e.after)}` : `instruction: ${e.instruction}`}
   decision: ${e.decision}`).join('\n')}

Rules:
- decision "apply" + before/after present -> replace the exact before-text with the after-text at the cited line.
- decision "apply" + instruction only -> perform the described edit (e.g. wrap a command in a fenced code block, add a per-file allow-stale frontmatter entry, delete a section).
- decision "skip" -> do nothing for that item; record status "skipped".
- any other decision text -> treat it as a refined instruction and apply THAT instead.
- Use the Read tool to load the file first, then Edit to make precise changes. Preserve surrounding formatting.
- If the before-text no longer matches (the file changed) or the edit is ambiguous, record status "failed" with a short note rather than guessing.

Return a summary: counts of applied/skipped/failed and a per-item action list.`
}

phase('Remediate')
// Default lane tier: sonnet at low effort. Remediation applies already-decided
// edits to disjoint files — the judgment happened at the Q&A gate — so the
// lane needs neither the session's main-loop model nor high reasoning effort.
// (Detect/classify lanes pin opus at high effort — criteria application is
// the audits' judgment core; each lane declares its right tier explicitly
// rather than inheriting whatever the session happens to run.)
const results = await parallel(actionable.map((f) => () =>
  agent(lanePrompt(f), {
    label: `fix:${f.file.split(/[\\/]/).pop()}`,
    phase: 'Remediate',
    model: 'sonnet',
    effort: 'low',
    schema: FILE_RESULT_SCHEMA,
  }).then((r) => ({ ...r, file: f.file }))
))

const summary = results.filter(Boolean).reduce(
  (acc, r) => {
    acc.applied += r.applied
    acc.skipped += r.skipped
    acc.failed += r.failed
    return acc
  },
  { applied: 0, skipped: 0, failed: 0 }
)
log(`Remediation across ${results.filter(Boolean).length} files — applied ${summary.applied}, skipped ${summary.skipped}, failed ${summary.failed}`)

return { perFile: results.filter(Boolean), summary }
