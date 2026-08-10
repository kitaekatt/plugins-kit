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
GIT_MD_DOMAIN_REVIEW = REPO_ROOT / "plugins/git-kit/skills/git-code-review/references/md-domain-review.md"
P4_MD_DOMAIN_REVIEW = REPO_ROOT / "plugins/p4-kit/skills/p4-code-review/references/md-domain-review.md"
GIT_DECLINED_LEDGER = REPO_ROOT / "plugins/git-kit/skills/git-code-review/references/declined-ledger.md"
P4_DECLINED_LEDGER = REPO_ROOT / "plugins/p4-kit/skills/p4-code-review/references/declined-ledger.md"

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
# SUBJECT-LENS md-domain CONTRIBUTOR (deliverable of this phase, shared).
# ---------------------------------------------------------------------------
# When skills-kit's md-domain skill is available, the code-review skills hand it
# the changed CLAUDE.md / SKILL.md files as a SUBJECT-lens reviewer: those files
# are claimed out of the generic fan-out (via prepare's `--claim`) and audited
# by md-domain's headless per-artifact detect lanes (workflow/*-detect.js), whose
# findings render as their own labeled section. When md-domain is absent, behavior
# is exactly today's -- the md files get thin generic data_only coverage. All three
# regions below are SHARED verbatim by both VCS skills. The heavy
# args/plugin-root/fallback detail lives in the generated
# references/md-domain-review.md so the step prose stays legible.
# ===========================================================================

# Injected into step 2's action (the prepare invocation) via the STEP2 fragments.
# Uses a plain-text sentinel (__CLAIM_PROBE__) substituted at module-def time so
# it never collides with the @TOKEN@ render pass.
CLAIM_PROBE = """\
            Claim probe -- decide the `--claim` flags BEFORE invoking prepare, and invoke prepare
            only ONCE. Check whether skills-kit's md-domain skill is available in this session (it
            appears in the available-skills list as `skills-kit:md-domain`). If it IS available, add
            `--claim '**/*.md'` to the prepare
            invocation below so EVERY changed Markdown file (any `.md` at any depth, root included --
            CLAUDE.md, SKILL.md, a skill's `references/*.md`, and generic docs alike) is held back
            from the generic reviewers and
            returned under `bundle.claimed_files` (each with a materialized `pre_image`) for the
            subject-lens md-domain pass in step 6. A skill's `references/*.md` IS claimed: the
            `audit_skill` lane owns both of the `skill` artifact's subject shapes and reads a
            reference document's prose under skill-standards.md section 10. THE RULE: never claim a
            shape no lane can audit, because a declined file returns NOT-AUDITED and a caller can
            misread it as a pass. A second `!**/skills/*/references/*.md` glob is NOT part of the
            default claim; it survives only as the step-6 skill-reference-skew compatibility shim,
            and references/md-domain-review.md carries both that tier and why the shape was once
            excluded. The single
            `**/*.md` glob supersedes the older two-glob form; `.md.html`
            (Markdeep) is NOT `.md`, so it is deliberately left to the generic reviewers. If
            md-domain is NOT available, invoke
            prepare with NO `--claim` flags -- degrade silently to today's behavior (the md files get
            thin generic data_only coverage), noting the degradation in one line. Do NOT run prepare
            twice."""

# Inserted into step 6's action, right after the dispatch rule.
MD_DOMAIN_LAUNCH = """\
            Triviality gate (pure-mechanical, decided by prepare_review -- do NOT re-judge it):
            each `bundle.claimed_files` entry carries `trivial` (bool) and `trivial_reasons` (the
            disqualifier codes when false). Partition the claimed files into TRIVIAL (`trivial == true`)
            and NON-TRIVIAL. Only the NON-TRIVIAL claimed files are audited below; a TRIVIAL file is
            NEVER sent to a detect lane and NEVER written to the ledger -- it gets the mechanical-checks
            line in step 9 instead. If EVERY claimed file is trivial AND `bundle.diff_chunks` is empty (no
            generic reviewer chunks either), skip the reviewer fan-out AND this md-domain pass ENTIRELY --
            launch nothing -- and jump to step 9 to render the mechanical-checks / audit-skipped section.
            The gate is mechanical memory, not a verdict: never label a skipped file DIFF-CLEAN and never
            present the skip as an audit. If the author or user explicitly asks for the full review,
            ignore the gate and audit every claimed file.
            Subject-lens md-domain pass -- run ONLY when at least one NON-TRIVIAL claimed file exists (per
            the triviality gate above); skip this entire paragraph otherwise. In the SAME message that
            launches the reviewer subagents (or the reviewer Workflow, per the dispatch rule above), ALSO
            invoke the Workflow tool with md-domain's headless detect lanes for the NON-TRIVIAL claimed
            files, routed THREE ways by basename (plus one path-shape rule) -- at
            most THREE Workflow calls total: (a) every claimed file named `CLAUDE.md` -> the
            `audit_claude_md` lane's `skills/md-domain/workflow/claude-md-detect.js`; (b) every claimed
            file named `SKILL.md` OR sitting inside a `*/skills/<name>/references/` folder -> the
            `audit_skill` lane's `skills/md-domain/workflow/skill-detect.js`
            (only if any; that lane owns BOTH subject shapes and picks the criteria set per file from
            the path); (c) every OTHER claimed `.md` file (generic docs) -> the `audit_project_doc`
            lane's `skills/md-domain/workflow/project-doc-detect.js` (only if any). Pass `review: true`
            and, per claimed
            file, `preImagePath` = its `pre_image` from the bundle (null for an add), with the per-lane
            `files[]` fields (CLAUDE.md: role / dimension / parentPath / ancestorClaudeMdPaths; SKILL.md
            and skill reference: ancestorClaudeMdPaths; project-doc: ancestorClaudeMdPaths) resolved from each claimed file's
            `claude_mds` per references/md-domain-review.md. Resolve the skills-kit plugin root and
            venvPython defensively per that reference. On a skills-kit version skew (a detect lane
            entry point or documented args contract missing, OR an installed `audit_skill` lane that
            predates the skill-REFERENCE subject), do NOT guess -- re-run prepare_review.py
            per the THREE-TIER fallback in references/md-domain-review.md (broad skew re-runs with no
            `--claim`; project-doc-only skew keeps the CLAUDE.md / SKILL.md / skill-reference
            claims; skill-reference skew re-adds the `!**/skills/*/references/*.md` exclusion as a
            compatibility shim). The third
            tier is detected by CAPABILITY -- `## 10. Skill reference documents` present in the
            installed `references/standards/skill-standards.md` -- because an older lane ships the
            same entry point and args contract and would otherwise decline the file silently. Those
            are the only sanctioned second prepare invocations.
            Then proceed with the normal fan-out. When the pass runs, the md-domain Workflow(s) execute in
            PARALLEL with the reviewer fan-out; keep each `{perFile, totals, review}` for step 9's labeled
            section."""

