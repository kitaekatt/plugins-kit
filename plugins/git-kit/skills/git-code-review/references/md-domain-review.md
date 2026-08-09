# Subject-lens md-domain contributor

When skills-kit's md-domain skill is available in the session, `git-code-review` treats it
as the SUBJECT-lens reviewer for EVERY changed Markdown file -- `**/*.md`, which is CLAUDE.md,
SKILL.md, a skill's `references/*.md`, and generic project docs alike (`.md.html` Markdeep files
are NOT `.md` and stay with the generic reviewers). Those files are CLAIMED out of the generic
reviewer fan-out (prepare_review.py's `--claim '**/*.md'` flag) and audited
by md-domain's headless per-artifact detect lanes (`workflow/*-detect.js`)
instead; their findings render as a separate labeled section. When md-domain is ABSENT the
mechanism degrades silently -- no `--claim`, no claimed files, the md files get the ordinary thin
data_only coverage. This doc is the operational detail behind step 6 (launch) and step 9 (render);
the SKILL body carries the decision flow.

## Why skill references route to the skill lane

Reproduced 2026-07-28. A changed `plugins/bootstrap/skills/bootstrap/references/engine-internals.md`
was claimed and routed by basename to the project-doc audit lane ("every other `.md`"), whose
criteria explicitly exclude anything inside a skills tree. It declined the file and returned a
passing verdict -- a fake gate. At the time no audit lane read a skill reference's prose, so the
shape was carved out of the claim entirely and returned to the generic reviewers.

That carve-out was a placeholder for the real fix, and the real fix has shipped: the `audit_skill`
lane now owns BOTH of the `skill` artifact's subject shapes -- the SKILL.md contract root AND the
skill's `references/*.md` documents, the latter under skill-standards.md section 10 (inbound anchor
integrity, internal consistency, claim calibration, reader fit, plus the shared ancestor-convention
and back-reference checks). The claim is therefore a single `**/*.md` glob again, and the routing in
"The Workflow calls" below sends a claimed `references/*.md` to `skill-detect.js`, not to the
project-doc lane.

The rule the carve-out encoded still stands in its general form: **never claim a shape no lane can
audit.** A claimed file whose lane declines it returns `NOT-AUDITED`, which is not a pass -- see
"Consuming the result". If a future shape gets claimed ahead of its criteria, that is the same
defect returning, and the fix is the criteria, not a wider claim.

## When it runs

Only when `bundle.claimed_files` is non-empty (i.e. the step-2 probe found md-domain available
AND at least one `.md` file changed). Otherwise skip everything here.

## Triviality gate (skip typo-sized changes)

prepare_review.py attaches a pure-mechanical triviality profile to EACH claimed file:
`trivial` (bool), `trivial_reasons` (disqualifier codes when false -- `too_large`,
`structure_changed`, `reference_changed`, `keyword_changed`, `yaml_touched`, `unparseable`), and,
for a trivial file, `trivial_checks` (`{ascii_clean, no_abs_paths}` over the changed lines). A file
is `trivial` ONLY when it is typo-sized (<= 5 changed lines), its Markdown skeleton is unchanged,
no link/path/anchor reference changed, no negation/modal/quantifier keyword was touched, and no
YAML front-matter or config fence was touched -- computed in `bootstrap_lib.code_review.triviality`
with zero inference. The profile fails CLOSED: an unreadable pre-image or unparseable diff yields
`trivial=false`, so the fallback is always the full audit.

The skill uses this to AVOID auditing mechanical changes: only NON-TRIVIAL claimed files are sent to
the detect lanes below; a trivial file is reported via the SKILL's `## Mechanical checks (audit skipped)`
section and is NEVER audited or written to the ledger. When every claimed file is trivial AND there
are no generic diff chunks, the whole review is skipped. A trivial file is never DIFF-CLEAN and never
an audit result -- it is an honest "checked mechanically, audit skipped" line. An author/user request
for the full review overrides the gate.

## Resolve the skills-kit plugin root and venvPython (defensively)

md-domain's detect lanes are native Workflow scripts; the code-review skill (running in the main
session) invokes them via the Workflow tool. Locate the INSTALLED skills-kit plugin:

- Plugin root (`<root>`): the newest version directory under the plugins cache for this
  marketplace -- `~/.claude/plugins/cache/plugins-kit/skills-kit/<version>/` (pick the highest
  semver dir present). `${CLAUDE_PLUGIN_ROOT}` of the CURRENT skill is NOT it -- that points at
  git-kit / p4-kit, not skills-kit.
- Detect-lane entry points, all under the one md-domain skill:
  `<root>/skills/md-domain/workflow/claude-md-detect.js` (the `audit_claude_md` lane, for CLAUDE.md
  subjects), `<root>/skills/md-domain/workflow/skill-detect.js` (the `audit_skill` lane, for
  SKILL.md subjects AND for a skill's own `references/*.md` documents), and
  `<root>/skills/md-domain/workflow/project-doc-detect.js` (the
  `audit_project_doc` lane, for every OTHER `.md` subject).
- venvPython: skills-kit's provisioned venv, which lives in the version-independent DATA dir --
  `~/.claude/plugins/data/plugins-kit/skills-kit/.venv/Scripts/python.exe` on Windows,
  `~/.claude/plugins/data/plugins-kit/skills-kit/.venv/bin/python` on macOS/Linux.

**Version-coupling safety valve (three-tier fallback).** Do NOT guess when an entry point is
missing, a documented args contract is not what this doc describes, or the installed lane predates
a subject shape this skill claims. Check the tiers in order and take the FIRST that matches:

- **Broad skew** -- `<root>` cannot be located, OR the `claude-md-detect.js` / `skill-detect.js`
  entry point or args contract is missing: emit a one-line warning and RE-RUN prepare_review.py
  WITHOUT any `--claim` flags. All claimed md files return to `changed_files` for generic review,
  and the whole md-domain section is skipped for this run.
- **project-doc-only skew** -- `claude-md-detect.js` and `skill-detect.js` are present but ONLY
  `project-doc-detect.js` is missing (a skills-kit that predates
  project-doc review): emit a one-line warning and RE-RUN prepare_review.py with
  `--claim '**/CLAUDE.md' --claim '**/SKILL.md' --claim '**/skills/*/references/*.md'`. CLAUDE.md,
  SKILL.md and skill references all keep their specialist coverage -- `skill-detect.js` is intact
  in this skew, so both of its subject shapes stay claimed; only the generic `.md` docs rejoin the
  generic review. (Do NOT write the references glob as
  `**/skills/*/references/**/*.md`: `matches_claim` treats a multi-segment tail as an fnmatch over
  the whole path, and that form misses the flat `references/<file>.md` case while the single-`*`
  form matches flat AND nested, repo-relative AND depot paths.)
- **skill-reference skew** -- all three entry points are present, but the installed
  `audit_skill` lane predates the skill-REFERENCE subject shape. Detect it by CAPABILITY, not by
  version number: read `<root>/skills/md-domain/references/standards/skill-standards.md` and look
  for the heading `## 10. Skill reference documents`. If it is ABSENT, that lane declines a
  `references/*.md` and returns NOT-AUDITED. Emit a one-line warning and RE-RUN prepare_review.py
  with `--claim '**/*.md' --claim '!**/skills/*/references/*.md'` -- the retired exclusion, used
  here as a COMPATIBILITY shim -- so skill references rejoin the generic reviewers for this run.
  Everything else keeps its specialist coverage.

  This tier exists because the other two cannot see the skew: an older skills-kit ships
  `skill-detect.js` at the same path with the same args contract, so presence-checking passes while
  the lane still declines the file. These kits declare no version constraint on skills-kit (the
  marketplace uses no version tags), so a capability probe is the only detection available. Without
  it, claiming the shape against an older lane recreates exactly the coverage loss the exclusion was
  introduced for -- the file is taken from the generic reviewers and handed to a lane that reads
  nothing.

These are the only sanctioned second prepare invocations.

## The Workflow calls (three-way by basename, then by path)

At most three, in the SAME message that launches the reviewer fan-out (or the reviewer Workflow).
Route by basename first; the ONE path-shape rule is the skill-reference case in (b):

1. **`audit_claude_md` lane** -- one call for every claimed file whose basename is `CLAUDE.md`.
   `scriptPath = <root>/skills/md-domain/workflow/claude-md-detect.js`, `args` =
   `{ files: [...], review: true, refs: { criteria: <root>/skills/md-domain/references/standards/claude-md-standards.md, codeDirFilter: <root>/skills/md-domain/references/standards/claude-md-standards.md, densityCriteria: <root>/skills/md-domain/references/standards/claude-md-standards.md, pluginRoot: <root>, venvPython: <venvPython> } }` (one standards doc backs all three refs -- the code-directory dimension and the density lens are sections of it).
2. **`audit_skill` lane** -- one call for every claimed file that is EITHER (a) named `SKILL.md`
   OR (b) inside a `*/skills/<name>/references/` folder (only if any). Those are the `skill`
   artifact's two subject shapes and they share one lane and one Workflow call; the lane picks the
   criteria set per file from the path.
   `scriptPath = <root>/skills/md-domain/workflow/skill-detect.js`, `args` =
   `{ files: [...], review: true, refs: { pluginRoot: <root>, venvPython: <venvPython> } }`.
3. **`audit_project_doc` lane** -- one call for every OTHER claimed `.md` file (generic docs; only if any).
   `scriptPath = <root>/skills/md-domain/workflow/project-doc-detect.js`, `args` =
   `{ files: [...], review: true, refs: { criteria: <root>/skills/md-domain/references/standards/project-doc-standards.md, pluginRoot: <root> } }`.

`args` may be passed as an object or a JSON string; all `refs` paths must be ABSOLUTE (the
Workflow runs from the session cwd, not the skill dir). `review: true` forces the model pin and
per-file diff attribution; keep it true.

## Building `files[]` from `bundle.claimed_files`

Build `files[]` from the NON-TRIVIAL claimed files only (per the triviality gate above); trivial
files never reach a detect lane. Each claimed-file entry carries `local` (absolute path), `pre_image` (absolute path to the
materialized before-image via `git show <range-base>:<path>`, or `null` for an add), and `claude_mds` (the
nearest-ancestor-first CLAUDE.md chain, which for a CLAUDE.md subject INCLUDES the subject itself
as its first element).

Derive, per claimed file:

- `ancestorClaudeMdPaths` = `claude_mds` with the subject's OWN `local` removed (drop the
  self-entry a CLAUDE.md subject carries; a SKILL.md subject has nothing to drop). Nearest-ancestor
  first, excluding the subject -- exactly md-domain's H-11 / M ancestor-convention input. Compare paths
  case-INSENSITIVELY on Windows when removing the self-entry: the emitted `local` and the `claude_mds`
  chain are already normalized to agree byte-for-byte, but a case-insensitive compare is the
  belt-and-braces guard against any residual drive-letter casing skew.
