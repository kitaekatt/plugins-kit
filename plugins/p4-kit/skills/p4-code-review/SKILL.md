---
_schema_version: 1
name: p4-code-review
author: christina
skill-type: technique-skill
description: Use when reviewing a pending Perforce changelist, or before asking the user to submit a CL. Do NOT use for git diffs or submitted CLs.
---

# P4 Code Review

Run a multi-agent code review of a Perforce changelist directly in conversation. The diff is partitioned on disk into chunks (one per file boundary cluster, balanced under a 1 MB cap); reviewer subagents (set by the selected review profile) run **once per (role × chunk)** so a single large CL fans out across multiple parallel agents instead of forcing each reviewer to ingest the full diff. Each flagged issue is then validated by an independent subagent to suppress false positives. Path-scoped pre-submit reminders (submit gates) authored in ancestor CLAUDE.md files are surfaced alongside the review for author confirmation. Results are rendered as markdown -- no persistence to disk.

```yaml
technique_skill:
  _schema_version: "1"
  trigger_model: auto
  identity: Run a multi-agent code review of a Perforce changelist using parallel Claude subagents.
  scope:
    covers:
      - reviewing pending Perforce changelists by CL number
      - CLAUDE.md compliance audits in a P4 workspace
      - bug audits scoped to introduced code
      - surfacing path-scoped pre-submit reminders (submit gates) from CLAUDE.md
    excludes:
      - git diffs and non-Perforce review workflows
      - persisting review output to disk or Swarm
      - reviewing previously-submitted changelists
      - enforcing submit gates (advisory only; enforcement belongs in a pre-shelve/pre-submit hook)
  techniques:
    - id: full_review
      name: Full multi-agent review
      keywords: [code review, perforce review, CL review, multi-agent review, claude.md compliance, parallel reviewers, p4 review]
      goal: Produce a markdown summary of confirmed issues for one pending Perforce CL.
      preconditions:
        - User has at least one pending CL OR has passed a CL number argument.
      steps:
        - n: 1
          action: Resolve the CL number (from argument, else list pending CLs and prompt the user).
          tool: p4
          input: "p4 -ztag changes -s pending -u $(p4 set -q P4USER | cut -d= -f2) -m 20"
          expected: A single integer CL number confirmed by the user.
        - n: 2
          action: |
            Claim probe -- decide the `--claim` flags BEFORE invoking prepare, and invoke prepare
            only ONCE. Check whether skills-kit's md-audit skill is available in this session (it
            appears in the available-skills list as `skills-kit:md-audit`). If it IS available, add
            `--claim '**/*.md'` to the prepare invocation below so EVERY changed Markdown file (any
            `.md` at any depth, root included -- CLAUDE.md, SKILL.md, and generic docs alike) is held
            back from the generic reviewers and returned under `bundle.claimed_files` (each with a
            materialized `pre_image`) for the subject-lens md-audit pass in step 6. The single
            `**/*.md` glob supersedes the older two-glob form; `.md.html` (Markdeep) is NOT `.md`, so
            it is deliberately left to the generic reviewers. If md-audit is NOT available, invoke
            prepare with NO `--claim` flags -- degrade silently to today's behavior (the md files get
            thin generic data_only coverage), noting the degradation in one line. Do NOT run prepare
            twice.
            Then run prepare_review.py to fetch the diff (with shelved fallback; auto-shelves a pending CL with no existing shelf so the diff is fetchable), partition the diff into chunked .diff fragments on disk, map ancestor CLAUDE.md files for each changed file, detect unreconciled files in the directories the CL touches, detect unresolved merges in the CL, and scan ancestor CLAUDE.md files for submit-gate reminders that apply to this CL.
          tool: python3 ${CLAUDE_PLUGIN_ROOT}/scripts/prepare_review.py
          input: "<CL>  (append `--claim '**/*.md'` when md-audit is available, per the claim probe)"
          expected: |
            JSON with cl, description, bundle_dir, diff_chunks, changed_files, unique_claude_mds, unreconciled, unresolved, submit_gates, auto_shelved, shelf_fingerprint, change_id, ledger_baseline, ledger_hits, and -- only when --claim was passed -- claimed_files. The raw diff text is NOT inline -- it lives in per-chunk files at `<bundle_dir>/<diff_chunks[i].path>` (paths are relative to bundle_dir). Each `changed_files` entry carries `chunk_index` pointing to the chunk that contains its diff. `auto_shelved=true` means prepare_review created the shelf and step 10 must clean it up.
          on_failure: |
            Surface the stderr message to the user and stop. No retry.
            Launch note: ALWAYS invoke with an explicit `python3` interpreter (as shown in `tool:`), never as a bare path. Bare `${CLAUDE_PLUGIN_ROOT}/scripts/prepare_review.py <CL>` lets bash try to run the file as a shell script -- it has no shebang line in older checkouts and the exec bit does not survive on Windows checkouts, so bash parses the Python as sh and exits 2. The script self-relocates under the p4-kit venv via reexec, so any python3 launcher is sufficient. And NEVER pipe the invocation (`... | tail`, `... | head`): a pipe makes `$?` the last pipeline stage's status, not the script's, which silently masks a launch failure as success.
        - n: 3
          action: |
            If bundle.unreconciled is non-empty, list the files (grouped by action: add / edit / delete) and ask the user whether any should be folded into the CL before review.
            - If the user picks one or more: run `p4 reconcile -c <CL> <local-paths>` to open them directly into the CL, then re-run prepare_review.py and use the new bundle.
            - If the user declines all: continue with the current bundle.
            On the post-reconcile re-run, do NOT prompt again about unreconciled files even if some remain -- the user already decided.
            Skip this step entirely if bundle.unreconciled is empty.
          tool: AskUserQuestion + p4 reconcile + prepare_review.py
        - n: 4
          action: Read every CLAUDE.md path in unique_claude_mds. Subagents do not need to re-read.
          tool: Read
        - n: 5
          action: |
            If bundle.submit_gates is non-empty, surface each gate as a checklist item the
            author must confirm BEFORE the review renders. Issue ONE AskUserQuestion call
            with `multiSelect: true`, one option per gate, labeled with the gate's summary
            and (in the description) the source CLAUDE.md path and the triggering files.
            Phrase the question as: "Confirm each pre-submit obligation you've already
            completed for CL <CL>."
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
            Dispatch rule (deterministic -- compute the number, do not eyeball it): let
            lanes = R x K, where R = len(profile.reviewers) (2 for data_only, 3 for code)
            and K = len(bundle.diff_chunks). If lanes <= 6, launch the reviewer subagents
            DIRECTLY as parallel background Agent calls in a single message (the default
            path, steps 6-7 as written below). If lanes > 6, hand the reviewer fan-out and
            the validator wave to the Workflow tool instead of launching inline. Same
            reviewers, same validators, same output either way -- only the dispatch
            mechanism changes.
            Subject-lens md-audit pass -- run ONLY when `bundle.claimed_files` is non-empty; skip this
            entire paragraph otherwise. In the SAME message that launches the reviewer subagents (or the
            reviewer Workflow, per the dispatch rule above), ALSO invoke the Workflow tool with
            skills-kit's headless detect.js for the claimed files, routed THREE ways by basename -- at
            most THREE Workflow calls total: (a) every claimed file named `CLAUDE.md` -> claude-md-audit's
            `workflow/detect.js`; (b) every claimed file named `SKILL.md` -> skill-audit's
            `workflow/detect.js` (only if any); (c) every OTHER claimed `.md` file (generic docs) ->
            project-doc-audit's `workflow/detect.js` (only if any). Pass `review: true` and, per claimed
            file, `preImagePath` = its `pre_image` from the bundle (null for an add), with the per-member
            `files[]` fields (CLAUDE.md: role / dimension / parentPath / ancestorClaudeMdPaths; SKILL.md:
            ancestorClaudeMdPaths; project-doc: ancestorClaudeMdPaths) resolved from each claimed file's
            `claude_mds` per references/md-audit-review.md. Resolve the skills-kit plugin root and
            venvPython defensively per that reference. TWO-TIER version-skew fallback (the version-coupling
            safety valve): (1) if the claude-md-audit or skill-audit detect.js entry point / args contract
            is missing (a broad skills-kit skew), emit a one-line warning and re-run prepare_review.py
            WITHOUT any `--claim` flags so ALL claimed files rejoin the generic review; (2) if ONLY the
            project-doc-audit detect.js entry point is missing (an older skills-kit that predates
            project-doc review), fall back for JUST the generic-md claims -- emit a one-line warning and
            re-run prepare_review.py with only `--claim '**/CLAUDE.md' --claim '**/SKILL.md'` so CLAUDE.md
            and SKILL.md keep specialist coverage while the other `.md` files rejoin the generic review.
            Then proceed with the normal fan-out. When the pass runs, the md-audit Workflow(s) execute in
            PARALLEL with the reviewer fan-out; keep each `{perFile, totals, review}` for step 9's labeled
            section.
            Then launch one subagent per (reviewer × chunk) pair in parallel via
            a single message with R × K Agent calls, where R = len(profile.reviewers) and
            K = len(bundle.diff_chunks). Each subagent gets the chunk's absolute diff path
            (`<bundle.bundle_dir>/<diff_chunks[i].path>`), the depot paths of the files
            in that chunk (`diff_chunks[i].files`), and -- for reviewer_a -- the CLAUDE.md
            mapping restricted to those files. Reviewers not listed in the selected profile are
            NOT launched. If bundle.diff_chunks is empty (CL has no diff content), skip
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
            - When `bundle.unresolved` is non-empty, prepend a `## Unresolved merges`
              section listing each unresolved file with its resolve type. This is
              informational, not a finding -- the CL is not submittable until the
              user runs `p4 resolve` on each entry, but the review still renders.
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
              line; it is advisory, not a blocker.
            - Declined-findings ledger: `bundle.ledger_hits` lists findings the author previously
              DECLINED for this same CL whose baseline is still valid. Before the decision
              pass, compute each current code-review issue's and md-audit finding's ledger key
              (code-review: file + reason + normalized-description anchor; md-audit: file + criterion +
              taxonomy + normalized-message anchor -- never line numbers or exact wording; see
              references/declined-ledger.md) and, when it matches a `bundle.ledger_hits` entry, render it
              COLLAPSED under a one-line `previously declined (N): <labels>` note in its own section
              (code-review issues under the issue list; md-audit findings under the md-audit section)
              and do NOT re-ask it in the decision pass. EXCEPTION: a SERIOUS-severity md-audit finding
              is NEVER collapsed -- it always renders and is always decided, even against a ledger hit.
              The ledger is advisory memory, not a gate.
            Group the review body by file.
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
        - n: 11
          action: |
            Record declined findings so the next review of this same CL does not
            re-litigate them. After the decision pass, collect every finding the author DECLINED --
            both code-review issues they rejected and md-audit remediations they chose NOT to apply.
            Skip this step entirely when nothing was declined. Otherwise write a JSON file to
            `<bundle.bundle_dir>/declined.json`:
              {"change_id": "<bundle.change_id>", "baseline": "<bundle.ledger_baseline>",
               "declined": [
                 {"kind": "code_review", "file": "<path>", "reason": "bug"|"claude_md",
                  "description": "<the issue description>"},
                 {"kind": "md_audit", "file": "<path>", "criterion": "<criterion/group>",
                  "taxonomy": "<taxonomy>", "message": "<finding message>", "severity": "<severity>"}
               ]}
            Then run prepare_review.py --ledger-record on that file. The ledger keys each entry by a
            normalized anchor (never line numbers or exact wording) and NEVER records a SERIOUS
            md-audit finding (those always re-surface). Do NOT hand-edit the ledger JSON -- always go
            through --ledger-record so keying stays deterministic.
          tool: python3 ${CLAUDE_PLUGIN_ROOT}/scripts/prepare_review.py
          input: "--ledger-record <bundle.bundle_dir>/declined.json"
      checklist:
        - CL number resolved
        - Context bundled via prepare_review.py
        - Unreconciled files surfaced (and either folded in via `p4 reconcile -c <CL>` with a re-run, or explicitly declined)
        - All CLAUDE.md files read
        - Submit gates surfaced (if any) and author confirmation collected via a single AskUserQuestion
        - Review profile selected from review_profiles
        - Reviewers launched in parallel (single message, R × K Agent calls -- one per (reviewer × chunk) pair, where K = len(bundle.diff_chunks))
        - Validators launched in parallel (single message, N Agent calls), models picked from the profile's validator_models
        - Filtered to confirmed-only
        - md-audit subject-lens pass launched for bundle.claimed_files when skills-kit md-audit is available (or claimed files folded back into the generic review on version-skew fallback); skipped silently when md-audit is absent
        - Previously-declined findings collapsed via the ledger (bundle.ledger_hits); SERIOUS md-audit findings never collapsed
        - Markdown rendered to chat (Submit checklist section prepended when gates applied; Unresolved merges section prepended when bundle.unresolved is non-empty; separate `## md-audit (subject-lens) findings` section when the md-audit pass ran)
        - Auto-shelf cleanup invoked when bundle.auto_shelved is true (`prepare_review.py --cleanup <bundle_dir>`)
        - Newly declined findings recorded to the ledger via `prepare_review.py --ledger-record` (skipped when nothing was declined)
      gotchas:
        - Always quote the exact CLAUDE.md rule text when flagging a claude_md issue. If you cannot quote it verbatim, do not flag it.
        - Sequential reviewer or validator calls waste time. Reviewers run in one message with one concurrent Agent call per (reviewer × chunk) pair (R reviewers × K chunks). For a small CL (K=1) that's still 2 calls for data_only / 3 for code; for a large CL (K=N) it scales to R × N. Validators run in one message with N concurrent Agent calls.
        - Each reviewer subagent reads ONE chunk path, not the whole diff. Do not pass `bundle_dir` and expect the subagent to glob -- pass the absolute chunk path the subagent should Read.
        - Render only -- this skill outputs in chat. There is no Swarm comment, PR comment, or disk write step.
        - If prepare_review.py fails, report the error and stop. No retry.
        - Validators are independent of reviewers. The validator does not see who flagged the issue.
        - The unreconciled check must happen BEFORE reviewers spawn. Folding in forgotten files after agents have already reviewed the diff wastes their work and produces a stale review.
        - On the post-reconcile re-run, do NOT prompt again about unreconciled files. The user already chose. Re-prompting on the same list is annoying; re-prompting on a smaller list (because they only added some) implies the rest were forgotten when they were declined.
        - Submit gates are reminders, not findings -- they do NOT go through reviewer or validator subagents. They are parsed deterministically by prepare_review.py and rendered verbatim in a separate output section. Do not try to validate, score, or filter them.
        - The submit-gates AskUserQuestion fires once, regardless of gate count. multiSelect bundles all gates into one prompt. Re-prompting per gate is rude and adds no value -- the author's response is final either way.
        - Unconfirmed submit gates are NOT errors. Render them with ✗ so they're visible, but do not block the review or refuse to render the rest.
        - Unresolved merges are NOT findings -- they do NOT go through reviewer or validator subagents. They are detected deterministically by prepare_review.py (`p4 resolve -n -c <CL>`) and rendered verbatim in a separate output section. The reviewers see the raw diff (including any conflict markers) and may legitimately flag bugs in it; the unresolved section is a separate informational warning to the user.
        - Auto-shelf cleanup (step 10) must run whenever `bundle.auto_shelved` is true, no matter what happened in steps 3-9. The cleanup script is deterministic and safe (it only deletes the shelf when the live fingerprint exactly matches what we recorded), so there is no scenario where skipping it is the right call. Skipping leaves an orphan shelf the author didn't ask for.
        - --claim requires a PENDING CL. On a submitted CL, `#have` pre-images are POST-change once the workspace synced past the CL, so prepare_review exits with an error when --claim is passed on a submitted CL; re-run without --claim for a plain informational review.
        - md-audit findings are a SEPARATE, labeled section -- never interleave them with the code-review issue list. They come from skills-kit's detect.js (a subject-lens reviewer), not from the generic reviewer/validator subagents, so they are not filtered by the validators.
        - The claim decision happens ONCE, at the step-2 probe: md-audit available -> `--claim '**/*.md'` (one glob for CLAUDE.md, SKILL.md, and generic docs); md-audit absent -> no `--claim`. Do not run prepare a second time just to add claims -- the only re-runs are the two version-skew FALLBACKS (broad skew re-runs WITHOUT `--claim`; project-doc-only skew re-runs with only `--claim '**/CLAUDE.md' --claim '**/SKILL.md'`).
        - Claimed `.md` files route THREE ways by basename in step 6: `CLAUDE.md` -> claude-md-audit, `SKILL.md` -> skill-audit, every other `.md` -> project-doc-audit. `.md.html` is NOT `.md` and is never claimed -- it stays with the generic reviewers.
        - When skills-kit md-audit is absent the whole mechanism degrades silently: no `--claim`, no claimed_files, no md-audit section -- the md files get today's thin generic data_only coverage. Note the degradation in one line; do not treat it as an error.
        - The Workflow tool is unavailable inside subagents. Launch the md-audit detect.js Workflow from the MAIN session (the same message that fans out the reviewers), never from within a reviewer subagent.
        - The declined-findings ledger is advisory memory, not a gate. A collapsed finding is one the author already declined for THIS change at THIS baseline; when the baseline moves (the CL is reshelved, its content edited, or its revisions move) the entry goes stale and the finding re-surfaces on its own. Never let a ledger hit suppress a SERIOUS md-audit finding.
        - Record declined findings ONLY through `prepare_review.py --ledger-record <json>`. Never hand-edit ledger.json -- the key normalization (criterion/reason + taxonomy + normalized anchor) must be computed deterministically, not typed.
  narration:
    note: Reviews involve long silent stretches (batched file reads, parallel subagents that take 30s+). Post one short status line per step using these templates verbatim, filling in the bracketed counts. Do not paraphrase, omit, or add extras.
    templates:
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
        template: "Selected review profile: <P>. Diff partitioned into <K> chunk(s). Launching <RK> subagent(s) in parallel (<R> reviewer(s) × <K> chunk(s)): <reviewer_summary>."
      - when: "After step 6, before step 7 (X >= 1)"
        template: "Reviewers returned <X> candidate issue(s) (<B> bug, <C> CLAUDE.md). Launching <X> validator(s) in parallel."
      - when: "After step 6 (X = 0)"
        template: "Reviewers found no issues. Skipping validation."
      - when: "After step 7, before step 9"
        template: "Validators confirmed <Y> of <X>. Rendering review."
      - when: "After step 2 (bundle.auto_shelved is true)"
        template: "CL <CL> had no shelved content. Auto-shelved to fetch the diff -- will clean up after the review."
      - when: "After step 9 (bundle.auto_shelved is true)"
        template: "Cleaning up the auto-created shelf for CL <CL>."
    variables:
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
      "<V>": "len(bundle.unresolved)"
  review_profiles:
    description: |
      Routing table for selecting reviewers and models based on CL content. Exactly one
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
                  including them in a CL shouldn't force the heavier `code` profile.
            Use judgment: the question is "is there any file in this CL that needs Opus-level
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
      input: "absolute path to ONE chunk .diff file, the depot paths of the files in that chunk, the per-file CLAUDE.md mapping restricted to those files, and the full text of each relevant CLAUDE.md (read in step 4)"
      restrictions:
        - "Read the assigned chunk diff once (single Read call). Do not Read other chunks."
        - "Only consider CLAUDE.md files that share a path with the file being reviewed (use the per-file mapping; do not cross-apply)."
        - "Only flag issues in files present in your chunk -- files in other chunks are someone else's responsibility."
    - name: reviewer_b_diff_only_bugs
      subagent_type: general-purpose
      scope: obvious bugs visible in one chunk's diff alone
      input: "absolute path to ONE chunk .diff file, the depot paths of the files in that chunk, and the CL description"
      restrictions:
        - "Read the assigned chunk diff once. MUST NOT use Read for anything beyond that chunk."
        - "Only flag won't-compile, syntax/type errors, missing imports, unresolved references, definitely-wrong logic regardless of inputs."
        - "For data/doc files (data_only profile): focus on malformed syntax, duplicate keys, schema or column-count violations, and broken cross-file references."
        - "Only flag issues in files present in your chunk."
    - name: reviewer_c_introduced_code
      subagent_type: general-purpose
      scope: bugs/security/logic problems in the introduced code that need broader context, restricted to one chunk's files
      input: "absolute path to ONE chunk .diff file, the depot paths of the files in that chunk, local paths for those files, and the CL description"
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
        "file": "<depot or local path>",
        "lines": "<line range, e.g. 42 or 42-48>",
        "reason": "bug" | "claude_md",
        "description": "<one-sentence explanation>",
        "citation": "<exact rule quote, only for claude_md issues>"
      }]
  submit_gates:
    description: |
      Path-scoped pre-submit reminders authored in CLAUDE.md files. Surfaced verbatim at
      review time when at least one file in the CL falls within the gate's scope. Reminders
      are not findings -- they don't go through reviewer or validator subagents. Detection
      is deterministic, performed by prepare_review.py.
    authoring_format: "See references/submit-gates.md for the CLAUDE.md-author-facing guide to writing submit-gate blocks (block format, scope path semantics, multi-gate rules)."
    rendering: |
      When bundle.submit_gates is non-empty, the rendered review prepends a
      `## Submit checklist` section ABOVE the per-file review body. Each gate renders as:

        - **[✓|✗] <summary>** -- per `<source>`, triggered by `<file>` (+N more if many).
          > <rationale, indented as blockquote, omitted if empty>

      ✓ = author confirmed in the step-5 AskUserQuestion.
      ✗ = author did not confirm. NOT an error; the review still renders.

      Always show the section when gates applied -- including in the "no issues" path.
  output_format:
    description: "Final markdown rendered to chat. Unresolved merges (when applicable) and Submit checklist (when applicable) above the per-file review body."
    template: |
      ## Unresolved merges
      CL is not submittable until each file below is run through `p4 resolve`.
      - `path/to/file.cpp` -- content resolve pending (from `//depot/branch/file.cpp`)
      - `path/to/other.csv` -- branch resolve pending

      ## Submit checklist
      - **[✓] ./build.sh configbinaries must pass before submit** -- per `<path>/CLAUDE.md`, triggered by `GameConfigs/Real/x.csv`.
      - **[✗] Regenerate the asset index** -- per `<path>/CLAUDE.md`, triggered by `Content/Assets/y.uasset`.
        > <rationale if any, as a blockquote>

      ## Review: CL <CL> -- <description>

      Found N issues (M filtered as false positives).

      ### path/to/file.cpp
      - **[bug]** L42: Buffer overflow risk -- `items[i]` accessed without bounds check.
      - **[claude_md]** L78: Violates `src/CLAUDE.md` rule "Use absl::Status not bool returns".
    empty_template: |
      ## Submit checklist
      - **[✓] ./build.sh configbinaries must pass before submit** -- per `<path>/CLAUDE.md`, triggered by `GameConfigs/Real/x.csv`.

      ## Review: CL <CL> -- <description>

      No issues found. Reviewed for bugs and CLAUDE.md compliance.
    notes:
      - "Omit the Unresolved merges section entirely when bundle.unresolved is empty."
      - "Omit the Submit checklist section entirely when bundle.submit_gates is empty."
      - "When matched_files has >3 entries, render the first 3 then '(+N more)'."
      - "Rationale renders as a markdown blockquote (`> `) indented one level below the bullet, only if non-empty."
      - "Unresolved-merge entries render local path first (workspace-relative if possible); append `(from <fromFile>)` only when from_file is non-empty (integrations); omit for plain edit/sync resolves."
```