# Inserted into step 9's action, right after the unresolved-work section.
MD_DOMAIN_REPORT = """\
            - When the md-domain subject-lens pass ran (bundle.claimed_files was non-empty and the
              Workflow did NOT fall back), render its results as a distinct, clearly LABELED section
              titled `## md-domain (subject-lens) findings`, kept SEPARATE from the code-review issue
              list -- never merge the two. For each file in the md-domain `perFile` result, show its
              verdict (DIFF-CLEAN, NON-COMPLIANT, or NOT-AUDITED -- the last is a DECLINE, not a
              pass: state plainly that the file was not reviewed and name the auditor its routing
              finding points at) and, beneath it, each finding's severity, bucket,
              attributable flag, message, and remediation proposal. A SINGLE decision pass covers BOTH
              this section and the code-review issues; accepted md-domain remediations are applied as
              normal edits AFTER decisions. If the md-domain pass fell back to the generic review, do NOT
              render this section (the md files were reviewed as ordinary subjects).
            - Mechanical checks (audit-skipped) section: for every claimed file with `trivial == true`,
              render a distinct `## Mechanical checks (audit skipped)` section -- kept SEPARATE from both
              the code-review issues and the md-domain findings. For each such file, state in one line what
              was verified mechanically (the change is typo-sized -- <= 5 changed lines; Markdown structure
              unchanged; no link/path/anchor reference changed; no meaning-bearing keyword touched; no
              YAML/front-matter touched) plus its `trivial_checks` results (`ascii_clean`, `no_abs_paths`),
              then state plainly that the full audit was SKIPPED because the change is mechanical. NEVER
              call this DIFF-CLEAN and NEVER present it as an audit result; write NOTHING to the ledger for
              a skipped file. If the author or user asks for the full review, run the md-domain pass on these
              files instead of this section. Render this section whenever any claimed file is trivial --
              including the all-trivial fast path where the rest of the review was skipped.
            - Ruleset self-reference notice: if any claimed CLAUDE.md with a pending or accepted md-domain
              change lies on the ancestor chain of OTHER changed files in this review -- a cheap
              path-prefix check of that CLAUDE.md's directory against bundle.unique_claude_mds and the
              other changed files' paths -- print a one-line notice: "ruleset changed -- findings for
              <files> were judged against the working-tree version; consider a re-run." Keep it to one
              line; it is advisory, not a blocker."""

