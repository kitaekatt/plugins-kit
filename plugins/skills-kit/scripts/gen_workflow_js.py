"""gen_workflow_js.py -- canonical template for the workflow remediate.js trio.

The three audit skills (claude-md-audit, skill-audit, references-audit) ship a
fan-out remediation workflow script each. The scripts are ~90% identical: the
same args normalization, result schema shape, actionable filter, parallel
runner, and summary reducer -- they differ only in field names, the header
comment, and the lane prompt. That is a copy-paste-with-future-drift shape, so
the shared skeleton lives HERE as the canonical template, the per-skill
differences live here as fragment data, and a drift test
(tests/skills-kit/test_workflow_js_drift.py) asserts the shipped .js files are
byte-identical to the rendered template (the bootstrap_guard vendoring
pattern, applied to generated-not-copied files).

Edit flow: change the template or a fragment below, regenerate, commit both.

Usage:
    uv run python plugins/skills-kit/scripts/gen_workflow_js.py            # rewrite the .js files
    uv run python plugins/skills-kit/scripts/gen_workflow_js.py --check    # exit 1 on drift, write nothing

The detect/classify scripts are NOT fully generated (their bodies diverge more
than the remediate trio), but their shared skeleton chunks (args
normalization; the detect totals reducer) are enforced by check_shared_chunks().
"""

from __future__ import annotations

import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SKILLS = PLUGIN_ROOT / "skills"

EM = "\u2014"  # em-dash; the escape keeps this source file ASCII-only

# ---------------------------------------------------------------------------
# Canonical remediate.js template. Tokens (@...@) are filled per skill.
# ---------------------------------------------------------------------------

REMEDIATE_TEMPLATE = """\
@HEADER@
export const meta = {
  name: '@META_NAME@',
  description: '@META_DESC@',
  phases: [{ title: 'Remediate', detail: '@PHASE_DETAIL@' }],
}

const FILE_RESULT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    @KEY@: { type: 'string' },
    applied: { type: 'integer' },
    skipped: { type: 'integer' },
    failed: { type: 'integer' },
    actions: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        properties: {
          @ACTION_FIELD@: { type: 'string' },
          status: { type: 'string', enum: ['applied', 'skipped', 'failed'] },
          note: { type: 'string' },
        },
        required: ['@ACTION_FIELD@', 'status', 'note'],
      },
    },
  },
  required: ['@KEY@', 'applied', 'skipped', 'failed', 'actions'],
}

let input = args
if (typeof input === 'string') {
  try { input = JSON.parse(input) } catch (_) { input = null }
}
if (!input || !Array.isArray(input.perFile) || input.perFile.length === 0) {
  throw new Error('remediate.js requires args.perFile = [{@ERR_SHAPE@}]')
}

// Drop files whose every @ITEM_NOUN@ is a skip @EM@ nothing to do, no lane needed.
const actionable = input.perFile.filter(
  (f) => Array.isArray(f.@ITEMS@) && f.@ITEMS@.some((@IV@) => @IV@.decision !== 'skip')
)

@LANE_PROMPT_FN@

phase('Remediate')
const results = await parallel(actionable.map((f) => () =>
  agent(lanePrompt(f), {
    label: `fix:${f.@KEY@.split(/[\\\\/]/)@LABEL_TAIL@}`,
    phase: 'Remediate',
    schema: FILE_RESULT_SCHEMA,
  }).then((r) => ({ ...r, @KEY@: f.@KEY@ }))
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
log(`Remediation across ${results.filter(Boolean).length} @LOG_NOUN@ @EM@ applied ${summary.applied}, skipped ${summary.skipped}, failed ${summary.failed}`)

return { perFile: results.filter(Boolean), summary }
"""

# ---------------------------------------------------------------------------
# Per-skill fragments (the data side of the template).
# ---------------------------------------------------------------------------

CLAUDE_MD_HEADER = f"""\
// claude-md-audit {EM} REMEDIATE workflow (after-Q&A phase).
//
// Fan-out remediation, one lane per file, applying the decisions the main loop
// gathered during the Q&A gate (interactive) or inferred (non-interactive /
// "fast" intent). Runs AFTER detection + the user decision step {EM} never folded
// into detection (the `audit_then_self_remediate` anti-pattern keeps the two
// phases apart so re-running the audit reproduces the same findings).
//
// One lane per FILE (not per finding) so two lanes never edit the same file
// concurrently; within a lane, remediations are applied in order. No worktree
// isolation: lanes touch disjoint files, so they cannot conflict.
//
// Invoked by the claude-md-audit SKILL.md only when there is remediation work
// spanning 2+ files (the multi-file threshold that equalizes Workflow-tool
// overhead). Single-file remediation runs inline in the main loop.
//
// args = {{
//   perFile: [ {{
//     path: string,
//     role: string,
//     remediations: [ {{
//       criterion: string, taxonomy: string, bucket: "AUTO"|"DISCUSS"|"SPECIAL",
//       line: integer|null,
//       instruction: string,          // the concrete edit to make
//       decision: "apply"|"skip"|string  // user/inferred decision; free-text = a
//                                          // refined instruction to apply instead
//     }} ]
//   }} ]
// }}
"""

