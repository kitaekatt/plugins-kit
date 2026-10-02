"""gen_code_review_skills.py -- single source for the two code-review skills.

git-kit:git-code-review and p4-kit:p4-code-review run the SAME multi-agent
review pipeline (identical review_profiles, subagents, false_positive_guardrails,
agent_assumptions, issue_format, submit_gates.rendering, narration note). Only
the VCS front-half differs: target identity (git range-auto-detect vs p4 CL),
fold-in mechanics (git add/commit vs p4 reconcile), the unresolved-work wording
(merge conflicts vs pending resolves), a p4-only step 10 (auto-shelf cleanup)
plus its launch gotcha, and the output header line.

Historically the shared back-half drifted by accident -- a fix landed in one
kit's SKILL.md and never reached the other (findings G6/G7 of the 2026-06-09
architecture review). This generator makes that structurally impossible: ONE
template + per-VCS substitutions render both skills, their shared references,
and their effort agents. The rendered files are committed (skills must
stay readable on disk); a drift guard (--check) asserts the committed output is
byte-identical to what the template renders -- the same enforcement idea as
plugins/skills-kit/scripts/gen_workflow_js.py.

This tool spans two plugins, so it lives in the repo-level scripts/ dir next to
the other cross-plugin tooling (regen_marketplace.py, publish.py, dev-tree.py),
NOT inside either plugin. It writes files under both plugins; it does not move
content across the plugin boundary (the rendered files stay in their own
plugins), so the "plugin boundaries are hard boundaries" rule is respected --
this is shared tooling, not a relocated skill.

Edit flow: change the template or a fragment below, regenerate, and commit every
rendered file together.

Usage:
    uv run python scripts/gen_code_review_skills.py            # rewrite rendered files
    uv run python scripts/gen_code_review_skills.py --check    # exit 1 on drift, write nothing

The drift guard is wired into the test suite at
tests/bootstrap/code_review/test_skill_drift.py (byte-identity of every rendered
target), mirroring tests/skills-kit/test_workflow_js_drift.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The canonical reviewer prompts live in bootstrap_lib, NOT here, because the
# endpoint dispatch path imports them at run time. Rendering them into SKILL.md
# from that same source is what stops the Agent path and the endpoint path from
# reviewing the same diff by two different standards -- and the drift test
# (tests/bootstrap/code_review/test_skill_drift.py) turns "edited the prompt and
# forgot to regenerate" into a suite failure.
sys.path.insert(0, str(REPO_ROOT / "plugins" / "bootstrap"))
from bootstrap_lib.code_review import lane_prompts  # noqa: E402
from bootstrap_lib.code_review.review_profiles import EFFORT_LEVELS  # noqa: E402
from bootstrap_lib.env_var_check import plugin_root_env_var_name  # noqa: E402
from bootstrap_lib.interpreter_env import PLUGIN_CALL_SITE_EXPR  # noqa: E402

# The launcher form for a plugin script that re-execs into its own
# bootstrap-provisioned venv (interface-v3 section 6): the guarded
# `$BOOTSTRAP_PYTHON` expression, not `uv run --no-project python` -- an agent
# running this from a skill has neither `uv` nor the plugin venv resolved for
# it, and `$BOOTSTRAP_PYTHON` is exported into every bootstrap-managed session
# (see /bootstrap fact python_interpreter and python-interpreter.md). Single
# source for every prepare_review.py launch site rendered below.
# CLAUDE_PLUGIN_ROOT is substituted by Claude Code in a skill's own body, in a
# hooks.json `command:` field and in a `!` preload, but not in references/*.md,
# and it is unset in the Bash tool's environment. Launchers rendered into a
# generated SKILL.md therefore use CLAUDE_PLUGIN_ROOT (the *_SKILL constants);
# launchers rendered into a generated reference anchor on the `<PLUGIN>_ROOT`
# variable the bootstrap engine exports (name derived by
# plugin_root_env_var_name), guarded so an unset variable fails loudly instead
# of running `/scripts/x.py` (the unsuffixed constants). The kit name is the
# per-VCS KIT fragment value.
_KIT_BY_VCS = {"git": "git-kit", "p4": "p4-kit"}
_ROOT_UNSET_HINT = "requires a bootstrap engine pass; run bootstrap run"


def _launcher(vcs: str, script: str, surface: str = "reference") -> str:
    """One review-script launch expression for a rendering surface.

    `surface="skill"` is for text inside a generated SKILL.md, where Claude
    Code substitutes CLAUDE_PLUGIN_ROOT before the command reaches the shell
    (the kit's scripts live at the plugin root, not in the skill directory).
    `surface="reference"` is for a generated references/*.md file, which Claude
    Code does not substitute, so it anchors on the bootstrap-exported
    `<PLUGIN>_ROOT` variable with the loud `:?` guard.
    """
    if surface == "skill":
        root = "${CLAUDE_PLUGIN_ROOT}"
    elif surface == "reference":
        root = "${" + plugin_root_env_var_name(_KIT_BY_VCS[vcs]) + ":?" + _ROOT_UNSET_HINT + "}"
    else:
        raise ValueError(surface)
    return PLUGIN_CALL_SITE_EXPR + " " + chr(34) + root + "/scripts/" + script + chr(34)


PREPARE_LAUNCHER = {v: _launcher(v, "prepare_review.py") for v in _KIT_BY_VCS}
# A YAML `tool:` scalar cannot start with a quoted segment (PLUGIN_CALL_SITE_EXPR's
# own double quotes) and continue unquoted -- wrap the whole value in single
# quotes so it parses as one scalar; YAML strips only the outer pair, so the
# decoded value is still the exact bash command above. Markdown code blocks
# (declined-ledger.md) use the raw, unwrapped PREPARE_LAUNCHER instead, since a
# reader copy-pastes that straight into bash.
PREPARE_LAUNCHER_YAML = {v: "'" + PREPARE_LAUNCHER[v] + "'" for v in _KIT_BY_VCS}
# The same launcher form for the three other review scripts; each re-execs into
# its kit's plugin venv (reexec_under_plugin_venv via the vendored
# bootstrap_guard), so bootstrap's own interpreter is enough to start them.
# They are only ever rendered mid-line (prose, a table cell, a code block, or a
# `tool:` value that starts with other text), so they need no YAML wrapping.
LANE_LAUNCHER = {v: _launcher(v, "run_review_lane.py") for v in _KIT_BY_VCS}
PARSE_LAUNCHER = {v: _launcher(v, "parse_review_lane.py") for v in _KIT_BY_VCS}
RENDER_LAUNCHER = {v: _launcher(v, "render_review_profiles.py") for v in _KIT_BY_VCS}
# SKILL.md variants of the four launchers above (same scripts, harness-substituted root).
PREPARE_LAUNCHER_SKILL = {v: _launcher(v, "prepare_review.py", "skill") for v in _KIT_BY_VCS}
PREPARE_LAUNCHER_YAML_SKILL = {v: "'" + PREPARE_LAUNCHER_SKILL[v] + "'" for v in _KIT_BY_VCS}
LANE_LAUNCHER_SKILL = {v: _launcher(v, "run_review_lane.py", "skill") for v in _KIT_BY_VCS}
PARSE_LAUNCHER_SKILL = {v: _launcher(v, "parse_review_lane.py", "skill") for v in _KIT_BY_VCS}
RENDER_LAUNCHER_SKILL = {v: _launcher(v, "render_review_profiles.py", "skill") for v in _KIT_BY_VCS}
GIT_SKILL = REPO_ROOT / "plugins/git-kit/skills/git-code-review/SKILL.md"
P4_SKILL = REPO_ROOT / "plugins/p4-kit/skills/p4-code-review/SKILL.md"
GIT_AGENTS = REPO_ROOT / "plugins/git-kit/agents"
P4_AGENTS = REPO_ROOT / "plugins/p4-kit/agents"
GIT_SUBMIT_GATES = REPO_ROOT / "plugins/git-kit/skills/git-code-review/references/submit-gates.md"
P4_SUBMIT_GATES = REPO_ROOT / "plugins/p4-kit/skills/p4-code-review/references/submit-gates.md"
GIT_MD_DOMAIN_REVIEW = REPO_ROOT / "plugins/git-kit/skills/git-code-review/references/md-domain-review.md"
P4_MD_DOMAIN_REVIEW = REPO_ROOT / "plugins/p4-kit/skills/p4-code-review/references/md-domain-review.md"
GIT_DECLINED_LEDGER = REPO_ROOT / "plugins/git-kit/skills/git-code-review/references/declined-ledger.md"
P4_DECLINED_LEDGER = REPO_ROOT / "plugins/p4-kit/skills/p4-code-review/references/declined-ledger.md"
GIT_CONFIGURATION = REPO_ROOT / "plugins/git-kit/skills/git-code-review/references/configuration.md"
P4_CONFIGURATION = REPO_ROOT / "plugins/p4-kit/skills/p4-code-review/references/configuration.md"

# Punctuation the rendered files use. These are ASCII, and they have to be:
# the RENDERED SKILL.md files are tracked, so the root CLAUDE.md "ASCII only in
# tracked files" rule reaches them through this generator, and that rule names
# these exact characters -- "no other non-ASCII character is covered, including
# status glyphs such as a check mark or a ballot X". The box-drawing carve-out
# does not apply: none of these draws a diagram.
#
# An earlier revision held the Unicode glyphs here behind a comment claiming
# they were "escaped so THIS source stays ASCII". They were not escaped, so the
# generator was non-ASCII too, and every regeneration rewrote the violation into
# both kits.
X = "x"    # in "reviewer x chunk" / "R x K"
DOT = "|"  # separates the fields of the review header's Branch/HEAD line
CHK = "x"  # submit-gate MET, read as a ticked checkbox
CRS = "!"  # submit-gate NOT MET


# ===========================================================================
# EFFORT AGENTS (shared by BOTH kits).
# ---------------------------------------------------------------------------
# Review-profile validation accepts exactly EFFORT_LEVELS. Each accepted value
# must name a shipped dispatch target, so the generator emits one agent per
# level per kit from this single template and targets() puts all ten under the
# same drift guard as the skills that dispatch to them.
# ===========================================================================
AGENT_TEMPLATE = """\
---
name: review-lane-@EFFORT@
description: >-
  @EFFORT_TITLE@-effort executor for one @KIT@ reviewer or validator lane.
  @SKILL_NAME@ steps 6 and 7 select this agent when the resolved review profile
  states `effort: @EFFORT@` on the model entry that lane runs. It is not
  auto-selected.
effort: @EFFORT@
---

You run ONE reviewer or validator lane of a code review at @EFFORT@ reasoning
effort.

Your prompt carries the lane's own instructions verbatim. A reviewer lane's
instructions are rendered from `bootstrap_lib.code_review.lane_prompts` -- the
same text the endpoint dispatch path sends. A validator lane's instructions are
the skill's `validator` subagent definition. Those instructions are
authoritative and complete:

- Follow them exactly. Do not paraphrase, extend, or reinterpret the lane's
  scope, and do not review for concerns the prompt assigns to another lane.
- Return exactly the output the lane's prompt specifies and nothing else -- no
  preamble, no summary, no commentary about your own effort level.
- As a reviewer, report only issues in the files present in your assigned chunk.

This agent exists solely to bind an effort level. It adds no review criteria of
its own. The lane's model is supplied at the call site and overrides any model
this definition would otherwise imply.
"""

AGENT_FRAGMENTS = {
    "git": {"KIT": "git-kit", "SKILL_NAME": "git-code-review"},
    "p4": {"KIT": "p4-kit", "SKILL_NAME": "p4-code-review"},
}


def render_agent(vcs: str, effort: str) -> str:
    """Render one kit's dispatch-only agent for an accepted effort level."""
    if effort not in EFFORT_LEVELS:
        raise ValueError(f"unsupported effort level: {effort}")
    out = AGENT_TEMPLATE
    values = {
        **AGENT_FRAGMENTS[vcs],
        "EFFORT": effort,
        "EFFORT_TITLE": effort.capitalize(),
    }
    for token, value in values.items():
        out = _substitute(out, f"@{token}@", value)
    return out


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
# LANE ROUTING (model-declaration migration step 5).
# ---------------------------------------------------------------------------
# A reviewer's resolved `model` is a model declaration (bootstrap plugin-dev
# references/model-declaration.md): an ordered list of `{id, effort}` entries,
# each stating its own effort. A declaration with more than one entry is routed
# through llm-scripting-kit's `describe` on the entry ids: the skill prints its
# output verbatim -- which carries `Ranking.rule`, the choice and re-selection
# rule -- and does NOT restate that rule, so the rule is tested once, where it
# is owned (tests/llm-scripting-kit). The chosen id's effort is then looked up
# in the declaration. Each chosen entry is dispatched by its HARNESS: a `claude`
# entry is an Agent subagent of the `<kit>:review-lane-<effort>` type, any other
# runs through the lane runner with `--effort <effort>`.
#
# The edge is DEGRADE: without `describe` the core ids (sonnet, opus, haiku,
# fable -- routable by the harness alone) still route, and every other entry
# is skipped without comment. Skipping is silent; only a declaration with no
# usable entry left is reported, itemised, as a failed lane.
#
# Re-selection never reaches outside the declaration: a lane only ever runs on
# an entry its own configuration named, at the effort that entry states, and
# every choice from a multi-entry declaration is announced and carried into the
# rendered review. A runner refusal (exit 2) is a configuration error, not a
# trigger for re-selection.
# ===========================================================================
LANE_ROUTING = """\
            Lane routing rule (per reviewer): a reviewer's DECLARATION is its resolved
            `model` list, read off the RESOLVED table: entries of the form `{id, effort}`,
            in declared order. Validators are routed by step 7, not by this rule.
            - An entry's EFFORT is the `effort` stated beside its `id` in that list. Ids are
              unique within a declaration, so the chosen id -- a first choice or a
              re-selection alike -- has exactly one effort. Use that value as stated; never
              substitute, raise, or lower it. Step 0 stops the review before this point when
              any entry states no effort, so every entry you route has one.
            - A one-entry declaration has no menu and no announcement: dispatch its entry by
              the entry-harness rule below.
            - For each declaration with two or more entries, run
              `~/.claude/plugins/data/plugins-kit/llm-scripting-kit/.venv/bin/llm-scripting-kit describe <entry>... --caller session`
              (Windows: ~/.claude/plugins/data/plugins-kit/llm-scripting-kit/.venv/Scripts/llm-scripting-kit.exe)
              (the venv path works from every shell; a bare name resolves only inside a Claude Code
              Bash session) with each entry's `id` in
              declared order, plus `--project-root <bundle.project_root>` when the bundle
              has one, plus `--dispatchable transport` when the reviewer is not
              `reviewer_a_claude_md_compliance` or `reviewer_c_introduced_code` (the lane
              runner runs a transport entry for every other reviewer, so describe keeps
              transports in that menu), plus `--self <id>` when one declared entry is the model you are
              running on, so that entry is marked `[author]`. Run every such describe
              before the fan-out, in one message. For each, print its stdout verbatim:
              the menu, its `[default]` mark, and its `Rule:` and `Re-select:` lines (and
              `Independence:` when present). Choose one usable entry as that printed rule
              says, and announce the choice in the form that rule gives, with the
              reviewer name as the unit. The same entry serves every chunk of that
              reviewer. The menu shows only entries that are real on this machine; a
              declared entry it leaves out is skipped, and you say nothing about it.
            - When describe exits 1, its stderr is a JSON `error` itemising every declared
              entry and its disposition: no entry is usable. That reviewer does not run;
              report it in step 9 under `## Lane failures` with that itemised list, and mark
              its coverage missing. Any other non-zero describe exit is reported the same
              way, with its stderr.
            - When `llm-scripting-kit` is not on PATH, or its argument parser rejects
              `describe` as an invalid choice (a release older than 0.46.0), route without a
              menu: take the declaration's `sonnet`, `opus`, `haiku` and `fable` entries in
              declared order, dispatch the first, and skip every other entry without
              comment. A declaration with none of those four does not run; report it in
              step 9 under `## Lane failures`, naming each declared entry, and mark its
              coverage missing.
            Entry-harness rule (per lane, mechanical): an entry whose describe line reads
            `claude/agent` -- or, on a one-entry declaration or the no-menu route, whose id is
            one of `sonnet`, `opus`, `haiku`, `fable` -- launches an Agent subagent with
            `subagent_type: @KIT@:review-lane-<entry effort>` and `model: <entry id>`. Any
            other entry runs as a parallel Bash call to @LANE_TOOL@ instead of launching an
            Agent for it, passing `--lane <reviewer name>`, `--model <entry id>`, `--effort
            <entry effort>`, `--bundle <bundle.bundle_dir>/bundle.json`, and `--chunk-index
            <i>` (that chunk's index in `bundle.diff_chunks`). The runner
            reads the bundle itself and derives everything else -- the chunk diff path,
            that chunk's files, the claimed-file paths reviewer_a alone is entitled to, the
            mechanical-scan records reviewer_a and reviewer_b alone are entitled to, the
            change description, and the project root -- so nothing else needs building by
            hand. Its stdout is a JSON envelope whose `issues` array is that lane's candidate
            issues, in the same shape an Agent lane returns, and whose `effort` field records
            the effort the runner sent.
            Endpoint lanes and Agent lanes go out in the SAME message as one another; mixing
            the two dispatch mechanisms in one fan-out is normal and expected.
            A NON-ZERO exit is never an empty result. Before treating a lane as failed,
            apply the pre-dispatch launch-correction rule in references/configuration.md.
            Exit 2 from the runner is a REFUSAL: it declined the dispatch as configured --
            for example a lane it does not support by name, an effort outside a codex entry's
            effort menu, an effort a transport entry cannot deliver, or an installed
            llm-scripting-kit older than 0.60.0, the first release that accepts `--effort`. A
            refusal the launch-correction rule does not explain is a configuration error for
            the user to fix, not something to work around: report it in step 9 under
            `## Lane failures` with the runner's stderr verbatim and mark coverage missing.
            Do not re-select past it, and do not re-run the entry at another effort.
            For any other failure the launch-correction rule does not explain, follow the
            `Re-select:` line describe
            printed: run describe again with the same arguments plus one `--exclude <entry>`
            per entry this lane has already failed on, print its stdout verbatim, choose,
            and announce as its rule says, with `<reviewer name> chunk <i>` as the unit and
            the prior entry and its failure kind as the reason. Dispatch the chosen entry by
            the entry-harness rule, at the chosen entry's own effort. Each entry is tried at
            most once per lane. On the
            no-menu route, re-select the next of the four core entries in declared order
            and announce it as `route: <reviewer name> chunk <i> -> <entry>; <prior entry>
            failed: <kind>`. A one-entry declaration has nothing to re-select.
            When describe exits 1 on a re-run, or no entry is left to try, the lane has no
            usable entry left: report it in step 9 under `## Lane failures`, naming every
            entry tried and why it failed, and mark coverage missing -- never treat absent
            output as "no issues found".

            Effort rule (per entry, mechanical): every entry runs at the effort its own
            declaration entry states, whichever harness serves it. An AGENT entry receives it
            through its dispatch target: the Agent tool has no effort argument, so
            `@KIT@:review-lane-<effort>` binds the level in that agent's frontmatter. Pass the
            entry's `id` as `model` at the call site (a call-site model overrides an agent
            definition's own) and pass the lane's canonical prompt verbatim as always,
            because the agent adds no review criteria of its own. A LANE-TOOL entry receives
            it as `--effort <effort>`; the runner refuses, with exit 2, an effort a codex
            entry's effort menu lacks or a transport entry cannot deliver. A re-selected entry
            runs at ITS OWN stated effort, never at the failed entry's."""

# ===========================================================================
# SUBJECT-LENS md-domain CONTRIBUTOR (deliverable of this phase, shared).
# ---------------------------------------------------------------------------
# When skills-kit's md-domain skill is available, the code-review skills hand it
# the changed CLAUDE.md / SKILL.md files as a SUBJECT-lens reviewer: those files
# are claimed out of the generic fan-out (via prepare's `--claim`) and audited
# by md-domain's headless per-artifact detect lanes (workflow/*-detect.js), whose
# findings render as their own labeled section. When md-domain is absent, the md
# files keep their thin generic data_only coverage. All three
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
            prepare with NO `--claim` flags -- degrade silently to the generic review path (the md files get
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
            run md-domain's headless detect lanes for the NON-TRIVIAL claimed
            files, routed THREE ways by basename (plus one path-shape rule) -- at
            most THREE lane groups total: (a) every claimed file named `CLAUDE.md`, or `AGENTS.md` when it is ACTIVE (its directory has no
            `CLAUDE.md`; a claimed `AGENTS.md` whose directory has a `CLAUDE.md` is SHADOWED: drop it from
            every md-domain lane and audit it nowhere) -> the
            `audit_claude_md` lane's `skills/md-domain/workflow/claude-md-detect.js`; (b) every claimed
            file named `SKILL.md` OR sitting inside a `*/skills/<name>/references/` folder -> the
            `audit_skill` lane's `skills/md-domain/workflow/skill-detect.js`
            (only if any; that lane owns BOTH subject shapes and picks the criteria set per file from
            the path); (c) every OTHER claimed `.md` file (generic docs) -> the `audit_project_doc`
            lane's `skills/md-domain/workflow/project-doc-detect.js` (only if any). Pass `review: true`
            and, per claimed
            file, `preImagePath` = its `pre_image` from the bundle (null for an add), with the per-lane
            `files[]` fields (CLAUDE.md / active AGENTS.md: role / dimension / parentPath / ancestorClaudeMdPaths; SKILL.md
            and skill reference: ancestorClaudeMdPaths; project-doc: ancestorClaudeMdPaths), plus
            `mechanicalScan` = the claimed entry's sole `mechanical_scan.files[0]` record. Pass
            `mechanicalCheckPhrases` = `bundle.mechanical_check_phrases` once at the top level of
            EVERY lane args object. Resolve the remaining fields from each claimed file's
            `claude_mds` per references/md-domain-review.md. Resolve the skills-kit plugin root and
            venvPython defensively per that reference, then run skills-kit's
            `scripts/resolve_standards.py` ONCE per review under that venvPython (exact command in
            that reference) and pass its `disabled` list as `disabledCriteria` in EVERY lane args
            object -- an empty list when nothing is disabled; every detect lane throws without it --
            plus each file's `standardsPaths` from its `standards` map. A non-zero exit is never
            replaced by `[]`: run no lane and report every non-trivial claimed file
            `REVIEW INCOMPLETE` with the script's stderr line. Use the Workflow tool when callable, passing
            each installed lane's full text as `script` (never its installed path as `scriptPath`)
            per that reference; when the tool is unavailable or rejects the lane, use that reference's
            "Manual detect invocation" with the SAME installed lanes and args. Transport failure
            does not make md-domain absent and does not release its claimed files. On a skills-kit
            version skew (a detect lane
            entry point, `discover_claude_md.classify_dimension`, or a documented args contract
            missing, OR an installed `audit_skill` lane that predates the skill-REFERENCE subject),
            do NOT guess -- re-run prepare_review.py
            per the THREE-TIER fallback in references/md-domain-review.md (broad skew re-runs with no
            `--claim`; project-doc-only skew keeps the CLAUDE.md / SKILL.md / skill-reference
            claims; skill-reference skew re-adds the `!**/skills/*/references/*.md` exclusion as a
            compatibility shim). The third
            tier is detected by CAPABILITY -- `## 10. Skill reference documents` present in the
            installed `references/standards/skill-standards.md` -- because an older lane ships the
            same entry point and args contract and would otherwise decline the file silently. Those
            are the only sanctioned second prepare invocations.
            Then proceed with the normal fan-out. When the pass runs, the md-domain lanes execute in
            PARALLEL with the reviewer fan-out; keep each `{perFile, totals, review}` for step 9's labeled
            section."""

# Inserted into step 9's action, right after the unresolved-work section.
MD_DOMAIN_REPORT = """\
            - When the md-domain subject-lens pass ran (bundle.claimed_files was non-empty and the
              detect pass did NOT fall back), render its results as a distinct, clearly LABELED section
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
              then render its `mechanical_scan` coverage, findings, and diagnostics using
              `bundle.mechanical_check_phrases` (a diagnostic leaves that check uncovered),
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
        - The claim decision happens ONCE, at the step-2 probe: md-domain available -> `--claim '**/*.md'` (one glob covering CLAUDE.md, AGENTS.md, SKILL.md, a skill's `references/*.md`, and generic docs); md-domain absent -> no `--claim`. Claiming a skill's `references/*.md` assumes the INSTALLED audit_skill lane owns that subject shape; these kits declare no version constraint on skills-kit, so step 6 probes for it by capability and the skill-reference skew tier re-adds the exclusion when it is missing. Do not run prepare a second time just to add claims -- the only re-runs are the version-skew FALLBACKS (broad skew re-runs WITHOUT `--claim`; project-doc-only skew re-runs with `--claim '**/CLAUDE.md' --claim '**/AGENTS.md' --claim '**/SKILL.md' --claim '**/skills/*/references/*.md'`; skill-reference skew re-adds the `!**/skills/*/references/*.md` exclusion as a compatibility shim).
        - Claimed `.md` files route THREE ways in step 6 -- `CLAUDE.md` (or an active `AGENTS.md`) -> the `audit_claude_md` lane; a shadowed `AGENTS.md` is dropped; `SKILL.md` OR a file inside a `*/skills/<name>/references/` folder -> the `audit_skill` lane (its two subject shapes); every other `.md` -> the `audit_project_doc` lane (full routing table in references/md-domain-review.md; `.md.html` is never claimed). Never claim a shape no lane can audit: a declined file comes back NOT-AUDITED, which a caller can misread as a pass.
        - A `NOT-AUDITED` verdict from a lane is NOT a pass. It means the lane declined the file as outside its criteria and read nothing. Render it as its own line, never fold it into the clean count, and never let it satisfy a submit gate -- treat it like the `## Mechanical checks (audit skipped)` section: an honest "not reviewed", not a result. Seeing one on a claimed file means the claim routing sent a file somewhere that cannot audit it; report that rather than accepting the verdict.
        - When skills-kit md-domain is absent the whole mechanism degrades silently: no `--claim`, no claimed_files, no md-domain section -- the md files get thin generic data_only coverage. Note the degradation in one line; do not treat it as an error.
        - The triviality gate is pure-mechanical and decided by prepare_review (per-claimed-file `trivial` / `trivial_reasons`); the skill never re-judges it. A TRIVIAL claimed file is reported via the mechanical-checks line and is NEVER sent to a detect lane or written to the ledger. When EVERY claimed file is trivial and there are no generic diff chunks, the whole audit is skipped -- render the `## Mechanical checks (audit skipped)` section, never a DIFF-CLEAN verdict, and never present the skip as an audit. A user or author asking for the full review overrides the gate.
        - Workflow availability is a transport check, separate from md-domain availability. Prefer a main-session Workflow with each lane's text passed as `script`; if the tool is unavailable or rejects the lane, use "Manual detect invocation" in references/md-domain-review.md. Keep the claimed files with their existing specialist lanes. If neither invocation can complete, report the affected files as review incomplete; never present missing lane output as a clean audit."""


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
        - Detection is a UNION of two axes, decided by prepare_review, and the skill never re-judges it -- `content` (a generated-artifact banner) OR `declared_path` (the file lives under a path a plugin declares that it writes, such as a project's durable plugin-data directory). Either one is sufficient, and the second is what catches a generator that emits no banner at all -- nothing in such a file's bytes says a tool wrote it, but its location does, by construction.
        - Size is NEVER a criterion on either axis. A large hand-written file is chunked and fully reviewed as always; a small machine-emitted file is still excluded. The argument is authorship, not cost.
        - Either axis OUTRANKS a claim, so a claimed `.md` that is machine-emitted arrives in `bundle.machine_emitted_files` and NOT in `bundle.claimed_files` -- the two lists are disjoint. Do not send it to a detect lane and do not treat its absence from `claimed_files` as a bug in the claim globs. A claim promises a specialist reviewer only for AUTHORED files: a subject-lens auditor can act on a finding in generated Markdown no more than the generic lanes can, because the fix belongs in the generator and an edit to the artifact is reverted by its drift guard.
        - Do not review a machine-emitted artifact by reading it. If its content looks wrong, the finding belongs on the generator, or on the decision to check the artifact in -- say that, and name the generator when this change contains one.
        - The `--review-machine-emitted` flag is the override and it is the AUTHOR's call, never an inference. Pass it only when the user or the author explicitly asks for the machine-emitted files to be reviewed."""


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
# CONFIGURABLE REVIEW-PROFILE RESOLUTION (this phase).
# ---------------------------------------------------------------------------
# The `review_profiles` block in the template carries SELECTION GUIDANCE and
# RATIONALE prose only. The executable table (profile ids, reviewer rosters,
# per-reviewer models, validator_models) lives in bootstrap_lib's shipped
# defaults and is resolved per review by render_review_profiles.py, which
# layers a user and a project override on top. Appended to both gotcha blocks.
# ===========================================================================
PROFILE_GOTCHAS = """
        - The `review_profiles` block above is SELECTION GUIDANCE AND RATIONALE ONLY. It carries no reviewer roster, model, or validator_models -- that executable table is resolved per review by @RENDER_TOOL@ (step 0, before any other step), which merges the shipped bootstrap_lib defaults with any `~/.claude/config/review_profiles.yaml` (user) or `<project_root>/.claude/review_profiles.yaml` (project) override. Never merge those layers yourself and never hand-edit the resolved output.
        - The `profile` in steps 6-7 is always an entry from that RESOLVED table, never the guidance block. Match the guidance prose to decide which profile id fits the change, then read `reviewers` and `validator_models` off the resolved entry with that id.
        - See references/configuration.md for the layer precedence, merge rules (profiles/reviewers merge by id/name; validator_models and other mappings deep-merge; `disabled: true` removes a record; plain lists like `data_only_extensions`, a reviewer's `model` entry list, and a validator reason's entry list replace), the shipped default table, what a declaration entry may name, how a multi-entry declaration is routed, which lanes may take an endpoint entry, and what happens when a lane fails.
        - A reviewer's `model` is a model DECLARATION -- an ordered list of `{id, effort}` entries. Each entry is dispatched by its harness under step 6's entry-harness rule, at that entry's own effort -- a `claude` entry launches the `@KIT@:review-lane-<effort>` agent with its `id` as the model, any other entry runs through @LANE_TOOL@ with `--model <id> --effort <effort>`.
        - Apply references/configuration.md's pre-dispatch launch-correction rule before classifying a failed invocation.
        - Every model entry, reviewer and validator alike, states its own `effort` (`low`, `medium`, `high`, `xhigh`, `max`); the renderer refuses a table in which any entry does not, so an entry's stated effort is the only effort it runs at. For an Agent entry, effort selects the DISPATCH TARGET, not a parameter -- the Agent tool has no effort argument, so the entry goes to the `@KIT@:review-lane-<effort>` agent, whose frontmatter sets it. For a lane-tool entry it is the `--effort` argument. Do not attempt to pass effort as an Agent argument, and do not read an entry's effort off the agent's page -- the RESOLVED table is the authority.
        - Effort and model are independent and BOTH are honoured: the entry's `id` goes at the CALL SITE as `model`, where it overrides whatever the effort agent's own frontmatter would imply. Never move a lane to a different model to obtain an effort level, and never move it to a different effort to obtain a model. A re-selected entry runs at its own stated effort."""


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
            Require `bundle.mechanical_contract == 2` before consuming mechanical results.
            If absent or different, report an incompatible prepare producer and stop this review.
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
        - n: 0
          action: |
            Resolve the EXECUTABLE review-profile table FIRST -- before step 1, before
            prepare_review.py, and before any question to the user -- so a configuration
            error stops the review before any other work happens. Run @RENDER_TOOL@ with
            `--project-root <root>`, where <root> is @PROFILE_ROOT@
            NEVER merge the review-profile config layers (shipped / user / project)
            yourself -- the renderer is the only merge. Its stdout is the merged `profiles`
            table as YAML -- profile ids, reviewer rosters, each reviewer's `model` list of
            `{id, effort}` entries, and `validator_models` with one `{id, effort}` entry per
            reason -- followed by a `---` separator and layer provenance; parse only the
            YAML above the separator. Keep the resolved `profiles` list for steps 6 and 7.
            See references/configuration.md for the full layer/merge/override contract.
          tool: Bash running @RENDER_TOOL@
          on_failure: |
            A non-zero exit STOPS the review. Print the renderer's stderr verbatim and do
            nothing else: do not run step 1 or prepare_review.py, do not ask the user
            anything, and do not launch any reviewer or validator. Never guess, default, or
            fill in a missing model or effort. The stderr names each problem; for an entry
            that states no effort it names the layer file, the profile, the lane, and the
            entry. The user fixes those layers and runs the review again.
@STEP1@
@STEP2@
@STEP3@
        - n: 4
          action: |
            Read every path in unique_claude_mds (CLAUDE.md, or AGENTS.md where a directory has no
            CLAUDE.md -- the bundle already applies that precedence). Subagents do not need to re-read.
          tool: Read
        - n: 5
          action: |
            If bundle.submit_gates is non-empty, DISCHARGE each gate yourself. Do NOT ask the
            user to confirm it.
            Discharge each gate yourself against the change. A gate is evaluated from the diff,
            the repo, and commands you can run, not from anyone's memory of what was done. This
            rule applies when you made the edits in this session. It also applies when the user
            handed you a change they wrote by hand. In neither case is "did you do it?" evidence.
            An "I don't know" is neither a confirmation nor a decline. A gate answered that way
            collects nothing while appearing to have run.
            For each gate, decide from the change itself and record ONE verdict:
              - MET -- the obligation is satisfied. State HOW, citing the specific evidence in
                this range (a file, a key and its default, a test, a command you ran and its
                result). A bare "yes" is not a discharge.
              - NOT APPLICABLE -- the gate's scope matched a file but its subject is absent from
                this change. State what the gate asks for and why nothing here triggers it.
              - NOT MET -- the obligation applies and is not satisfied. State what is missing.
                This is a finding: render it, and do not present the review as clean.
              - NEEDS THE USER -- reserved for a gate that turns on a fact NOT derivable from the
                repo, the diff, or this session (a check that only runs on their hardware, an
                external system's state, an intent only they hold). Only here may you ask, and
                you ask for THAT SPECIFIC FACT -- never "did you do it". Reaching for this
                verdict because a gate is laborious to evaluate is the failure mode it exists to
                prevent; evaluate it.
            Do NOT skip a gate and do NOT collapse several into one verdict.
            Skip this step entirely if bundle.submit_gates is empty.
          tool: Read + the repo itself (AskUserQuestion ONLY for a NEEDS THE USER gate)
        - n: 6
          action: |
            Select one profile from the RESOLVED table fetched in step 0, using
            `review_profiles.profiles[].selection.guidance` above (this SKILL's guidance prose,
            each entry naming the profile it documents) -- this is an inference call, not regex.
            Read each profile's guidance, weigh the actual contents of `bundle.changed_files`, and
            pick the most appropriate profile id from the resolved table. Default to `code` when
            uncertain. `profile` below is that resolved-table entry -- its `reviewers` and
            `validator_models` come from step 0, never hand-constructed.
@DISPATCH@
@LANE_ROUTING@
@MD_DOMAIN_LAUNCH@
            Then launch one subagent per (reviewer @X@ chunk) pair in parallel via
            a single message with R @X@ K Agent calls, where R = len(profile.reviewers) and
            K = len(bundle.diff_chunks). Each subagent gets the chunk's absolute diff path
            (`<bundle.bundle_dir>/<diff_chunks[i].path>`), the @FILEPATHS@ of the files
            in that chunk (`diff_chunks[i].files`), and -- for reviewer_a -- the CLAUDE.md
            mapping restricted to those files PLUS the repo-relative PATHS of every
            `bundle.claimed_files` entry (paths only, never content), under the heading
            "Also changed in this review". Claimed files are absent from the chunk, so
            without that list reviewer_a reads the change as though the Markdown were
            never touched and reports a docs-currency rule as violated when the update is
            in fact present -- a false positive that is indistinguishable from a true one.
            An endpoint-dispatched reviewer_a gets the same list automatically -- the lane
            runner reads it out of `bundle.claimed_files` itself when dispatched with
            `--chunk-index`; the other reviewers do not receive it.

            Mechanical scan results -- reviewer_a and reviewer_b ONLY. Each chunk carries
            `diff_chunks[i].mechanical_scan`, shaped as `{schema_version: 2, files:
            [{file, checks_run, findings}]}`. Render its coverage and findings under
            "Mechanical scan", one file at a time. For each file,
            derive the covered-check list from THAT record's `checks_run`; render each id
            with its human phrase from `bundle.mechanical_check_phrases`. If an id
            is absent from that map, render the bare id; this is the
            forward-compatible case, not an error. Render each finding as
            `- <file>:<line> [<check>] <detail>`. An empty `checks_run` means no mechanical coverage for this file.
            Render each record's diagnostics as unavailable coverage, preserving the text
            without assigning a source line. A compiler diagnostic with line 0 is unlocated.
            Named checks with an empty findings list mean those
            checks ran cleanly. These states are different and neither may be omitted.
            Preserve each record's `mechanical_contract: 2` marker in endpoint arguments.
            Tell the lane to use each file/check answer only for its explicitly declared
            covered question and not to repeat that question. An unlisted-hit restriction
            applies only where a check enumerates hits within its declared scope.
            A check omitted for one file remains reviewer scope for that
            file, regardless of another file's coverage. State explicitly that the scan
            DETECTS but does not DECIDE: a listed hit is a location. The lane judges
            whether the diff introduced a reportable issue within its assigned scope;
            standards findings require a quotable rule, and bugs require its bug criteria.
            For `python_syntax`, the covered question is whole-post-image compilation
            under the nearest snapshot `.python-version`, using the matching CPython
            minor grammar. The result is success or the first compiler diagnostic,
            including on unchanged lines. Do not repeat that compilation question.
            Later errors hidden by the first diagnostic remain reviewer scope and may
            be reported when the existing bug criteria establish them. Types, imports,
            and causation are not covered. `structured_parse` likewise covers whole-post-image parsing
            and its first diagnostic, including on unchanged lines or at EOF; later
            hidden errors remain reviewer scope and may be reported; causation is not
            covered. Other shipped checks retain their
            added-line scope. Omit the
            section ENTIRELY only for reviewer_c, which is not assigned mechanical checks.
            Reviewers not listed in the selected profile are
            NOT launched. If bundle.diff_chunks is empty (@RANGE_OR_CL@ has no diff content) and
            no claimed file is NON-TRIVIAL (per the triviality gate above -- when a non-trivial
            claimed file exists, the md-domain pass above still runs on it even with zero
            diff_chunks), skip the reviewer fan-out and jump to step 9 with zero code-review
            issues.

            Parse every NATIVE Agent lane's returned array before treating it as candidate
            issues. Write that lane's raw response verbatim to a distinct temporary file under
            `bundle.bundle_dir`, then run `@PARSE_TOOL@ --lane <reviewer name> --response
            <that file> --bundle <bundle.bundle_dir>/bundle.json`. Replace the raw array with
            the parser's stdout array. The executable parser validates every lane and, for
            reviewer_a, verifies each citation against the reported file's governing CLAUDE.md
            chain. Endpoint envelopes already contain output from the same shared parser. A
            non-zero parser exit is a FAILED lane under the existing failure rule; never pass
            its unparsed issues to validators.
          tool: Bash (the llm-scripting-kit venv CLI, `describe`) + Agent (per the entry-harness rule, a lane whose entry is not a `claude` entry runs as a Bash call to @LANE_TOOL@ instead)
          expected: JSON arrays of candidate issues from each launched reviewer (one array per (reviewer, chunk) lane), plus a recorded failure for any lane that exited non-zero.
        - n: 7
          action: |
            Launch one validator subagent per candidate issue, all in parallel via a single message.
            The selected profile's `validator_models[reason]` (from the RESOLVED table fetched in
            step 0) is a list of exactly one `{id, effort}` entry, chosen per issue by its reason.
            Its `id` is one of `sonnet`, `opus`, `haiku`, `fable`: launch an Agent with
            `subagent_type: @KIT@:review-lane-<effort>` and `model: <id>`, using that entry's own
            stated effort, and give it the `validator` subagent definition below with the issue.
            No validator lane is endpoint-eligible: the runner refuses one and exits 2, because the
            validator is the control that suppresses a weak reviewer's noise and must not be
            replaced in the same change as a reviewer. Any other id in `validator_models` is
            therefore a configuration error to report, not a lane to run.
          tool: Agent
          expected: CONFIRMED or REJECTED per issue.
        - n: 8
          action: Drop rejected issues from the findings. Do not detail them, but state the count in one line ("N candidate issues did not survive validation") so the user can ask rather than being told nothing.
        - n: 9
          action: |
            Render the markdown review.
            - Report each corrected launch's original stderr, no-dispatch evidence,
              correction, and final outcome in a `## Launch corrections` section.
              Only a completed, schema-valid reviewer result restores that lane's coverage.
            - When any lane FAILED (including an unsuccessful launch correction
              or a lane refused as a configuration error), prepend a `## Lane failures`
              section naming each failed lane, the model entry and effort it was configured with,
              and the runner's stderr reason. State plainly which files that lane would have covered
              and that they did NOT receive its review. This section is not decoration: the
              rest of the review looks identical whether a lane ran or not, so without it a
              partial review is indistinguishable from a complete one. Never describe a
              failed lane's files as clean, and never re-run the lane on a model its own
              declaration did not name -- a lane reaches this section only when its
              declaration has no usable entry left; report it and let the user decide.
            - When any reviewer's declaration had two or more entries, prepend a
              `## Lane routes` section carrying every `route:` line announced in step 6,
              verbatim, re-selections included. This is a disclosure, not a warning: the
              rendered review looks identical whichever entry ran, so the reader must never
              have to infer which model actually reviewed their change.
            - When `bundle.submit_gates` is non-empty, prepend a `## Submit checklist`
              section, each gate carrying its step-5 verdict and the evidence for it.
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
      `selection.guidance` below and pick the most appropriate one based on the actual contents
      of `bundle.changed_files`. Default to `code` when uncertain.
      The EXECUTABLE table -- profile ids, reviewer rosters, per-reviewer models, and
      validator_models -- is NOT inline here. It is resolved at review time by step 0
      (@RENDER_TOOL@), which merges the shipped bootstrap_lib defaults with any user/project
      override. Never merge those layers by hand. See references/configuration.md for the full
      layer/merge/override contract and the shipped default table.
    profiles:
      - id: data_only
        selection:
          guidance: |
            Select the `data_only` profile when every changed file is either:
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
      - id: code
        selection:
          guidance: |
            Default profile (`code`). Use whenever any changed file contains executable logic
            (source code, scripts, build configuration that runs code) -- i.e. anytime
            `data_only` doesn't clearly apply.
        rationale: "Full reviewer set with Opus where deep semantic reasoning pays off."
  # subagents: reviewer/validator definitions (scope, input, restrictions).
  # Models are NOT set here -- they are bound by the selected `review_profiles` entry.
  subagents:
    - name: reviewer_a_claude_md_compliance
      subagent_type: "@KIT@:review-lane-<entry effort> (step 6 entry-harness rule)"
      scope: CLAUDE.md compliance only, restricted to the files in one chunk
      input: "absolute path to ONE chunk .diff file, the @FILEPATHS@ of the files in that chunk, the per-file CLAUDE.md mapping restricted to those files, the full text of each relevant CLAUDE.md (read in step 4), and the paths of every claimed file (paths only -- their content belongs to the subject-lens reviewer)"
      canonical_prompt_note: |
        This lane can run EITHER as an Agent subagent or, when its chosen entry is not a
        `claude` entry, as a plain completion (see the step-6 entry-harness rule). Both paths must
        review by the same standard, so the prompt below is the single source: it is rendered
        here from bootstrap_lib.code_review.lane_prompts, which is also what the endpoint
        runner sends. When launching this lane as an Agent, use it as the subagent's
        instructions verbatim, then append the chunk path, file list, and (per step 4) the
        per-file CLAUDE.md mapping and text. Do not paraphrase it.
      canonical_prompt: |
@REVIEWER_A_PROMPT@
      restrictions:
        - "Read the assigned chunk diff once (single Read call). Do not Read other chunks."
        - "Only consider CLAUDE.md files that share a path with the file being reviewed (use the per-file mapping when supplied; do not cross-apply)."
        - "Only flag issues in files present in your chunk -- files in other chunks are someone else's responsibility."
    - name: reviewer_b_diff_only_bugs
      subagent_type: "@KIT@:review-lane-<entry effort> (step 6 entry-harness rule)"
      scope: obvious bugs visible in one chunk's diff alone
      input: "absolute path to ONE chunk .diff file, the @FILEPATHS@ of the files in that chunk, and the @CHANGE_DESC@"
      canonical_prompt_note: |
        This lane can run EITHER as an Agent subagent or, when its chosen entry is not a
        `claude` entry, as a plain completion (see the step-6 entry-harness rule). Both paths must
        review by the same standard, so the prompt below is the single source: it is rendered
        here from bootstrap_lib.code_review.lane_prompts, which is also what the endpoint
        runner sends. When launching this lane as an Agent, use it as the subagent's
        instructions verbatim, then append the chunk path and file list. Do not paraphrase it.
      canonical_prompt: |
@REVIEWER_B_PROMPT@
      restrictions:
        - "Read the assigned chunk diff once. MUST NOT use Read for anything beyond that chunk."
        - "Only flag won't-compile, syntax/type errors, missing imports, unresolved references, definitely-wrong logic regardless of inputs."
        - "For data/doc files (data_only profile): focus on malformed syntax, duplicate keys, schema or column-count violations, and broken cross-file references."
        - "Only flag issues in files present in your chunk."
    - name: reviewer_c_introduced_code
      subagent_type: "@KIT@:review-lane-<entry effort> (step 6 entry-harness rule)"
      scope: bugs/security/logic problems in the introduced code that need broader context, restricted to one chunk's files
      input: "absolute path to ONE chunk .diff file, the @FILEPATHS@ of the files in that chunk, local paths for those files, and the @CHANGE_DESC@"
      canonical_prompt_note: |
        This lane can run EITHER as an Agent subagent or, when its chosen entry is not a
        `claude` entry, as a plain completion (see the step-6 entry-harness rule). Both paths must
        review by the same standard, so the prompt below is the single source: it is rendered
        here from bootstrap_lib.code_review.lane_prompts, which is also what the endpoint
        runner sends. When launching this lane as an Agent, use it as the subagent's
        instructions verbatim, then append the chunk path, file list, local paths, and change
        description. Do not paraphrase it.
      canonical_prompt: |
@REVIEWER_C_PROMPT@
      restrictions:
        - "Read the assigned chunk diff first."
        - "MAY use Read to look at surrounding context in the changed files (the LOCAL paths you were given) when needed."
        - "MAY find and Read the consumers of an input the change newly admits, outside the chunk's files, for the silenced-error rule in the canonical prompt only."
        - "Examples: concurrency issues, lifetime bugs, security holes, an input that used to be refused and is now accepted and ignored."
        - "Only flag issues in files present in your chunk."
    - name: validator
      subagent_type: "@KIT@:review-lane-<entry effort> (step 7)"
      scope: confirm or reject one candidate issue with high confidence
      input: "the issue (JSON), the chunk diff, [if claude_md: relevant CLAUDE.md contents]"
      output_format: "exactly one line: 'CONFIRMED: <one-sentence reason>' or 'REJECTED: <one-sentence reason>'"
      restrictions:
        - "Validator does not see who flagged the issue. Independence is the value."
        - "For a silenced-error issue (an input the change newly admits that a consumer ignores), MAY Read the consumer the description names. CONFIRMED when no consumer acts on the admitted input; it is not an input-dependent issue."
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

        - **[@CHK@|@CRS@|-|?] <summary>** -- per `<source>`, triggered by `<file>` (+N more if many).
          <the step-5 verdict, then the evidence for it, on one line>
          > <rationale, indented as blockquote, omitted if empty>

      @CHK@ = MET. The evidence line names what satisfies it -- a file, a key and its
              default, a test, or a command and its result.
      @CRS@ = NOT MET. The obligation applies and is unsatisfied; this is a finding, and
              the review is not clean.
      -     = NOT APPLICABLE. Scope matched but the subject is absent from this change;
              the evidence line says why.
      ?     = NEEDS THE USER. Turns on a fact not derivable from the repo, the diff, or
              this session; the evidence line names the specific fact wanted.

      Every gate carries a verdict. A gate rendered without one means step 5 was skipped,
      which is a defect, not a neutral outcome.

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
    'review and discharged by the agent against the change. Results are rendered as markdown; the '
    'diff chunks, bundle.json, and pre-images in bundle.bundle_dir are transient scratch under the '
    'plugin data root, while declined findings persist in a durable ledger (references/declined-ledger.md).'
)