# Appended to both gotcha blocks (plain text -- no f-string braces).
MD_DOMAIN_GOTCHAS = """
        - md-domain findings are a SEPARATE, labeled section -- never interleave them with the code-review issue list. They come from md-domain's detect lanes (a subject-lens reviewer), not from the generic reviewer/validator subagents, so they are not filtered by the validators.
        - The claim decision happens ONCE, at the step-2 probe: md-domain available -> `--claim '**/*.md'` (one glob covering CLAUDE.md, SKILL.md, a skill's `references/*.md`, and generic docs); md-domain absent -> no `--claim`. Claiming a skill's `references/*.md` assumes the INSTALLED audit_skill lane owns that subject shape; these kits declare no version constraint on skills-kit, so step 6 probes for it by capability and the skill-reference skew tier re-adds the exclusion when it is missing. Do not run prepare a second time just to add claims -- the only re-runs are the version-skew FALLBACKS (broad skew re-runs WITHOUT `--claim`; project-doc-only skew re-runs with `--claim '**/CLAUDE.md' --claim '**/SKILL.md' --claim '**/skills/*/references/*.md'`; skill-reference skew re-adds the `!**/skills/*/references/*.md` exclusion as a compatibility shim).
        - Claimed `.md` files route THREE ways in step 6 -- `CLAUDE.md` -> the `audit_claude_md` lane; `SKILL.md` OR a file inside a `*/skills/<name>/references/` folder -> the `audit_skill` lane (its two subject shapes); every other `.md` -> the `audit_project_doc` lane (full routing table in references/md-domain-review.md; `.md.html` is never claimed). Never claim a shape no lane can audit: a declined file comes back NOT-AUDITED, which a caller can misread as a pass.
        - A `NOT-AUDITED` verdict from a lane is NOT a pass. It means the lane declined the file as outside its criteria and read nothing. Render it as its own line, never fold it into the clean count, and never let it satisfy a submit gate -- treat it like the `## Mechanical checks (audit skipped)` section: an honest "not reviewed", not a result. Seeing one on a claimed file means the claim routing sent a file somewhere that cannot audit it; report that rather than accepting the verdict.
        - When skills-kit md-domain is absent the whole mechanism degrades silently: no `--claim`, no claimed_files, no md-domain section -- the md files get today's thin generic data_only coverage. Note the degradation in one line; do not treat it as an error.
        - The triviality gate is pure-mechanical and decided by prepare_review (per-claimed-file `trivial` / `trivial_reasons`); the skill never re-judges it. A TRIVIAL claimed file is reported via the mechanical-checks line and is NEVER sent to a detect lane or written to the ledger. When EVERY claimed file is trivial and there are no generic diff chunks, the whole audit is skipped -- render the `## Mechanical checks (audit skipped)` section, never a DIFF-CLEAN verdict, and never present the skip as an audit. A user or author asking for the full review overrides the gate.
        - The Workflow tool is unavailable inside subagents. Launch the md-domain detect-lane Workflow from the MAIN session (the same message that fans out the reviewers), never from within a reviewer subagent."""


# ===========================================================================
# MACHINE-EMITTED ARTIFACTS (shared by BOTH skills).
# ---------------------------------------------------------------------------
# prepare_review detects a machine-emitted file on either of two INDEPENDENT
# axes -- a CONTENT banner in its leading lines, or a DECLARED PATH a plugin
# writes (a project's durable plugin-data directory, a manifest write target) --
# and excludes it from the diff chunks entirely, surfacing it under
# `bundle.machine_emitted_files` with the axis that matched. Neither axis subsumes the
# other: a generator may emit no banner at all, and then only its location says a
# tool wrote it. Size is never a criterion. Reviewing generator OUTPUT is waste:
# nobody wrote a line of it, and the only meaningful review target is the
# GENERATOR, which is reviewed separately as ordinary source. The skill's job is
# to say so honestly -- an excluded file is NOT a pass.
# ===========================================================================

# Inserted into step 9's action, after the md-domain report region.
GENERATED_REPORT = """\
            - Machine-emitted artifacts section: if `bundle.machine_emitted_files` is non-empty, render a distinct
              `## Machine-emitted artifacts (not reviewed)` section -- kept SEPARATE from the code-review
              issues, the md-domain findings, and the mechanical-checks section. One line per entry:
              its path (`identifier`), its `size_bytes`, and WHY it was excluded -- `machine_emitted_axis`
              (`content` = a generated-artifact banner matched; `declared_path` = it lives under a
              path a plugin declares that it writes) together with the `machine_emitted_signature` naming
              the exact banner or path rule.
              Then state once that these files were NOT reviewed because they are machine-emitted,
              and that review of machine-emitted output belongs on the GENERATOR -- reviewed as ordinary
              source when this change contains it, and otherwise not covered by this review. NEVER
              call a machine-emitted file DIFF-CLEAN, never fold it into the clean count, and never let it
              satisfy a submit gate. If the author or user asks for these files to be reviewed, re-run
              prepare with `--review-machine-emitted` and review them normally instead of rendering this
              section."""

# Appended to both gotcha blocks (plain text -- no f-string braces).
GENERATED_GOTCHAS = """
        - A machine-emitted file is NEVER a pass. `bundle.machine_emitted_files` means "not reviewed", exactly like a `NOT-AUDITED` verdict or the `## Mechanical checks (audit skipped)` section: render it as its own honest line, never inside the clean count, never as DIFF-CLEAN, and never as satisfying a submit gate.
        - Detection is a UNION of two axes, decided by prepare_review, and the skill never re-judges it: `content` (a generated-artifact banner) OR `declared_path` (the file lives under a path a plugin declares that it writes, such as a project's durable plugin-data directory). Either one is sufficient, and the second is what catches a generator that emits no banner at all -- nothing in such a file's bytes says a tool wrote it, but its location does, by construction.
        - Size is NEVER a criterion on either axis. A large hand-written file is chunked and fully reviewed as always; a small machine-emitted file is still excluded. The argument is authorship, not cost.
        - Do not review a machine-emitted artifact by reading it. If its content looks wrong, the finding belongs on the generator, or on the decision to check the artifact in -- say that, and name the generator when this change contains one.
        - `--review-machine-emitted` is the override and it is the AUTHOR's call, never an inference. Pass it only when the user or the author explicitly asks for the machine-emitted files to be reviewed."""


