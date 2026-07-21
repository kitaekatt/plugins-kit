"""gen_code_review_skills.py -- single source for the two code-review skills.

git-kit:git-code-review and p4-kit:p4-code-review run the SAME multi-agent
review pipeline (identical review_profiles, subagents, false_positive_guardrails,
agent_assumptions, issue_format, submit_gates.rendering, narration note). Only
the VCS front-half differs: target identity (git range-auto-detect vs p4 CL),
fold-in mechanics (git add/commit vs p4 reconcile), the unresolved-work wording
(merge conflicts vs pending resolves), a p4-only step 10 (auto-shelf cleanup)
plus its python3 launch gotcha, and the output header line.

Historically the shared back-half drifted by accident -- a fix landed in one
kit's SKILL.md and never reached the other (findings G6/G7 of the 2026-06-09
architecture review). This generator makes that structurally impossible: ONE
template + a per-VCS substitution table renders BOTH SKILL.md files (and BOTH
references/submit-gates.md files). The rendered files are committed (skills must
stay readable on disk); a drift guard (--check) asserts the committed output is
byte-identical to what the template renders -- the same enforcement idea as
plugins/skills-kit/scripts/gen_workflow_js.py.

This tool spans two plugins, so it lives in the repo-level scripts/ dir next to
the other cross-plugin tooling (regen_marketplace.py, publish.py, dev-tree.py),
NOT inside either plugin. It writes files under both plugins; it does not move
content across the plugin boundary (the rendered files stay in their own
plugins), so the "plugin boundaries are hard boundaries" rule is respected --
this is shared tooling, not a relocated skill.

Edit flow: change the template or a fragment below, regenerate, commit all four
rendered files together.

Usage:
    uv run python scripts/gen_code_review_skills.py            # rewrite the 4 files
    uv run python scripts/gen_code_review_skills.py --check    # exit 1 on drift, write nothing

The drift guard is wired into the test suite at
tests/bootstrap/code_review/test_skill_drift.py (byte-identity of every rendered
target), mirroring tests/skills-kit/test_workflow_js_drift.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GIT_SKILL = REPO_ROOT / "plugins/git-kit/skills/git-code-review/SKILL.md"
P4_SKILL = REPO_ROOT / "plugins/p4-kit/skills/p4-code-review/SKILL.md"
GIT_SUBMIT_GATES = REPO_ROOT / "plugins/git-kit/skills/git-code-review/references/submit-gates.md"
P4_SUBMIT_GATES = REPO_ROOT / "plugins/p4-kit/skills/p4-code-review/references/submit-gates.md"
GIT_MD_AUDIT_REVIEW = REPO_ROOT / "plugins/git-kit/skills/git-code-review/references/md-audit-review.md"
P4_MD_AUDIT_REVIEW = REPO_ROOT / "plugins/p4-kit/skills/p4-code-review/references/md-audit-review.md"

# Non-ASCII glyphs the rendered files use, escaped so THIS source stays ASCII
# (matching gen_workflow_js.py's EM convention).
X = "×"   # multiplication sign, in "reviewer x chunk" / "R x K"
DOT = "·"  # middle dot, in git's "Branch: ...  .  HEAD: ..." header
CHK = "✓"  # check mark, submit-gate confirmed
CRS = "✗"  # ballot X, submit-gate unconfirmed


# ===========================================================================
# DISPATCH RULE (deliverable 2 -- behaviour change, shared by BOTH skills).
# ---------------------------------------------------------------------------
# The fan-out is R reviewers x K chunks = "lanes". R is bounded: 2 (data_only)
# or 3 (code). K is 1 for any diff/CL under the 1 MB chunk cap -- the common
# case -- and grows only for large multi-file changes. So the realistic lane
# counts are:
#     K=1 -> 2 or 3 lanes      K=2 -> 4 or 6 lanes
#     K=3 -> 6 or 9 lanes      K=4 -> 8 or 12 lanes
# Threshold N = 6: at or below 6 lanes (the code profile across up to 2 chunks,
# or data_only across up to 3) a single-message parallel fan-out stays legible
# and inside practical concurrent-agent limits, so launching the subagents
# directly is cheaper than standing up a Workflow. Above 6 (the code profile on
# 3+ chunks) the managed fan-out/reduce the Workflow tool provides earns its
# overhead. The rule is stated as a computed inequality (lanes <= 6), not a
# vibe, so the model cannot reinterpret it.
# ===========================================================================
DISPATCH = """\
            Dispatch rule (deterministic -- compute the number, do not eyeball it): let
            lanes = R x K, where R = len(profile.reviewers) (2 for data_only, 3 for code)
            and K = len(bundle.diff_chunks). If lanes <= 6, launch the reviewer subagents
            DIRECTLY as parallel background Agent calls in a single message (the default
            path, steps 6-7 as written below). If lanes > 6, hand the reviewer fan-out and
            the validator wave to the Workflow tool instead of launching inline. Same
            reviewers, same validators, same output either way -- only the dispatch
            mechanism changes."""


# ===========================================================================
# SUBJECT-LENS md-audit CONTRIBUTOR (deliverable of this phase, shared).
# ---------------------------------------------------------------------------
# When skills-kit's md-audit skill is available, the code-review skills hand it
# the changed CLAUDE.md / SKILL.md files as a SUBJECT-lens reviewer: those files
# are claimed out of the generic fan-out (via prepare's `--claim`) and audited
# by skills-kit's headless detect.js Workflow, whose findings render as their own
# labeled section. When md-audit is absent, behavior is exactly today's -- the md
# files get thin generic data_only coverage. All three regions below are SHARED
# verbatim by both VCS skills. The heavy args/plugin-root/fallback detail lives in
# the generated references/md-audit-review.md so the step prose stays legible.
# ===========================================================================

# Injected into step 2's action (the prepare invocation) via the STEP2 fragments.
# Uses a plain-text sentinel (__CLAIM_PROBE__) substituted at module-def time so
# it never collides with the @TOKEN@ render pass.
CLAIM_PROBE = """\
            Claim probe -- decide the `--claim` flags BEFORE invoking prepare, and invoke prepare
            only ONCE. Check whether skills-kit's md-audit skill is available in this session (it
            appears in the available-skills list as `skills-kit:md-audit`). If it IS available, add
            `--claim '**/CLAUDE.md' --claim '**/SKILL.md'` to the prepare invocation below so those
            files are held back from the generic reviewers and returned under `bundle.claimed_files`
            (each with a materialized `pre_image`) for the subject-lens md-audit pass in step 6. If it
            is NOT available, invoke prepare with NO `--claim` flags -- degrade silently to today's
            behavior (the md files get thin generic data_only coverage), noting the degradation in one
            line. Do NOT run prepare twice."""

# Inserted into step 6's action, right after the dispatch rule.
MD_AUDIT_LAUNCH = """\
            Subject-lens md-audit pass -- run ONLY when `bundle.claimed_files` is non-empty; skip this
            entire paragraph otherwise. In the SAME message that launches the reviewer subagents (or the
            reviewer Workflow, per the dispatch rule above), ALSO invoke the Workflow tool with
            skills-kit's headless detect.js for the claimed files: one Workflow call for the claimed
            `**/CLAUDE.md` files (claude-md-audit's `workflow/detect.js`) and, only if any `**/SKILL.md`
            files are claimed, a second Workflow call for those (skill-audit's `workflow/detect.js`) --
            at most two Workflow calls. Pass `review: true` and, per claimed file, `preImagePath` = its
            `pre_image` from the bundle (null for an add), with role / dimension / parentPath /
            ancestorClaudeMdPaths resolved from each claimed file's `claude_mds` per
            references/md-audit-review.md. Resolve the skills-kit plugin root and venvPython defensively
            per that reference; if detect.js or the documented args contract is not found where expected
            (version skew), FALL BACK cleanly: emit a one-line warning and re-run prepare_review.py
            WITHOUT any `--claim` flags so the claimed files rejoin the generic review, then proceed with
            the normal fan-out. This probe-and-fallback is the version-coupling safety valve. When it
            runs, the md-audit Workflow executes in PARALLEL with the reviewer fan-out; keep its
            `{perFile, totals, review}` for step 9's labeled section."""

# Inserted into step 9's action, right after the unresolved-work section.
MD_AUDIT_REPORT = """\
            - When the md-audit subject-lens pass ran (bundle.claimed_files was non-empty and the
              Workflow did NOT fall back), render its results as a distinct, clearly LABELED section
              titled `## md-audit (subject-lens) findings`, kept SEPARATE from the code-review issue
              list -- never merge the two. For each file in the md-audit `perFile` result, show its
              verdict (DIFF-CLEAN or NON-COMPLIANT) and, beneath it, each finding's severity, bucket,
              attributable flag, message, and remediation proposal. A SINGLE decision pass covers BOTH
              this section and the code-review issues; accepted md-audit remediations are applied as
              normal edits AFTER decisions. If the md-audit pass fell back to the generic review, do NOT
              render this section (the md files were reviewed as ordinary subjects).
            - Ruleset self-reference notice: if any claimed CLAUDE.md with a pending or accepted md-audit
              change lies on the ancestor chain of OTHER changed files in this review -- a cheap
              path-prefix check of that CLAUDE.md's directory against bundle.unique_claude_mds and the
              other changed files' paths -- print a one-line notice: "ruleset changed -- findings for
              <files> were judged against the working-tree version; consider a re-run." Keep it to one
              line; it is advisory, not a blocker."""

# Appended to both gotcha blocks (plain text -- no f-string braces).
MD_AUDIT_GOTCHAS = """
        - md-audit findings are a SEPARATE, labeled section -- never interleave them with the code-review issue list. They come from skills-kit's detect.js (a subject-lens reviewer), not from the generic reviewer/validator subagents, so they are not filtered by the validators.
        - The claim decision happens ONCE, at the step-2 probe, and controls whether prepare gets `--claim`. Do not run prepare a second time just to add claims -- the only re-run is the version-skew FALLBACK, which re-runs WITHOUT `--claim`.
        - When skills-kit md-audit is absent the whole mechanism degrades silently: no `--claim`, no claimed_files, no md-audit section -- the md files get today's thin generic data_only coverage. Note the degradation in one line; do not treat it as an error.
        - The Workflow tool is unavailable inside subagents. Launch the md-audit detect.js Workflow from the MAIN session (the same message that fans out the reviewers), never from within a reviewer subagent."""


# ===========================================================================
# The canonical SKILL.md template. Shared prose is inline (one copy); tokens
# (@NAME@ ...) carry the genuinely per-VCS regions, filled from FRAGMENTS.
# ===========================================================================
SKILL_TEMPLATE = """\
---
_schema_version: 1
name: @NAME@
author: christina
skill-type: technique-skill
description: @DESC@
---

# @TITLE@

@INTRO@

```yaml
technique_skill:
  _schema_version: "1"
  trigger_model: auto
  identity: @IDENTITY@
  scope:
    covers:
@SCOPE_COVERS_HEAD@
      - bug audits scoped to introduced code
      - surfacing path-scoped pre-submit reminders (submit gates) from CLAUDE.md
    excludes:
@SCOPE_EXCLUDES@
  techniques:
    - id: full_review
      name: Full multi-agent review
      keywords: [@KEYWORDS@]
      goal: @GOAL@
      preconditions:
@PRECONDITIONS@
      steps:
@STEP1@
@STEP2@
@STEP3@
        - n: 4
          action: Read every CLAUDE.md path in unique_claude_mds. Subagents do not need to re-read.
          tool: Read
        - n: 5
          action: |
            If bundle.submit_gates is non-empty, surface each gate as a checklist item the
            author must confirm BEFORE the review renders. Issue ONE AskUserQuestion call
            with `multiSelect: true`, one option per gate, labeled with the gate's summary
            and (in the description) the source CLAUDE.md path and the triggering files.
@STEP5_PHRASE@
            - Selected options become CONFIRMED gates.
            - Unselected options become UNCONFIRMED gates -- still rendered, just marked.
            Do NOT skip a gate, do NOT collapse multiple gates into one option, do NOT
            re-prompt. The author's answer (or lack thereof) is final.
            Skip this step entirely if bundle.submit_gates is empty.
          tool: AskUserQuestion
        - n: 6
          action: |
            Select one profile from `review_profiles` using its `selection.guidance` -- this is
            an inference call, not regex. Read each profile's guidance, weigh the actual contents
            of `bundle.changed_files`, and pick the most appropriate profile. Default to `code`
            when uncertain.
@DISPATCH@
@MD_AUDIT_LAUNCH@
            Then launch one subagent per (reviewer @X@ chunk) pair in parallel via
            a single message with R @X@ K Agent calls, where R = len(profile.reviewers) and
            K = len(bundle.diff_chunks). Each subagent gets the chunk's absolute diff path
            (`<bundle.bundle_dir>/<diff_chunks[i].path>`), the @FILEPATHS@ of the files
            in that chunk (`diff_chunks[i].files`), and -- for reviewer_a -- the CLAUDE.md
            mapping restricted to those files. Reviewers not listed in the selected profile are
            NOT launched. If bundle.diff_chunks is empty (@RANGE_OR_CL@ has no diff content), skip
            step 6 and jump to step 9 with zero issues.
          tool: Agent
          expected: JSON arrays of candidate issues from each launched reviewer (one array per (reviewer, chunk) subagent).
        - n: 7
          action: |
            Launch one validator subagent per candidate issue, all in parallel via a single message.
            Use the selected profile's `validator_models[reason]` to pick the model per issue.
          tool: Agent
          expected: CONFIRMED or REJECTED per issue.
        - n: 8
          action: Drop rejected issues silently (do not report rejected issues to the user).
        - n: 9
          action: |
            Render the markdown review.
            - When `bundle.submit_gates` is non-empty, prepend a `## Submit checklist`
              section (confirmed and unconfirmed gates both rendered).
@STEP9_TAIL@
@MD_AUDIT_REPORT@
            Group the review body by file.
@STEP10@      checklist:
@CHECKLIST@
      gotchas:
@GOTCHAS@
  narration:
    note: Reviews involve long silent stretches (batched file reads, parallel subagents that take 30s+). Post one short status line per step using these templates verbatim, filling in the bracketed counts. Do not paraphrase, omit, or add extras.
    templates:
@NARRATION_TEMPLATES@
    variables:
@NARRATION_VARIABLES@
  review_profiles:
    description: |
      Routing table for selecting reviewers and models based on @DIFF_OR_CL@ content. Exactly one
      profile is selected per review. Selection is an inference call -- read each profile's
      `selection.guidance` and pick the most appropriate one based on the actual contents
      of `bundle.changed_files`. Default to `code` when uncertain.
    profiles:
      - id: data_only
        selection:
          data_only_extensions: [".csv", ".yaml", ".yml", ".json", ".tsv", ".md"]
          guidance: |
            Select this profile when every changed file is either:
              (a) in `data_only_extensions` (flat data / docs), OR
              (b) an inert binary asset -- images, audio, video, fonts, compiled binaries,
                  3D/animation assets -- whose presence wouldn't change what a code-grade
                  review would find. These files aren't reviewable for logic anyway, so
                  including them in a @DIFF_OR_CL@ shouldn't force the heavier `code` profile.
            Use judgment: the question is "is there any file in this @DIFF_OR_CL@ that needs Opus-level
            semantic reasoning to review?" -- not "is every extension on a fixed list?"

            Pick `code` instead the moment any changed file contains executable logic
            (source code, scripts, build configuration that runs code, templated configs
            that are interpreted as code, etc.).
        rationale: |
          Flat data and doc files don't exhibit the failure modes Opus is uniquely good at
          (concurrency, lifetime, deep semantic reasoning). Bugs in these files are
          surface-level: malformed syntax, duplicate keys, column-count mismatches, broken
          cross-file references, schema violations -- pattern-matching tasks where Sonnet is
          at near-parity with Opus. `reviewer_c_introduced_code`'s scope is essentially empty
          for data/doc files; running it just burns tokens and generates hallucinations the
          validator must reject.
        reviewers:
          - { name: reviewer_a_claude_md_compliance, model: sonnet }
          - { name: reviewer_b_diff_only_bugs,       model: sonnet }
        validator_models:
          bug: sonnet
          claude_md: sonnet
      - id: code
        selection:
          guidance: |
            Default profile. Use whenever any changed file contains executable logic
            (source code, scripts, build configuration that runs code) -- i.e. anytime
            `data_only` doesn't clearly apply.
        rationale: "Full reviewer set with Opus where deep semantic reasoning pays off."
        reviewers:
          - { name: reviewer_a_claude_md_compliance, model: sonnet }
          - { name: reviewer_b_diff_only_bugs,       model: opus }
          - { name: reviewer_c_introduced_code,      model: opus }
        validator_models:
          bug: opus
          claude_md: sonnet
  # subagents: reviewer/validator definitions (scope, input, restrictions).
  # Models are NOT set here -- they are bound by the selected `review_profiles` entry.
  subagents:
    - name: reviewer_a_claude_md_compliance
      subagent_type: general-purpose
      scope: CLAUDE.md compliance only, restricted to the files in one chunk
      input: "absolute path to ONE chunk .diff file, the @FILEPATHS@ of the files in that chunk, the per-file CLAUDE.md mapping restricted to those files, and the full text of each relevant CLAUDE.md (read in step 4)"
      restrictions:
        - "Read the assigned chunk diff once (single Read call). Do not Read other chunks."
        - "Only consider CLAUDE.md files that share a path with the file being reviewed (use the per-file mapping; do not cross-apply)."
        - "Only flag issues in files present in your chunk -- files in other chunks are someone else's responsibility."
    - name: reviewer_b_diff_only_bugs
      subagent_type: general-purpose
      scope: obvious bugs visible in one chunk's diff alone
      input: "absolute path to ONE chunk .diff file, the @FILEPATHS@ of the files in that chunk, and the @CHANGE_DESC@"
      restrictions:
        - "Read the assigned chunk diff once. MUST NOT use Read for anything beyond that chunk."
        - "Only flag won't-compile, syntax/type errors, missing imports, unresolved references, definitely-wrong logic regardless of inputs."
        - "For data/doc files (data_only profile): focus on malformed syntax, duplicate keys, schema or column-count violations, and broken cross-file references."
        - "Only flag issues in files present in your chunk."
    - name: reviewer_c_introduced_code
      subagent_type: general-purpose
      scope: bugs/security/logic problems in the introduced code that need broader context, restricted to one chunk's files
      input: "absolute path to ONE chunk .diff file, the @FILEPATHS@ of the files in that chunk, local paths for those files, and the @CHANGE_DESC@"
      restrictions:
        - "Read the assigned chunk diff first."
        - "MAY use Read to look at surrounding context in the changed files (the LOCAL paths you were given) when needed."
        - "Examples: concurrency issues, lifetime bugs, security holes."
        - "Only flag issues in files present in your chunk."
    - name: validator
      subagent_type: general-purpose
      scope: confirm or reject one candidate issue with high confidence
      input: "the issue (JSON), the chunk diff, [if claude_md: relevant CLAUDE.md contents]"
      output_format: "exactly one line: 'CONFIRMED: <one-sentence reason>' or 'REJECTED: <one-sentence reason>'"
      restrictions:
        - "Validator does not see who flagged the issue. Independence is the value."
  false_positive_guardrails:
    only_flag:
      - "code that will fail to compile or parse (syntax errors, type errors, missing imports, unresolved references)"
      - "code that will definitely produce wrong results regardless of inputs (clear logic errors)"
      - "a CLAUDE.md rule clearly and unambiguously violated, with the exact rule quotable"
    do_not_flag:
      - "code style or quality concerns"
      - "potential issues that depend on specific inputs or state"
      - "subjective suggestions or improvements"
      - "pre-existing issues (only review the diff)"
      - "anything a linter would catch (do not run a linter)"
      - "issues that appear in CLAUDE.md but are explicitly silenced in the code (e.g. lint-ignore comments)"
    rule: "If you are not certain an issue is real, do not flag it. False positives erode trust."
  agent_assumptions:
    - "All tools are functional. Do not test tools or make exploratory calls."
    - "Only call a tool if it is required to complete the task."
  issue_format:
    description: "JSON shape returned by reviewer subagents and accepted by validators."
    schema: |
      [{
        "file": "@ISSUE_PATH@",
        "lines": "<line range, e.g. 42 or 42-48>",
        "reason": "bug" | "claude_md",
        "description": "<one-sentence explanation>",
        "citation": "<exact rule quote, only for claude_md issues>"
      }]
  submit_gates:
    description: |
@SG_DESC@
    authoring_format: "See references/submit-gates.md for the CLAUDE.md-author-facing guide to writing submit-gate blocks (block format, scope path semantics, multi-gate rules)."
    rendering: |
      When bundle.submit_gates is non-empty, the rendered review prepends a
      `## Submit checklist` section ABOVE the per-file review body. Each gate renders as:

        - **[@CHK@|@CRS@] <summary>** -- per `<source>`, triggered by `<file>` (+N more if many).
          > <rationale, indented as blockquote, omitted if empty>

      @CHK@ = author confirmed in the step-5 AskUserQuestion.
      @CRS@ = author did not confirm. NOT an error; the review still renders.

      Always show the section when gates applied -- including in the "no issues" path.
@OUTPUT_FORMAT@
```
"""


# ---------------------------------------------------------------------------
# Per-VCS block fragments.
# ---------------------------------------------------------------------------

GIT_INTRO = (
    'Run a multi-agent code review of a git diff range directly in conversation. The default '
    'diff range is inferred from workspace state (mid-merge / mid-rebase / branch-with-upstream '
    '/ origin-main-fallback), so the agent does the right thing for "review what I\'m about to '
    'push" without forcing the user to spell out a range; arguments accepted for explicit '
    'control. The diff is partitioned on disk into chunks (one per file boundary cluster, '
    'balanced under a 1 MB cap); reviewer subagents (set by the selected review profile) run '
    '**once per (role @X@ chunk)** so a single large branch fans out across multiple parallel '
    'agents instead of forcing each reviewer to ingest the full diff. Each flagged issue is then '
    'validated by an independent subagent to suppress false positives. Path-scoped pre-submit '
    'reminders (submit gates) authored in ancestor CLAUDE.md files are surfaced alongside the '
    'review for author confirmation. Results are rendered as markdown -- no persistence to disk.'
)

P4_INTRO = (
    'Run a multi-agent code review of a Perforce changelist directly in conversation. The diff '
    'is partitioned on disk into chunks (one per file boundary cluster, balanced under a 1 MB '
    'cap); reviewer subagents (set by the selected review profile) run **once per (role @X@ '
    'chunk)** so a single large CL fans out across multiple parallel agents instead of forcing '
    'each reviewer to ingest the full diff. Each flagged issue is then validated by an '
    'independent subagent to suppress false positives. Path-scoped pre-submit reminders (submit '
    'gates) authored in ancestor CLAUDE.md files are surfaced alongside the review for author '
    'confirmation. Results are rendered as markdown -- no persistence to disk.'
)

GIT_SCOPE_COVERS_HEAD = """\
      - reviewing the current branch's changes (default = upstream..HEAD, with auto-detect fallbacks)
      - reviewing an explicit ref / range / staged / working-tree mode
      - reviewing an in-progress merge or rebase
      - CLAUDE.md compliance audits in a git repo"""

P4_SCOPE_COVERS_HEAD = """\
      - reviewing pending Perforce changelists by CL number
      - CLAUDE.md compliance audits in a P4 workspace"""

GIT_SCOPE_EXCLUDES = """\
      - Perforce workflows (use /p4-code-review)
      - reviewing a remote PR by URL or PR number (this skill works against the local working copy / refs only)
      - persisting review output to disk or a PR comment
      - enforcing submit gates (advisory only; enforcement belongs in a pre-push hook)"""

P4_SCOPE_EXCLUDES = """\
      - git diffs and non-Perforce review workflows
      - persisting review output to disk or Swarm
      - reviewing previously-submitted changelists
      - enforcing submit gates (advisory only; enforcement belongs in a pre-shelve/pre-submit hook)"""

GIT_PRECONDITIONS = "        - cwd is inside a git repository."
P4_PRECONDITIONS = "        - User has at least one pending CL OR has passed a CL number argument."

GIT_STEP1 = """\
        - n: 1
          action: |
            Resolve the diff range.
            - If the user passed an explicit argument (`<ref>`, `<a>..<b>`, `<a>...<b>`, `--staged`, `--working`), use it verbatim.
            - Otherwise let prepare_review.py auto-detect from workspace state. The detection order is:
              1. mid-merge (MERGE_HEAD present) -> review the in-progress merge
              2. mid-rebase -> review the in-progress rebase
              3. @{upstream}..HEAD if upstream is set
              4. origin/main..HEAD / origin/master..HEAD / main..HEAD / master..HEAD as fallbacks
              5. else error with a hint to pass an explicit range
            If auto-detect fails (detached HEAD with no fallback, or no upstream and no main/master), surface the error to the user and ask them for an explicit range. Do NOT guess.
          tool: prepare_review.py
          input: "[<ref>|<a>..<b>|<a>...<b>|--staged|--working]"
          expected: The script's stdout JSON includes `range` and (when auto-detected) `auto_detected_reason`. Restate the chosen range to the user in the step-1 narration line so they can correct if the wrong one was inferred."""

P4_STEP1 = """\
        - n: 1
          action: Resolve the CL number (from argument, else list pending CLs and prompt the user).
          tool: p4
          input: "p4 -ztag changes -s pending -u $(p4 set -q P4USER | cut -d= -f2) -m 20"
          expected: A single integer CL number confirmed by the user."""

GIT_STEP2 = """\
        - n: 2
          action: |
__CLAIM_PROBE__
            Then run prepare_review.py to fetch the diff, partition it into chunked .diff fragments on disk, enumerate changed files via `git diff --name-status`, map ancestor CLAUDE.md files for each, detect untracked-or-unstaged files in the directories the diff touches, detect unresolved merge conflicts, and scan ancestor CLAUDE.md files for submit-gate reminders that apply to this range.
          tool: ${CLAUDE_PLUGIN_ROOT}/scripts/prepare_review.py
          input: "<range or argument from step 1>  (append `--claim '**/CLAUDE.md' --claim '**/SKILL.md'` when md-audit is available, per the claim probe)"
          expected: |
            JSON with vcs, range, head_sha, branch, description, bundle_dir, diff_chunks, changed_files, unique_claude_mds, untracked_or_unstaged, merge_conflicts, submit_gates, and -- only when --claim was passed -- claimed_files. The raw diff text is NOT inline -- it lives in per-chunk files at `<bundle_dir>/<diff_chunks[i].path>` (paths are relative to bundle_dir). Each `changed_files` entry carries `chunk_index` pointing to the chunk that contains its diff.
          on_failure: Surface the stderr message to the user and stop. No retry.""".replace(
    "__CLAIM_PROBE__", CLAIM_PROBE
)

P4_STEP2 = """\
        - n: 2
          action: |
__CLAIM_PROBE__
            Then run prepare_review.py to fetch the diff (with shelved fallback; auto-shelves a pending CL with no existing shelf so the diff is fetchable), partition the diff into chunked .diff fragments on disk, map ancestor CLAUDE.md files for each changed file, detect unreconciled files in the directories the CL touches, detect unresolved merges in the CL, and scan ancestor CLAUDE.md files for submit-gate reminders that apply to this CL.
          tool: python3 ${CLAUDE_PLUGIN_ROOT}/scripts/prepare_review.py
          input: "<CL>  (append `--claim '**/CLAUDE.md' --claim '**/SKILL.md'` when md-audit is available, per the claim probe)"
          expected: |
            JSON with cl, description, bundle_dir, diff_chunks, changed_files, unique_claude_mds, unreconciled, unresolved, submit_gates, auto_shelved, shelf_fingerprint, and -- only when --claim was passed -- claimed_files. The raw diff text is NOT inline -- it lives in per-chunk files at `<bundle_dir>/<diff_chunks[i].path>` (paths are relative to bundle_dir). Each `changed_files` entry carries `chunk_index` pointing to the chunk that contains its diff. `auto_shelved=true` means prepare_review created the shelf and step 10 must clean it up.
          on_failure: |
            Surface the stderr message to the user and stop. No retry.
            Launch note: ALWAYS invoke with an explicit `python3` interpreter (as shown in `tool:`), never as a bare path. Bare `${CLAUDE_PLUGIN_ROOT}/scripts/prepare_review.py <CL>` lets bash try to run the file as a shell script -- it has no shebang line in older checkouts and the exec bit does not survive on Windows checkouts, so bash parses the Python as sh and exits 2. The script self-relocates under the p4-kit venv via reexec, so any python3 launcher is sufficient. And NEVER pipe the invocation (`... | tail`, `... | head`): a pipe makes `$?` the last pipeline stage's status, not the script's, which silently masks a launch failure as success.""".replace(
    "__CLAIM_PROBE__", CLAIM_PROBE
)

GIT_STEP3 = """\
        - n: 3
          action: |
            If bundle.untracked_or_unstaged is non-empty, list the files (grouped by `kind`: untracked / unstaged_modified / unstaged_deleted / staged_uncommitted) and ask the user whether any should be folded into the review before reviewers spawn.
            - If the user picks one or more untracked / unstaged files: run `git add <paths>` to stage them, optionally commit them with `git commit -m "<message>"` to include in the range, and re-run prepare_review.py with the same range. Use the new bundle.
            - For `staged_uncommitted` files the user wants in: same flow -- commit them so they land in the range. (`--staged` mode already includes them; the prompt is for committed-range modes.)
            - If the user declines all: continue with the current bundle.
            On the post-fold re-run, do NOT prompt again about untracked_or_unstaged files even if some remain -- the user already decided.
            Skip this step entirely if bundle.untracked_or_unstaged is empty.
          tool: AskUserQuestion + git add/commit + prepare_review.py"""

P4_STEP3 = """\
        - n: 3
          action: |
            If bundle.unreconciled is non-empty, list the files (grouped by action: add / edit / delete) and ask the user whether any should be folded into the CL before review.
            - If the user picks one or more: run `p4 reconcile -c <CL> <local-paths>` to open them directly into the CL, then re-run prepare_review.py and use the new bundle.
            - If the user declines all: continue with the current bundle.
            On the post-reconcile re-run, do NOT prompt again about unreconciled files even if some remain -- the user already decided.
            Skip this step entirely if bundle.unreconciled is empty.
          tool: AskUserQuestion + p4 reconcile + prepare_review.py"""

GIT_STEP5_PHRASE = """\
            Phrase the question as: "Confirm each pre-push obligation you've already
            completed for <range>." """.rstrip()

P4_STEP5_PHRASE = """\
            Phrase the question as: "Confirm each pre-submit obligation you've already
            completed for CL <CL>." """.rstrip()

GIT_STEP9_TAIL = """\
            - When `bundle.merge_conflicts` is non-empty, prepend a `## Unresolved merge conflicts`
              section listing each conflicted file. This is informational, not a finding --
              the merge cannot be completed until each file is resolved (`git add <file>`
              after editing), but the review still renders."""

P4_STEP9_TAIL = """\
            - When `bundle.unresolved` is non-empty, prepend a `## Unresolved merges`
              section listing each unresolved file with its resolve type. This is
              informational, not a finding -- the CL is not submittable until the
              user runs `p4 resolve` on each entry, but the review still renders."""

GIT_STEP10 = ""

P4_STEP10 = """\
        - n: 10
          action: |
            Run prepare_review.py --cleanup <bundle.bundle_dir> to delete the
            auto-created shelf -- but only if step 2 set `bundle.auto_shelved = true`.
            The script is deterministic: it re-fingerprints the live shelf and
            deletes only on exact match; any mismatch (author reshelved, added
            files, edited content, or already deleted) is a silent no-op so the
            author's work is never overwritten.

            ALWAYS run this step when `bundle.auto_shelved` is true, regardless
            of review outcome -- even if the review found bugs, even if the
            author wants to revisit findings, even if the rendering failed.
            Skipping leaves an orphan shelf the author didn't ask for.

            Skip this step entirely when `bundle.auto_shelved` is false (we did
            not create the shelf and must not touch it).
          tool: python3 ${CLAUDE_PLUGIN_ROOT}/scripts/prepare_review.py
          input: "--cleanup <bundle.bundle_dir>"
"""

GIT_CHECKLIST = f"""\
        - Diff range resolved (auto-detected from workspace state OR explicit user arg) and surfaced in the step-1 narration line
        - Context bundled via prepare_review.py
        - Untracked/unstaged files surfaced (and either folded in via `git add`/`git commit` with a re-run, or explicitly declined)
        - All CLAUDE.md files read
        - Submit gates surfaced (if any) and author confirmation collected via a single AskUserQuestion
        - Review profile selected from review_profiles
        - Reviewers launched in parallel (single message, R {X} K Agent calls -- one per (reviewer {X} chunk) pair, where K = len(bundle.diff_chunks))
        - Validators launched in parallel (single message, N Agent calls), models picked from the profile's validator_models
        - Filtered to confirmed-only
        - md-audit subject-lens pass launched for bundle.claimed_files when skills-kit md-audit is available (or claimed files folded back into the generic review on version-skew fallback); skipped silently when md-audit is absent
        - Markdown rendered to chat (Submit checklist section prepended when gates applied; Unresolved merge conflicts section prepended when bundle.merge_conflicts is non-empty; separate `## md-audit (subject-lens) findings` section when the md-audit pass ran)"""

P4_CHECKLIST = f"""\
        - CL number resolved
        - Context bundled via prepare_review.py
        - Unreconciled files surfaced (and either folded in via `p4 reconcile -c <CL>` with a re-run, or explicitly declined)
        - All CLAUDE.md files read
        - Submit gates surfaced (if any) and author confirmation collected via a single AskUserQuestion
        - Review profile selected from review_profiles
        - Reviewers launched in parallel (single message, R {X} K Agent calls -- one per (reviewer {X} chunk) pair, where K = len(bundle.diff_chunks))
        - Validators launched in parallel (single message, N Agent calls), models picked from the profile's validator_models
        - Filtered to confirmed-only
        - md-audit subject-lens pass launched for bundle.claimed_files when skills-kit md-audit is available (or claimed files folded back into the generic review on version-skew fallback); skipped silently when md-audit is absent
        - Markdown rendered to chat (Submit checklist section prepended when gates applied; Unresolved merges section prepended when bundle.unresolved is non-empty; separate `## md-audit (subject-lens) findings` section when the md-audit pass ran)
        - Auto-shelf cleanup invoked when bundle.auto_shelved is true (`prepare_review.py --cleanup <bundle_dir>`)"""

GIT_GOTCHAS = f"""\
        - Always quote the exact CLAUDE.md rule text when flagging a claude_md issue. If you cannot quote it verbatim, do not flag it.
        - Sequential reviewer or validator calls waste time. Reviewers run in one message with one concurrent Agent call per (reviewer {X} chunk) pair (R reviewers {X} K chunks). For a small diff (K=1) that's still 2 calls for data_only / 3 for code; for a large diff (K=N) it scales to R {X} N. Validators run in one message with N concurrent Agent calls.
        - Each reviewer subagent reads ONE chunk path, not the whole diff. Do not pass `bundle_dir` and expect the subagent to glob -- pass the absolute chunk path the subagent should Read.
        - Render only -- this skill outputs in chat. There is no PR comment or disk write step.
        - If prepare_review.py fails, report the error and stop. No retry.
        - Validators are independent of reviewers. The validator does not see who flagged the issue.
        - The untracked/unstaged check must happen BEFORE reviewers spawn. Folding in forgotten files after agents have already reviewed the diff wastes their work and produces a stale review.
        - On the post-fold re-run, do NOT prompt again about untracked_or_unstaged files. The user already chose. Re-prompting on the same list is annoying; re-prompting on a smaller list (because they only added some) implies the rest were forgotten when they were declined.
        - Submit gates are reminders, not findings -- they do NOT go through reviewer or validator subagents. They are parsed deterministically by prepare_review.py and rendered verbatim in a separate output section. Do not try to validate, score, or filter them.
        - The submit-gates AskUserQuestion fires once, regardless of gate count. multiSelect bundles all gates into one prompt. Re-prompting per gate is rude and adds no value -- the author's response is final either way.
        - Unconfirmed submit gates are NOT errors. Render them with {CRS} so they're visible, but do not block the review or refuse to render the rest.
        - Merge conflicts are NOT findings -- they do NOT go through reviewer subagents. They are detected deterministically by prepare_review.py (`git ls-files -u`). The reviewers see the raw diff (including any conflict markers) and may legitimately flag bugs in it; the merge-conflicts section is a separate informational warning to the user.
        - Auto-detect is convenient, not authoritative. Always restate the chosen range in the step-1 narration line; a user reviewing the wrong branch will catch it there before subagents spawn.
        - Detached HEAD with no main/master fallback is a real failure mode; surface the error and ask for an explicit range. Do not guess at a "probably right" base.""" + MD_AUDIT_GOTCHAS

P4_GOTCHAS = f"""\
        - Always quote the exact CLAUDE.md rule text when flagging a claude_md issue. If you cannot quote it verbatim, do not flag it.
        - Sequential reviewer or validator calls waste time. Reviewers run in one message with one concurrent Agent call per (reviewer {X} chunk) pair (R reviewers {X} K chunks). For a small CL (K=1) that's still 2 calls for data_only / 3 for code; for a large CL (K=N) it scales to R {X} N. Validators run in one message with N concurrent Agent calls.
        - Each reviewer subagent reads ONE chunk path, not the whole diff. Do not pass `bundle_dir` and expect the subagent to glob -- pass the absolute chunk path the subagent should Read.
        - Render only -- this skill outputs in chat. There is no Swarm comment, PR comment, or disk write step.
        - If prepare_review.py fails, report the error and stop. No retry.
        - Validators are independent of reviewers. The validator does not see who flagged the issue.
        - The unreconciled check must happen BEFORE reviewers spawn. Folding in forgotten files after agents have already reviewed the diff wastes their work and produces a stale review.
        - On the post-reconcile re-run, do NOT prompt again about unreconciled files. The user already chose. Re-prompting on the same list is annoying; re-prompting on a smaller list (because they only added some) implies the rest were forgotten when they were declined.
        - Submit gates are reminders, not findings -- they do NOT go through reviewer or validator subagents. They are parsed deterministically by prepare_review.py and rendered verbatim in a separate output section. Do not try to validate, score, or filter them.
        - The submit-gates AskUserQuestion fires once, regardless of gate count. multiSelect bundles all gates into one prompt. Re-prompting per gate is rude and adds no value -- the author's response is final either way.
        - Unconfirmed submit gates are NOT errors. Render them with {CRS} so they're visible, but do not block the review or refuse to render the rest.
        - Unresolved merges are NOT findings -- they do NOT go through reviewer or validator subagents. They are detected deterministically by prepare_review.py (`p4 resolve -n -c <CL>`) and rendered verbatim in a separate output section. The reviewers see the raw diff (including any conflict markers) and may legitimately flag bugs in it; the unresolved section is a separate informational warning to the user.
        - Auto-shelf cleanup (step 10) must run whenever `bundle.auto_shelved` is true, no matter what happened in steps 3-9. The cleanup script is deterministic and safe (it only deletes the shelf when the live fingerprint exactly matches what we recorded), so there is no scenario where skipping it is the right call. Skipping leaves an orphan shelf the author didn't ask for.""" + MD_AUDIT_GOTCHAS

GIT_NARRATION_TEMPLATES = f"""\
      - when: "Before step 2"
        template: "Gathering context for <range> (<auto_or_explicit>): fetching diff, mapping CLAUDE.md scopes, scanning for untracked/unstaged files."
      - when: "Before step 3 (U >= 1)"
        template: "Found <U> untracked/unstaged file(s) in the directories this range touches. Asking before reviewing."
      - when: "After step 3 if user folded files in (U_added >= 1)"
        template: "Folded <U_added> file(s) into the range via `git add`/`git commit`. Re-running prepare to refresh the diff."
      - when: "After step 3 if user declined (U_added = 0 and U >= 1)"
        template: "Continuing with <range> as-is."
      - when: "After step 3, before step 4 (M >= 1)"
        template: "Got <N> changed file(s) and <M> unique CLAUDE.md scope(s). Reading them now."
      - when: "After step 3, before step 4 (M = 0)"
        template: "Got <N> changed file(s); no CLAUDE.md scopes apply."
      - when: "After step 2 (V >= 1)"
        template: "Found <V> file(s) with unresolved merge conflicts. Will surface in the review output -- the merge cannot complete until resolved."
      - when: "Before step 5 (G >= 1)"
        template: "Found <G> submit-gate reminder(s) applying to this range. Asking the author to confirm."
      - when: "Before step 6"
        template: "Selected review profile: <P>. Diff partitioned into <K> chunk(s). Launching <RK> subagent(s) in parallel (<R> reviewer(s) {X} <K> chunk(s)): <reviewer_summary>."
      - when: "After step 6, before step 7 (X >= 1)"
        template: "Reviewers returned <X> candidate issue(s) (<B> bug, <C> CLAUDE.md). Launching <X> validator(s) in parallel."
      - when: "After step 6 (X = 0)"
        template: "Reviewers found no issues. Skipping validation."
      - when: "After step 7, before step 9"
        template: "Validators confirmed <Y> of <X>. Rendering review." """.rstrip()

P4_NARRATION_TEMPLATES = f"""\
      - when: "Before step 1 (only if no CL arg was passed)"
        template: "Listing your pending changelists."
      - when: "Before step 2"
        template: "Gathering context for CL <CL>: fetching diff, mapping CLAUDE.md scopes, scanning for unreconciled files."
      - when: "Before step 3 (U >= 1)"
        template: "Found <U> unreconciled file(s) in the directories this CL touches. Asking before reviewing."
      - when: "After step 3 if user folded files in (U_added >= 1)"
        template: "Folded <U_added> file(s) into CL <CL> via `p4 reconcile`. Re-running prepare to refresh the diff."
      - when: "After step 3 if user declined (U_added = 0 and U >= 1)"
        template: "Continuing with CL <CL> as-is."
      - when: "After step 3, before step 4 (M >= 1)"
        template: "Got <N> changed file(s) and <M> unique CLAUDE.md scope(s). Reading them now."
      - when: "After step 3, before step 4 (M = 0)"
        template: "Got <N> changed file(s); no CLAUDE.md scopes apply."
      - when: "After step 2 (V >= 1)"
        template: "Found <V> file(s) with unresolved merges in CL <CL>. Will surface in the review output -- CL is not submittable until resolved."
      - when: "Before step 5 (G >= 1)"
        template: "Found <G> submit-gate reminder(s) applying to this CL. Asking the author to confirm."
      - when: "Before step 6"
        template: "Selected review profile: <P>. Diff partitioned into <K> chunk(s). Launching <RK> subagent(s) in parallel (<R> reviewer(s) {X} <K> chunk(s)): <reviewer_summary>."
      - when: "After step 6, before step 7 (X >= 1)"
        template: "Reviewers returned <X> candidate issue(s) (<B> bug, <C> CLAUDE.md). Launching <X> validator(s) in parallel."
      - when: "After step 6 (X = 0)"
        template: "Reviewers found no issues. Skipping validation."
      - when: "After step 7, before step 9"
        template: "Validators confirmed <Y> of <X>. Rendering review."
      - when: "After step 2 (bundle.auto_shelved is true)"
        template: "CL <CL> had no shelved content. Auto-shelved to fetch the diff -- will clean up after the review."
      - when: "After step 9 (bundle.auto_shelved is true)"
        template: "Cleaning up the auto-created shelf for CL <CL>." """.rstrip()

GIT_NARRATION_VARIABLES = """\
      "<range>": "bundle.range"
      "<auto_or_explicit>": "'auto-detected: <bundle.auto_detected_reason>' if auto_detected_reason is set, else 'explicit'"
      "<N>": "len(bundle.changed_files)"
      "<M>": "len(bundle.unique_claude_mds)"
      "<U>": "len(bundle.untracked_or_unstaged)"
      "<U_added>": "count of files the user chose to fold into the range"
      "<X>": "total candidate issues from all launched reviewers combined"
      "<B>": "count where reason == 'bug'"
      "<C>": "count where reason == 'claude_md'"
      "<Y>": "count of validators returning CONFIRMED"
      "<P>": "selected review profile id (e.g. code, data_only)"
      "<R>": "count of reviewers in the selected profile"
      "<K>": "len(bundle.diff_chunks)"
      "<RK>": "<R> * <K>"
      "<reviewer_summary>": "comma-separated '<model> <reviewer short name>' for each reviewer in the profile (e.g. 'sonnet CLAUDE.md compliance, opus diff-only bugs, opus introduced-code') -- each is fanned out across all K chunks"
      "<G>": "len(bundle.submit_gates)"
      "<V>": "len(bundle.merge_conflicts)" """.rstrip()

P4_NARRATION_VARIABLES = """\
      "<CL>": "the changelist number"
      "<N>": "len(bundle.changed_files)"
      "<M>": "len(bundle.unique_claude_mds)"
      "<U>": "len(bundle.unreconciled)"
      "<U_added>": "count of files the user chose to fold into the CL"
      "<X>": "total candidate issues from all launched reviewers combined"
      "<B>": "count where reason == 'bug'"
      "<C>": "count where reason == 'claude_md'"
      "<Y>": "count of validators returning CONFIRMED"
      "<P>": "selected review profile id (e.g. code, data_only)"
      "<R>": "count of reviewers in the selected profile"
      "<K>": "len(bundle.diff_chunks)"
      "<RK>": "<R> * <K>"
      "<reviewer_summary>": "comma-separated '<model> <reviewer short name>' for each reviewer in the profile (e.g. 'sonnet CLAUDE.md compliance, opus diff-only bugs, opus introduced-code') -- each is fanned out across all K chunks"
      "<G>": "len(bundle.submit_gates)"
      "<V>": "len(bundle.unresolved)" """.rstrip()

GIT_SG_DESC = """\
      Path-scoped pre-push reminders authored in CLAUDE.md files. Surfaced verbatim at
      review time when at least one file in the range falls within the gate's scope.
      Reminders are not findings -- they don't go through reviewer or validator subagents.
      Detection is deterministic, performed by prepare_review.py (same parser as p4-code-review)."""

P4_SG_DESC = """\
      Path-scoped pre-submit reminders authored in CLAUDE.md files. Surfaced verbatim at
      review time when at least one file in the CL falls within the gate's scope. Reminders
      are not findings -- they don't go through reviewer or validator subagents. Detection
      is deterministic, performed by prepare_review.py."""

GIT_OUTPUT_FORMAT = f"""\
  output_format:
    description: "Final markdown rendered to chat. Unresolved merge conflicts (when applicable) and Submit checklist (when applicable) above the per-file review body."
    template: |
      ## Unresolved merge conflicts
      The merge cannot complete until each file below is resolved (`git add <file>` after editing).
      - `path/to/file.cpp`
      - `path/to/other.csv`

      ## Submit checklist
      - **[{CHK}] ./build.sh configbinaries must pass before push** -- per `<path>/CLAUDE.md`, triggered by `GameConfigs/Real/x.csv`.
      - **[{CRS}] Regenerate the asset index** -- per `<path>/CLAUDE.md`, triggered by `Content/Assets/y.uasset`.
        > <rationale if any, as a blockquote>

      ## Review: <range> -- <description>

      Branch: <branch>  {DOT}  HEAD: <head_sha>

      Found N issues (M filtered as false positives).

      ### path/to/file.cpp
      - **[bug]** L42: Buffer overflow risk -- `items[i]` accessed without bounds check.
      - **[claude_md]** L78: Violates `src/CLAUDE.md` rule "Use absl::Status not bool returns".
    empty_template: |
      ## Submit checklist
      - **[{CHK}] ./build.sh configbinaries must pass before push** -- per `<path>/CLAUDE.md`, triggered by `GameConfigs/Real/x.csv`.

      ## Review: <range> -- <description>

      Branch: <branch>  {DOT}  HEAD: <head_sha>

      No issues found. Reviewed for bugs and CLAUDE.md compliance.
    notes:
      - "Omit the Unresolved merge conflicts section entirely when bundle.merge_conflicts is empty."
      - "Omit the Submit checklist section entirely when bundle.submit_gates is empty."
      - "When matched_files has >3 entries, render the first 3 then '(+N more)'."
      - "Rationale renders as a markdown blockquote (`> `) indented one level below the bullet, only if non-empty."
      - "<range>, <branch>, <head_sha>, <description> come from the top-level bundle fields." """.rstrip()

P4_OUTPUT_FORMAT = f"""\
  output_format:
    description: "Final markdown rendered to chat. Unresolved merges (when applicable) and Submit checklist (when applicable) above the per-file review body."
    template: |
      ## Unresolved merges
      CL is not submittable until each file below is run through `p4 resolve`.
      - `path/to/file.cpp` -- content resolve pending (from `//depot/branch/file.cpp`)
      - `path/to/other.csv` -- branch resolve pending

      ## Submit checklist
      - **[{CHK}] ./build.sh configbinaries must pass before submit** -- per `<path>/CLAUDE.md`, triggered by `GameConfigs/Real/x.csv`.
      - **[{CRS}] Regenerate the asset index** -- per `<path>/CLAUDE.md`, triggered by `Content/Assets/y.uasset`.
        > <rationale if any, as a blockquote>

      ## Review: CL <CL> -- <description>

      Found N issues (M filtered as false positives).

      ### path/to/file.cpp
      - **[bug]** L42: Buffer overflow risk -- `items[i]` accessed without bounds check.
      - **[claude_md]** L78: Violates `src/CLAUDE.md` rule "Use absl::Status not bool returns".
    empty_template: |
      ## Submit checklist
      - **[{CHK}] ./build.sh configbinaries must pass before submit** -- per `<path>/CLAUDE.md`, triggered by `GameConfigs/Real/x.csv`.

      ## Review: CL <CL> -- <description>

      No issues found. Reviewed for bugs and CLAUDE.md compliance.
    notes:
      - "Omit the Unresolved merges section entirely when bundle.unresolved is empty."
      - "Omit the Submit checklist section entirely when bundle.submit_gates is empty."
      - "When matched_files has >3 entries, render the first 3 then '(+N more)'."
      - "Rationale renders as a markdown blockquote (`> `) indented one level below the bullet, only if non-empty."
      - "Unresolved-merge entries render local path first (workspace-relative if possible); append `(from <fromFile>)` only when from_file is non-empty (integrations); omit for plain edit/sync resolves." """.rstrip()


FRAGMENTS = {
    "git": {
        "NAME": "git-code-review",
        "DESC": "Use when reviewing local git changes -- before push, before opening a PR, or auditing a branch. Do NOT use for Perforce CLs or existing PRs by URL.",
        "TITLE": "Git Code Review",
        "INTRO": GIT_INTRO,
        "IDENTITY": "Run a multi-agent code review of a git diff range using parallel Claude subagents.",
        "SCOPE_COVERS_HEAD": GIT_SCOPE_COVERS_HEAD,
        "SCOPE_EXCLUDES": GIT_SCOPE_EXCLUDES,
        "KEYWORDS": "code review, git review, branch review, multi-agent review, claude.md compliance, parallel reviewers, pre-push review",
        "GOAL": "Produce a markdown summary of confirmed issues for one git diff range.",
        "PRECONDITIONS": GIT_PRECONDITIONS,
        "STEP1": GIT_STEP1,
        "STEP2": GIT_STEP2,
        "STEP3": GIT_STEP3,
        "STEP5_PHRASE": GIT_STEP5_PHRASE,
        "STEP9_TAIL": GIT_STEP9_TAIL,
        "STEP10": GIT_STEP10,
        "CHECKLIST": GIT_CHECKLIST,
        "GOTCHAS": GIT_GOTCHAS,
        "NARRATION_TEMPLATES": GIT_NARRATION_TEMPLATES,
        "NARRATION_VARIABLES": GIT_NARRATION_VARIABLES,
        "DIFF_OR_CL": "diff",
        "RANGE_OR_CL": "range",
        "FILEPATHS": "repo-relative paths",
        "CHANGE_DESC": "diff description",
        "ISSUE_PATH": "<repo-relative or absolute path>",
        "SG_DESC": GIT_SG_DESC,
        "OUTPUT_FORMAT": GIT_OUTPUT_FORMAT,
    },
    "p4": {
        "NAME": "p4-code-review",
        "DESC": "Use when reviewing a pending Perforce changelist, or before asking the user to submit a CL. Do NOT use for git diffs or submitted CLs.",
        "TITLE": "P4 Code Review",
        "INTRO": P4_INTRO,
        "IDENTITY": "Run a multi-agent code review of a Perforce changelist using parallel Claude subagents.",
        "SCOPE_COVERS_HEAD": P4_SCOPE_COVERS_HEAD,
        "SCOPE_EXCLUDES": P4_SCOPE_EXCLUDES,
        "KEYWORDS": "code review, perforce review, CL review, multi-agent review, claude.md compliance, parallel reviewers, p4 review",
        "GOAL": "Produce a markdown summary of confirmed issues for one pending Perforce CL.",
        "PRECONDITIONS": P4_PRECONDITIONS,
        "STEP1": P4_STEP1,
        "STEP2": P4_STEP2,
        "STEP3": P4_STEP3,
        "STEP5_PHRASE": P4_STEP5_PHRASE,
        "STEP9_TAIL": P4_STEP9_TAIL,
        "STEP10": P4_STEP10,
        "CHECKLIST": P4_CHECKLIST,
        "GOTCHAS": P4_GOTCHAS,
        "NARRATION_TEMPLATES": P4_NARRATION_TEMPLATES,
        "NARRATION_VARIABLES": P4_NARRATION_VARIABLES,
        "DIFF_OR_CL": "CL",
        "RANGE_OR_CL": "CL",
        "FILEPATHS": "depot paths",
        "CHANGE_DESC": "CL description",
        "ISSUE_PATH": "<depot or local path>",
        "SG_DESC": P4_SG_DESC,
        "OUTPUT_FORMAT": P4_OUTPUT_FORMAT,
    },
}

# Shared tokens (identical for both VCS): the dispatch rule, the md-audit
# contributor regions, and the glyphs.
_SHARED = {
    "DISPATCH": DISPATCH,
    "MD_AUDIT_LAUNCH": MD_AUDIT_LAUNCH,
    "MD_AUDIT_REPORT": MD_AUDIT_REPORT,
    "X": X,
    "CHK": CHK,
    "CRS": CRS,
}

_SKILL_TOKEN_ORDER = [
    "DISPATCH",  # multi-line, contains no other @tokens@; substitute first
    "MD_AUDIT_LAUNCH", "MD_AUDIT_REPORT",  # shared, no nested @tokens@
    "NAME", "DESC", "TITLE", "INTRO", "IDENTITY",
    "SCOPE_COVERS_HEAD", "SCOPE_EXCLUDES", "KEYWORDS", "GOAL", "PRECONDITIONS",
    "STEP1", "STEP2", "STEP3", "STEP5_PHRASE", "STEP9_TAIL", "STEP10",
    "CHECKLIST", "GOTCHAS", "NARRATION_TEMPLATES", "NARRATION_VARIABLES",
    "DIFF_OR_CL", "RANGE_OR_CL", "FILEPATHS", "CHANGE_DESC", "ISSUE_PATH",
    "SG_DESC", "OUTPUT_FORMAT",
    # glyph tokens last -- they appear inside already-substituted blocks too,
    # but those blocks embed the literal glyph (via f-strings), so the only
    # remaining @X@/@CHK@/@CRS@ markers are in the template body.
    "X", "CHK", "CRS",
]


def render_skill(vcs: str) -> str:
    frags = dict(_SHARED)
    frags.update(FRAGMENTS[vcs])
    out = SKILL_TEMPLATE
    for token in _SKILL_TOKEN_ORDER:
        out = out.replace(f"@{token}@", frags[token])
    return out


# ===========================================================================
# submit-gates.md -- one parameterized source rendering both references.
# ===========================================================================
SUBMIT_GATES_TEMPLATE = """\
# Authoring submit gates

@SG_HEADER@

## Authoring format

Add this block to any CLAUDE.md (root, subdirectory, or both):

```
**Submit gate:** <imperative -- what the author must do>.
Applies to:
- <path prefix or glob>
- <path prefix or glob>

<optional rationale paragraph, rendered verbatim with the gate>
```

Scope path semantics:

- No glob characters (`*`, `?`, `[`): prefix match. `Foo/Bar/` matches every file under Foo/Bar/. `Foo/Bar` (no trailing slash) is equivalent and does NOT accidentally match `Foo/BarBaz/`.
- Contains glob characters: fnmatch-style glob, anchored to the @SG_ANCHOR@. `*` matches anything including `/`; `?` matches one character.
- Case-insensitive on Windows, case-sensitive elsewhere.

Multiple gates per CLAUDE.md allowed; blocks must be separated by a blank line. Malformed blocks (missing `Applies to:`, empty scope list) are skipped with a one-line stderr warning -- never silently dropped.
"""

SUBMIT_GATES_FRAGMENTS = {
    "git": {
        "SG_HEADER": (
            "The CLAUDE.md-author-facing guide for writing submit-gate blocks. Submit gates are "
            "path-scoped pre-push reminders authored in CLAUDE.md files; `git-code-review` detects "
            "them deterministically (via `prepare_review.py`, the same parser as `p4-code-review`) "
            "and surfaces them verbatim at review time when at least one file in the range falls "
            "within a gate's scope. This doc covers only how to author them; detection and rendering "
            "are described in the `submit_gates` block of the SKILL.md contract."
        ),
        "SG_ANCHOR": "repo root",
    },
    "p4": {
        "SG_HEADER": (
            "The CLAUDE.md-author-facing guide for writing submit-gate blocks. Submit gates are "
            "path-scoped pre-submit reminders authored in CLAUDE.md files; `p4-code-review` detects "
            "them deterministically (via `prepare_review.py`) and surfaces them verbatim at review "
            "time when at least one file in the CL falls within a gate's scope. This doc covers only "
            "how to author them; detection and rendering are described in the `submit_gates` block "
            "of the SKILL.md contract."
        ),
        "SG_ANCHOR": "workspace root",
    },
}


def render_submit_gates(vcs: str) -> str:
    out = SUBMIT_GATES_TEMPLATE
    for token, value in SUBMIT_GATES_FRAGMENTS[vcs].items():
        out = out.replace(f"@{token}@", value)
    return out


# ===========================================================================
# md-audit-review.md -- one parameterized source rendering both references.
# The full args/plugin-root/fallback detail the SKILL step-6/step-9 prose points
# at, kept out of the drift-tested SKILL body so that body stays legible.
# ===========================================================================
MD_AUDIT_REVIEW_TEMPLATE = """\
# Subject-lens md-audit contributor

When skills-kit's md-audit skill is available in the session, `@SKILL_NAME@` treats it
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
materialized before-image via @PREIMAGE_ORIGIN@, or `null` for an add), and `claude_mds` (the
nearest-ancestor-first CLAUDE.md chain, which for a CLAUDE.md subject INCLUDES the subject itself
as its first element).

Derive, per claimed file:

- `ancestorClaudeMdPaths` = `claude_mds` with the subject's OWN `local` removed (drop the
  self-entry a CLAUDE.md subject carries; a SKILL.md subject has nothing to drop). Nearest-ancestor
  first, excluding the subject -- exactly md-audit's H-11 / M ancestor-convention input.
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
"""

MD_AUDIT_REVIEW_FRAGMENTS = {
    "git": {
        "SKILL_NAME": "git-code-review",
        "PREIMAGE_ORIGIN": "`git show <range-base>:<path>`",
    },
    "p4": {
        "SKILL_NAME": "p4-code-review",
        "PREIMAGE_ORIGIN": "`p4 print -q -o <dest> //depot/path#have`",
    },
}


def render_md_audit_review(vcs: str) -> str:
    out = MD_AUDIT_REVIEW_TEMPLATE
    for token, value in MD_AUDIT_REVIEW_FRAGMENTS[vcs].items():
        out = out.replace(f"@{token}@", value)
    return out


# ---------------------------------------------------------------------------
# Targets + write/check driver.
# ---------------------------------------------------------------------------

def targets() -> dict[Path, str]:
    """Map each rendered file path to its rendered content."""
    return {
        GIT_SKILL: render_skill("git"),
        P4_SKILL: render_skill("p4"),
        GIT_SUBMIT_GATES: render_submit_gates("git"),
        P4_SUBMIT_GATES: render_submit_gates("p4"),
        GIT_MD_AUDIT_REVIEW: render_md_audit_review("git"),
        P4_MD_AUDIT_REVIEW: render_md_audit_review("p4"),
    }


def check() -> list[str]:
    problems: list[str] = []
    for path, rendered in targets().items():
        try:
            on_disk = path.read_text(encoding="utf-8")
        except OSError as e:
            problems.append(f"{path}: unreadable ({e})")
            continue
        if on_disk != rendered:
            problems.append(
                f"{path}: drifted from the canonical template "
                "(edit gen_code_review_skills.py and regenerate, or revert the file edit)"
            )
    return problems


def main(argv: list[str]) -> int:
    if "--check" in argv:
        problems = check()
        for p in problems:
            print(p, file=sys.stderr)
        print(f"code-review skill drift check: {len(problems)} problem(s)")
        return 1 if problems else 0

    for path, rendered in targets().items():
        # newline="\n" forces LF regardless of platform, matching the LF blobs
        # git stores (core.autocrlf converts to CRLF on Windows checkout, which
        # Python's read_text normalizes back to LF for the drift compare).
        path.write_text(rendered, encoding="utf-8", newline="\n")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