P4_INTRO = (
    'Run a multi-agent code review of a Perforce changelist directly in conversation. The diff '
    'is partitioned on disk into chunks (one per file boundary cluster, balanced under a 1 MB '
    'cap); reviewer subagents (set by the selected review profile) run **once per (role @X@ '
    'chunk)** so a single large CL fans out across multiple parallel agents instead of forcing '
    'each reviewer to ingest the full diff. Each flagged issue is then validated by an '
    'independent subagent to suppress false positives. Path-scoped pre-submit reminders (submit '
    'gates) authored in ancestor CLAUDE.md files are surfaced alongside the review for author '
    'confirmation. Results are rendered as markdown; the diff chunks, bundle.json, and pre-images '
    'in bundle.bundle_dir are transient scratch under the plugin data root, while declined '
    'findings persist in a durable ledger (references/declined-ledger.md).'
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
      - publishing the rendered review to a PR comment
      - enforcing submit gates (advisory only; enforcement belongs in a pre-push hook)"""

P4_SCOPE_EXCLUDES = """\
      - git diffs and non-Perforce review workflows
      - publishing the rendered review to Swarm or a PR comment
      - reviewing previously-submitted changelists
      - enforcing submit gates (advisory only; enforcement belongs in a pre-shelve/pre-submit hook)"""

GIT_PRECONDITIONS = "        - cwd is inside a git repository."
P4_PRECONDITIONS = "        - User has at least one pending CL OR has passed a CL number argument."

# Step 0 resolves the review-profile table before prepare_review.py runs, so it
# cannot read `bundle.project_root`. Each kit derives the root the same way its
# prepare_review.py does (git: `git rev-parse --show-toplevel`; p4: the
# `clientRoot` field of `p4 -ztag info`), so step 0 resolves the layers prepare
# would have pointed it at.
PROFILE_ROOT = {
    "git": (
        "the output of\n"
        "            `git rev-parse --show-toplevel` -- the same root prepare_review.py later reports\n"
        "            as `bundle.project_root`. If that command fails, print its stderr and stop the\n"
        "            review: prepare_review.py needs the same repository."
    ),
    "p4": (
        "the output of\n"
        "            `p4 -ztag -F %clientRoot% info` -- the same root prepare_review.py later reports\n"
        "            as `bundle.project_root`. When it prints nothing, no client workspace resolves:\n"
        "            omit `--project-root`, as prepare's `bundle.project_root` is then null too and\n"
        "            the resolver reads the project layer from the process working directory."
    ),
}

GIT_STEP1 = """\
        - n: 1
          action: |
            Resolve the diff range.
            - If the user passed an explicit argument (`<ref>`, `<a>..<b>`, `<a>...<b>`, `--staged`, `--working`), use it verbatim.
            - `--working` diffs the worktree against HEAD, so it sees MODIFIED tracked files only: a brand-new
              (untracked) file is not in the diff and the review of it is silently empty. To review new files,
              `git add` them and use `--staged`.
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
          input: "p4 changes --me -s pending -m 20"
          expected: A single integer CL number confirmed by the user."""

GIT_STEP2 = """\
        - n: 2
          action: |
__CLAIM_PROBE__
            Then run prepare_review.py to fetch the diff, partition it into chunked .diff fragments on disk, enumerate changed files via `git diff --name-status`, map ancestor CLAUDE.md files for each, detect untracked-or-unstaged files in the directories the diff touches, detect unresolved merge conflicts, and scan ancestor CLAUDE.md files for submit-gate reminders that apply to this range.
__LAUNCH_EMIT__
          tool: __PREPARE_LAUNCHER__
          input: "<range or argument from step 1>  (append `--claim '**/*.md'` when md-domain is available, per the claim probe)"
          expected: |
            JSON with vcs, range, head_sha, branch, description, project_root, bundle_dir, diff_chunks, changed_files, unique_claude_mds, untracked_or_unstaged, merge_conflicts, submit_gates, change_id, ledger_baseline, ledger_hits, -- only when --claim was passed -- claimed_files, and -- only when a changed file was detected as machine-emitted -- machine_emitted_files (each entry carries identifier, local, size_bytes, and the axis that matched -- machine_emitted_axis `content` or `declared_path` plus the naming machine_emitted_signature; such files are excluded from diff_chunks and changed_files, and `--review-machine-emitted` turns that exclusion off). The raw diff text is NOT inline -- it lives in per-chunk files at `<bundle_dir>/<diff_chunks[i].path>` (paths are relative to bundle_dir). Each `changed_files` entry carries `chunk_index` pointing to the chunk that contains its diff.
          on_failure: Surface the stderr message to the user and stop. No retry.""".replace(
    "__CLAIM_PROBE__", CLAIM_PROBE
).replace(
    "__PREPARE_LAUNCHER__", PREPARE_LAUNCHER_YAML_SKILL["git"]
).replace(
    "__LAUNCH_EMIT__", LAUNCH_EMIT
)

def _substitute(text: str, old: str, new: str) -> str:
    """Replace `old` with `new`, refusing a substitution that matches nothing.

    A bare str.replace that finds no match returns the input unchanged, so a
    derived constant would keep shipping the text it was written to override
    and no artifact-versus-generator check could see it: regenerating moves
    both sides together and they agree. Raising here makes the mismatch a
    build failure at the point the source text drifts.
    """
    if old not in text:
        raise ValueError(f"substitution source not found: {old!r}")
    return text.replace(old, new)


# The p4 skill runs prepare a second time when a foreign-client CL refuses
# --claim, so the shared probe's single-invocation wording does not hold here.
P4_CLAIM_PROBE = _substitute(
    _substitute(
        CLAIM_PROBE,
        "and invoke prepare\n            only ONCE.",
        "and invoke prepare\n"
        "            once unless the foreign-client fallback below applies.",
    ),
    "noting the degradation in one line. Do NOT run prepare\n            twice.",
    "noting the degradation in one line. A second prepare invocation is\n"
    "            reserved for the foreign-client claim refusal below.",
)

P4_STEP2 = """\
        - n: 2
          action: |
__CLAIM_PROBE__
            Then run prepare_review.py to fetch the diff (with shelved fallback; auto-shelves a pending CL with no existing shelf so the diff is fetchable), partition the diff into chunked .diff fragments on disk, map ancestor CLAUDE.md files for each changed file, detect unreconciled and default-changelist files in the directories the CL touches, detect unresolved merges in the CL, and scan ancestor CLAUDE.md files for submit-gate reminders that apply to this CL.
__LAUNCH_EMIT__
          tool: __PREPARE_LAUNCHER__
          input: "<CL>  (append `--claim '**/*.md'` when md-domain is available, per the claim probe)"
          expected: |
            JSON with cl, description, project_root, bundle_dir, diff_chunks, changed_files, unique_claude_mds, unreconciled, default_open, stale_open, shelf_drift, unresolved, hygiene_incomplete, submit_gates, auto_shelved, shelf_fingerprint, change_id, ledger_baseline, ledger_hits, -- only when the CL belongs to a different client -- foreign_change, -- only when --claim was passed -- claimed_files, and -- only when a changed file was detected as machine-emitted -- machine_emitted_files (each entry carries identifier, local, size_bytes, and the axis that matched -- machine_emitted_axis `content` or `declared_path` plus the naming machine_emitted_signature; such files are excluded from diff_chunks and changed_files, and `--review-machine-emitted` turns that exclusion off). The raw diff text is NOT inline -- it lives in per-chunk files at `<bundle_dir>/<diff_chunks[i].path>` (paths are relative to bundle_dir). Each `changed_files` entry carries `chunk_index` pointing to the chunk that contains its diff. `auto_shelved=true` means prepare_review created the shelf and step 10 must clean it up.
          on_failure: |
            If prepare reports that the CL belongs to a foreign client, re-run once without `--claim` and use that bundle. State that md-domain subject-lens review is unavailable because claim pre-images depend on the author's client workspace.
            For any other failure, surface the stderr message to the user and stop. No retry.
            Launch note: ALWAYS invoke through `$BOOTSTRAP_PYTHON` (the guarded expression shown in `tool:`), never as a bare path and never as bare `python`/`python3` -- a bare name is not guaranteed to resolve to any interpreter that can run this script, and `python3` in particular can be absent from PATH on Windows (see /bootstrap fact python_interpreter and python-interpreter.md). Bare `$P4_KIT_ROOT/scripts/prepare_review.py <CL>` lets bash try to run the file as a shell script -- it has no shebang line in older checkouts and the exec bit does not survive on Windows checkouts, so bash parses the Python as sh and exits 2. Passing the script as an argument to `$BOOTSTRAP_PYTHON` avoids that entirely: bash only launches the interpreter, never the file. The script self-relocates under the p4-kit venv via reexec, so bootstrap's own interpreter is sufficient. And NEVER pipe the invocation (`... | tail`, `... | head`): a pipe makes `$?` the last pipeline stage's status, not the script's, which silently masks a launch failure as success.""".replace(
    "__CLAIM_PROBE__", P4_CLAIM_PROBE
).replace(
    "__PREPARE_LAUNCHER__", PREPARE_LAUNCHER_YAML_SKILL["p4"]
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
            If bundle.unreconciled or bundle.default_open is non-empty, list each non-empty group by action and ask one question about which files should be folded into the CL before review.
            - For `bundle.unreconciled`, use `p4 reconcile -c <CL> <local-paths>` on the selected files.
            - For `bundle.default_open`, use `p4 reopen -c <CL> <local-paths>` on the selected files.
            - If the user picks files from either group, run the command for each selected group, then re-run prepare_review.py and use the new bundle.
            - If the user declines all: continue with the current bundle.
            On the post-fold re-run, do NOT prompt again about either group even if files remain -- the user already decided.
            Skip this step entirely if both groups are empty.
          tool: AskUserQuestion + p4 reconcile/reopen + prepare_review.py"""

GIT_STEP9_TAIL = """\
            - When `bundle.merge_conflicts` is non-empty, prepend a `## Unresolved merge conflicts`
              section listing each conflicted file. This is informational, not a finding --
              the merge cannot be completed until each file is resolved (`git add <file>`
              after editing), but the review still renders."""

P4_STEP9_TAIL = """\
            - When `bundle.unresolved` or `bundle.stale_open` is non-empty, prepend a
              `## CL is not in a submittable state -- fix before review` section.
              This is informational, not a finding -- the review still renders.
              - When `bundle.unresolved` is non-empty, list each unresolved file
                with its resolve type. The CL is not submittable until the user
                runs `p4 resolve` on each entry.
              - When `bundle.stale_open` is non-empty, list each entry's depot
                path, open action, and workspace state, with the repair commands:
                `p4 reconcile <path>` flips the CL's open action to match what is
                on disk (edit -> delete when the file is missing, delete -> add
                when the file is present); `p4 revert <path>` discards the CL's
                open action instead and restores the workspace to match the
                depot. The CL is not submittable until one of the two is run on
                each entry.
            - When `bundle.shelf_drift` is non-empty, disclose each depot path
              whose shelf content differs from the local file. Each entry
              contains the depot path and local path; this is a warning, not a
              refusal, because the shelf digest is server-normalized.
            - When `bundle.foreign_change` is present, disclose the author and foreign client
              and state that client-local hygiene scans were skipped. CLAUDE.md scopes come
              from the reviewer's workspace, as listed by `bundle.unique_claude_mds`."""

GIT_STEP10 = ""

P4_STEP10 = f"""\
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
          tool: {PREPARE_LAUNCHER_YAML_SKILL["p4"]}
          input: "--cleanup <bundle.bundle_dir>"
"""

GIT_CHECKLIST = f"""\
        - Diff range resolved (auto-detected from workspace state OR explicit user arg) and surfaced in the step-1 narration line
        - Context bundled via prepare_review.py
        - Untracked/unstaged files surfaced (and either folded in via `git add`/`git commit` with a re-run, or explicitly declined)
        - All CLAUDE.md files read
        - Submit gates discharged by the agent (if any), each with a MET / NOT APPLICABLE / NOT MET / NEEDS THE USER verdict and its evidence
        - Executable review-profile table resolved via render_review_profiles.py FIRST (step 0, before prepare_review.py and before any question to the user); a non-zero exit stopped the review with its stderr printed verbatim; profile selected from the resolved table using review_profiles guidance
        - Reviewers launched in parallel (single message, R {X} K Agent calls -- one per (reviewer {X} chunk) pair, where K = len(bundle.diff_chunks))
        - Validators launched in parallel (single message, N Agent calls), each entry's id and effort taken from the profile's validator_models
        - Every reviewer and validator entry dispatched at its own stated effort -- an Agent entry as `@KIT@:review-lane-<effort>` with its `id` as the model, a lane-tool entry with `--effort <effort>`; a re-selected entry at its own effort; a runner refusal (exit 2) reported under `## Lane failures`, not re-selected past
        - Filtered to confirmed-only
        - Launch rationale line emitted once (file-type-driven; md_trivial variant when the change is all-mechanical)
        - Every multi-entry reviewer declaration routed through `llm-scripting-kit describe` (stdout printed verbatim, choice announced as a `route:` line) or, without describe, through its core entries; a FAILED lane re-selected per the printed `Re-select:` line, each entry at most once; every `route:` line carried into the `## Lane routes` section; a lane with no usable entry left still reports a `## Lane failures` entry with coverage missing
        - md-domain subject-lens pass launched for the NON-TRIVIAL bundle.claimed_files when skills-kit md-domain is available (or claimed files folded back into the generic review on version-skew fallback); skipped silently when md-domain is absent
        - Trivial claimed files (prepare's `trivial` flag) reported via the `## Mechanical checks (audit skipped)` section, never as an audit or DIFF-CLEAN; nothing written to the ledger for them; whole review skipped when every claimed file is trivial and there are no generic diff chunks
        - Machine-emitted artifacts (bundle.machine_emitted_files) reported via the `## Machine-emitted artifacts (not reviewed)` section, naming each file's exclusion axis (content banner or declared plugin-write path) and the rule that matched, never as an audit or DIFF-CLEAN; review of machine-emitted output belongs on the generator
        - Previously-declined findings collapsed via the ledger (bundle.ledger_hits); SERIOUS md-domain findings never collapsed
        - Markdown rendered to chat (Submit checklist section prepended when gates applied; Unresolved merge conflicts section prepended when bundle.merge_conflicts is non-empty; separate `## md-domain (subject-lens) findings` section when the md-domain pass ran)
        - Newly declined findings recorded to the ledger via `prepare_review.py --ledger-record` (skipped when nothing was declined)"""

P4_CHECKLIST = f"""\
        - CL number resolved
        - Context bundled via prepare_review.py
        - Foreign CL ownership disclosed when bundle.foreign_change is present, including the skipped client-local scans
        - Unreconciled and default-changelist files surfaced in one question (and either folded in via the matching `p4 reconcile -c <CL>` or `p4 reopen -c <CL>` command with a re-run, or explicitly declined)
        - All CLAUDE.md files read
        - Submit gates discharged by the agent (if any), each with a MET / NOT APPLICABLE / NOT MET / NEEDS THE USER verdict and its evidence
        - Executable review-profile table resolved via render_review_profiles.py FIRST (step 0, before prepare_review.py and before any question to the user); a non-zero exit stopped the review with its stderr printed verbatim; profile selected from the resolved table using review_profiles guidance
        - Reviewers launched in parallel (single message, R {X} K Agent calls -- one per (reviewer {X} chunk) pair, where K = len(bundle.diff_chunks))
        - Validators launched in parallel (single message, N Agent calls), each entry's id and effort taken from the profile's validator_models
        - Every reviewer and validator entry dispatched at its own stated effort -- an Agent entry as `@KIT@:review-lane-<effort>` with its `id` as the model, a lane-tool entry with `--effort <effort>`; a re-selected entry at its own effort; a runner refusal (exit 2) reported under `## Lane failures`, not re-selected past
        - Filtered to confirmed-only
        - Launch rationale line emitted once (file-type-driven; md_trivial variant when the change is all-mechanical)
        - Every multi-entry reviewer declaration routed through `llm-scripting-kit describe` (stdout printed verbatim, choice announced as a `route:` line) or, without describe, through its core entries; a FAILED lane re-selected per the printed `Re-select:` line, each entry at most once; every `route:` line carried into the `## Lane routes` section; a lane with no usable entry left still reports a `## Lane failures` entry with coverage missing
        - md-domain subject-lens pass launched for the NON-TRIVIAL bundle.claimed_files when skills-kit md-domain is available (or claimed files folded back into the generic review on version-skew fallback); skipped silently when md-domain is absent
        - Trivial claimed files (prepare's `trivial` flag) reported via the `## Mechanical checks (audit skipped)` section, never as an audit or DIFF-CLEAN; nothing written to the ledger for them; whole review skipped when every claimed file is trivial and there are no generic diff chunks
        - Machine-emitted artifacts (bundle.machine_emitted_files) reported via the `## Machine-emitted artifacts (not reviewed)` section, naming each file's exclusion axis (content banner or declared plugin-write path) and the rule that matched, never as an audit or DIFF-CLEAN; review of machine-emitted output belongs on the generator
        - Previously-declined findings collapsed via the ledger (bundle.ledger_hits); SERIOUS md-domain findings never collapsed
        - Markdown rendered to chat (Submit checklist section prepended when gates applied; CL-not-submittable section prepended when bundle.unresolved or bundle.stale_open is non-empty; separate `## md-domain (subject-lens) findings` section when the md-domain pass ran)
        - Auto-shelf cleanup invoked when bundle.auto_shelved is true (`prepare_review.py --cleanup <bundle_dir>`)
        - Newly declined findings recorded to the ledger via `prepare_review.py --ledger-record` (skipped when nothing was declined)"""

GATE_GOTCHAS = """
        - Discharge each submit gate yourself against the change. A gate is evaluated from the diff, the repo, and commands you can run, not from anyone's memory of what was done. This rule applies when you made the edits in this session. It also applies when the user handed you a change they wrote by hand. In neither case is "did you do it?" evidence. An "I don't know" is neither a confirmation nor a decline. A gate answered that way collects nothing while appearing to have run.
        - A MET verdict means met WITH EVIDENCE. Name the file, the key and its default, the test, or the command and its result. A verdict with no evidence is the same empty signal as an unanswered prompt, just harder to notice.
        - NEEDS THE USER is for a fact you cannot derive -- an external system's state, a check that only runs on their hardware, an intent only they hold. It is not an escape hatch for a gate that is tedious to evaluate, and when you do use it, ask for that specific fact rather than asking whether they did the work."""

GIT_GOTCHAS = f"""\
        - Always quote the exact CLAUDE.md rule text when flagging a claude_md issue. If you cannot quote it verbatim, do not flag it.
        - Sequential reviewer or validator calls waste time. Reviewers run in one message with one concurrent Agent call per (reviewer {X} chunk) pair (R reviewers {X} K chunks). For a small diff (K=1) that's still 2 calls for data_only / 3 for code; for a large diff (K=N) it scales to R {X} N. Validators run in one message with N concurrent Agent calls.
        - Each reviewer subagent reads ONE chunk path, not the whole diff. Do not pass `bundle_dir` and expect the subagent to glob -- pass the absolute chunk path the subagent should Read.
        - The rendered review stays in chat. This skill does not post a PR comment. prepare_review.py writes transient diff chunks, bundle.json, and pre-images under bundle.bundle_dir. ledger.py writes a durable ledger.json.
        - If prepare_review.py fails, report the error and stop. No retry.
        - Validators are independent of reviewers. The validator does not see who flagged the issue.
        - The untracked/unstaged check must happen BEFORE reviewers spawn. Folding in forgotten files after agents have already reviewed the diff wastes their work and produces a stale review.
        - On the post-fold re-run, do NOT prompt again about untracked_or_unstaged files. The user already chose. Re-prompting on the same list is annoying; re-prompting on a smaller list (because they only added some) implies the rest were forgotten when they were declined.
        - Submit gates are reminders, not findings -- they do NOT go through reviewer or validator subagents. They are parsed deterministically by prepare_review.py and rendered verbatim in a separate output section. Do not try to validate, score, or filter them.
        - A NOT MET gate is a finding. Render it and do not describe the review as clean.
        - Merge conflicts are NOT findings -- they do NOT go through reviewer subagents. They are detected deterministically by prepare_review.py (`git ls-files -u`). The reviewers see the raw diff (including any conflict markers) and may legitimately flag bugs in it; the merge-conflicts section is a separate informational warning to the user.
        - Auto-detect is convenient, not authoritative. Always restate the chosen range in the step-1 narration line; a user reviewing the wrong branch will catch it there before subagents spawn.
        - Detached HEAD with no main/master fallback is a real failure mode; surface the error and ask for an explicit range. Do not guess at a "probably right" base.""" + GATE_GOTCHAS + MD_DOMAIN_GOTCHAS + GENERATED_GOTCHAS + LEDGER_GOTCHAS + PROFILE_GOTCHAS

P4_GOTCHAS = f"""\
        - Always quote the exact CLAUDE.md rule text when flagging a claude_md issue. If you cannot quote it verbatim, do not flag it.
        - Sequential reviewer or validator calls waste time. Reviewers run in one message with one concurrent Agent call per (reviewer {X} chunk) pair (R reviewers {X} K chunks). For a small CL (K=1) that's still 2 calls for data_only / 3 for code; for a large CL (K=N) it scales to R {X} N. Validators run in one message with N concurrent Agent calls.
        - Each reviewer subagent reads ONE chunk path, not the whole diff. Do not pass `bundle_dir` and expect the subagent to glob -- pass the absolute chunk path the subagent should Read.
        - The rendered review stays in chat. This skill does not post a Swarm or PR comment. prepare_review.py writes transient diff chunks, bundle.json, and pre-images under bundle.bundle_dir. ledger.py writes a durable ledger.json.
        - If prepare_review.py fails, report the error and stop. No retry.
        - Validators are independent of reviewers. The validator does not see who flagged the issue.
        - The unreconciled and default-changelist checks must happen BEFORE reviewers spawn. Folding in files after agents reviewed the diff wastes their work and produces a stale review.
        - On the post-fold re-run, do NOT prompt again about unreconciled or default-changelist files. The user chose once. Re-prompting on the same list is annoying; re-prompting on a smaller list implies the rest were forgotten when they were declined.
        - Submit gates are reminders, not findings -- they do NOT go through reviewer or validator subagents. They are parsed deterministically by prepare_review.py and rendered verbatim in a separate output section. Do not try to validate, score, or filter them.
        - A NOT MET gate is a finding. Render it and do not describe the review as clean.
        - Unresolved merges are NOT findings -- they do NOT go through reviewer or validator subagents. They are detected deterministically by prepare_review.py (`p4 resolve -n -c <CL>`) and rendered verbatim in a separate output section. The reviewers see the raw diff (including any conflict markers) and may legitimately flag bugs in it; the unresolved section is a separate informational warning to the user.
        - Auto-shelf cleanup (step 10) must run whenever `bundle.auto_shelved` is true, no matter what happened in steps 3-9. The cleanup script is deterministic and safe (it only deletes the shelf when the live fingerprint exactly matches what we recorded), so there is no scenario where skipping it is the right call. Skipping leaves an orphan shelf the author didn't ask for.
        - --claim requires a PENDING CL. On a submitted CL, `#have` pre-images are POST-change once the workspace synced past the CL, so prepare_review exits with an error when --claim is passed on a submitted CL; re-run without --claim for a plain informational review.""" + GATE_GOTCHAS + MD_DOMAIN_GOTCHAS + GENERATED_GOTCHAS + LEDGER_GOTCHAS + PROFILE_GOTCHAS

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
        template: "Found <G> submit-gate reminder(s) applying to this range. Discharging each against the change."
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
        template: "Gathering context for CL <CL>: fetching diff, mapping CLAUDE.md scopes, scanning client-local file state."
      - when: "Before step 3 (F >= 1)"
        template: "Found <U> unreconciled and <D> default-changelist file(s) in the directories this CL touches. Asking one fold-in question before reviewing."
      - when: "After step 3 if user folded files in (F_added >= 1)"
        template: "Folded <F_added> file(s) into CL <CL> via `p4 reconcile` or `p4 reopen`. Re-running prepare to refresh the diff."
      - when: "After step 3 if user declined (F_added = 0 and F >= 1)"
        template: "Continuing with CL <CL> as-is."
      - when: "After step 3, before step 4 (M >= 1)"
        template: "Got <N> changed file(s) and <M> unique CLAUDE.md scope(s). Reading them now."
      - when: "After step 3, before step 4 (M = 0)"
        template: "Got <N> changed file(s); no CLAUDE.md scopes apply."
      - when: "After step 2 (V >= 1)"
        template: "Found <V> file(s) with unresolved merges in CL <CL>. Will surface in the review output -- CL is not submittable until resolved."
      - when: "Before step 5 (G >= 1)"
        template: "Found <G> submit-gate reminder(s) applying to this CL. Discharging each against the change."
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
      "<D>": "len(bundle.default_open)"
      "<F>": "<U> + <D>"
      "<F_added>": "count of unreconciled and default-changelist files the user chose to fold into the CL"
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
        "KIT": "git-kit",
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
        "PREPARE_TOOL": PREPARE_LAUNCHER_YAML_SKILL["git"],
        "LANE_TOOL": LANE_LAUNCHER_SKILL["git"],
        "PARSE_TOOL": PARSE_LAUNCHER_SKILL["git"],
        "RENDER_TOOL": RENDER_LAUNCHER_SKILL["git"],
        "PROFILE_ROOT": PROFILE_ROOT["git"],
        "LEDGER_RECORD_N": "10",
        "BASELINE_DESC": "the range base SHA advances -- origin/main moves, or HEAD changes for a working-tree review",
    },
    "p4": {
        "NAME": "p4-code-review",
        "KIT": "p4-kit",
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
        "PREPARE_TOOL": PREPARE_LAUNCHER_YAML_SKILL["p4"],
        "LANE_TOOL": LANE_LAUNCHER_SKILL["p4"],
        "PARSE_TOOL": PARSE_LAUNCHER_SKILL["p4"],
        "RENDER_TOOL": RENDER_LAUNCHER_SKILL["p4"],
        "PROFILE_ROOT": PROFILE_ROOT["p4"],
        "LEDGER_RECORD_N": "11",
        "BASELINE_DESC": "the CL is reshelved, its content edited, or its revisions move",
    },
}

