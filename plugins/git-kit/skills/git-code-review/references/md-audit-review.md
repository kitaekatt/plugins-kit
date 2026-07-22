# Subject-lens md-audit contributor

When skills-kit's md-audit skill is available in the session, `git-code-review` treats it
as the SUBJECT-lens reviewer for the changed `**/CLAUDE.md` and `**/SKILL.md` files. Those
files are CLAIMED out of the generic reviewer fan-out (prepare_review.py's `--claim` flag)
and audited by skills-kit's headless `detect.js` Workflow instead; its findings render as a
separate labeled section. When md-audit is ABSENT the mechanism degrades silently -- no
`--claim`, no claimed files, the md files get the ordinary thin data_only coverage. This doc
is the operational detail behind step 6 (launch) and step 9 (render); the SKILL body carries
the decision flow.

## When it runs

Only when `bundle.claimed_files` is non-empty (i.e. the step-2 probe found md-audit available
AND at least one CLAUDE.md/SKILL.md changed). Otherwise skip everything here.

## Resolve the skills-kit plugin root and venvPython (defensively)

md-audit's `detect.js` is a native Workflow script; the code-review skill (running in the main
session) invokes it via the Workflow tool. Locate the INSTALLED skills-kit plugin:

- Plugin root (`<root>`): the newest version directory under the plugins cache for this
  marketplace -- `~/.claude/plugins/cache/plugins-kit/skills-kit/<version>/` (pick the highest
  semver dir present). `${CLAUDE_PLUGIN_ROOT}` of the CURRENT skill is NOT it -- that points at
  git-kit / p4-kit, not skills-kit.
- detect.js entry points: `<root>/skills/claude-md-audit/workflow/detect.js` (for CLAUDE.md
  subjects) and `<root>/skills/skill-audit/workflow/detect.js` (for SKILL.md subjects).
- venvPython: skills-kit's provisioned venv, which lives in the version-independent DATA dir --
  `~/.claude/plugins/data/plugins-kit/skills-kit/.venv/Scripts/python.exe` on Windows,
  `~/.claude/plugins/data/plugins-kit/skills-kit/.venv/bin/python` on macOS/Linux.

**Version-coupling safety valve (the fallback).** If `<root>` cannot be located, either detect.js
entry point is missing, or its documented args contract is not what this doc describes (a
skills-kit major/contract skew), do NOT guess: emit a one-line warning and RE-RUN
prepare_review.py WITHOUT any `--claim` flags. That returns the md files to `changed_files` so
they get generic review, and the whole md-audit section is skipped for this run. This is the only
sanctioned second prepare invocation.

## The two Workflow calls

At most two, in the SAME message that launches the reviewer fan-out (or the reviewer Workflow):

1. **claude-md-audit** -- one call for every claimed file whose basename is `CLAUDE.md`.
   `scriptPath = <root>/skills/claude-md-audit/workflow/detect.js`, `args` =
   `{ files: [...], review: true, refs: { criteria: <root>/skills/claude-md-audit/references/audit-criteria.md, codeDirFilter: <root>/skills/claude-md-audit/references/code-dir-insight-filter.md, densityCriteria: <root>/skills/claude-md-audit/references/density-criteria.md, pluginRoot: <root>, venvPython: <venvPython> } }`.
2. **skill-audit** -- one call for every claimed file whose basename is `SKILL.md` (only if any).
   `scriptPath = <root>/skills/skill-audit/workflow/detect.js`, `args` =
   `{ files: [...], review: true, refs: { pluginRoot: <root>, venvPython: <venvPython> } }`.

`args` may be passed as an object or a JSON string; all `refs` paths must be ABSOLUTE (the
Workflow runs from the session cwd, not the skill dir). `review: true` forces the model pin and
per-file diff attribution; keep it true.

## Building `files[]` from `bundle.claimed_files`

Each claimed-file entry carries `local` (absolute path), `pre_image` (absolute path to the
materialized before-image via `git show <range-base>:<path>`, or `null` for an add), and `claude_mds` (the
nearest-ancestor-first CLAUDE.md chain, which for a CLAUDE.md subject INCLUDES the subject itself
as its first element).

Derive, per claimed file:

- `ancestorClaudeMdPaths` = `claude_mds` with the subject's OWN `local` removed (drop the
  self-entry a CLAUDE.md subject carries; a SKILL.md subject has nothing to drop). Nearest-ancestor
  first, excluding the subject -- exactly md-audit's H-11 / M ancestor-convention input. Compare paths
  case-INSENSITIVELY on Windows when removing the self-entry: the emitted `local` and the `claude_mds`
  chain are already normalized to agree byte-for-byte, but a case-insensitive compare is the
  belt-and-braces guard against any residual drive-letter casing skew.
- `preImagePath` = the entry's `pre_image` (pass `null` through unchanged -- an add is fully
  attributable).

For a **CLAUDE.md** file (claude-md-audit `files[]`):
- `path` = `local`.
- `role` = `"child"` when `ancestorClaudeMdPaths` is non-empty, else `"root"` (a standalone file
  with no ancestor CLAUDE.md audits as its natural role). Use `"local"` for a `CLAUDE.local.md`.
- `dimension` = `"classic"` by default; `"code-directory"` only if the file has code/yaml/csv
  siblings and no `claude_md:` block (skills-kit's discover.py heuristic). When unsure, `"classic"`.
- `parentPath` = the FIRST entry of `ancestorClaudeMdPaths` (the nearest ancestor CLAUDE.md), else
  `null`.
- `parentPreImagePath` = if that `parentPath` is ITSELF a claimed file (it changed in this review),
  its `pre_image`; else `null` (judge against the current parent).

For a **SKILL.md** file (skill-audit `files[]`):
- `path` = `local`.
- `skillType` = omit (let detect.js read it from the frontmatter) unless you already know it.
- `ancestorClaudeMdPaths`, `preImagePath` as above. (No `role` / `dimension` / `parentPath` /
  `density` in the skill-audit contract.)

## Consuming the result

Each Workflow returns `{ perFile, totals, review }`. `perFile[i]` carries `verdict`
(`DIFF-CLEAN` = the change introduced no failure, or `NON-COMPLIANT`), and `findings[]` each with
`severity`, `bucket`, `group`, `taxonomy`, `attributable`, `message`, `remediation`. Render these
in the SKILL's step-9 `## md-audit (subject-lens) findings` section -- separate from the
code-review issues, one decision pass over both. Accepted remediations are applied as normal edits
after decisions. See the step-9 action for the ruleset self-reference notice.

Scope: this integration is v1 and hardcoded to skills-kit's md-audit (claude-md-audit + skill-audit
members). It targets skills-kit's 0.30.0 contract; the defensive probe above is what keeps a later
skills-kit version skew from breaking the review.
