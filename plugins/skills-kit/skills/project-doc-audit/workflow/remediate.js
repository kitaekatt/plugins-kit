// project-doc-audit — REMEDIATE workflow (after-Q&A phase).
//
// Fan-out remediation, one lane per project document, applying the decisions the
// main loop gathered during the Q&A gate (interactive) or inferred
// (non-interactive / "fast" intent). Runs AFTER detection + the user decision
// step — never folded into detection (the `audit_then_self_remediate`
// anti-pattern keeps the two phases apart so re-running the audit reproduces the
// same findings).
//
// One lane per FILE (not per finding) so two lanes never edit the same file
// concurrently; within a lane, remediations are applied in order. No worktree
// isolation: lanes touch disjoint files, so they cannot conflict. Some
// remediations are structural MOVES (graduate-to-skill B, fold-into-CLAUDE.md C,
// move-into-skill D) — the lane applies the move it is instructed to make;
// authoring a brand-new skill beyond a simple move is handed to /md-authoring
// skill by the main loop, not performed blind in a lane.
//
// Invoked by the project-doc-audit SKILL.md only when there is remediation work
// spanning 2+ files (the multi-file threshold that equalizes Workflow-tool
// overhead). Single-file remediation runs inline in the main loop.
//
// args = {
//   perFile: [ {
//     path: string,
//     remediations: [ {
//       criterion: string, taxonomy: string, bucket: "FIX"|"SERIOUS"|"IMPROVE"|"SILENT"|"SPECIAL",
//       line: integer|null,
//       instruction: string,          // the concrete edit/move to make
//       decision: "apply"|"skip"|string  // user/inferred decision; free-text = a
//                                          // refined instruction to apply instead
//     } ]
//   } ]
// }

export const meta = {
  name: 'project-doc-audit-remediate',
  description: 'Fan-out project-document remediation: apply the decided edits/moves, one lane per file (after-Q&A phase)',
  phases: [{ title: 'Remediate', detail: 'one lane per project document' }],
}

const FILE_RESULT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    path: { type: 'string' },
    applied: { type: 'integer' },
    skipped: { type: 'integer' },
    failed: { type: 'integer' },
    actions: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          criterion: { type: 'string' },
          status: { type: 'string', enum: ['applied', 'skipped', 'failed'] },
          note: { type: 'string' },
        },
        required: ['criterion', 'status', 'note'],
      },
    },
  },
  required: ['path', 'applied', 'skipped', 'failed', 'actions'],
}

let input = args
if (typeof input === 'string') {
  try { input = JSON.parse(input) } catch (_) { input = null }
}
if (!input || !Array.isArray(input.perFile) || input.perFile.length === 0) {
  throw new Error('remediate.js requires args.perFile = [{path, remediations}]')
}

// Drop files whose every remediation is a skip — nothing to do, no lane needed.
const actionable = input.perFile.filter(
  (f) => Array.isArray(f.remediations) && f.remediations.some((r) => r.decision !== 'skip')
)

function lanePrompt(f) {
  return `You are ONE lane of a project-document remediation pass. Apply the decided edits to exactly one file. Make ONLY the edits listed; do not audit, re-scan, or fix anything not listed here.

Target: ${f.path}

Remediations (apply in order):
${f.remediations.map((r, i) => `${i + 1}. [${r.bucket} / taxonomy ${r.taxonomy} / ${r.criterion}${r.line != null ? ` @ line ${r.line}` : ''}]
   instruction: ${r.instruction}
   decision: ${r.decision}`).join('\n')}

Rules:
- decision "apply"  -> make the edit exactly as the instruction describes.
- decision "skip"   -> do nothing for that item; record status "skipped".
- any other decision text -> treat it as a refined instruction and apply THAT instead of the original.
- Some remediations are structural moves: fold-into-CLAUDE.md (C) appends the content to the named CLAUDE.md and deletes the standalone doc; move-into-skill (D) moves the file into the named skill's references/ folder; collapse-duplication (I) replaces duplicated prose with a pointer to the owning skill. The instruction names the exact destination. Graduate-to-skill (B) beyond a simple file move is NOT done here -- if the instruction asks for new-skill authoring, record status "skipped" with a note that it is routed to /md-authoring skill.
- Use the Read tool to load the file (and any destination file) first, then Edit to make precise changes. Preserve surrounding formatting.
- If an edit cannot be applied safely (anchor not found, ambiguous, destination missing), record status "failed" with a short note rather than guessing.

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
    label: `fix:${f.path.split(/[\\/]/).pop()}`,
    phase: 'Remediate',
    model: 'sonnet',
    effort: 'low',
    schema: FILE_RESULT_SCHEMA,
  }).then((r) => ({ ...r, path: f.path }))
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
log(`Remediation across ${results.filter(Boolean).length} docs — applied ${summary.applied}, skipped ${summary.skipped}, failed ${summary.failed}`)

return { perFile: results.filter(Boolean), summary }