# Shared tokens (identical for both VCS): the dispatch rule, the md-domain
# contributor regions, and the glyphs.
_SHARED = {
    "DISPATCH": DISPATCH,
    "LANE_ROUTING": LANE_ROUTING,
    # The canonical reviewer prompts, rendered from the module the endpoint
    # runner imports so the two dispatch paths cannot state different rules.
    # Indented to sit under `canonical_prompt: |` in the subagents block.
    "REVIEWER_A_PROMPT": "\n".join(
        ("        " + line).rstrip()
        for line in lane_prompts.REVIEWER_A_SYSTEM.splitlines()
    ),
    "REVIEWER_B_PROMPT": "\n".join(
        ("        " + line).rstrip()
        for line in lane_prompts.REVIEWER_B_SYSTEM.splitlines()
    ),
    "REVIEWER_C_PROMPT": "\n".join(
        ("        " + line).rstrip()
        for line in lane_prompts.REVIEWER_C_SYSTEM.splitlines()
    ),
    "MD_DOMAIN_LAUNCH": MD_DOMAIN_LAUNCH,
    "MD_DOMAIN_REPORT": MD_DOMAIN_REPORT,
    "GENERATED_REPORT": GENERATED_REPORT,
    "LEDGER_STEP9": LEDGER_STEP9,
    "LEDGER_RECORD_STEP": LEDGER_RECORD_STEP,
    "LAUNCH_NARRATION": LAUNCH_NARRATION,
    # render_review_profiles.py resolves the review-profile config layers; its
    # launch gotcha (missing shebang / lost exec bit on Windows checkouts making
    # a bare path parse as sh) is the same hazard PREPARE_TOOL guards against
    # for BOTH kits (git's prepare_review.py ships mode 100644 with no shebang
    # and exits 126 on a bare-path launch), so both launch it as an argument to
    # the interpreter, exactly like PREPARE_TOOL.
    "X": X,
    "CHK": CHK,
    "CRS": CRS,
}