CLAUDE_MD_LANE_PROMPT = """\
function lanePrompt(f) {
  return `You are ONE lane of a CLAUDE.md remediation pass. Apply the decided edits to exactly one file. Make ONLY the edits listed; do not audit, re-scan, or fix anything not listed here.

Target: ${f.path}
Role:   ${f.role}

Remediations (apply in order):
${f.remediations.map((r, i) => `${i + 1}. [${r.bucket} / taxonomy ${r.taxonomy} / ${r.criterion}${r.line != null ? ` @ line ${r.line}` : ''}]
   instruction: ${r.instruction}
   decision: ${r.decision}`).join('\\n')}

Rules:
- decision "apply"  -> make the edit exactly as the instruction describes.
- decision "skip"   -> do nothing for that item; record status "skipped".
- any other decision text -> treat it as a refined instruction and apply THAT instead of the original.
- Use the Read tool to load the file first, then Edit to make precise changes. Preserve surrounding formatting.
- If an edit cannot be applied safely (anchor not found, ambiguous), record status "failed" with a short note rather than guessing.

Return a summary: counts of applied/skipped/failed and a per-item action list.`
}"""

SKILL_AUDIT_HEADER = f"""\
// skill-audit {EM} REMEDIATE workflow (after-Q&A phase).
//
// Fan-out remediation, one lane per SKILL.md, applying the decisions the main
// loop gathered during the Q&A gate (interactive) or inferred (non-interactive).
// Runs AFTER detection + the user decision step {EM} never folded into detection
// (the `audit_then_self_remediate` anti-pattern keeps the two phases apart so
// re-running the audit reproduces the same findings).
//
// One lane per FILE (not per finding) so two lanes never edit the same file
// concurrently; within a lane, remediations are applied in order. No worktree
// isolation: lanes touch disjoint files, so they cannot conflict. Note: some
// remediations also touch the skill's co-located CLAUDE.md (taxonomy I) or a
// references/*.md (taxonomy H) {EM} those still live under the skill's own
// directory, so disjoint-skill lanes remain conflict-free.
//
// Invoked by the skill-audit SKILL.md only when there is remediation work
// spanning 2+ files (the multi-file threshold that equalizes Workflow-tool
// overhead). Single-file remediation runs inline in the main loop.
//
// args = {{
//   perFile: [ {{
//     path: string,
//     remediations: [ {{
//       criterion: string, taxonomy: string, bucket: "AUTO"|"DISCUSS"|"SPECIAL",
//       line: integer|null,
//       instruction: string,          // the concrete edit to make
//       decision: "apply"|"skip"|string  // user/inferred decision; free-text = a
//                                          // refined instruction to apply instead
//     }} ]
//   }} ]
// }}
"""

SKILL_AUDIT_LANE_PROMPT = """\
function lanePrompt(f) {
  return `You are ONE lane of a SKILL.md remediation pass. Apply the decided edits for exactly one skill. Make ONLY the edits listed; do not audit, re-scan, or fix anything not listed here.

Target SKILL.md: ${f.path}

Remediations (apply in order):
${f.remediations.map((r, i) => `${i + 1}. [${r.bucket} / taxonomy ${r.taxonomy} / ${r.criterion}${r.line != null ? ` @ line ${r.line}` : ''}]
   instruction: ${r.instruction}
   decision: ${r.decision}`).join('\\n')}

Rules:
- decision "apply"  -> make the edit exactly as the instruction describes.
- decision "skip"   -> do nothing for that item; record status "skipped".
- any other decision text -> treat it as a refined instruction and apply THAT instead of the original.
- Most edits touch the SKILL.md itself. Two taxonomies edit a sibling instead: taxonomy I (decision-provenance) MOVES the Dec-N lines from the SKILL.md into the skill's co-located CLAUDE.md (create it if absent), leaving only the resulting rule in SKILL.md; taxonomy H (ADP back-reference) edits the cited references/*.md to remove the back-citation. The instruction names the exact target file and lines.
- Use the Read tool to load the target file first, then Edit to make precise changes. Preserve surrounding formatting.
- If an edit cannot be applied safely (anchor not found, ambiguous), record status "failed" with a short note rather than guessing.

Return a summary: counts of applied/skipped/failed and a per-item action list.`
}"""

