# Subject-lens md-domain contributor

When skills-kit's md-domain skill is available in the session, `p4-code-review` treats it
as the SUBJECT-lens reviewer for EVERY changed Markdown file -- `**/*.md`, which is CLAUDE.md,
SKILL.md, and generic project docs alike (`.md.html` Markdeep files are NOT `.md` and stay with
the generic reviewers) -- with ONE carve-out: a skill's `references/*.md` is deliberately NOT
claimed. Those files are CLAIMED out of the generic reviewer fan-out
(prepare_review.py's `--claim '**/*.md' --claim '!**/skills/*/references/*.md'` flags) and audited
by md-domain's headless per-artifact detect lanes (`workflow/*-detect.js`)
instead; their findings render as a separate labeled section. When md-domain is ABSENT the
mechanism degrades silently -- no `--claim`, no claimed files, the md files get the ordinary thin
data_only coverage. This doc is the operational detail behind step 6 (launch) and step 9 (render);
the SKILL body carries the decision flow.

## Why skill references are carved out

Reproduced 2026-07-28. A changed `plugins/bootstrap/skills/bootstrap/references/engine-internals.md`
was claimed and routed by basename to the project-doc audit lane ("every other `.md`"), whose
criteria explicitly exclude anything inside a skills tree. It declined the file and returned a
passing verdict. Taking its advice and dispatching the skill audit lane on the owning SKILL.md does
not help either: that lane audits the SKILL.md's contract, schema and load graph, never the
reference's prose.

So **no audit lane reads a skill reference's content** -- and claiming the file removed
the reviewers that would have. An opus generic reviewer given the same diff found five real defects
(a renamed heading that broke six citing files, a self-contradicting paragraph, an overstated claim,
temporal deixis, non-ASCII lines), none reachable by either audit lane.

The `!**/skills/*/references/*.md` exclusion returns that shape to the generic reviewers. Do not
remove it to "simplify the claim" -- doing so restores the fake gate. If a lane ever gains real
skill-reference-prose criteria, drop the exclusion in the SAME change that ships those criteria.

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
  SKILL.md subjects), and `<root>/skills/md-domain/workflow/project-doc-detect.js` (the
  `audit_project_doc` lane, for every OTHER `.md` subject).
- venvPython: skills-kit's provisioned venv, which lives in the version-independent DATA dir --
  `~/.claude/plugins/data/plugins-kit/skills-kit/.venv/Scripts/python.exe` on Windows,
  `~/.claude/plugins/data/plugins-kit/skills-kit/.venv/bin/python` on macOS/Linux.

**Version-coupling safety valve (two-tier fallback).** Do NOT guess when an entry point is
missing or a documented args contract is not what this doc describes:

- **Broad skew** -- `<root>` cannot be located, OR the `claude-md-detect.js` / `skill-detect.js`
  entry point or args contract is missing: emit a one-line warning and RE-RUN prepare_review.py
  WITHOUT any `--claim` flags. All claimed md files return to `changed_files` for generic review,
  and the whole md-domain section is skipped for this run.
- **project-doc-only skew** -- `claude-md-detect.js` and `skill-detect.js` are present but ONLY
  `project-doc-detect.js` is missing (a skills-kit that predates
  project-doc review): emit a one-line warning and RE-RUN prepare_review.py with only
  `--claim '**/CLAUDE.md' --claim '**/SKILL.md'`. CLAUDE.md and SKILL.md keep their specialist
  coverage; only the generic `.md` docs rejoin the generic review.

These are the only sanctioned second prepare invocations.

## The Workflow calls (three-way by basename)

At most three, in the SAME message that launches the reviewer fan-out (or the reviewer Workflow):

1. **`audit_claude_md` lane** -- one call for every claimed file whose basename is `CLAUDE.md`.
   `scriptPath = <root>/skills/md-domain/workflow/claude-md-detect.js`, `args` =
   `{ files: [...], review: true, refs: { criteria: <root>/skills/md-domain/references/standards/claude-md-standards.md, codeDirFilter: <root>/skills/md-domain/references/standards/claude-md-standards.md, densityCriteria: <root>/skills/md-domain/references/standards/claude-md-standards.md, pluginRoot: <root>, venvPython: <venvPython> } }` (one standards doc backs all three refs -- the code-directory dimension and the density lens are sections of it).
2. **`audit_skill` lane** -- one call for every claimed file whose basename is `SKILL.md` (only if any).
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
materialized before-image via `p4 print -q -o <dest> //depot/path#have`, or `null` for an add), and `claude_mds` (the
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
On a correctly-configured run it should not appear at all: the claim carve-out above keeps the one
shape that provoked it out of the claim, so a NOT-AUDITED verdict means the routing sent a file to a
lane that cannot audit it. Report that, rather than accepting the verdict.

Scope: this integration is hardcoded to skills-kit's md-domain skill (its `audit_claude_md`,
`audit_skill` and `audit_project_doc` detect lanes). It targets the md-domain lane contract --
one skill directory, one `workflow/<artifact>-detect.js` per lane; the two-tier defensive probe
above is what keeps a later skills-kit version skew -- or a skills-kit that predates project-doc
review -- from breaking the review.