# ===========================================================================
# DECLINED-FINDINGS LEDGER (deliverable of this phase, shared by BOTH skills).
# ---------------------------------------------------------------------------
# Reviews re-run against the same change re-surface findings the author already
# declined -- both generic code-review issues and md-domain subject-lens findings.
# The HOST kits own change identity, so prepare_review.py emits `ledger_hits`
# (previously-declined findings still valid at the current baseline); step 9
# renders matching findings COLLAPSED and does not re-ask them, and a post-
# decision step records newly-declined findings via `--ledger-record`. Shared
# implementation lives in bootstrap_lib.code_review.ledger. A SERIOUS md-domain
# finding is NEVER collapsed (mirrors skills-kit's reducer rule). All regions
# below are SHARED verbatim by both VCS skills; the key/baseline detail lives in
# the generated references/declined-ledger.md so the step prose stays legible.
# ===========================================================================

# Inserted into step 9's action, right after the md-domain report region.
LEDGER_STEP9 = """\
            - Declined-findings ledger: `bundle.ledger_hits` lists findings the author previously
              DECLINED for this same @RANGE_OR_CL@ whose baseline is still valid. Before the decision
              pass, compute each current code-review issue's and md-domain finding's ledger key
              (code-review: file + reason + normalized-description anchor; md-domain: file + criterion +
              taxonomy + normalized-message anchor -- never line numbers or exact wording; see
              references/declined-ledger.md) and, when it matches a `bundle.ledger_hits` entry, render it
              COLLAPSED under a one-line `previously declined (N): <labels>` note in its own section
              (code-review issues under the issue list; md-domain findings under the md-domain section)
              and do NOT re-ask it in the decision pass. EXCEPTION: a SERIOUS-severity md-domain finding
              is NEVER collapsed -- it always renders and is always decided, even against a ledger hit.
              The ledger is advisory memory, not a gate."""

# A dedicated post-decision step (shared body; per-VCS step number, launch
# prefix, and change-noun). For git it is step 10 (git has no other step 10);
# for p4 it is step 11 (after the auto-shelf cleanup step 10).
LEDGER_RECORD_STEP = """\
        - n: @LEDGER_RECORD_N@
          action: |
            Record declined findings so the next review of this same @RANGE_OR_CL@ does not
            re-litigate them. After the decision pass, collect every finding the author DECLINED --
            both code-review issues they rejected and md-domain remediations they chose NOT to apply.
            Skip this step entirely when nothing was declined. Otherwise write a JSON file to
            `<bundle.bundle_dir>/declined.json`:
              {"change_id": "<bundle.change_id>", "baseline": "<bundle.ledger_baseline>",
               "declined": [
                 {"kind": "code_review", "file": "<path>", "reason": "bug"|"claude_md",
                  "description": "<the issue description>"},
                 {"kind": "md_audit", "file": "<path>", "criterion": "<criterion/group>",
                  "taxonomy": "<taxonomy>", "message": "<finding message>", "severity": "<severity>"}
               ]}
            (`"kind": "md_audit"` is the ledger's WIRE value for a md-domain finding -- it is the
            literal `bootstrap_lib.code_review.ledger` accepts; do not rename it.)
            Then run prepare_review.py --ledger-record on that file. The ledger keys each entry by a
            normalized anchor (never line numbers or exact wording) and NEVER records a SERIOUS
            md-domain finding (those always re-surface). Do NOT hand-edit the ledger JSON -- always go
            through --ledger-record so keying stays deterministic.
          tool: @PREPARE_TOOL@
          input: "--ledger-record <bundle.bundle_dir>/declined.json"
"""

# Appended to both gotcha blocks (after the md-domain gotchas). Plain text.
LEDGER_GOTCHAS = """
        - The declined-findings ledger is advisory memory, not a gate. A collapsed finding is one the author already declined for THIS change at THIS baseline; when the baseline moves (@BASELINE_DESC@) the entry goes stale and the finding re-surfaces on its own. Never let a ledger hit suppress a SERIOUS md-domain finding.
        - Record declined findings ONLY through `prepare_review.py --ledger-record <json>`. Never hand-edit ledger.json -- the key normalization (criterion/reason + taxonomy + normalized anchor) must be computed deterministically, not typed."""