REFERENCES_HEADER = f"""\
// references-audit {EM} REMEDIATE workflow (after-Q&A phase).
//
// Fan-out remediation, one lane per file, applying the decided reference fixes
// the main loop gathered during the Q&A gate (interactive) or the AUTO bucket
// (no decision needed). Runs AFTER classification + the user decision step {EM}
// never folded into classification (detection and remediation stay separate so
// re-running the scan reproduces the same findings).
//
// One lane per FILE (not per finding) so two lanes never edit the same file
// concurrently; within a lane, edits are applied in order. No worktree isolation:
// lanes touch disjoint files, so they cannot conflict.
//
// Invoked by the references-audit SKILL.md only when remediation work spans 2+
// files (the multi-file threshold that equalizes Workflow-tool overhead). A
// single file's edits are applied inline in the main loop.
//
// args = {{
//   perFile: [ {{
//     file: string,
//     edits: [ {{
//       category: string, bucket: "AUTO"|"DISCUSS"|"SPECIAL",
//       line: integer|null,
//       before: string,                // exact current text (AUTO)
//       after: string,                 // replacement text (AUTO)
//       instruction: string,           // human-readable edit description (DISCUSS/SPECIAL)
//       decision: "apply"|"skip"|string  // user/inferred decision; free-text = refined instruction
//     }} ]
//   }} ]
// }}
"""

REFERENCES_LANE_PROMPT = """\
function lanePrompt(f) {
  return `You are ONE lane of a references remediation pass. Apply the decided reference fixes to exactly one file. Make ONLY the edits listed; do not re-scan or fix anything not listed here.

Target file: ${f.file}

Edits (apply in order):
${f.edits.map((e, i) => `${i + 1}. [${e.bucket} / category ${e.category}${e.line != null ? ` @ line ${e.line}` : ''}]
   ${e.before ? `before: ${JSON.stringify(e.before)}\\n   after:  ${JSON.stringify(e.after)}` : `instruction: ${e.instruction}`}
   decision: ${e.decision}`).join('\\n')}

Rules:
- decision "apply" + before/after present -> replace the exact before-text with the after-text at the cited line.
- decision "apply" + instruction only -> perform the described edit (e.g. wrap a command in a fenced code block, add a per-file allow-stale frontmatter entry, delete a section).
- decision "skip" -> do nothing for that item; record status "skipped".
- any other decision text -> treat it as a refined instruction and apply THAT instead.
- Use the Read tool to load the file first, then Edit to make precise changes. Preserve surrounding formatting.
- If the before-text no longer matches (the file changed) or the edit is ambiguous, record status "failed" with a short note rather than guessing.

Return a summary: counts of applied/skipped/failed and a per-item action list.`
}"""

PROJECT_DOC_HEADER = f"""\
// project-doc-audit {EM} REMEDIATE workflow (after-Q&A phase).
//
// Fan-out remediation, one lane per project document, applying the decisions the
// main loop gathered during the Q&A gate (interactive) or inferred
// (non-interactive / "fast" intent). Runs AFTER detection + the user decision
// step {EM} never folded into detection (the `audit_then_self_remediate`
// anti-pattern keeps the two phases apart so re-running the audit reproduces the
// same findings).
//
// One lane per FILE (not per finding) so two lanes never edit the same file
// concurrently; within a lane, remediations are applied in order. No worktree
// isolation: lanes touch disjoint files, so they cannot conflict. Some
// remediations are structural MOVES (graduate-to-skill B, fold-into-CLAUDE.md C,
// move-into-skill D) {EM} the lane applies the move it is instructed to make;
// authoring a brand-new skill beyond a simple move is handed to /md-authoring
// skill by the main loop, not performed blind in a lane.
//
// Invoked by the project-doc-audit SKILL.md only when there is remediation work
// spanning 2+ files (the multi-file threshold that equalizes Workflow-tool
// overhead). Single-file remediation runs inline in the main loop.
//
// args = {{
//   perFile: [ {{
//     path: string,
//     remediations: [ {{
//       criterion: string, taxonomy: string, bucket: "AUTO"|"DISCUSS"|"SPECIAL",
//       line: integer|null,
//       instruction: string,          // the concrete edit/move to make
//       decision: "apply"|"skip"|string  // user/inferred decision; free-text = a
//                                          // refined instruction to apply instead
//     }} ]
//   }} ]
// }}
"""

