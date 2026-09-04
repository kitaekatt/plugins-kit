---
_schema_version: 1
name: git-code-review
author: christina
skill-type: technique-skill
description: Use when reviewing local git changes -- before push, before opening a PR, or auditing a branch. Do NOT use for Perforce CLs or existing PRs by URL.
---

# Git Code Review

Run a multi-agent code review of a git diff range directly in conversation. The default diff range is inferred from workspace state (mid-merge / mid-rebase / branch-with-upstream / origin-main-fallback), so the agent does the right thing for "review what I'm about to push" without forcing the user to spell out a range; arguments accepted for explicit control. The diff is partitioned on disk into chunks (one per file boundary cluster, balanced under a 1 MB cap); reviewer subagents (set by the selected review profile) run **once per (role × chunk)** so a single large branch fans out across multiple parallel agents instead of forcing each reviewer to ingest the full diff. Each flagged issue is then validated by an independent subagent to suppress false positives. Path-scoped pre-submit reminders (submit gates) authored in ancestor CLAUDE.md files are surfaced alongside the review and discharged by the agent against the change. Results are rendered as markdown -- no persistence to disk.

```yaml
technique_skill:
  _schema_version: "1"
  trigger_model: auto
  identity: Run a multi-agent code review of a git diff range using parallel Claude subagents.
  scope:
    covers:
      - reviewing the current branch's changes (default = upstream..HEAD, with auto-detect fallbacks)
      - reviewing an explicit ref / range / staged / working-tree mode
      - reviewing an in-progress merge or rebase
      - CLAUDE.md compliance audits in a git repo
      - bug audits scoped to introduced code
      - surfacing path-scoped pre-submit reminders (submit gates) from CLAUDE.md
    excludes:
      - Perforce workflows (use /p4-code-review)
      - reviewing a remote PR by URL or PR number (this skill works against the local working copy / refs only)
      - persisting review output to disk or a PR comment
      - enforcing submit gates (advisory only; enforcement belongs in a pre-push hook)
  techniques:
    - id: full_review
      name: Full multi-agent review
      keywords: [code review, git review, branch review, multi-agent review, claude.md compliance, parallel reviewers, pre-push review]
      goal: Produce a markdown summary of confirmed issues for one git diff range.
      preconditions:
        - cwd is inside a git repository.
      steps:
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
          expected: The script's stdout JSON includes `range` and (when auto-detected) `auto_detected_reason`. Restate the chosen range to the user in the step-1 narration line so they can correct if the wrong one was inferred.
        - n: 2
          action: |
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
            twice.
            Then run prepare_review.py to fetch the diff, partition it into chunked .diff fragments on disk, enumerate changed files via `git diff --name-status`, map ancestor CLAUDE.md files for each, detect untracked-or-unstaged files in the directories the diff touches, detect unresolved merge conflicts, and scan ancestor CLAUDE.md files for submit-gate reminders that apply to this range.
            After prepare returns, emit the launch rationale line ONCE (see narration.launch_message):
            select the row from the file-type mix of the changed + claimed files, or the md_trivial row
            when the step-6 triviality gate will fire. This is the single launch message -- do not repeat it.
          tool: ${CLAUDE_PLUGIN_ROOT}/scripts/prepare_review.py
          input: "<range or argument from step 1>  (append `--claim '**/*.md'` when md-domain is available, per the claim probe)"
          expected: |
            JSON with vcs, range, head_sha, branch, description, project_root, bundle_dir, diff_chunks, changed_files, unique_claude_mds, untracked_or_unstaged, merge_conflicts, submit_gates, change_id, ledger_baseline, ledger_hits, -- only when --claim was passed -- claimed_files, and -- only when a changed file was detected as machine-emitted -- machine_emitted_files (each entry carries identifier, local, size_bytes, and the axis that matched -- machine_emitted_axis `content` or `declared_path` plus the naming machine_emitted_signature; such files are excluded from diff_chunks and changed_files, and `--review-machine-emitted` turns that exclusion off). The raw diff text is NOT inline -- it lives in per-chunk files at `<bundle_dir>/<diff_chunks[i].path>` (paths are relative to bundle_dir). Each `changed_files` entry carries `chunk_index` pointing to the chunk that contains its diff.
          on_failure: Surface the stderr message to the user and stop. No retry.
        - n: 3
          action: |
            If bundle.untracked_or_unstaged is non-empty, list the files (grouped by `kind`: untracked / unstaged_modified / unstaged_deleted / staged_uncommitted) and ask the user whether any should be folded into the review before reviewers spawn.
            - If the user picks one or more untracked / unstaged files: run `git add <paths>` to stage them, optionally commit them with `git commit -m "<message>"` to include in the range, and re-run prepare_review.py with the same range. Use the new bundle.
            - For `staged_uncommitted` files the user wants in: same flow -- commit them so they land in the range. (`--staged` mode already includes them; the prompt is for committed-range modes.)
            - If the user declines all: continue with the current bundle.
            On the post-fold re-run, do NOT prompt again about untracked_or_unstaged files even if some remain -- the user already decided.
            Skip this step entirely if bundle.untracked_or_unstaged is empty.
          tool: AskUserQuestion + git add/commit + prepare_review.py
        - n: 4
          action: |
            Read every CLAUDE.md path in unique_claude_mds. Subagents do not need to re-read.
            Also resolve the EXECUTABLE review-profile table -- profile ids, reviewer rosters,
            per-reviewer models, and validator_models -- by running python3 ${CLAUDE_PLUGIN_ROOT}/scripts/render_review_profiles.py with
            `--project-root <bundle.project_root>` (omit the flag when bundle.project_root is
            unset; the resolver then falls back to the process cwd). NEVER merge the
            review-profile config layers (shipped / user / project) yourself -- the renderer is
            the only merge. Its stdout is the merged `profiles` table as YAML, followed by a
            `---` separator and layer provenance; parse only the YAML above the separator. Keep
            the resolved `profiles` list for steps 6 and 7. See references/configuration.md for
            the full layer/merge/override contract.
            The renderer may also print `peer_when_available:` lines on STDERR. Each one names a
            lane whose resolved `model` was replaced by a peer endpoint, and it is the only
            record that the lane did not run on the model the shipped table states. Keep every
            such line and repeat it verbatim in the step-9 review header, under
            `## Peer-seat substitutions`. Never drop it, and never edit the resolved table to
            undo it. Silence on stderr means no substitution happened; there is nothing to
            report and nothing to install.
          tool: Read + python3 ${CLAUDE_PLUGIN_ROOT}/scripts/render_review_profiles.py
        - n: 5
          action: |
            If bundle.submit_gates is non-empty, DISCHARGE each gate yourself. Do NOT ask the
            user to confirm it.
            A submit gate is an instruction to whoever performed the work in this range. In an
            agent-driven session that is YOU: you made these edits, so you are the one who can
            say whether the obligation is met. Asking the user "which of these have you already
            done?" asks them to account for work they did not do -- they cannot answer it, and
            an "I don't know" is neither a confirmation nor a decline, so the gate collects
            nothing. A gate is preflight, and preflight is the operator's job, not the
            passenger's.
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
            Select one profile from the RESOLVED table fetched in step 4, using
            `review_profiles.profiles[].selection.guidance` above (this SKILL's guidance prose,
            each entry naming the profile it documents) -- this is an inference call, not regex.
            Read each profile's guidance, weigh the actual contents of `bundle.changed_files`, and
            pick the most appropriate profile id from the resolved table. Default to `code` when
            uncertain. `profile` below is that resolved-table entry -- its `reviewers` and
            `validator_models` come from step 4, never hand-constructed.
            Dispatch rule (deterministic -- compute the number, do not eyeball it): let
            lanes = R x K, where R = len(profile.reviewers) (2 for data_only, 3 for code)
            and K = len(bundle.diff_chunks). If lanes <= 6, launch the reviewer subagents
            DIRECTLY as parallel background Agent calls in a single message (the default
            path, steps 6-7 as written below). If lanes > 6, hand the reviewer fan-out and
            the validator wave to the Workflow tool instead of launching inline. Same
            reviewers, same validators, same output either way -- only the dispatch
            mechanism changes.
            Model-kind rule (per lane, mechanical -- read the value, do not interpret it):
            each reviewer's `model` and each `validator_models[reason]` value from the
            RESOLVED table is EITHER one of the Agent tool's aliases -- `sonnet`, `opus`,
            `haiku`, `fable` -- OR an llm-scripting-kit endpoint id. An alias launches an
            Agent subagent (the default path; the shipped table is
            all aliases, so an unconfigured review behaves identically to before). Any other
            value is an endpoint id: run that lane as a parallel Bash call to
            python3 ${CLAUDE_PLUGIN_ROOT}/scripts/run_review_lane.py instead of launching an Agent for it, passing `--lane <reviewer
            name>`, `--model <the value>`, `--chunk <absolute chunk diff path>`, one
            `--file` per repo-relative path in that chunk, `--description <the change
            description>`, and `--project-root <bundle.project_root>` when the bundle has
            one. Its stdout is a JSON envelope whose `issues` array is that lane's candidate
            issues, in the same shape an Agent lane returns.
            Endpoint lanes and Agent lanes go out in the SAME message as one another; mixing
            the two dispatch mechanisms in one fan-out is normal and expected.
            A NON-ZERO exit is a FAILED lane, never an empty result: do NOT retry it, do NOT
            silently substitute an Agent, and do NOT treat its absence as "no issues found".
            Keep its stderr line, report the lane as failed in step 9, and mark its coverage
            missing. Only the lanes the runner supports may carry an endpoint id; it refuses
            the rest by name and exits 2, which is a configuration error for the user to fix,
            not something to work around.
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
            section.
            Then launch one subagent per (reviewer × chunk) pair in parallel via
            a single message with R × K Agent calls, where R = len(profile.reviewers) and
            K = len(bundle.diff_chunks). Each subagent gets the chunk's absolute diff path
            (`<bundle.bundle_dir>/<diff_chunks[i].path>`), the repo-relative paths of the files
            in that chunk (`diff_chunks[i].files`), and -- for reviewer_a -- the CLAUDE.md
            mapping restricted to those files. Reviewers not listed in the selected profile are
            NOT launched. If bundle.diff_chunks is empty (range has no diff content), skip
            step 6 and jump to step 9 with zero issues.
          tool: Agent (per the model-kind rule, a lane whose model is an endpoint id runs as a Bash call to python3 ${CLAUDE_PLUGIN_ROOT}/scripts/run_review_lane.py instead)
          expected: JSON arrays of candidate issues from each launched reviewer (one array per (reviewer, chunk) lane), plus a recorded failure for any lane that exited non-zero.
        - n: 7
          action: |
            Launch one validator subagent per candidate issue, all in parallel via a single message.
            Use the selected profile's `validator_models[reason]` (from the RESOLVED table fetched
            in step 4) to pick the model per issue. The model-kind rule from step 6 applies here
            too, but no validator lane is endpoint-eligible: the runner refuses one and
            exits 2, because the validator is the control that suppresses a weak reviewer's noise
            and must not be replaced in the same change as a reviewer. An endpoint id in
            `validator_models` is therefore a configuration error to report, not a lane to run.
          tool: Agent
          expected: CONFIRMED or REJECTED per issue.
        - n: 8
          action: Drop rejected issues from the findings. Do not detail them, but state the count in one line ("N candidate issues did not survive validation") so the user can ask rather than being told nothing.
        - n: 9
          action: |
            Render the markdown review.
            - When any lane FAILED (an endpoint-dispatched reviewer that exited non-zero in
              step 6, or a lane refused as a configuration error), prepend a `## Lane failures`
              section naming each failed lane, the model it was configured with, and the
              runner's stderr reason. State plainly which files that lane would have covered
              and that they did NOT receive its review. This section is not decoration: the
              rest of the review looks identical whether a lane ran or not, so without it a
              partial review is indistinguishable from a complete one. Never describe a
              failed lane's files as clean, and never re-run the lane on a different model to
              paper over the gap -- report it and let the user decide.
            - When the step-4 renderer printed any `peer_when_available:` line on stderr,
              prepend a `## Peer-seat substitutions` section carrying each line verbatim. A
              substituted lane ran on a DIFFERENT model from the one the review-profile table
              ships, and the rendered review looks identical either way, so omitting this
              would let the reader believe a model reviewed their change that never saw it.
              This is a disclosure, not a warning: the substitution is the configured
              behaviour and nothing needs fixing.
            - When `bundle.submit_gates` is non-empty, prepend a `## Submit checklist`
              section, each gate carrying its step-5 verdict and the evidence for it.
            - When `bundle.merge_conflicts` is non-empty, prepend a `## Unresolved merge conflicts`
              section listing each conflicted file. This is informational, not a finding --
              the merge cannot be completed until each file is resolved (`git add <file>`
              after editing), but the review still renders.
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
              line; it is advisory, not a blocker.
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
              section.
            - Declined-findings ledger: `bundle.ledger_hits` lists findings the author previously
              DECLINED for this same range whose baseline is still valid. Before the decision
              pass, compute each current code-review issue's and md-domain finding's ledger key
              (code-review: file + reason + normalized-description anchor; md-domain: file + criterion +
              taxonomy + normalized-message anchor -- never line numbers or exact wording; see
              references/declined-ledger.md) and, when it matches a `bundle.ledger_hits` entry, render it
              COLLAPSED under a one-line `previously declined (N): <labels>` note in its own section
              (code-review issues under the issue list; md-domain findings under the md-domain section)
              and do NOT re-ask it in the decision pass. EXCEPTION: a SERIOUS-severity md-domain finding
              is NEVER collapsed -- it always renders and is always decided, even against a ledger hit.
              The ledger is advisory memory, not a gate.
            Group the review body by file.
        - n: 10
          action: |
            Record declined findings so the next review of this same range does not
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
          tool: ${CLAUDE_PLUGIN_ROOT}/scripts/prepare_review.py
          input: "--ledger-record <bundle.bundle_dir>/declined.json"
      checklist:
        - Diff range resolved (auto-detected from workspace state OR explicit user arg) and surfaced in the step-1 narration line
        - Context bundled via prepare_review.py
        - Untracked/unstaged files surfaced (and either folded in via `git add`/`git commit` with a re-run, or explicitly declined)
        - All CLAUDE.md files read
        - Submit gates discharged by the agent (if any), each with a MET / NOT APPLICABLE / NOT MET / NEEDS THE USER verdict and its evidence
        - Executable review-profile table resolved via render_review_profiles.py (step 4); profile selected from the resolved table using review_profiles guidance
        - Reviewers launched in parallel (single message, R × K Agent calls -- one per (reviewer × chunk) pair, where K = len(bundle.diff_chunks))
        - Validators launched in parallel (single message, N Agent calls), models picked from the profile's validator_models
        - Filtered to confirmed-only
        - Launch rationale line emitted once (file-type-driven; md_trivial variant when the change is all-mechanical)
        - md-domain subject-lens pass launched for the NON-TRIVIAL bundle.claimed_files when skills-kit md-domain is available (or claimed files folded back into the generic review on version-skew fallback); skipped silently when md-domain is absent
        - Trivial claimed files (prepare's `trivial` flag) reported via the `## Mechanical checks (audit skipped)` section, never as an audit or DIFF-CLEAN; nothing written to the ledger for them; whole review skipped when every claimed file is trivial and there are no generic diff chunks
        - Machine-emitted artifacts (bundle.machine_emitted_files) reported via the `## Machine-emitted artifacts (not reviewed)` section, naming each file's exclusion axis (content banner or declared plugin-write path) and the rule that matched, never as an audit or DIFF-CLEAN; review of machine-emitted output belongs on the generator
        - Previously-declined findings collapsed via the ledger (bundle.ledger_hits); SERIOUS md-domain findings never collapsed
        - Markdown rendered to chat (Submit checklist section prepended when gates applied; Unresolved merge conflicts section prepended when bundle.merge_conflicts is non-empty; separate `## md-domain (subject-lens) findings` section when the md-domain pass ran)
        - Newly declined findings recorded to the ledger via `prepare_review.py --ledger-record` (skipped when nothing was declined)
      gotchas:
        - Always quote the exact CLAUDE.md rule text when flagging a claude_md issue. If you cannot quote it verbatim, do not flag it.
        - Sequential reviewer or validator calls waste time. Reviewers run in one message with one concurrent Agent call per (reviewer × chunk) pair (R reviewers × K chunks). For a small diff (K=1) that's still 2 calls for data_only / 3 for code; for a large diff (K=N) it scales to R × N. Validators run in one message with N concurrent Agent calls.
        - Each reviewer subagent reads ONE chunk path, not the whole diff. Do not pass `bundle_dir` and expect the subagent to glob -- pass the absolute chunk path the subagent should Read.
        - Render only -- this skill outputs in chat. There is no PR comment or disk write step.
        - If prepare_review.py fails, report the error and stop. No retry.
        - Validators are independent of reviewers. The validator does not see who flagged the issue.
        - The untracked/unstaged check must happen BEFORE reviewers spawn. Folding in forgotten files after agents have already reviewed the diff wastes their work and produces a stale review.
        - On the post-fold re-run, do NOT prompt again about untracked_or_unstaged files. The user already chose. Re-prompting on the same list is annoying; re-prompting on a smaller list (because they only added some) implies the rest were forgotten when they were declined.
        - Submit gates are reminders, not findings -- they do NOT go through reviewer or validator subagents. They are parsed deterministically by prepare_review.py and rendered verbatim in a separate output section. Do not try to validate, score, or filter them.
        - A submit gate is addressed to whoever did the work, and in an agent-driven session that is YOU. Discharge it yourself against the change; never ask the user which obligations they have completed. They did not make these edits and cannot answer, and an "I don't know how to answer this" is neither a confirmation nor a decline -- the gate then collects nothing while appearing to have run. Preflight is the operator's job, not the passenger's.
        - A MET verdict means met WITH EVIDENCE. Name the file, the key and its default, the test, or the command and its result. A verdict with no evidence is the same empty signal as an unanswered prompt, just harder to notice.
        - NEEDS THE USER is for a fact you cannot derive -- an external system's state, a check that only runs on their hardware, an intent only they hold. It is not an escape hatch for a gate that is tedious to evaluate, and when you do use it, ask for that specific fact rather than asking whether they did the work.
        - A NOT MET gate is a finding. Render it and do not describe the review as clean.
        - Merge conflicts are NOT findings -- they do NOT go through reviewer subagents. They are detected deterministically by prepare_review.py (`git ls-files -u`). The reviewers see the raw diff (including any conflict markers) and may legitimately flag bugs in it; the merge-conflicts section is a separate informational warning to the user.
        - Auto-detect is convenient, not authoritative. Always restate the chosen range in the step-1 narration line; a user reviewing the wrong branch will catch it there before subagents spawn.
        - Detached HEAD with no main/master fallback is a real failure mode; surface the error and ask for an explicit range. Do not guess at a "probably right" base.
        - md-domain findings are a SEPARATE, labeled section -- never interleave them with the code-review issue list. They come from md-domain's detect lanes (a subject-lens reviewer), not from the generic reviewer/validator subagents, so they are not filtered by the validators.
        - The claim decision happens ONCE, at the step-2 probe: md-domain available -> `--claim '**/*.md'` (one glob covering CLAUDE.md, SKILL.md, a skill's `references/*.md`, and generic docs); md-domain absent -> no `--claim`. Claiming a skill's `references/*.md` assumes the INSTALLED audit_skill lane owns that subject shape; these kits declare no version constraint on skills-kit, so step 6 probes for it by capability and the skill-reference skew tier re-adds the exclusion when it is missing. Do not run prepare a second time just to add claims -- the only re-runs are the version-skew FALLBACKS (broad skew re-runs WITHOUT `--claim`; project-doc-only skew re-runs with `--claim '**/CLAUDE.md' --claim '**/SKILL.md' --claim '**/skills/*/references/*.md'`; skill-reference skew re-adds the `!**/skills/*/references/*.md` exclusion as a compatibility shim).
        - Claimed `.md` files route THREE ways in step 6 -- `CLAUDE.md` -> the `audit_claude_md` lane; `SKILL.md` OR a file inside a `*/skills/<name>/references/` folder -> the `audit_skill` lane (its two subject shapes); every other `.md` -> the `audit_project_doc` lane (full routing table in references/md-domain-review.md; `.md.html` is never claimed). Never claim a shape no lane can audit: a declined file comes back NOT-AUDITED, which a caller can misread as a pass.
        - A `NOT-AUDITED` verdict from a lane is NOT a pass. It means the lane declined the file as outside its criteria and read nothing. Render it as its own line, never fold it into the clean count, and never let it satisfy a submit gate -- treat it like the `## Mechanical checks (audit skipped)` section: an honest "not reviewed", not a result. Seeing one on a claimed file means the claim routing sent a file somewhere that cannot audit it; report that rather than accepting the verdict.
        - When skills-kit md-domain is absent the whole mechanism degrades silently: no `--claim`, no claimed_files, no md-domain section -- the md files get today's thin generic data_only coverage. Note the degradation in one line; do not treat it as an error.
        - The triviality gate is pure-mechanical and decided by prepare_review (per-claimed-file `trivial` / `trivial_reasons`); the skill never re-judges it. A TRIVIAL claimed file is reported via the mechanical-checks line and is NEVER sent to a detect lane or written to the ledger. When EVERY claimed file is trivial and there are no generic diff chunks, the whole audit is skipped -- render the `## Mechanical checks (audit skipped)` section, never a DIFF-CLEAN verdict, and never present the skip as an audit. A user or author asking for the full review overrides the gate.
        - The Workflow tool is unavailable inside subagents. Launch the md-domain detect-lane Workflow from the MAIN session (the same message that fans out the reviewers), never from within a reviewer subagent.
        - A machine-emitted file is NEVER a pass. `bundle.machine_emitted_files` means "not reviewed", exactly like a `NOT-AUDITED` verdict or the `## Mechanical checks (audit skipped)` section: render it as its own honest line, never inside the clean count, never as DIFF-CLEAN, and never as satisfying a submit gate.
        - Detection is a UNION of two axes, decided by prepare_review, and the skill never re-judges it -- `content` (a generated-artifact banner) OR `declared_path` (the file lives under a path a plugin declares that it writes, such as a project's durable plugin-data directory). Either one is sufficient, and the second is what catches a generator that emits no banner at all -- nothing in such a file's bytes says a tool wrote it, but its location does, by construction.
        - Size is NEVER a criterion on either axis. A large hand-written file is chunked and fully reviewed as always; a small machine-emitted file is still excluded. The argument is authorship, not cost.
        - Do not review a machine-emitted artifact by reading it. If its content looks wrong, the finding belongs on the generator, or on the decision to check the artifact in -- say that, and name the generator when this change contains one.
        - The `--review-machine-emitted` flag is the override and it is the AUTHOR's call, never an inference. Pass it only when the user or the author explicitly asks for the machine-emitted files to be reviewed.
        - The declined-findings ledger is advisory memory, not a gate. A collapsed finding is one the author already declined for THIS change at THIS baseline; when the baseline moves (the range base SHA advances -- origin/main moves, or HEAD changes for a working-tree review) the entry goes stale and the finding re-surfaces on its own. Never let a ledger hit suppress a SERIOUS md-domain finding.
        - Record declined findings ONLY through `prepare_review.py --ledger-record <json>`. Never hand-edit ledger.json -- the key normalization (criterion/reason + taxonomy + normalized anchor) must be computed deterministically, not typed.
        - The `review_profiles` block above is SELECTION GUIDANCE AND RATIONALE ONLY. It carries no reviewer roster, model, or validator_models -- that executable table is resolved per review by python3 ${CLAUDE_PLUGIN_ROOT}/scripts/render_review_profiles.py (step 4), which merges the shipped bootstrap_lib defaults with any `~/.claude/config/review_profiles.yaml` (user) or `<project_root>/.claude/review_profiles.yaml` (project) override. Never merge those layers yourself and never hand-edit the resolved output.
        - The `profile` in steps 6-7 is always an entry from that RESOLVED table, never the guidance block. Match the guidance prose to decide which profile id fits the change, then read `reviewers` and `validator_models` off the resolved entry with that id.
        - See references/configuration.md for the layer precedence, merge rules (profiles/reviewers merge by id/name; validator_models and other mappings deep-merge; `disabled: true` removes a record; plain lists like `data_only_extensions` replace), the shipped default table, what a `model` value may name, which lanes may take an endpoint id, what happens when an endpoint lane fails, and how a reviewer record's `peer_when_available` resolves a peer seat (plus the `--explain-peer-seats` diagnostic).
        - A `model` value is NOT always an Agent-tool model. The four aliases `sonnet`, `opus`, `haiku` and `fable` name the Agent tool; every other value is an llm-scripting-kit endpoint id and that lane runs through python3 ${CLAUDE_PLUGIN_ROOT}/scripts/run_review_lane.py instead (step 6's model-kind rule). The shipped table is all aliases, so a review with no user or project override dispatches every lane as an Agent subagent.
        - A reviewer record may set `peer_when_available` true, asking the renderer to run that lane on a reachable PEER endpoint -- same tier as the stated model, different model family -- when llm-scripting-kit is installed and current. The renderer resolves it, so the table you read already carries the substituted endpoint id as that lane's `model`, and the lane dispatches through python3 ${CLAUDE_PLUGIN_ROOT}/scripts/run_review_lane.py under the ordinary step-6 model-kind rule. Do not probe for a peer yourself, and do not treat a substituted value as an override the user forgot to make.
        - An endpoint lane that fails is a FAILED lane. There is no fallback to an Agent, by design: silently substituting one produces a review the user reads as having run on the model they configured, which is a false claim about the change's coverage. Report it and mark the coverage missing.
  narration:
    note: Reviews involve long silent stretches (batched file reads, parallel subagents that take 30s+). Post one short status line per step using these templates verbatim, filling in the bracketed counts. Do not paraphrase, omit, or add extras.
    templates:
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
        template: "Selected review profile: <P>. Diff partitioned into <K> chunk(s). Launching <RK> subagent(s) in parallel (<R> reviewer(s) × <K> chunk(s)): <reviewer_summary>."
      - when: "After step 6, before step 7 (X >= 1)"
        template: "Reviewers returned <X> candidate issue(s) (<B> bug, <C> CLAUDE.md). Launching <X> validator(s) in parallel."
      - when: "After step 6 (X = 0)"
        template: "Reviewers found no issues. Skipping validation."
      - when: "After step 7, before step 9"
        template: "Validators confirmed <Y> of <X>. Rendering review."
    variables:
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
      "<V>": "len(bundle.merge_conflicts)"
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
        all_md: "Running git-code-review: this audits .md file changes against project standards and verifies references."
        all_data: "Running git-code-review: this checks the changed config files for schema, reference, and consistency problems."
        mixed: "Running git-code-review: this reviews the code changes and audits the .md changes against project standards."
        all_code: "Running git-code-review: this reviews the changes for bugs and project-standard compliance."
        md_trivial: "Running git-code-review: the .md changes are mechanical (typo-sized); running quick standards checks only."
      style: |
        State what is running and what it does, in plain short sentences, and let the reader draw the
        conclusion. Two anti-patterns are BANNED (they read as defensive and invite the doubt they try
        to pre-empt):
          - Negative direction -- "don't stop this", "do not skip", "this will only take a second".
            Never tell the reader what NOT to do.
          - Asserting it is not a mistake -- "this is not an error", "don't worry, this is intentional".
            Informing plainly already makes that self-evident; asserting it invites doubt.

  review_profiles:
    description: |
      Routing table for selecting reviewers and models based on diff content. Exactly one
      profile is selected per review. Selection is an inference call -- read each profile's
      `selection.guidance` below and pick the most appropriate one based on the actual contents
      of `bundle.changed_files`. Default to `code` when uncertain.
      The EXECUTABLE table -- profile ids, reviewer rosters, per-reviewer models, and
      validator_models -- is NOT inline here. It is resolved at review time by step 4
      (python3 ${CLAUDE_PLUGIN_ROOT}/scripts/render_review_profiles.py), which merges the shipped bootstrap_lib defaults with any user/project
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
                  including them in a diff shouldn't force the heavier `code` profile.
            Use judgment: the question is "is there any file in this diff that needs Opus-level
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
      subagent_type: general-purpose
      scope: CLAUDE.md compliance only, restricted to the files in one chunk
      input: "absolute path to ONE chunk .diff file, the repo-relative paths of the files in that chunk, the per-file CLAUDE.md mapping restricted to those files, and the full text of each relevant CLAUDE.md (read in step 4)"
      canonical_prompt_note: |
        This lane can run EITHER as an Agent subagent or, when its resolved `model` is an
        endpoint id, as a plain completion (see the step-6 model-kind rule). Both paths must
        review by the same standard, so the prompt below is the single source: it is rendered
        here from bootstrap_lib.code_review.lane_prompts, which is also what the endpoint
        runner sends. When launching this lane as an Agent, use it as the subagent's
        instructions verbatim, then append the chunk path, file list, and (per step 4) the
        per-file CLAUDE.md mapping and text. Do not paraphrase it.
      canonical_prompt: |
        You are reviewing one chunk of a code change for violations of the project's own
        written standards. You are one of several independent reviewers; other concerns
        belong to other reviewers.

        Scope. Project-standard compliance only. Every issue you report uses the reason
        "claude_md" and carries the exact rule text in "citation"; if you cannot quote
        the rule, you do not have a finding.

        Governing standards. The standards live in CLAUDE.md files inside this
        repository. For each file in the diff, the governing CLAUDE.md files are the
        one in that file's own directory and every CLAUDE.md in a parent directory up
        to the repository root. A CLAUDE.md that does not share a path with the file
        being reviewed does not govern it -- never cross-apply a rule between
        directories.

        Context you must gather yourself, unless it is supplied with the chunk. If a
        per-file CLAUDE.md mapping and/or the text of the relevant CLAUDE.md files is
        supplied to you alongside the chunk, use exactly those and do not go looking
        for more. Otherwise, for each file in the diff, read the CLAUDE.md in that
        file's own directory and every CLAUDE.md in a parent directory up to the
        repository root yourself. Either way, read only CLAUDE.md files: no source
        files, no documentation, no history.

        Restrictions. Only report issues in files that appear in this diff, and only for
        what this change introduces -- a pre-existing violation is not yours to report.

        Only flag an issue when it is one of these:
        - code that will fail to compile or parse (syntax errors, type errors, missing
          imports, unresolved references)
        - code that will definitely produce wrong results regardless of inputs (clear
          logic errors)
        - a project-standard rule clearly and unambiguously violated, with the exact
          rule quotable

        Never flag any of these:
        - code style or quality concerns
        - potential issues that depend on specific inputs or state
        - subjective suggestions or improvements
        - pre-existing issues (only review the diff)
        - anything a linter would catch (do not run a linter)
        - issues that appear in a standards file but are explicitly silenced in the
          code (for example a lint-ignore comment)

        If you are not certain an issue is real, do not flag it. False positives erode
        trust: an empty array is a perfectly good answer and is much better than a
        speculative finding.

        Respond with a JSON array and nothing else. No prose before it, no prose after
        it, no Markdown code fence. Each element is an object with exactly these keys:

          "file"        the path of the file the issue is in, as it appears in the diff
          "lines"       the affected line or range, for example "42" or "42-48"
          "reason"      exactly "bug" or "claude_md"
          "description" one sentence explaining the problem
          "citation"    optional, and only for "claude_md": the exact rule text quoted

        Return [] when there is nothing to report.
      restrictions:
        - "Read the assigned chunk diff once (single Read call). Do not Read other chunks."
        - "Only consider CLAUDE.md files that share a path with the file being reviewed (use the per-file mapping when supplied; do not cross-apply)."
        - "Only flag issues in files present in your chunk -- files in other chunks are someone else's responsibility."
    - name: reviewer_b_diff_only_bugs
      subagent_type: general-purpose
      scope: obvious bugs visible in one chunk's diff alone
      input: "absolute path to ONE chunk .diff file, the repo-relative paths of the files in that chunk, and the diff description"
      canonical_prompt_note: |
        This lane can run EITHER as an Agent subagent or, when its resolved `model` is an
        endpoint id, as a plain completion (see the step-6 model-kind rule). Both paths must
        review by the same standard, so the prompt below is the single source: it is rendered
        here from bootstrap_lib.code_review.lane_prompts, which is also what the endpoint
        runner sends. When launching this lane as an Agent, use it as the subagent's
        instructions verbatim, then append the chunk path and file list. Do not paraphrase it.
      canonical_prompt: |
        You are reviewing one chunk of a code change for obvious bugs that are visible
        in the diff alone. You are one of several independent reviewers; other files
        and other concerns belong to other reviewers.

        Scope. Report only won't-compile problems, syntax and type errors, missing
        imports, unresolved references, and logic that is definitely wrong regardless
        of inputs. For data and documentation files, report malformed syntax, duplicate
        keys, schema or column-count violations, and broken cross-file references.

        Restrictions. The diff below is everything you get and everything you may
        consider. Do not ask for other files, do not reason about code you cannot see,
        and do not report an issue in a file that does not appear in this diff.

        Only flag an issue when it is one of these:
        - code that will fail to compile or parse (syntax errors, type errors, missing
          imports, unresolved references)
        - code that will definitely produce wrong results regardless of inputs (clear
          logic errors)
        - a project-standard rule clearly and unambiguously violated, with the exact
          rule quotable

        Never flag any of these:
        - code style or quality concerns
        - potential issues that depend on specific inputs or state
        - subjective suggestions or improvements
        - pre-existing issues (only review the diff)
        - anything a linter would catch (do not run a linter)
        - issues that appear in a standards file but are explicitly silenced in the
          code (for example a lint-ignore comment)

        If you are not certain an issue is real, do not flag it. False positives erode
        trust: an empty array is a perfectly good answer and is much better than a
        speculative finding.

        Respond with a JSON array and nothing else. No prose before it, no prose after
        it, no Markdown code fence. Each element is an object with exactly these keys:

          "file"        the path of the file the issue is in, as it appears in the diff
          "lines"       the affected line or range, for example "42" or "42-48"
          "reason"      exactly "bug" or "claude_md"
          "description" one sentence explaining the problem
          "citation"    optional, and only for "claude_md": the exact rule text quoted

        Return [] when there is nothing to report.
      restrictions:
        - "Read the assigned chunk diff once. MUST NOT use Read for anything beyond that chunk."
        - "Only flag won't-compile, syntax/type errors, missing imports, unresolved references, definitely-wrong logic regardless of inputs."
        - "For data/doc files (data_only profile): focus on malformed syntax, duplicate keys, schema or column-count violations, and broken cross-file references."
        - "Only flag issues in files present in your chunk."
    - name: reviewer_c_introduced_code
      subagent_type: general-purpose
      scope: bugs/security/logic problems in the introduced code that need broader context, restricted to one chunk's files
      input: "absolute path to ONE chunk .diff file, the repo-relative paths of the files in that chunk, local paths for those files, and the diff description"
      canonical_prompt_note: |
        This lane can run EITHER as an Agent subagent or, when its resolved `model` is an
        endpoint id, as a plain completion (see the step-6 model-kind rule). Both paths must
        review by the same standard, so the prompt below is the single source: it is rendered
        here from bootstrap_lib.code_review.lane_prompts, which is also what the endpoint
        runner sends. When launching this lane as an Agent, use it as the subagent's
        instructions verbatim, then append the chunk path, file list, local paths, and change
        description. Do not paraphrase it.
      canonical_prompt: |
        You are reviewing one chunk of a code change for bugs in the code it
        INTRODUCES -- the ones the diff alone cannot settle because they turn on the
        code around the change. You are one of several independent reviewers; other
        files and other concerns belong to other reviewers.

        Scope. Logic errors, concurrency and lifetime bugs, resource leaks, and security
        holes in the introduced code. Report only what this change introduces, never a
        pre-existing problem.

        Context you must gather yourself. You may read the files listed below, at the
        paths as given, to see the code surrounding the change. Read only those files.
        Do not modify anything, do not run anything, and do not go browsing the rest of
        the repository.

        Restrictions. Only report issues in files that appear in this diff. When the
        context you would need to settle an issue is not in one of those files, you
        cannot settle it -- do not report it.

        Only flag an issue when it is one of these:
        - code that will fail to compile or parse (syntax errors, type errors, missing
          imports, unresolved references)
        - code that will definitely produce wrong results regardless of inputs (clear
          logic errors)
        - a project-standard rule clearly and unambiguously violated, with the exact
          rule quotable

        Never flag any of these:
        - code style or quality concerns
        - potential issues that depend on specific inputs or state
        - subjective suggestions or improvements
        - pre-existing issues (only review the diff)
        - anything a linter would catch (do not run a linter)
        - issues that appear in a standards file but are explicitly silenced in the
          code (for example a lint-ignore comment)

        If you are not certain an issue is real, do not flag it. False positives erode
        trust: an empty array is a perfectly good answer and is much better than a
        speculative finding.

        Respond with a JSON array and nothing else. No prose before it, no prose after
        it, no Markdown code fence. Each element is an object with exactly these keys:

          "file"        the path of the file the issue is in, as it appears in the diff
          "lines"       the affected line or range, for example "42" or "42-48"
          "reason"      exactly "bug" or "claude_md"
          "description" one sentence explaining the problem
          "citation"    optional, and only for "claude_md": the exact rule text quoted

        Return [] when there is nothing to report.
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
        "file": "<repo-relative or absolute path>",
        "lines": "<line range, e.g. 42 or 42-48>",
        "reason": "bug" | "claude_md",
        "description": "<one-sentence explanation>",
        "citation": "<exact rule quote, only for claude_md issues>"
      }]
  submit_gates:
    description: |
      Path-scoped pre-push reminders authored in CLAUDE.md files. Surfaced verbatim at
      review time when at least one file in the range falls within the gate's scope.
      Reminders are not findings -- they don't go through reviewer or validator subagents.
      Detection is deterministic, performed by prepare_review.py (same parser as p4-code-review).
    authoring_format: "See references/submit-gates.md for the CLAUDE.md-author-facing guide to writing submit-gate blocks (block format, scope path semantics, multi-gate rules)."
    rendering: |
      When bundle.submit_gates is non-empty, the rendered review prepends a
      `## Submit checklist` section ABOVE the per-file review body. Each gate renders as:

        - **[✓|✗|-|?] <summary>** -- per `<source>`, triggered by `<file>` (+N more if many).
          <the step-5 verdict, then the evidence for it, on one line>
          > <rationale, indented as blockquote, omitted if empty>

      ✓ = MET. The evidence line names what satisfies it -- a file, a key and its
              default, a test, or a command and its result.
      ✗ = NOT MET. The obligation applies and is unsatisfied; this is a finding, and
              the review is not clean.
      -     = NOT APPLICABLE. Scope matched but the subject is absent from this change;
              the evidence line says why.
      ?     = NEEDS THE USER. Turns on a fact not derivable from the repo, the diff, or
              this session; the evidence line names the specific fact wanted.

      Every gate carries a verdict. A gate rendered without one means step 5 was skipped,
      which is a defect, not a neutral outcome.

      Always show the section when gates applied -- including in the "no issues" path.
  output_format:
    description: "Final markdown rendered to chat. Unresolved merge conflicts (when applicable) and Submit checklist (when applicable) above the per-file review body."
    template: |
      ## Unresolved merge conflicts
      The merge cannot complete until each file below is resolved (`git add <file>` after editing).
      - `path/to/file.cpp`
      - `path/to/other.csv`

      ## Submit checklist
      - **[✓] ./build.sh configbinaries must pass before push** -- per `<path>/CLAUDE.md`, triggered by `GameConfigs/Real/x.csv`.
      - **[✗] Regenerate the asset index** -- per `<path>/CLAUDE.md`, triggered by `Content/Assets/y.uasset`.
        > <rationale if any, as a blockquote>

      ## Review: <range> -- <description>

      Branch: <branch>  ·  HEAD: <head_sha>

      Found N issues (M filtered as false positives).

      ### path/to/file.cpp
      - **[bug]** L42: Buffer overflow risk -- `items[i]` accessed without bounds check.
      - **[claude_md]** L78: Violates `src/CLAUDE.md` rule "Use absl::Status not bool returns".
    empty_template: |
      ## Submit checklist
      - **[✓] ./build.sh configbinaries must pass before push** -- per `<path>/CLAUDE.md`, triggered by `GameConfigs/Real/x.csv`.

      ## Review: <range> -- <description>

      Branch: <branch>  ·  HEAD: <head_sha>

      No issues found. Reviewed for bugs and CLAUDE.md compliance.
    notes:
      - "Omit the Unresolved merge conflicts section entirely when bundle.merge_conflicts is empty."
      - "Omit the Submit checklist section entirely when bundle.submit_gates is empty."
      - "When matched_files has >3 entries, render the first 3 then '(+N more)'."
      - "Rationale renders as a markdown blockquote (`> `) indented one level below the bullet, only if non-empty."
      - "<range>, <branch>, <head_sha>, <description> come from the top-level bundle fields."
```