_SKILL_TOKEN_ORDER = [
    "DISPATCH",  # multi-line, contains no other @tokens@; substitute first
    # Lane routing rule: shared body carrying nested @LANE_TOOL@ and @KIT@ --
    # substitute the block first, then those tokens resolve below.
    "LANE_ROUTING",
    "REVIEWER_A_PROMPT", "REVIEWER_B_PROMPT", "REVIEWER_C_PROMPT",  # rendered prompt text, no nested @tokens@
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
    "STEP1", "STEP2", "STEP3", "STEP9_TAIL", "STEP10", "PROFILE_ROOT",
    "CHECKLIST", "GOTCHAS", "NARRATION_TEMPLATES", "NARRATION_VARIABLES",
    "DIFF_OR_CL", "RANGE_OR_CL", "FILEPATHS", "CHANGE_DESC", "ISSUE_PATH",
    "SG_DESC", "OUTPUT_FORMAT", "PREPARE_TOOL", "RENDER_TOOL", "LANE_TOOL",
    "PARSE_TOOL", "LEDGER_RECORD_N", "BASELINE_DESC", "KIT",
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

Add this block to any CLAUDE.md (root, subdirectory, or both), or to an AGENTS.md in a directory that has no CLAUDE.md (a directory with both reads only CLAUDE.md):

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

Multiple gates per instruction file allowed; blocks must be separated by a blank line. Malformed blocks (missing `Applies to:`, empty scope list) are skipped with a one-line stderr warning -- never silently dropped.
"""

SUBMIT_GATES_FRAGMENTS = {
    "git": {
        "SG_HEADER": (
            "The CLAUDE.md/AGENTS.md-author-facing guide for writing submit-gate blocks. Submit gates are "
            "path-scoped pre-push reminders authored in CLAUDE.md or AGENTS.md files; `git-code-review` detects "
            "them deterministically (via `prepare_review.py`, the same parser as `p4-code-review`) "
            "and surfaces them verbatim at review time when at least one file in the range falls "
            "within a gate's scope. This doc covers only how to author them; detection and rendering "
            "are described in the `submit_gates` block of the SKILL.md contract."
        ),
        "SG_ANCHOR": "repo root",
    },
    "p4": {
        "SG_HEADER": (
            "The CLAUDE.md/AGENTS.md-author-facing guide for writing submit-gate blocks. Submit gates are "
            "path-scoped pre-submit reminders authored in CLAUDE.md or AGENTS.md files; `p4-code-review` detects "
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
an active AGENTS.md (its directory has no CLAUDE.md), SKILL.md, a skill's `references/*.md`, and generic project docs alike (`.md.html` Markdeep files
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
lane owns BOTH of the `skill` artifact's subject shapes -- the SKILL.md contract root AND the
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

md-domain's detect lanes are native Workflow scripts. Use the Workflow tool when callable,
passing each lane as described in "Passing a lane script to the Workflow tool" below; otherwise
use "Manual detect invocation" below. Locate the INSTALLED skills-kit plugin:

- Plugin root (`<root>`): resolve via the REGISTRY first, falling back to a cache scan only
  when the registry is empty or unreadable. Read `~/.claude/plugins/installed_plugins.json`;
  when its `plugins["skills-kit@plugins-kit"]` array is present and non-empty, `<root>` is
  entry `[0]`'s `installPath` -- the ACTIVE install, which can differ from the highest cached
  version after a downgrade, a scoped install, or a dev-tree entry. Only when that key is
  missing, the array is empty, or the file cannot be read, fall back to the newest version
  directory under the plugins cache for this marketplace --
  `~/.claude/plugins/cache/plugins-kit/skills-kit/<version>/` (pick the highest semver dir
  present). `${CLAUDE_PLUGIN_ROOT}` of the CURRENT skill is NOT it -- that points at git-kit /
  p4-kit, not skills-kit.
- Detect-lane entry points, all under the one md-domain skill:
  `<root>/skills/md-domain/workflow/claude-md-detect.js` (the `audit_claude_md` lane, for CLAUDE.md
  subjects), `<root>/skills/md-domain/workflow/skill-detect.js` (the `audit_skill` lane, for
  SKILL.md subjects AND for a skill's own `references/*.md` documents), and
  `<root>/skills/md-domain/workflow/project-doc-detect.js` (the
  `audit_project_doc` lane, for every OTHER `.md` subject).
- Standards resolver: `<root>/scripts/resolve_standards.py` (see "Resolve the run's standards
  configuration" below).
- venvPython: skills-kit's provisioned venv, which lives in the version-independent DATA dir --
  `~/.claude/plugins/data/plugins-kit/skills-kit/.venv/Scripts/python.exe` on Windows,
  `~/.claude/plugins/data/plugins-kit/skills-kit/.venv/bin/python` on macOS/Linux.

**Version-coupling safety valve (three-tier fallback).** Do NOT guess when an entry point is
missing, a documented args contract is not what this doc describes, or the installed lane predates
a subject shape this skill claims. Check the tiers in order and take the FIRST that matches:

- **Broad skew** -- `<root>` cannot be located, OR the `claude-md-detect.js` / `skill-detect.js`
  entry point or args contract is missing, OR `<root>/scripts/resolve_standards.py` is missing,
  OR `discover_claude_md.classify_dimension is unavailable`:
  emit a one-line warning and RE-RUN prepare_review.py WITHOUT any `--claim` flags. All claimed md
  files return to `changed_files` for generic review, and the whole md-domain section is skipped for
  this run.
- **project-doc-only skew** -- `claude-md-detect.js` and `skill-detect.js` are present but ONLY
  `project-doc-detect.js` is missing (a skills-kit that predates
  project-doc review): emit a one-line warning and RE-RUN prepare_review.py with
  `--claim '**/CLAUDE.md' --claim '**/AGENTS.md' --claim '**/SKILL.md' --claim '**/skills/*/references/*.md'`.
  CLAUDE.md, AGENTS.md,
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

Transport failure is not skills-kit version skew. Keep the current bundle and claims.
Do not rerun prepare_review.py for a transport failure. Use the manual invocation below.

## Resolve the run's standards configuration (once per review)

Every detect lane REQUIRES `disabledCriteria`, the run's resolved list of disabled optional
criterion ids, and throws before dispatching any agent when it is absent. An empty list means
nothing is disabled; an absent list is never read that way. Resolve it ONCE per review, after
`<root>` and venvPython and before any lane call, with the same skills-kit install the lanes come
from:

    "<venvPython>" "<root>/scripts/resolve_standards.py" --project-root "<project root>"

`<project root>` = @PROJECT_ROOT@. Omit `--primitive`, so the `standards` map covers every
primitive this review may route. The script makes `<root>` importable itself, so no `cd` is needed.
Run it under venvPython only: the resolver needs pyyaml, which skills-kit's venv carries.

On exit 0, parse stdout as `{ disabled, thresholds, standards, audit, notes }`:

- `disabledCriteria` = `disabled`, passed at the top level of EVERY lane args object below --
  including the empty list.
- per file, `standardsPaths` = `standards.<primitive>` (absent key -> `[]`), where `<primitive>` is
  `claude_md` for a CLAUDE.md / active AGENTS.md, `skill_md` for a SKILL.md, `reference_doc` for a
  skill reference document, and `plain_md` for a generic project doc. This is how project- and
  user-authored standards reach the lanes.
- A non-empty `notes` array is rendered verbatim at the top of the md-domain findings section.
- `thresholds` and `audit` need no threading here: the lanes' mechanical validator reads thresholds
  itself under `--config`, and `audit.fix_mode` governs a remediate step this review does not run.

On a non-zero exit (1: a malformed config layer, an un-tunable rule id, or an interpreter without
pyyaml; 2: a usage error) the script prints nothing on stdout and one diagnostic line on stderr.
Do NOT substitute `[]` and do NOT run any lane on a guessed configuration: an unread config is
indistinguishable from an empty one. Run no detect lane, keep the files claimed (this is not version
skew, so they do not return to the generic reviewers), and report
`REVIEW INCOMPLETE: <file> - resolve_standards.py exited <code>: <stderr line>` for every
non-trivial claimed file. Incomplete coverage cannot satisfy a submit gate.

## The Workflow calls (three-way by basename, then by path)

At most three, in the SAME message that launches the reviewer fan-out (or the reviewer Workflow).
Route by basename first; the ONE path-shape rule is the skill-reference case in (b):

1. **`audit_claude_md` lane** -- one call for every claimed file whose basename is `CLAUDE.md`, or
   `AGENTS.md` when active. An `AGENTS.md` is ACTIVE only when its directory has no `CLAUDE.md`
   (CLAUDE.md takes precedence); a claimed `AGENTS.md` sitting beside a `CLAUDE.md` is SHADOWED
   and is dropped from ALL three lanes -- never audited as a claude-md and never as a project doc.
   `script` = the text of `<root>/skills/md-domain/workflow/claude-md-detect.js`, `args` =
   `{ files: [...], disabledCriteria: <resolved disabled>, mechanicalCheckPhrases: bundle.mechanical_check_phrases, review: true, refs: { criteria: <root>/skills/md-domain/references/standards/claude-md-standards.md, codeDirFilter: <root>/skills/md-domain/references/standards/claude-md-standards.md, densityCriteria: <root>/skills/md-domain/references/standards/claude-md-standards.md, pluginRoot: <root>, venvPython: <venvPython> } }` (one standards doc backs all three refs -- the code-directory dimension and the density lens are sections of it).
2. **`audit_skill` lane** -- one call for every claimed file that is EITHER (a) named `SKILL.md`
   OR (b) inside a `*/skills/<name>/references/` folder (only if any). Those are the `skill`
   artifact's two subject shapes and they share one lane and one Workflow call; the lane picks the
   criteria set per file from the path.
   `script` = the text of `<root>/skills/md-domain/workflow/skill-detect.js`, `args` =
   `{ files: [...], disabledCriteria: <resolved disabled>, mechanicalCheckPhrases: bundle.mechanical_check_phrases, review: true, refs: { pluginRoot: <root>, venvPython: <venvPython> } }`.
3. **`audit_project_doc` lane** -- one call for every OTHER claimed `.md` file (generic docs; only if any).
   `script` = the text of `<root>/skills/md-domain/workflow/project-doc-detect.js`, `args` =
   `{ files: [...], disabledCriteria: <resolved disabled>, mechanicalCheckPhrases: bundle.mechanical_check_phrases, review: true, refs: { criteria: <root>/skills/md-domain/references/standards/project-doc-standards.md, pluginRoot: <root> } }`.

`args` may be passed as an object or a JSON string; all `refs` paths must be ABSOLUTE (the
Workflow runs from the session cwd, not the skill dir). `review: true` forces the model pin and
per-file diff attribution; keep it true. `<resolved disabled>` is the `disabled` list from
"Resolve the run's standards configuration" above, the same list in every lane; a lane call
without it throws.

**Passing a lane script to the Workflow tool.** Read the installed lane script and pass its full
text VERBATIM as `script`. Do not pass the installed path as `scriptPath`: the tool refuses the
plugin-cache path, and it has also been observed refusing a copy placed in the working
directory, so copying the script does not help. Every Workflow result names a saved
script file; a later call in the same session for the SAME lane may pass that returned path as
`scriptPath` instead of the text again. A rejected `scriptPath` is not fixed by changing how the
path is spelled; pass `script`.

## Manual detect invocation

Use this route when the Workflow tool is unavailable (including inside a subagent) or
rejects the lane script. Run the existing detect script's audit through the Agent tool.
The installed script remains the source of the prompt and result contract.

1. Read the applicable existing detect script in full. Build the same args described above and
   below, including `review: true`, each file's `preImagePath`, `standardsPaths` and
   `mechanicalScan`, and the top-level `disabledCriteria` and `mechanicalCheckPhrases`. Resolve every referenced file against the installed root.
2. For each file, invoke Agent with `subagent_type: @KIT@:review-lane-high` and `model: opus`.
   The Agent tool has no effort argument; the subtype's `effort: high` frontmatter binds effort.
   Set `prompt` to the installed script's instantiated `lanePrompt` plus its exact installed
   `FILE_FINDINGS_SCHEMA`, with an instruction to return only one JSON object matching that schema.
   Preserve all prompt instructions, standards, ancestor context, and attribution input.
   Confirm the installed script still specifies `model: 'opus'` and `effort: 'high'` before dispatch;
   a different pin requires a matching Agent transport or the incomplete terminal below.
3. Parse each Agent response as JSON and validate it against the installed `FILE_FINDINGS_SCHEMA`
   before running the reducer. Require one schema-valid result for every requested file.
   Missing or invalid results mean REVIEW INCOMPLETE; never substitute empty findings or DIFF-CLEAN.
   For valid results, retain the input path and mechanical scan as the script does. Apply the same
   installed script's review reducer and totals calculation, preserving attribution filtering,
   SERIOUS retention, and NOT-AUDITED handling. Return the same `{ perFile, totals, review }` envelope.

Transport failure never authorizes a generic-review fallback or a change to the lane's model,
effort, schema, or criteria. Keep the claimed files assigned to their existing specialist lanes.

Use the native Workflow result for any lane group that already completed; invoke only outstanding
groups manually. If Agent is unavailable, its subtype or model pin cannot be honored, or any result
is missing or invalid, report `REVIEW INCOMPLETE: <file> - <invocation or validation failure>` for
each affected file. Incomplete coverage cannot satisfy a submit gate.

## Building `files[]` from `bundle.claimed_files`

Build `files[]` from the NON-TRIVIAL claimed files only (per the triviality gate above); trivial
files never reach a detect lane. Each claimed-file entry carries `local` (absolute path), `pre_image` (absolute path to the
materialized before-image via @PREIMAGE_ORIGIN@, or `null` for an add), and `claude_mds` (the
nearest-ancestor-first CLAUDE.md (or active AGENTS.md) chain, which for such a subject INCLUDES the subject itself
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
- `standardsPaths` = the resolved `standards.<primitive>` list for this file's primitive (see
  "Resolve the run's standards configuration" above).
- `mechanicalScan` = the entry's `mechanical_scan.files[0]` record. The wrapper has exactly one
  record for this claimed file. Do not flatten it or infer coverage from findings: an empty
  `checks_run` is uncovered, while non-empty `checks_run` with no findings is a clean scan.

Pass `mechanicalCheckPhrases` = `bundle.mechanical_check_phrases` once at the top level of each
Workflow call. Each lane renders ids through this map and falls back to the bare id when a newer
producer supplies an unknown check. The scan answers only its mechanical questions; it does not
audit the file, satisfy the specialist lane, or change a NOT-AUDITED verdict.

For a **CLAUDE.md** or active **AGENTS.md** file (`audit_claude_md` lane `files[]`):
- `path` = `local`.
- `role` = `"child"` when `ancestorClaudeMdPaths` is non-empty, else `"root"` (a standalone file
  with no ancestor instruction file audits as its natural role). Use `"local"` for a `CLAUDE.local.md`.
- `dimension` = call the shipped classifier for this file and use its stdout (`"classic"` or
  `"code-directory"`) verbatim. Run it with the already-resolved skills-kit interpreter and root:

      "<venvPython>" -c 'import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from discover_claude_md import classify_dimension; print(classify_dimension(Path(sys.argv[2])))' "<root>/skills/md-domain/scripts" "<local>"

  The imported script makes the skills-kit plugin root available to its own dependencies. If the
  import or call fails, take the broad-skew fallback above. Do not derive the dimension by hand.
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

Each Workflow returns `{ perFile, totals, review }`. `perFile[i]` retains the input
`mechanicalScan` record and carries `verdict`
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
        "KIT": "git-kit",
        "PREIMAGE_ORIGIN": "`git show <range-base>:<path>`",
        "PROJECT_ROOT": "`bundle.project_root` (the git repo root)",
    },
    "p4": {
        "SKILL_NAME": "p4-code-review",
        "KIT": "p4-kit",
        "PREIMAGE_ORIGIN": "`p4 print -q -o <dest> //depot/path#have`",
        "PROJECT_ROOT": (
            "`bundle.project_root` (the p4 workspace root), or the session cwd when it\n"
            "is null (an unresolvable workspace root)"
        ),
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
        "PREPARE_TOOL": PREPARE_LAUNCHER["git"],
        "LEDGER_STORE": "~/.claude/plugins/data/plugins-kit/git-kit/reviews/ledger.json",
    },
    "p4": {
        "SKILL_NAME": "p4-code-review",
        "CHANGE_ID_LEDGER": "the CL number",
        "BASELINE_LEDGER": (
            "a hash over the CL's shelf fingerprint (content) plus its per-file "
            "(rev, action) map (identity)"
        ),
        "PREPARE_TOOL": PREPARE_LAUNCHER["p4"],
        "LEDGER_STORE": "~/.claude/plugins/data/plugins-kit/p4-kit/reviews/ledger.json",
    },
}


def render_declined_ledger(vcs: str) -> str:
    out = DECLINED_LEDGER_TEMPLATE
    for token, value in DECLINED_LEDGER_FRAGMENTS[vcs].items():
        out = out.replace(f"@{token}@", value)
    return out


# ===========================================================================
# configuration.md -- one parameterized source rendering both references.
# The consumer-facing OP-4 documentation for the review-profile config: the
# three layer paths, the merge rules bootstrap_lib.code_review.review_profiles
# actually implements, the shipped default table (reproduced verbatim from
# bootstrap_lib/code_review/defaults/review_profiles.yaml), and a worked
# override example. The SKILL body's review_profiles.description points here.
# ===========================================================================
CONFIGURATION_TEMPLATE = """\
# Configuring review profiles

`@SKILL_NAME@` selects a review profile -- reviewer roster, each reviewer's model entries,
and validator_models per reason, every entry stating its own effort -- from configuration
resolved at review time, not from a table baked into SKILL.md. The SKILL body's `review_profiles` block carries only the SELECTION
GUIDANCE and RATIONALE prose that helps pick a profile; the EXECUTABLE table lives in
bootstrap_lib's shipped defaults (reproduced below) and is resolved per review by
`bootstrap_lib.code_review.review_profiles`, invoked through this plugin's venv entry point:

    @RENDER_TOOL@ --project-root <project root>

## Mechanical syntax coverage

These skills and prepare scripts use mechanical contract 2. Prepare requests it
explicitly from bootstrap. The bundle and each file record carry the version;
native dispatch requires it, and endpoint dispatch preserves the record marker.
Older callers omit this capability and receive contract 1: added-line structured
parsing and no Python syntax check. A version bump alone does not opt callers in.

`python_syntax` uses the nearest ancestor `.python-version` in the frozen review
snapshot. It accepts one numeric CPython version, such as `3.12` or `3.12.9`.
The available parser must match its major and minor version. Missing, ambiguous,
unreadable, or unsupported mappings leave this check uncovered. A nearer mapping
always takes precedence, including when its content cannot be read.

The check compiles each authored `.py` post-image in memory without executing
code, importing modules, or running project commands. Coverage means compilation
succeeded or the first compiler diagnostic was supplied. The diagnostic may be
on an unchanged line. Reviewers judge whether the diff introduced a reportable bug.
Errors hidden by the first diagnostic remain reviewer scope and may be reported
when the existing criteria establish them. Imports and types also remain outside
this check. The unlisted-hit restriction applies only to the covered question,
not to all syntax errors in the file.

`structured_parse` also covers the whole post-image and its first parser diagnostic.
An unlocatable diagnostic leaves coverage unavailable. Other shipped checks retain
their added-line scope. Both reviewer dispatch paths receive the covered question
with instructions not to repeat it. These results do not select or suppress lanes.

## Layers

Three layers are merged from lowest to highest precedence:

| Layer | Path | Use for |
|---|---|---|
| shipped | `bootstrap_lib/code_review/defaults/review_profiles.yaml`, bundled with the bootstrap plugin's shared lib | the opinionated default reviewer/model table |
| user | `~/.claude/config/review_profiles.yaml` | this user's policy, across every project |
| project | `<project_root>/.claude/review_profiles.yaml` | policy for one repository |

`<project root>` is the root `@SKILL_NAME@` resolves at step 0, before it prepares the diff:
@CONFIG_PROFILE_ROOT@ It is the same root the prepare bundle later reports as
`bundle.project_root`.

## Merge rules

- Top-level `profiles` is a list of records identified by `id`: a layer patching a known `id`
  is deep-merged into it; an unknown `id` is appended as a new profile.
- Within one profile record, `reviewers` is a list of records identified by `name`, merged the
  same way -- a higher layer only needs to restate the reviewer it is changing. A reviewer
  record's fields are `name`, `model`, and `disabled`; any other key is a hard error rather
  than an ignored one. A lane-level `effort` is one of those errors: effort is stated on each
  model entry instead (see below).
- A reviewer's `model` is ALWAYS a list of entries, and each entry is a mapping with exactly two
  fields, `id` and `effort`, both required. There is no bare-string or bare-mapping shorthand.
  The list is a PLAIN list: a higher layer stating `model` replaces the lower layer's list
  outright, entries and efforts alike, rather than merging entries.
- Every other mapping -- a profile's `selection`, and `validator_models` -- deep-merges key by
  key, so a higher layer states only the keys it changes.
- `validator_models` reason keys (`bug`, `claude_md`, ...) are extensible: a higher layer can
  add a new reason without restating the shipped ones. Each reason's value is a list of exactly
  one `{id, effort}` entry, and a layer stating a reason replaces that reason's list outright.
- `disabled: true` on a profile or a reviewer record removes that record entirely from the
  resolved table, not just its fields.
- Every other list, such as `selection.data_only_extensions`, is also a PLAIN list that a higher
  layer replaces outright.

Malformed or unreadable YAML in any layer is a hard error (`ConfigError`); resolution never
falls back to a partial or best-effort merge.

### Every stated entry is complete in its own layer

Completeness is checked on each RAW layer, before any merge, and a layer never borrows a
missing field from the layer below:

- Every reviewer record a layer states, other than a record that is only `disabled: true`,
  must state a complete `model` list in THAT layer -- every entry with both `id` and `effort`.
- Every `validator_models` reason a layer states must be complete in that layer: exactly one
  entry, with both `id` and `effort`.
- Nothing under a profile the layer disables is checked.

The resolver collects every finding across every layer and reports them all at once, one per
line. Each line names the layer, its file, and the path to the offending record or entry; an
entry missing its effort reads:

    <layer> <path>: profiles[<id>].reviewers[<name>].model[<i>] (<model id>): missing effort

The same path form names a `validator_models.<reason>` entry. The old shapes -- a bare model
id, a list of bare ids, a scalar validator value, or a lane-level `effort` -- are findings too,
each with a message naming the per-entry form to write instead. Any finding stops resolution: the
renderer exits non-zero, and `@SKILL_NAME@` stops at step 0 -- before it prepares the diff or
asks you anything -- and prints that output verbatim. To list the findings without starting a
review, run

    @RENDER_TOOL@ --check --project-root <project root>

It prints every finding to stderr and no table, and exits 0 when every layer is complete, 1
when there are findings, and 2 when a layer is malformed or invalid.

## Shipped defaults

```yaml
profiles:
- id: data_only
  selection:
    data_only_extensions:
    - .csv
    - .yaml
    - .yml
    - .json
    - .tsv
    - .md
  reviewers:
  - name: reviewer_a_claude_md_compliance
    model: sonnet
    effort: low
  - name: reviewer_b_diff_only_bugs
    model: sonnet
    effort: low
  validator_models:
    bug: sonnet
    claude_md: sonnet
- id: code
  selection: {}
  reviewers:
  - name: reviewer_a_claude_md_compliance
    model: sonnet
    effort: low
  - name: reviewer_b_diff_only_bugs
    model: opus
    effort: medium
  - name: reviewer_c_introduced_code
    model:
    - sol
    - opus
    effort: high
  validator_models:
    bug: opus
    claude_md: sonnet
```

## What an `effort` value may name

Every model entry states an `effort`, one of `low`, `medium`, `high`, `xhigh`, `max` -- a
CLOSED menu, unlike `model`, so an unknown level is a hard error at resolve time rather than a
dispatch to an agent that does not exist. There is no default and no inherited effort: an entry
without one is a finding, and the review does not start. Which levels a model actually offers
depends on the model; the resolver validates the name, not the pairing.

Effort is stated PER ENTRY, so each entry of one reviewer's declaration may run at a different
level. Whichever entry runs -- the first choice or a re-selection after a failure -- runs at the
effort stated beside its own `id`.

How the effort reaches the model depends on the entry's harness:

- A `claude` entry is dispatched to `@KIT@:review-lane-<effort>`, because the Agent tool has no
  effort argument -- effort is set in an agent definition's frontmatter. This plugin ships one
  agent per level in the menu. The menu and the shipped agents are the same set, so accepting a
  level at resolve time guarantees that its dispatch target exists. The entry's `id` is passed
  as `model` at the CALL SITE, where it overrides whatever model the effort agent's own
  frontmatter would imply. Validators are dispatched the same way.
- Any other entry runs through `@LANE_TOOL@` with `--effort <effort>`, which hands the level to
  llm-scripting-kit. The runner refuses the lane with exit 2 when a codex entry's effort menu
  does not include the level, or when a transport entry cannot deliver an effort at all; the
  review reports that refusal as a configuration error. The runner's JSON envelope records the
  effort it sent in its `effort` field. `--effort` needs llm-scripting-kit 0.60.0 or later; the
  runner refuses an older release.

`model` and `effort` are therefore independent: any model may pair with any effort the model's
harness accepts, and neither is ever traded for the other.

### Worked effort override

To change the CLAUDE.md-compliance lane's effort in one project, add to
`<project_root>/.claude/review_profiles.yaml`:

```yaml
profiles:
- id: data_only
  reviewers:
  - name: reviewer_a_claude_md_compliance
    model:
    - {id: sonnet, effort: <level>}
```

Replace `<level>` with one of `low`, `medium`, `high`, `xhigh`, `max`, and `sonnet` with the
lane's id from the shipped table if it differs. The record restates the whole `model` list even
though only the effort changes: the list replaces the shipped one outright, and a restated
reviewer whose list is missing, or has an entry without an effort, is a finding. The other
reviewers, `selection`, and `validator_models` are untouched because the patch omits them.

## What a declaration entry may name

A reviewer's `model` is a MODEL DECLARATION: an ordered list of `{id, effort}` entries. The ids
follow the shared format specified in the bootstrap plugin's
`skills/plugin-dev/references/model-declaration.md`: an empty list, or a list naming the same id
twice, is a configuration error at resolve time. The renderer prints each reviewer's `model` as
that entry list, in declared order; `@SKILL_NAME@` routes the ids and looks the chosen id's
effort up in the same list. A `validator_models` value is a list of exactly one entry -- a
validator is never endpoint-eligible, so it has nothing to choose between.

Each entry is an id in the llm-scripting-kit model registry, and which harness serves it decides
how that lane is dispatched:

| Entry | Dispatch |
|---|---|
| a `claude` harness entry: `sonnet`, `opus`, `haiku`, `fable` | an Agent subagent of type `@KIT@:review-lane-<effort>` with `model: <id>` |
| any other entry (a codex or opencode harness entry, or a transport endpoint) | a Bash call to `@LANE_TOOL@` with `--model <id> --effort <effort>` |

`sonnet`, `opus`, `haiku` and `fable` are routable without llm-scripting-kit, because the Agent
tool itself defines them. Every other id is resolved by llm-scripting-kit, so it may name a CLI
harness or an OpenAI-compatible transport -- whatever that plugin's configuration and your
`~/.claude/config/model-endpoints.yaml` declare. Endpoint ids are private to your fleet; the
example below uses a placeholder. Naming such an id is the whole override mechanism -- there is
no separate field to set.

### How a multi-entry declaration is routed

For a reviewer whose declaration has two or more entries, the skill runs

    ~/.claude/plugins/data/plugins-kit/llm-scripting-kit/.venv/bin/llm-scripting-kit describe <id>... --caller session [--project-root <root>] [--dispatchable transport] [--self <id>]

(Windows: ~/.claude/plugins/data/plugins-kit/llm-scripting-kit/.venv/Scripts/llm-scripting-kit.exe), and prints its output verbatim: the entries this machine can use or will be able to use, in pace
order, the one marked `[default]`, and the rule text that says how to choose, how to announce
the choice, and when to re-select. The skill follows that printed rule rather than restating
it, so the rule you read in a review is the rule llm-scripting-kit applied. `--self` names the
model the reviewing session runs on; describe marks that entry `[author]`, and its rule
prefers a non-author entry.

describe leaves out every entry this machine cannot run: an id the registry does not know, an
entry this caller cannot drive, and an excluded one. A left-out entry is skipped without
comment. `--caller session` is the in-session caller kind, and by itself it drives only harness
entries. `--dispatchable transport` tells describe this caller can also run transport
endpoints, through `@LANE_TOOL@`, so they stay in the menu. The skill passes it for every
reviewer except `reviewer_a_claude_md_compliance` and `reviewer_c_introduced_code`, the two
lanes the runner binds only to a harness entry (see "Which lanes may take an endpoint entry"
below); for those two a transport endpoint is left out of the menu. Out-of-quota and unreachable
entries stay in the menu, with their reset time or state, because they are real on this
machine; they are not usable until that changes. When no entry is usable, describe exits 1
with a JSON error that itemises every declared entry, left-out ones included, and the skill
reports that lane as failed.

A one-entry declaration has no menu and no announcement: its entry is dispatched by the table
above.

When `llm-scripting-kit` is not installed, or is a release older than 0.46.0 that has no
`describe`, the skill routes on the declaration's `sonnet`, `opus`, `haiku` and `fable`
entries in declared order and skips every other entry without comment. A declaration with none
of those four does not run, and the review reports that lane as failed with each declared
entry named.

### Which lanes may take an endpoint entry

The three REVIEWER lanes -- the set is `ENDPOINT_ELIGIBLE_LANES` in
`bootstrap_lib.code_review.lane_prompts`, which is the authority; this prose is not. The
runner refuses any other lane by name and exits 2 (a configuration error).

The validator is deliberately excluded. It is the control that suppresses a weak reviewer's
false positives, so replacing it in the same change as a reviewer would remove the instrument
the reviewer change has to be measured with.

Eligibility is not the only gate. `reviewer_a_claude_md_compliance` and
`reviewer_c_introduced_code` read files beyond their chunk, so they need an agent loop
(`LANES_REQUIRING_AGENT_LOOP`): the runner binds them only to a HARNESS endpoint -- one
declaring `harness:` rather than `base_url:` -- and refuses a plain-completion (`transport`)
endpoint rather than produce a reviewer that hallucinates context it cannot fetch.

### When a lane fails

**Pre-dispatch launch-correction rule (all reviewer lanes).** Correct a local invocation
error and retry the same intended lane only with positive evidence that no reviewer process
or Agent started and no provider request was sent. Eligible examples are CLI argument/JSON
quoting errors and an Agent alias sent to the endpoint runner, when diagnostics or the
launcher's verified control flow establish rejection before dispatch. Preserve the same
resolved model, effort, chunk, files, and review criteria; correcting the launcher to the
mechanism required by that model is not model substitution. Retain the original stderr and
no-dispatch evidence for the review's launch-correction report. A non-zero exit alone is not
that evidence; uncertain dispatch state is treated as a failed lane, not a retry opportunity.

Provider/auth/quota/network failures, timeouts, and invalid reviewer output are not eligible
launch corrections, even if a provider rejected the request before inference. Permission or
sharing denials require the normal approval flow and are not eligible launch corrections.
Unsupported lane/model configurations remain errors to report; this exception does not
authorize changing the resolved profile, bypassing capability gates, or retrying to obtain
a preferred verdict. If the invocation cannot be corrected within these bounds, report
the failure and missing coverage.

**Re-selection.** A failure the launch-correction rule does not explain is handled by the
re-selection rule describe printed for that declaration. The skill runs describe again with one
`--exclude <entry>` per entry the lane has already failed on, prints that output, chooses,
and announces the choice with the prior entry and its failure kind as the reason. The chosen
entry runs at its own stated effort. Each entry is tried at most once per lane, and a lane only
ever runs on an entry its own declaration named. A runner refusal (exit 2) that the
launch-correction rule does not explain -- an unsupported lane, an effort the entry cannot take,
or an llm-scripting-kit too old for `--effort` -- is a configuration error: the lane is reported
under `## Lane failures` and is not re-selected.
Every choice from a multi-entry declaration, first choice and re-selections alike, is
announced as a `route:` line, and the rendered review carries a `## Lane routes` section with
every such line, so you can always see which model reviewed which files.

A lane reaches `## Lane failures` only when no usable entry of its declaration is left. The
review then renders without that lane, with its coverage marked missing. That happens when a
one-entry declaration's lane fails, when describe exits 1, or when every usable entry has been
tried and failed. Causes are the endpoint being unreachable or out of quota, a chunk that does
not fit its context window, or output that is not a valid issue array after one repair attempt
-- the stderr line says which.

### Worked endpoint override

To run the diff-only bug reviewer on a local endpoint for every project, and keep the shipped
model as the entry that runs whenever the endpoint cannot, add to
`~/.claude/config/review_profiles.yaml`:

```yaml
profiles:
- id: code
  reviewers:
  - name: reviewer_b_diff_only_bugs
    model:
    - {id: my-local-endpoint, effort: <level>}
    - {id: opus, effort: <level>}
```

`my-local-endpoint` is a placeholder: use an id your llm-scripting-kit configuration or
`~/.claude/config/model-endpoints.yaml` actually declares. Replace each `<level>` with the
effort you want that entry to run at; the two entries need not match. A transport endpoint
must be able to deliver an effort, or the runner refuses the lane. Everything else about the
review is unchanged -- the other reviewers and all validators keep their shipped declarations,
so the endpoint reviewer's findings still pass through the same validation. Stating the
endpoint alone, `model: [{id: my-local-endpoint, effort: <level>}]`, makes it a one-entry
declaration: the lane runs there or fails.

## Worked override example

To run the `code` profile's `reviewer_c_introduced_code` on Sonnet alone for one project
(cheaper, lower-fidelity), add to `<project_root>/.claude/review_profiles.yaml`:

```yaml
profiles:
- id: code
  reviewers:
  - name: reviewer_c_introduced_code
    model:
    - {id: sonnet, effort: <level>}
```

Replace `<level>` with the effort you want. Only the changed reviewer needs restating --
`reviewer_a_claude_md_compliance` and `reviewer_b_diff_only_bugs` keep their shipped
declarations via the by-name merge, and `selection` and `validator_models` are untouched
because the patch omits them. The list REPLACES the shipped list outright, so this lane is
pinned to Sonnet with nothing to choose between. To keep a cross-family entry ahead of it,
state the list you want instead:
`model: [{id: luna, effort: <level>}, {id: sonnet, effort: <level>}]`.

## Inspecting the resolved table

    @RENDER_TOOL@ --project-root <project root>

prints the merged `profiles` table as YAML, then a `---` separator, then which layers were
applied and (for any absent override) the path that would create it. Each reviewer's `model`
is its `{id, effort}` entry list in declared order, and each validator reason is a one-entry
list. When any layer is incomplete it prints the findings to stderr instead and exits
non-zero; add `--check` to list the findings alone (see "Every stated entry is complete in its
own layer" above). This is the same
resolution step 0 of `@SKILL_NAME@` performs -- never merge the layers by hand.
"""

CONFIGURATION_FRAGMENTS = {
    "git": {
        "SKILL_NAME": "git-code-review",
        "KIT": "git-kit",
        "RENDER_TOOL": RENDER_LAUNCHER["git"],
        "LANE_TOOL": LANE_LAUNCHER["git"],
        "CONFIG_PROFILE_ROOT": (
            "the output of `git rev-parse --show-toplevel`. When that command fails, the\n"
            "review stops."
        ),
    },
    "p4": {
        "SKILL_NAME": "p4-code-review",
        "KIT": "p4-kit",
        "RENDER_TOOL": RENDER_LAUNCHER["p4"],
        "LANE_TOOL": LANE_LAUNCHER["p4"],
        "CONFIG_PROFILE_ROOT": (
            "the output of `p4 -ztag -F %clientRoot% info`. When that prints nothing, no client\n"
            "workspace resolves: `--project-root` is omitted and the resolver reads the project\n"
            "layer from the process working directory."
        ),
    },
}


def render_configuration(vcs: str) -> str:
    out = CONFIGURATION_TEMPLATE
    for token, value in CONFIGURATION_FRAGMENTS[vcs].items():
        out = out.replace(f"@{token}@", value)
    return out


# ---------------------------------------------------------------------------
# Targets + write/check driver.
# ---------------------------------------------------------------------------

# The machine-emitted banner stamped into every rendered file. Two readers
# depend on it and neither is a human:
#
# - bootstrap_lib.code_review.machine_emitted's "at-generated marker" signature,
#   which is how a code review learns these files are not an authored review
#   target. Without it a review audits their CONTENT, where no finding can be
#   acted on -- the fix always belongs in this generator, and an edit to the
#   artifact is reverted by the drift check below.
# - scripts/precommit_guard.py, whose "generated artifact" rule would otherwise
#   refuse the commit. Every path returned by targets() must be allowlisted there
#   BECAUSE the banner is what makes the rendered file detectable in the first place.
#
# Keep the `@generated` token: it is the part both readers match on.
BANNER = (
    "<!-- @generated by scripts/gen_code_review_skills.py"
    " -- edit the generator, not this file. -->"
)


def _with_banner(text: str) -> str:
    """Stamp BANNER into `text`, after YAML frontmatter when there is any.

    A SKILL.md opens with frontmatter that must start at line 1, so the banner
    goes immediately below its closing fence; a reference document has none and
    takes the banner as its first line. Either way the banner lands far inside
    the detector's head window (machine_emitted.DEFAULT_HEAD_LINES).
    """
    if text.startswith("---\n"):
        end = text.find("\n---\n", 3)
        if end != -1:
            cut = end + len("\n---\n")
            return text[:cut] + BANNER + "\n" + text[cut:]
    return BANNER + "\n" + text


def targets() -> dict[Path, str]:
    """Map each rendered file path to its rendered content."""
    rendered = {
        GIT_SKILL: _with_banner(render_skill("git")),
        P4_SKILL: _with_banner(render_skill("p4")),
        GIT_SUBMIT_GATES: _with_banner(render_submit_gates("git")),
        P4_SUBMIT_GATES: _with_banner(render_submit_gates("p4")),
        GIT_MD_DOMAIN_REVIEW: _with_banner(render_md_domain_review("git")),
        P4_MD_DOMAIN_REVIEW: _with_banner(render_md_domain_review("p4")),
        GIT_DECLINED_LEDGER: _with_banner(render_declined_ledger("git")),
        P4_DECLINED_LEDGER: _with_banner(render_declined_ledger("p4")),
        GIT_CONFIGURATION: _with_banner(render_configuration("git")),
        P4_CONFIGURATION: _with_banner(render_configuration("p4")),
    }
    for vcs, agent_dir in (("git", GIT_AGENTS), ("p4", P4_AGENTS)):
        for effort in EFFORT_LEVELS:
            rendered[agent_dir / f"review-lane-{effort}.md"] = _with_banner(
                render_agent(vcs, effort)
            )
    return rendered


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