PROJECT_DOC_LANE_PROMPT = """\
function lanePrompt(f) {
  return `You are ONE lane of a project-document remediation pass. Apply the decided edits to exactly one file. Make ONLY the edits listed; do not audit, re-scan, or fix anything not listed here.

Target: ${f.path}

Remediations (apply in order):
${f.remediations.map((r, i) => `${i + 1}. [${r.bucket} / taxonomy ${r.taxonomy} / ${r.criterion}${r.line != null ? ` @ line ${r.line}` : ''}]
   instruction: ${r.instruction}
   decision: ${r.decision}`).join('\\n')}

Rules:
- decision "apply"  -> make the edit exactly as the instruction describes.
- decision "skip"   -> do nothing for that item; record status "skipped".
- any other decision text -> treat it as a refined instruction and apply THAT instead of the original.
- Some remediations are structural moves: fold-into-CLAUDE.md (C) appends the content to the named CLAUDE.md and deletes the standalone doc; move-into-skill (D) moves the file into the named skill's references/ folder; collapse-duplication (I) replaces duplicated prose with a pointer to the owning skill. The instruction names the exact destination. Graduate-to-skill (B) beyond a simple file move is NOT done here -- if the instruction asks for new-skill authoring, record status "skipped" with a note that it is routed to /md-authoring skill.
- Use the Read tool to load the file (and any destination file) first, then Edit to make precise changes. Preserve surrounding formatting.
- If an edit cannot be applied safely (anchor not found, ambiguous, destination missing), record status "failed" with a short note rather than guessing.

Return a summary: counts of applied/skipped/failed and a per-item action list.`
}"""

REMEDIATE_FRAGMENTS = {
    "claude-md-audit": {
        "HEADER": CLAUDE_MD_HEADER,
        "META_NAME": "claude-md-audit-remediate",
        "META_DESC": "Fan-out CLAUDE.md remediation: apply the decided edits, one lane per file (after-Q&A phase)",
        "PHASE_DETAIL": "one lane per file",
        "KEY": "path",
        "ACTION_FIELD": "criterion",
        "ERR_SHAPE": "path, role, remediations",
        "ITEM_NOUN": "remediation",
        "ITEMS": "remediations",
        "IV": "r",
        "LANE_PROMPT_FN": CLAUDE_MD_LANE_PROMPT,
        "LABEL_TAIL": ".pop()",
        "LOG_NOUN": "files",
    },
    "skill-audit": {
        "HEADER": SKILL_AUDIT_HEADER,
        "META_NAME": "skill-audit-remediate",
        "META_DESC": "Fan-out SKILL.md remediation: apply the decided edits, one lane per file (after-Q&A phase)",
        "PHASE_DETAIL": "one lane per SKILL.md",
        "KEY": "path",
        "ACTION_FIELD": "criterion",
        "ERR_SHAPE": "path, remediations",
        "ITEM_NOUN": "remediation",
        "ITEMS": "remediations",
        "IV": "r",
        "LANE_PROMPT_FN": SKILL_AUDIT_LANE_PROMPT,
        "LABEL_TAIL": ".slice(-2).join('/')",
        "LOG_NOUN": "skills",
    },
    "references-audit": {
        "HEADER": REFERENCES_HEADER,
        "META_NAME": "references-audit-remediate",
        "META_DESC": "Fan-out reference-fix remediation: apply the decided edits, one lane per file (after-Q&A phase)",
        "PHASE_DETAIL": "one lane per file",
        "KEY": "file",
        "ACTION_FIELD": "category",
        "ERR_SHAPE": "file, edits",
        "ITEM_NOUN": "edit",
        "ITEMS": "edits",
        "IV": "e",
        "LANE_PROMPT_FN": REFERENCES_LANE_PROMPT,
        "LABEL_TAIL": ".pop()",
        "LOG_NOUN": "files",
    },
    "project-doc-audit": {
        "HEADER": PROJECT_DOC_HEADER,
        "META_NAME": "project-doc-audit-remediate",
        "META_DESC": "Fan-out project-document remediation: apply the decided edits/moves, one lane per file (after-Q&A phase)",
        "PHASE_DETAIL": "one lane per project document",
        "KEY": "path",
        "ACTION_FIELD": "criterion",
        "ERR_SHAPE": "path, remediations",
        "ITEM_NOUN": "remediation",
        "ITEMS": "remediations",
        "IV": "r",
        "LANE_PROMPT_FN": PROJECT_DOC_LANE_PROMPT,
        "LABEL_TAIL": ".pop()",
        "LOG_NOUN": "docs",
    },
}