# ===========================================================================
# LAUNCH NARRATION (deliverable 1 -- shared by BOTH skills).
# ---------------------------------------------------------------------------
# A short, file-type-driven rationale line emitted ONCE at launch, so a user
# editing docs does not see "code review" spin up and cancel it as a mistake.
# The line is selected purely by the extension MIX of the changed + claimed
# files (content is not read at launch), from a small deterministic table. The
# md_trivial row is emitted instead when the step-6 triviality gate fires.
# ===========================================================================
LAUNCH_NARRATION = """\
    launch_message:
      note: |
        Emit ONE short rationale line at launch -- with, or just before, the step-2 prepare
        narration and BEFORE any reviewer subagent or md-domain Workflow spins up -- so the user
        sees WHY a review is running on THIS change. It exists because a user editing documentation
        can see "code review" launch and cancel it, thinking it a mistake. The table rows below are
        the exact lines to emit -- copy the selected one verbatim.
      selection: |
        Selection is by FILE TYPE, never by content (nothing is read yet). After prepare returns,
        compute the extension set over EVERY changed AND claimed file, then pick the FIRST matching row:
          - every file ends in .md                                   -> all_md
          - every file is config/data (.yaml/.yml/.csv/.json/.tsv)   -> all_data
          - at least one .md AND at least one code file              -> mixed
          - otherwise                                                 -> all_code (default)
        Override: when the step-6 triviality gate fires (every claimed file `trivial` AND no generic
        diff chunks -- an all-mechanical change), emit the `md_trivial` row INSTEAD of the row above.
        Emit exactly one line; do not repeat it later in the run.
      table:
        all_md: "Running @NAME@: this audits .md file changes against project standards and verifies references."
        all_data: "Running @NAME@: this checks the changed config files for schema, reference, and consistency problems."
        mixed: "Running @NAME@: this reviews the code changes and audits the .md changes against project standards."
        all_code: "Running @NAME@: this reviews the changes for bugs and project-standard compliance."
        md_trivial: "Running @NAME@: the .md changes are mechanical (typo-sized); running quick standards checks only."
      style: |
        State what is running and what it does, in plain short sentences, and let the reader draw the
        conclusion. Two anti-patterns are BANNED (they read as defensive and invite the doubt they try
        to pre-empt):
          - Negative direction -- "don't stop this", "do not skip", "this will only take a second".
            Never tell the reader what NOT to do.
          - Asserting it is not a mistake -- "this is not an error", "don't worry, this is intentional".
            Informing plainly already makes that self-evident; asserting it invites doubt.
"""

# Appended into both step-2 actions (shared) so the launch line is emitted right
# after prepare returns, before the step-6 fan-out. Plain text, no @tokens@.
LAUNCH_EMIT = """\
            After prepare returns, emit the launch rationale line ONCE (see narration.launch_message):
            select the row from the file-type mix of the changed + claimed files, or the md_trivial row
            when the step-6 triviality gate will fire. This is the single launch message -- do not repeat it."""


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
@MD_DOMAIN_LAUNCH@
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
@MD_DOMAIN_REPORT@
@GENERATED_REPORT@
@LEDGER_STEP9@
            Group the review body by file.
@STEP10@@LEDGER_RECORD_STEP@      checklist:
@CHECKLIST@
      gotchas:
@GOTCHAS@
  narration:
    note: Reviews involve long silent stretches (batched file reads, parallel subagents that take 30s+). Post one short status line per step using these templates verbatim, filling in the bracketed counts. Do not paraphrase, omit, or add extras.
    templates:
@NARRATION_TEMPLATES@
    variables:
@NARRATION_VARIABLES@
@LAUNCH_NARRATION@
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
__LAUNCH_EMIT__
          tool: ${CLAUDE_PLUGIN_ROOT}/scripts/prepare_review.py
          input: "<range or argument from step 1>  (append `--claim '**/*.md'` when md-domain is available, per the claim probe)"
          expected: |
            JSON with vcs, range, head_sha, branch, description, bundle_dir, diff_chunks, changed_files, unique_claude_mds, untracked_or_unstaged, merge_conflicts, submit_gates, change_id, ledger_baseline, ledger_hits, -- only when --claim was passed -- claimed_files, and -- only when a changed file was detected as machine-emitted -- machine_emitted_files (each entry carries identifier, local, size_bytes, and the axis that matched -- machine_emitted_axis `content` or `declared_path` plus the naming machine_emitted_signature; such files are excluded from diff_chunks and changed_files, and `--review-machine-emitted` turns that exclusion off). The raw diff text is NOT inline -- it lives in per-chunk files at `<bundle_dir>/<diff_chunks[i].path>` (paths are relative to bundle_dir). Each `changed_files` entry carries `chunk_index` pointing to the chunk that contains its diff.
          on_failure: Surface the stderr message to the user and stop. No retry.""".replace(
    "__CLAIM_PROBE__", CLAIM_PROBE
).replace(
    "__LAUNCH_EMIT__", LAUNCH_EMIT
)

P4_STEP2 = """\
        - n: 2
          action: |
__CLAIM_PROBE__
            Then run prepare_review.py to fetch the diff (with shelved fallback; auto-shelves a pending CL with no existing shelf so the diff is fetchable), partition the diff into chunked .diff fragments on disk, map ancestor CLAUDE.md files for each changed file, detect unreconciled files in the directories the CL touches, detect unresolved merges in the CL, and scan ancestor CLAUDE.md files for submit-gate reminders that apply to this CL.