- `preImagePath` = the entry's `pre_image` (pass `null` through unchanged -- an add is fully
  attributable).

For a **CLAUDE.md** file (`audit_claude_md` lane `files[]`):
- `path` = `local`.
- `role` = `"child"` when `ancestorClaudeMdPaths` is non-empty, else `"root"` (a standalone file
  with no ancestor CLAUDE.md audits as its natural role). Use `"local"` for a `CLAUDE.local.md`.
- `dimension` = `"classic"` by default; `"code-directory"` only if the file has code/yaml/csv
  siblings and no `claude_md:` block (the heuristic in
  `<root>/skills/md-domain/scripts/discover_claude_md.py`). When unsure, `"classic"`.
- `parentPath` = the FIRST entry of `ancestorClaudeMdPaths` (the nearest ancestor CLAUDE.md), else
  `null`.
- `parentPreImagePath` = if that `parentPath` is ITSELF a claimed file (it changed in this review),
  its `pre_image`; else `null` (judge against the current parent).

For a **SKILL.md** file (`audit_skill` lane `files[]`):
- `path` = `local`.
- `skillType` = omit (let the lane read it from the frontmatter) unless you already know it.
- `ancestorClaudeMdPaths`, `preImagePath` as above. (No `role` / `dimension` / `parentPath` /
  `density` in the `audit_skill` contract.)