def render_remediate(skill: str) -> str:
    frags = REMEDIATE_FRAGMENTS[skill]
    out = REMEDIATE_TEMPLATE
    out = out.replace("@EM@", EM)
    out = out.replace("@HEADER@", frags["HEADER"])
    for token in ("META_NAME", "META_DESC", "PHASE_DETAIL", "KEY", "ACTION_FIELD",
                  "ERR_SHAPE", "ITEM_NOUN", "ITEMS", "IV", "LANE_PROMPT_FN",
                  "LABEL_TAIL", "LOG_NOUN"):
        out = out.replace(f"@{token}@", frags[token])
    return out


def remediate_targets() -> dict[str, Path]:
    return {
        skill: SKILLS / skill / "workflow" / "remediate.js"
        for skill in REMEDIATE_FRAGMENTS
    }


# ---------------------------------------------------------------------------
# Shared skeleton chunks for the detect/classify scripts (not fully generated;
# the chunks below must appear verbatim, so the copy-paste skeleton cannot
# drift silently).
# ---------------------------------------------------------------------------

ARGS_NORM_CHUNK = """\
let input = args
if (typeof input === 'string') {
  try { input = JSON.parse(input) } catch (_) { input = null }
}
if (!input || !Array.isArray(input.files) || input.files.length === 0) {
"""

DETECT_TOTALS_CHUNK = """\
const results = perFile.filter(Boolean)
const totals = results.reduce((acc, r) => {
  for (const fnd of r.findings) {
    if (fnd.bucket === 'AUTO') acc.auto++
    else if (fnd.bucket === 'DISCUSS') acc.discuss++
    else if (fnd.bucket === 'SPECIAL') acc.special++
    if (fnd.severity === 'FAIL') acc.fail++
  }
  if (r.verdict === 'NON-COMPLIANT') acc.nonCompliant++
  return acc
}, { auto: 0, discuss: 0, special: 0, fail: 0, nonCompliant: 0 })
"""

SHARED_CHUNK_TARGETS = {
    SKILLS / "claude-md-audit" / "workflow" / "detect.js": [ARGS_NORM_CHUNK, DETECT_TOTALS_CHUNK],
    SKILLS / "skill-audit" / "workflow" / "detect.js": [ARGS_NORM_CHUNK, DETECT_TOTALS_CHUNK],
    SKILLS / "project-doc-audit" / "workflow" / "detect.js": [ARGS_NORM_CHUNK, DETECT_TOTALS_CHUNK],
    SKILLS / "references-audit" / "workflow" / "classify.js": [ARGS_NORM_CHUNK],
}


def check_shared_chunks() -> list[str]:
    """Return drift messages for detect/classify shared-skeleton chunks."""
    problems: list[str] = []
    for path, chunks in SHARED_CHUNK_TARGETS.items():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            problems.append(f"{path}: unreadable ({e})")
            continue
        for chunk in chunks:
            if chunk not in text:
                first_line = chunk.splitlines()[0]
                problems.append(
                    f"{path}: shared skeleton chunk starting '{first_line}' "
                    "not found verbatim"
                )
    return problems


def check_remediate() -> list[str]:
    problems: list[str] = []
    for skill, path in remediate_targets().items():
        rendered = render_remediate(skill)
        try:
            on_disk = path.read_text(encoding="utf-8")
        except OSError as e:
            problems.append(f"{path}: unreadable ({e})")
            continue
        if on_disk != rendered:
            problems.append(
                f"{path}: drifted from the canonical template "
                "(edit gen_workflow_js.py and regenerate, or revert the .js edit)"
            )
    return problems


def main(argv: list[str]) -> int:
    check_only = "--check" in argv
    if check_only:
        problems = check_remediate() + check_shared_chunks()
        for p in problems:
            print(p, file=sys.stderr)
        print(f"workflow-js drift check: {len(problems)} problem(s)")
        return 1 if problems else 0

    for skill, path in remediate_targets().items():
        # newline="\n" forces LF regardless of platform -- without it, Python's
        # text-mode write translates \n to \r\n on Windows, leaving the generated
        # .js files perpetually "modified" (CRLF) against the LF-committed blobs.
        path.write_text(render_remediate(skill), encoding="utf-8", newline="\n")
        print(f"wrote {path}")
    problems = check_shared_chunks()
    for p in problems:
        print(p, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