__LAUNCH_EMIT__
          tool: python3 ${CLAUDE_PLUGIN_ROOT}/scripts/prepare_review.py
          input: "<CL>  (append `--claim '**/*.md'` when md-domain is available, per the claim probe)"
          expected: |
            JSON with cl, description, bundle_dir, diff_chunks, changed_files, unique_claude_mds, unreconciled, unresolved, submit_gates, auto_shelved, shelf_fingerprint, change_id, ledger_baseline, ledger_hits, -- only when --claim was passed -- claimed_files, and -- only when a changed file was detected as machine-emitted -- machine_emitted_files (each entry carries identifier, local, size_bytes, and the axis that matched -- machine_emitted_axis `content` or `declared_path` plus the naming machine_emitted_signature; such files are excluded from diff_chunks and changed_files, and `--review-machine-emitted` turns that exclusion off). The raw diff text is NOT inline -- it lives in per-chunk files at `<bundle_dir>/<diff_chunks[i].path>` (paths are relative to bundle_dir). Each `changed_files` entry carries `chunk_index` pointing to the chunk that contains its diff. `auto_shelved=true` means prepare_review created the shelf and step 10 must clean it up.
          on_failure: |
            Surface the stderr message to the user and stop. No retry.
            Launch note: ALWAYS invoke with an explicit `python3` interpreter (as shown in `tool:`), never as a bare path. Bare `${CLAUDE_PLUGIN_ROOT}/scripts/prepare_review.py <CL>` lets bash try to run the file as a shell script -- it has no shebang line in older checkouts and the exec bit does not survive on Windows checkouts, so bash parses the Python as sh and exits 2. The script self-relocates under the p4-kit venv via reexec, so any python3 launcher is sufficient. And NEVER pipe the invocation (`... | tail`, `... | head`): a pipe makes `$?` the last pipeline stage's status, not the script's, which silently masks a launch failure as success.""".replace(
    "__CLAIM_PROBE__", CLAIM_PROBE
).replace(
    "__LAUNCH_EMIT__", LAUNCH_EMIT
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
        - Launch rationale line emitted once (file-type-driven; md_trivial variant when the change is all-mechanical)
        - md-domain subject-lens pass launched for the NON-TRIVIAL bundle.claimed_files when skills-kit md-domain is available (or claimed files folded back into the generic review on version-skew fallback); skipped silently when md-domain is absent
        - Trivial claimed files (prepare's `trivial` flag) reported via the `## Mechanical checks (audit skipped)` section, never as an audit or DIFF-CLEAN; nothing written to the ledger for them; whole review skipped when every claimed file is trivial and there are no generic diff chunks
        - Machine-emitted artifacts (bundle.machine_emitted_files) reported via the `## Machine-emitted artifacts (not reviewed)` section, naming each file's exclusion axis (content banner or declared plugin-write path) and the rule that matched, never as an audit or DIFF-CLEAN; review of machine-emitted output belongs on the generator
        - Previously-declined findings collapsed via the ledger (bundle.ledger_hits); SERIOUS md-domain findings never collapsed
        - Markdown rendered to chat (Submit checklist section prepended when gates applied; Unresolved merge conflicts section prepended when bundle.merge_conflicts is non-empty; separate `## md-domain (subject-lens) findings` section when the md-domain pass ran)
        - Newly declined findings recorded to the ledger via `prepare_review.py --ledger-record` (skipped when nothing was declined)"""

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
        - Launch rationale line emitted once (file-type-driven; md_trivial variant when the change is all-mechanical)
        - md-domain subject-lens pass launched for the NON-TRIVIAL bundle.claimed_files when skills-kit md-domain is available (or claimed files folded back into the generic review on version-skew fallback); skipped silently when md-domain is absent
        - Trivial claimed files (prepare's `trivial` flag) reported via the `## Mechanical checks (audit skipped)` section, never as an audit or DIFF-CLEAN; nothing written to the ledger for them; whole review skipped when every claimed file is trivial and there are no generic diff chunks
        - Machine-emitted artifacts (bundle.machine_emitted_files) reported via the `## Machine-emitted artifacts (not reviewed)` section, naming each file's exclusion axis (content banner or declared plugin-write path) and the rule that matched, never as an audit or DIFF-CLEAN; review of machine-emitted output belongs on the generator
        - Previously-declined findings collapsed via the ledger (bundle.ledger_hits); SERIOUS md-domain findings never collapsed
        - Markdown rendered to chat (Submit checklist section prepended when gates applied; Unresolved merges section prepended when bundle.unresolved is non-empty; separate `## md-domain (subject-lens) findings` section when the md-domain pass ran)
        - Auto-shelf cleanup invoked when bundle.auto_shelved is true (`prepare_review.py --cleanup <bundle_dir>`)
        - Newly declined findings recorded to the ledger via `prepare_review.py --ledger-record` (skipped when nothing was declined)"""

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
        - Detached HEAD with no main/master fallback is a real failure mode; surface the error and ask for an explicit range. Do not guess at a "probably right" base.""" + MD_DOMAIN_GOTCHAS + GENERATED_GOTCHAS + LEDGER_GOTCHAS

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
        - Auto-shelf cleanup (step 10) must run whenever `bundle.auto_shelved` is true, no matter what happened in steps 3-9. The cleanup script is deterministic and safe (it only deletes the shelf when the live fingerprint exactly matches what we recorded), so there is no scenario where skipping it is the right call. Skipping leaves an orphan shelf the author didn't ask for.
        - --claim requires a PENDING CL. On a submitted CL, `#have` pre-images are POST-change once the workspace synced past the CL, so prepare_review exits with an error when --claim is passed on a submitted CL; re-run without --claim for a plain informational review.""" + MD_DOMAIN_GOTCHAS + GENERATED_GOTCHAS + LEDGER_GOTCHAS

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
        "PREPARE_TOOL": "${CLAUDE_PLUGIN_ROOT}/scripts/prepare_review.py",
        "LEDGER_RECORD_N": "10",
        "BASELINE_DESC": "the range base SHA advances -- origin/main moves, or HEAD changes for a working-tree review",
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
        "PREPARE_TOOL": "python3 ${CLAUDE_PLUGIN_ROOT}/scripts/prepare_review.py",
        "LEDGER_RECORD_N": "11",
        "BASELINE_DESC": "the CL is reshelved, its content edited, or its revisions move",
    },
}

# Shared tokens (identical for both VCS): the dispatch rule, the md-domain
# contributor regions, and the glyphs.
_SHARED = {
    "DISPATCH": DISPATCH,
    "MD_DOMAIN_LAUNCH": MD_DOMAIN_LAUNCH,
    "MD_DOMAIN_REPORT": MD_DOMAIN_REPORT,
    "GENERATED_REPORT": GENERATED_REPORT,
    "LEDGER_STEP9": LEDGER_STEP9,
    "LEDGER_RECORD_STEP": LEDGER_RECORD_STEP,
    "LAUNCH_NARRATION": LAUNCH_NARRATION,
    "X": X,
    "CHK": CHK,
    "CRS": CRS,
}

_SKILL_TOKEN_ORDER = [
    "DISPATCH",  # multi-line, contains no other @tokens@; substitute first
    "MD_DOMAIN_LAUNCH", "MD_DOMAIN_REPORT",  # shared, no nested @tokens@
    "GENERATED_REPORT",  # shared, no nested @tokens@
    # Ledger regions: shared bodies that DO carry nested per-VCS @tokens@
    # (@RANGE_OR_CL@, @PREPARE_TOOL@, @LEDGER_RECORD_N@, @BASELINE_DESC@) --
    # substitute the region first, then those tokens resolve below.
    "LEDGER_STEP9", "LEDGER_RECORD_STEP",
    # Launch-narration block: shared body carrying a nested @NAME@ -- substitute
    # the block first, then NAME resolves below.
    "LAUNCH_NARRATION",
    "NAME", "DESC", "TITLE", "INTRO", "IDENTITY",
    "SCOPE_COVERS_HEAD", "SCOPE_EXCLUDES", "KEYWORDS", "GOAL", "PRECONDITIONS",
    "STEP1", "STEP2", "STEP3", "STEP5_PHRASE", "STEP9_TAIL", "STEP10",
    "CHECKLIST", "GOTCHAS", "NARRATION_TEMPLATES", "NARRATION_VARIABLES",
    "DIFF_OR_CL", "RANGE_OR_CL", "FILEPATHS", "CHANGE_DESC", "ISSUE_PATH",
    "SG_DESC", "OUTPUT_FORMAT", "PREPARE_TOOL", "LEDGER_RECORD_N", "BASELINE_DESC",
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
# md-domain-review.md -- one parameterized source rendering both references.
# The full args/plugin-root/fallback detail the SKILL step-6/step-9 prose points
# at, kept out of the drift-tested SKILL body so that body stays legible.
# ===========================================================================
MD_DOMAIN_REVIEW_TEMPLATE = """\
# Subject-lens md-domain contributor

When skills-kit's md-domain skill is available in the session, `@SKILL_NAME@` treats it
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
materialized before-image via @PREIMAGE_ORIGIN@, or `null` for an add), and `claude_mds` (the
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
"""

MD_DOMAIN_REVIEW_FRAGMENTS = {
    "git": {
        "SKILL_NAME": "git-code-review",
        "PREIMAGE_ORIGIN": "`git show <range-base>:<path>`",
    },
    "p4": {
        "SKILL_NAME": "p4-code-review",
        "PREIMAGE_ORIGIN": "`p4 print -q -o <dest> //depot/path#have`",
    },
}


def render_md_domain_review(vcs: str) -> str:
    out = MD_DOMAIN_REVIEW_TEMPLATE
    for token, value in MD_DOMAIN_REVIEW_FRAGMENTS[vcs].items():
        out = out.replace(f"@{token}@", value)
    return out


# ===========================================================================
# declined-ledger.md -- one parameterized source rendering both references.
# The key/baseline/collapse/record detail behind the step-9 collapse region and
# the post-decision record step, kept out of the drift-tested SKILL body.
# ===========================================================================
DECLINED_LEDGER_TEMPLATE = """\
# Declined-findings ledger

Reviews re-run against the same change re-surface findings the author already
declined -- both generic code-review issues and md-domain subject-lens findings.
`@SKILL_NAME@` keeps a small ledger so a re-run renders those previously-declined
findings COLLAPSED instead of re-litigating them. The ledger is advisory memory,
NOT a gate: it never changes a verdict, only whether a finding is re-asked.

Shared implementation: `bootstrap_lib.code_review.ledger` (consumed by both
git-code-review and p4-code-review via prepare_review.py, like the rest of the
pipeline). This doc is the operational detail behind step 9's collapse region
and the post-decision `--ledger-record` step.

## Change identity + baseline

- `change_id` (`bundle.change_id`) = @CHANGE_ID_LEDGER@. It is the outer ledger
  key -- entries are bucketed per change.
- `baseline` (`bundle.ledger_baseline`) = @BASELINE_LEDGER@. Every recorded entry
  stores the baseline it was declined at; on a later run prepare_review recomputes
  the current baseline and emits only entries whose baseline STILL MATCHES as
  `bundle.ledger_hits`. When the baseline moves the entry is stale and the finding
  re-surfaces (it is re-asked, and `record_declined` prunes it).

## The key (aligned with skills-kit attribution)

A finding is keyed by criterion/reason + taxonomy + a NORMALIZED anchor -- never
line numbers, never exact wording (both churn on trivial edits):

- code-review issue: `file` + `reason` (`bug`|`claude_md`) + normalized-`description` anchor.
- md-domain finding: `file` + `criterion` + `taxonomy` + normalized-`message` anchor.
  (Its wire `kind` in `declined.json` is the literal `md_audit` -- the value
  `bootstrap_lib.code_review.ledger` keys on. Do not rename it.)

The normalized anchor is the lowercased first 8 alphanumeric tokens of the
message/description. File paths are lowercased + posix-slashed for matching.

**Limits (know them).** The anchor is a lossy fingerprint. Two distinct findings
that share file + criterion/reason and open with the same 8 tokens collapse to one
key (false merge); a finding reworded in its FIRST 8 tokens gets a new key and
re-surfaces (false miss). Both degrade only to "asked once more" / "not re-asked
once" -- never to a wrong verdict -- which is why the ledger is advisory.

## Collapse (step 9)

For each current issue / md-domain finding, compute its key and check
`bundle.ledger_hits`. On a match, render it COLLAPSED under a one-line
`previously declined (N): <labels>` note in its own section and do not re-ask it
in the decision pass. EXCEPTION: a **SERIOUS** md-domain finding is NEVER collapsed
-- it always renders and is always decided. (SERIOUS findings are never written to
the ledger in the first place, so a hit can never exist for one; the collapse rule
is belt-and-braces.)

## Record (post-decision step)

After the decision pass, collect the findings the author DECLINED (code-review
issues rejected + md-domain remediations not applied), write them to
`<bundle.bundle_dir>/declined.json`, and run:

    @PREPARE_TOOL@ --ledger-record <bundle.bundle_dir>/declined.json

The payload is `{change_id, baseline, declined:[{kind, file, ...}, ...]}` using
`bundle.change_id` and `bundle.ledger_baseline`. `--ledger-record` computes keys
deterministically, drops SERIOUS md-domain findings, prunes stale entries for the
change, and dedups by key. NEVER hand-edit the ledger JSON -- always go through
`--ledger-record`.

## Storage

A single JSON file in the plugin's version-independent data dir, a sibling of the
per-change bundle dirs: `@LEDGER_STORE@`. Never written into the user's repo
working tree. Shape:

    {"version": 1, "changes": {"<change_id>": {"entries": [<entry>, ...]}}}
"""

DECLINED_LEDGER_FRAGMENTS = {
    "git": {
        "SKILL_NAME": "git-code-review",
        "CHANGE_ID_LEDGER": "the diff range spec (e.g. `origin/main..HEAD`)",
        "BASELINE_LEDGER": "the range base SHA (`git rev-parse <base>`)",
        "PREPARE_TOOL": "${CLAUDE_PLUGIN_ROOT}/scripts/prepare_review.py",
        "LEDGER_STORE": "~/.claude/plugins/data/plugins-kit/git-kit/reviews/ledger.json",
    },
    "p4": {
        "SKILL_NAME": "p4-code-review",
        "CHANGE_ID_LEDGER": "the CL number",
        "BASELINE_LEDGER": (
            "a hash over the CL's shelf fingerprint (content) plus its per-file "
            "(rev, action) map (identity)"
        ),
        "PREPARE_TOOL": "python3 ${CLAUDE_PLUGIN_ROOT}/scripts/prepare_review.py",
        "LEDGER_STORE": "~/.claude/plugins/data/plugins-kit/p4-kit/reviews/ledger.json",
    },
}


def render_declined_ledger(vcs: str) -> str:
    out = DECLINED_LEDGER_TEMPLATE
    for token, value in DECLINED_LEDGER_FRAGMENTS[vcs].items():
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
        GIT_MD_DOMAIN_REVIEW: render_md_domain_review("git"),
        P4_MD_DOMAIN_REVIEW: render_md_domain_review("p4"),
        GIT_DECLINED_LEDGER: render_declined_ledger("git"),
        P4_DECLINED_LEDGER: render_declined_ledger("p4"),
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