For a **skill reference document** (same `audit_skill` lane, same `files[]` array):
- `path` = `local`.
- `ancestorClaudeMdPaths`, `preImagePath` as above. Do NOT pass `skillType` -- a reference declares
  none. You may pass `kind: "skill_reference"`, but the lane derives the same answer from the path,
  so omitting it is fine and is the normal case for a review-mode call.

For a **generic project doc** (any other claimed `.md`; `audit_project_doc` lane `files[]`):
- `path` = `local`.
- `ancestorClaudeMdPaths`, `preImagePath` as above. (No `role` / `dimension` / `parentPath` /
  `kind` / `lines` / `inbound_citations` in the review-mode contract -- the
  `<root>/skills/md-domain/scripts/discover_project_doc.py` signals
  the own-skill path computes are OPTIONAL, and the lane degrades gracefully without them: it
  counts the body itself and skips the orphan check, which needs the citer scan not run here.)

## Consuming the result

Each Workflow returns `{ perFile, totals, review }`. `perFile[i]` carries `verdict`
(`DIFF-CLEAN` = the change introduced no failure; `NON-COMPLIANT`; or `NOT-AUDITED` = the lane
DECLINED the file as outside its criteria and read nothing -- `totals.notAudited` counts these apart
from `totals.diffClean`), and `findings[]` each with
`severity`, `bucket`, `group`, `taxonomy`, `attributable`, `message`, `remediation`. Render these
in the SKILL's step-9 `## md-domain (subject-lens) findings` section -- separate from the
code-review issues, one decision pass over both. Accepted remediations are applied as normal edits
after decisions. See the step-9 action for the ruleset self-reference notice.

A `NOT-AUDITED` file gets its own rendered line saying it was NOT reviewed, naming the auditor its
routing finding points at. Never present it as a result, never count it clean, and never let it
satisfy a submit gate -- the same rule the `## Mechanical checks (audit skipped)` section follows.
On a correctly-configured run it should not appear at all: every claimed shape has a lane that
audits it, so a NOT-AUDITED verdict means the routing sent a file to a
lane that cannot audit it. Report that, rather than accepting the verdict.

Scope: this integration is hardcoded to skills-kit's md-domain skill (its `audit_claude_md`,
`audit_skill` and `audit_project_doc` detect lanes). It targets the md-domain lane contract --
one skill directory, one `workflow/<artifact>-detect.js` per lane; the two-tier defensive probe
above is what keeps a later skills-kit version skew -- or a skills-kit that predates project-doc
review -- from breaking the review.
