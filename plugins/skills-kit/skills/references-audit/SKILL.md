---
_schema_version: 1
name: references-audit
author: christina
skill-type: audit-skill
description: Use when md-audit dispatches a scan for broken skill cross-references; fans multi-file runs via the Workflow tool. Do NOT use for single-skill validation.
disable-model-invocation: true
user-invocable: false
argument-hint: "[--scope skills|references|md|all] [--path FILE] [--ignore-dir GLOB] [--ignore-file GLOB] [--verbose] [--json]"
---

# References Audit

## Plugin version (always echo first)

!`uv run python "${CLAUDE_PLUGIN_ROOT}/scripts/print_version.py"`

The first line of your response MUST be the `Running ...` line printed above. This gives the user immediate confirmation of which plugin version actually executed (the slash registry can lag the on-disk cache; this is the only reliable signal).

## Framework

This skill operationalizes the **references-audit** audit-kind under the shared audit framework. The shared glossary -- `subject`, `primitive`, `composition`, `discovery`, `audit-kind`, `rule`, `finding`, `severity`, `taxonomy`, `bucket`, `corpus`, `scaffolding` -- is canonical at `skills-kit:md-audit/references/audit-framework.md`, with the data side (primitives + compositions + audit-kind registry) at `audit-framework.yaml` alongside. Definitions live there; this file describes only how the audit applies the framework.

In framework terms, references-audit (reached via `/md-audit references`) is:

- **Subject:** a `directory` composition (the default), or one of `skill | plugin | project` when discovery hits that marker, or a single primitive `md` file when `--path` names one.
- **Primitive consumed:** `md` (today's only scanner input; `script` and `code` are listed as future stubs in `audit-framework.yaml`).
- **Discovery:** walks the scan tree; activates plugin rules when it hits `.claude-plugin/plugin.json`, skill rules when it hits `SKILL.md`, directory rules otherwise. Rules stack rather than override.
- **Scaffolding:** `scripts/references_audit.py` -- the one repeatable invocation that replaces what would otherwise be agent inference over every file.
- **Rules per composition:** the bindings table in `audit-framework.yaml::audit_kinds.references_audit.rules_per_composition`. Canonical rule definitions (id, severity, summary, detail) live in this skill's own `criteria:` block below -- the framework registry only catalogs which rule ids bind to which compositions. Today's implemented set: `hard_dep_missing`, `soft_ref_missing`, `name_mismatch`, `shadowing`. Tracked in `audit-framework.yaml::future_rules`: `manifest_declarations_resolve`, `no_cross_scope_personal_refs`. (The formerly-parked `references_reachable_from_skill_md` shipped 2026-07-15 under `skill_md_audit` -- its subject is one skill composition, so it lives in the per-skill validator, not this corpus scanner.)
- **Taxonomy + dispositions:** the A-K categories below classify findings; each finding gets one of the ratified four dispositions -- FIX (auto-applied) / SERIOUS (summarized at top) / IMPROVE (count + one-liners, opt-in) / SILENT (not surfaced); K -> SPECIAL. FIX applies in the REMEDIATE phase (inline Edit for a single affected file, `workflow/remediate.js` lanes for 2+), IMPROVE/SPECIAL go through a foreground Q&A, SERIOUS is surfaced at the top, SILENT is omitted.

```yaml
audit_skill:
  _schema_version: "1"
  identity: "Scan markdown files for broken Claude Code skill cross-references, classify each finding into a taxonomy category, and assign each a disposition instance-level under the ratified four-disposition contract (FIX auto-applied / SERIOUS summarized at top / IMPROVE opt-in one-liners / SILENT not surfaced; K -> SPECIAL). The taxonomy default is a starting point; the classifier decides per finding against the master razor."
  scope:
    covers:
      - "scanning SKILL.md, reference docs (references/*.md, in-skill CLAUDE.md), and arbitrary markdown for broken `/example:skill-name` references and `skill: \"...\"` hard-dep invocations"
      - "classifying findings into categories A-K (renamed / retired / merged / scope-violating / false positives / illustrative / proposed / unclassified)"
      - "assigning each finding a disposition -- FIX (mechanical fix applied in the REMEDIATE phase -- inline Edit for a single file, workflow/remediate.js lanes for 2+), SERIOUS (surfaced summarized at the top, never auto-fixed), IMPROVE (opt-in one-liners via foreground Q&A), or SILENT (not surfaced); SPECIAL (K escape hatch for unanticipated cases)"
      - "re-running the audit after remediation to verify no new findings surfaced from the fixes"
    excludes:
      - "single-skill contract validation (use /md-audit skill)"
      - "CLAUDE.md cohesion auditing (use /md-audit claude-md)"
      - "non-markdown files (settings.json, *.py, etc.) -- not scanned at any scope"
      - "runtime resolution of references during skill execution (this is static-analysis only)"
  subject:
    what: "markdown files (SKILL.md, references/*.md, in-skill CLAUDE.md, standalone docs) checked for broken cross-references to Claude Code skills"
    subject_type: "corpus"
  criteria:
    - id: "hard_dep_missing"
      name: "Hard dependency targets a non-existent skill"
      keywords: ["hard dep", "skill invocation", "runtime failure", "ERROR finding", "skill tool"]
      summary: "A Skill-tool invocation (`skill: \"...\"`) in scanned source targets a skill that does not exist in the resolved skill pool."
      severity: "FAIL"
      detail: "Will fail at runtime when the code path fires. Surfaced by the scanner as ERROR. This is the only severity that gates exit code 1. Disposition: FIX when a mechanical re-point/prefix resolves it (known rename, example:/proposed: escape); SERIOUS when the invoked skill is genuinely gone with no replacement and no escape -- a live runtime-crash path with no surviving mechanism, surfaced summarized at the top, never auto-fixed."
    - id: "soft_ref_missing"
      name: "Prose reference to a non-existent skill"
      keywords: ["soft ref", "prose reference", "slash reference", "WARNING finding"]
      summary: "A `/example:skill-name` reference in scanned prose does not resolve against the skill pool."
      severity: "INFO"
      detail: "Misleading to readers but not a runtime failure. Surfaced by the scanner as WARNING. The taxonomy below categorizes the *why* and remediates accordingly. Disposition: FIX by default (decidable against the verified skill pool + escape-prefix / code-fence conventions), refined per taxonomy category -- retired (B non-incidental), scope (D), and harness (H) refine to IMPROVE."
    - id: "name_mismatch"
      name: "Frontmatter name diverges from directory name"
      keywords: ["name mismatch", "frontmatter name", "directory name", "inconsistency"]
      summary: "A SKILL.md's frontmatter `name:` field does not match the directory the file lives in."
      severity: "INFO"
      detail: "May cause confusion when looking up the skill. Surfaced by the scanner as WARNING. Usually a rename leftover. Disposition: FIX (align frontmatter name and directory) after verifying which side inbound refs use -- loss-free guard; IMPROVE when inbound refs disagree on the canonical name (renaming would break real refs)."
    - id: "shadowing"
      name: "User skill shadows a project skill"
      keywords: ["shadowing", "user skill", "project skill", "override", "precedence"]
      summary: "A user-level skill (`~/.claude/skills/<name>`) has the same name as a project-level skill and overrides it at runtime."
      severity: "INFO"
      detail: "Intentional in some workflows (personal overrides), accidental in others. Surfaced by the scanner as INFO; the agent surfaces but does not remediate without user direction. Disposition: IMPROVE when it looks accidental (one-line 'user skill X shadows project skill X -- intended?'); SILENT when confirmed an intentional personal override (an accepted structural pattern). A precedence relationship is never auto-edited."
  taxonomy:
    - id: "A_renamed"
      name: "Renamed skill (1:1 replacement exists)"
      keywords: ["renamed", "rename map", "mechanical replacement", "find-and-replace"]
      detection_signal: "WARNING `/example:old-name` (or ERROR `skill: \"example:old-name\"`); a current skill `/example:new-name` clearly covers the same responsibility (confirmable from upstream CHANGELOG, the new skill's description, or an explicit 'renamed from' line)."
      default_remediation: "Mechanical find/replace of the old name with the new name within the file. If the surrounding sentence describes old behavior, also update the prose so it matches the new skill. FIX when the 1:1 mapping is known (decidable against the skill pool); IMPROVE when it is not (offer the best-guess new name as a one-liner)."
      bucket: "FIX"
      examples:
        - before: "references using this prefix are not flagged as `/skill-deps`"
          after: "references using this prefix are not flagged as `/references-audit`"
    - id: "B_retired"
      name: "Retired or deleted skill (no replacement)"
      keywords: ["retired", "deleted", "no replacement", "demote to backtick", "allow-stale"]
      detection_signal: "WARNING `/example:old-name`; no current skill covers the responsibility. The reference is often the subject of a whole section or paragraph."
      default_remediation: "Four sub-cases by structural context -- (1) reference is the subject of a section: delete the section; (2) incidental clause: delete the clause, keep the surrounding sentence; (3) historical context inside a mixed-live-and-stale doc: demote to backticked literal; (4) whole doc is a historical artifact: add to `references-audit-allow-stale` frontmatter list with an editor's note. IMPROVE by default (the sub-case protects surrounding true content; loss-free-deletion guard applies). FIX only in sub-case 2: a broken incidental clause whose removal loses nothing is falsified-content deletion."
      bucket: "IMPROVE"
    - id: "C_merged"
      name: "Merged skill (subskill folded into parent)"
      keywords: ["merged", "folded", "parent skill", "sub-skill", "dispatch alias"]
      detection_signal: "WARNING `/example:parent-sub`; current skill `/example:parent` exists; release notes or SKILL.md document the merge."
      default_remediation: "In prose: rewrite the slash form (`/example:parent-sub`) to the new dispatch form (`/example:parent sub`). In dispatch alias tables / synonyms lists: keep the literal name in backticks (not a callable slash reference). FIX -- both are mechanical convention fixes decidable from the documented merge."
      bucket: "FIX"
      examples:
        - before: "Via `/playtest preflight` (or the legacy `/playtest-preflight`) for standalone validation"
          after: "Via `/playtest preflight` (or the legacy `playtest-preflight` argument) for standalone validation"
    - id: "D_scope_violating"
      name: "Scope-violating cross-reference (project / personal boundary)"
      keywords: ["scope violation", "personal skill", "project skill", "shipped plugin", "cross-scope"]
      detection_signal: "WARNING `/example:ref-name`; the referenced skill exists but in the opposite scope (project skill referencing a personal skill, or a shipped plugin skill referencing a project-only skill)."
      default_remediation: "Project / plugin -> personal: delete the cross-reference (a shipped skill cannot assume the personal skill is installed). Personal -> project: usually fine; only flag if the personal skill is meant to be portable. IMPROVE by default -- deleting a cross-scope reference may drop true comparison content (loss-free guard); FIX only for an incidental, purely-misleading mention whose removal loses nothing."
      bucket: "IMPROVE"
    - id: "E_compound_adjective"
      name: "Compound-adjective false positive (slash as punctuation)"
      keywords: ["false positive", "compound adjective", "punctuation slash", "prose rewrite"]
      detection_signal: "WARNING `/example:word-foo`; the literal text contains `X-/Y-thing` (compound adjective with embedded slash) or other prose where a slash appears as punctuation, not as a skill reference."
      default_remediation: "Reword the prose to eliminate the slash; preserve the technical meaning (the rewrite is 'express the same idea differently', not 'escape the scanner'). FIX -- a false positive; rewording loses nothing."
      bucket: "FIX"
      examples:
        - before: "Slack file downloads are bot-/user-token-gated."
          after: "Slack file downloads are gated by bot or user token scopes."
    - id: "F_cli_flag"
      name: "Non-skill CLI flag false positive"
      keywords: ["false positive", "cli flag", "devenv", "msbuild", "shell command", "code fence"]
      detection_signal: "WARNING `/example:flag-name`; surrounding text is a shell or CLI invocation (binary name + flags). Common with MSBuild, `devenv`, `cl.exe`, the linker, and other Windows-native tools."
      default_remediation: "Wrap the whole command in a fenced code block. The scanner masks fenced regions, so refs inside them produce no findings. FIX -- a false positive; fencing loses nothing."
      bucket: "FIX"
    - id: "G_xml_template"
      name: "XML or template placeholder false positive"
      keywords: ["false positive", "xml tag", "html tag", "template placeholder", "angle brackets"]
      detection_signal: "WARNING `/example:tag-name`; surrounding text contains XML or HTML closing tags (such as `</example:foo>`) or template placeholders inside angle brackets."
      default_remediation: "Wrap the XML or template example in a fenced code block. Same scanner masking as category F. FIX -- a false positive; fencing loses nothing."
      bucket: "FIX"
    - id: "H_harness_transcript"
      name: "Harness transcript false positive"
      keywords: ["false positive", "harness transcript", "session log", "ignore-dir", "claude feedback"]
      detection_signal: "Many WARNINGs in the same file or directory; references match Claude-harness vocabulary (`/example:command-args`, `/example:system-reminder`, `/example:task-id`, `/example:tool-use-id`, `/example:command-name`, `/example:command-message`, etc.)."
      default_remediation: "Add the directory to the scanner's `--ignore-dir` flag in the project's invocation wrapper (one config entry, no per-file edits). IMPROVE -- a batch config decision recommended once as a one-liner; it edits the invocation wrapper (out of the scanned files), so the user confirms the mechanism and location."
      bucket: "IMPROVE"
    - id: "I_illustrative"
      name: "Illustrative example in a design doc"
      keywords: ["illustrative", "meta-descriptive", "design doc", "syntax example", "example prefix"]
      detection_signal: "WARNING `/example:foo` or ERROR `skill: \"example:foo\"`; the surrounding sentence is describing skill-reference syntax in the abstract -- the doc is about references, not making one."
      default_remediation: "Add the `example:` prefix to the slash-form, and likewise to any `skill: \"...\"` hard-dep literal. Both are documented escape prefixes that the scanner ignores. FIX when the sentence is clearly meta-descriptive (your verified reading discharges the hedge); IMPROVE when the ref sits inside a live procedure (it could be a real, currently-broken instruction)."
      bucket: "FIX"
      examples:
        - before: "soft references (`/name` in documentation text that mislead)"
          after: "soft references (`/example:name` in documentation text that mislead)"
    - id: "J_forward_looking"
      name: "Forward-looking or proposed skill"
      keywords: ["forward-looking", "proposed", "future", "planned", "aspirational", "proposed prefix"]
      detection_signal: "WARNING `/example:foo`; no current skill named `foo`; surrounding prose frames it as 'planned', 'future', 'we should build', 'today: <legacy approach>'."
      default_remediation: "Add the `proposed:` prefix to the slash-form (a documented escape prefix). Optionally append a one-line '(planned, not built)' note if the context isn't already explicit. FIX when the prose explicitly cues 'planned'/'future'/'proposed'; IMPROVE if ambiguous (could be a real ref to a deleted skill)."
      bucket: "FIX"
    - id: "K_unclassified"
      name: "Unclassified / special case"
      keywords: ["unclassified", "special case", "escape hatch", "K bucket", "surface to user"]
      detection_signal: "None of A-J fit cleanly after a deliberate attempt."
      default_remediation: "Surface the finding to the user with the report line, what you tried to match, and why none of A-J fit. The user decides the strategy."
      bucket: "SPECIAL"
      note: "K is the only SPECIAL. All other categories resolve to FIX / SERIOUS / IMPROVE / SILENT per the master razor; SERIOUS is reserved for a hard-dep invocation to a genuinely-gone skill with no surviving mechanism."
  procedures:
    - id: "scan_and_report"
      name: "Scan markdown corpus for broken cross-references"
      keywords: ["scan", "audit", "discover", "run scanner", "report"]
      goal: "Run the references_audit.py script over the chosen scope and emit a markdown or JSON report of findings (ERRORs, WARNINGs, INFOs) against the resolved skill pool."
      preconditions:
        - "Python is available on PATH (stdlib only; no external dependencies)."
        - "Installed-plugins manifest exists at `~/.claude/plugins/installed_plugins.json` if plugin skills should be in the pool."
      steps:
        - n: 1
          action: "Parse the user's scope intent from $ARGUMENTS into one or more of `--scope skills|references|md|all` (combine with commas; default `skills`). If the user named a specific file, capture `--path PATH` (repeatable)."
          expected: "A flag set ready to pass to the scanner."
        - n: 2
          action: "Run the scanner via the plugin venv. Pass through any additional flags from $ARGUMENTS (`--verbose`, `--ignore-dir`, `--ignore-file`, `--json`)."
          tool: "references_audit.py"
          input: |
            uv run python "${CLAUDE_PLUGIN_ROOT}/skills/references-audit/scripts/references_audit.py" \
              --project-dir .claude/skills \
              --user-dir $HOME/.claude/skills \
              $ARGUMENTS
          expected: "Markdown (default) or JSON (when --json given) report containing: scope summary, skill-pool counts, issues by severity, hard-dependency graph, summary counts."
        - n: 3
          action: "If remediation will follow (the user asked to fix findings, not just see them), re-run with `--json` so the classify procedure can consume structured findings."
          expected: "JSON findings ready for taxonomy classification."
      gotchas:
        - "Prose-form hard deps (e.g. 'invoke `/example:foo` using the Skill tool') are caught as WARNING soft refs, not as ERROR hard deps. The runtime risk is still real; do not dismiss the WARNING."
        - "Frontmatter parsing is regex-based; the skill pool is built from frontmatter `name:` only. A skill that fails to parse frontmatter (e.g. truncated `---` fence) is silently absent from the pool."
        - "The NON_SKILL_WORDS exclusion list can filter a legitimate skill name colliding with a common path segment (e.g. a skill literally named `build`). Inspect the exclusion list in references_audit.py if a real ref is mis-reported as missing."
    - id: "classify_and_dispatch"
      name: "Categorize findings per taxonomy and dispatch by bucket"
      keywords: ["classify", "categorize", "taxonomy", "disposition", "dispatch", "FIX SERIOUS IMPROVE SILENT"]
      goal: "For each finding from the scan, assign exactly one category from A-K and one of the four dispositions (FIX / SERIOUS / IMPROVE / SILENT; K -> SPECIAL), then remediate in two phases split by a Q&A gate: classify (fan-out), gather IMPROVE/SPECIAL decisions, remediate (fan-out). FIX applies by definition; SERIOUS is surfaced summarized at the top; SILENT is omitted. The Workflow tool fans both the classification and the remediation out across files; the scan itself stays a single script run."
      preconditions:
        - "The scan procedure has completed; findings are available as JSON."
        - "workflow/classify.js and workflow/remediate.js are present (used for the 2+-file fan-out)."
      steps:
        - n: 1
          action: "Group the scanner's findings by containing file. CLASSIFY phase (no edits). Choose mode by how many FILES carry findings -- this threshold equalizes the Workflow tool's per-run overhead. ONE file: classify inline in the main loop (read the file around each cited line; match each finding to a taxonomy category A-K; assign its disposition FIX/SERIOUS/IMPROVE/SILENT (K -> SPECIAL) instance-level against the master razor; for a before/after FIX compute the exact before/after). TWO OR MORE files: call the Workflow tool with scriptPath ${CLAUDE_PLUGIN_ROOT}/skills/references-audit/workflow/classify.js and args = { files:[{file, findings:[{severity,line,ref}]}], refs:{taxonomyDoc} }. One lane per file; returns { perFile, totals }. No edits in this phase."
          tool: "Workflow | inline"
          input: "classify.js args.refs: taxonomyDoc=${CLAUDE_PLUGIN_ROOT}/skills/references-audit/references/finding-taxonomy.md."
          expected: "Every finding has exactly one category (A-K) + a disposition (FIX/SERIOUS/IMPROVE/SILENT; K -> SPECIAL); FIX before/after findings carry before/after text."
        - n: 2
          action: "Q&A GATE. FIX findings need no decision (mechanical -- apply by definition). SERIOUS findings are surfaced summarized at the TOP of the report (never auto-fixed). SILENT findings are omitted. For IMPROVE + SPECIAL findings, batch them all into ONE foreground question round (a numbered list with category, options, and a recommendation) and collect the user's decisions. Do NOT per-finding round-trip."
          expected: "A decision attached to every IMPROVE/SPECIAL finding; FIX findings ready to apply; SERIOUS surfaced at the top."
        - n: 3
          action: "REMEDIATE phase (after-Q&A). Assemble per-file edit lists (FIX=apply; IMPROVE/SPECIAL=per decision; SERIOUS and SILENT never enter the edit lists; drop skips). Choose mode by how many FILES carry edits. ONE file: apply inline with Edit. TWO OR MORE files: call the Workflow tool with scriptPath ${CLAUDE_PLUGIN_ROOT}/skills/references-audit/workflow/remediate.js and args = { perFile:[{file, edits:[{category,bucket,line,before,after,instruction,decision}]}] }. One lane per file (disjoint files never conflict)."
          tool: "Workflow | inline"
          expected: "Edits applied; per-file applied/skipped/failed summary."
        - n: 4
          action: "Re-run the scan procedure to verify. Iterate only on newly-surfaced findings (do not re-classify already-resolved ones)."
          expected: "Audit confirms no new findings introduced; remaining findings (if any) are surfaced for next-pass remediation."
      output_template: |
        ## Audit summary

        Scope: <scopes scanned>
        Files: <N scanned>
        Skills pool: <project N> / <user N> / <plugin N>

        ## Report (SERIOUS -> FIX -> IMPROVE; SILENT omitted, no hedging)

        SERIOUS (N): Found N serious issue(s) that require fixing [per-issue one-line summary; file:line + why the invocation has no surviving mechanism]
        FIX (N applied, in the reviewable remediation CL): [list with file:line + category + before/after]
        IMPROVE (N): Audit found N improvement opportunit(ies). Do you want to discuss them? [list with file:line + category + options + recommendation]
        SPECIAL (N): [list with file:line + rationale]

        ## Remediation results

        Applied: <N>
        Skipped (file changed): <N>
        Outstanding (newly surfaced or unclassifiable): <N>
      gotchas:
        - "Do not reclassify findings the taxonomy has already settled. The lane's job is the category match + judgment on OPTIONS within a category, not second-guessing the taxonomy."
        - "The remediate lanes do not classify -- they apply edits from the decided list. Carry exact before/after text for a before/after FIX; a lane records 'failed' (not a guess) when the before-text no longer matches."
        - "Re-running the audit after remediation is required; newly-surfaced findings often appear (e.g. a backticked literal reveals another broken ref nearby that was previously masked)."
        - "Detection and remediation are separate phases split by the Q&A gate. FIX findings need no decision, but they are still applied in the REMEDIATE phase (alongside the decided IMPROVE edits), not during classification -- this keeps the scan idempotent so a re-run reproduces the same findings. SERIOUS and SILENT never enter the edit lists."
  # Disposition mapping (four-disposition model): the auto / discuss / special
  # lanes are retained for schema stability across audit members. auto = FIX
  # categories (auto-applied in the REMEDIATE phase). discuss = the categories
  # whose disposition is IMPROVE (opt-in one-liner) or SERIOUS (surfaced at top,
  # never auto) -- disposition tagged per entry. special = K. The final
  # per-finding disposition is assigned instance-level by classify.js against the
  # master razor; these are the taxonomy defaults.
  remediations:
    auto:
      - category: "A_renamed"
        procedure: "[FIX] Mechanical find/replace old-name -> new-name in the file (known 1:1 mapping). Update surrounding prose if it describes old behavior. Unknown mapping -> IMPROVE (offer the best guess)."
        agent_template: "See references/finding-taxonomy.md 'Background-agent brief template'."
      - category: "C_merged"
        procedure: "[FIX] Rewrite `/example:parent-sub` prose to `/example:parent sub`. In dispatch alias tables, demote to backticked literal instead."
        agent_template: "See references/finding-taxonomy.md 'Background-agent brief template'."
      - category: "E_compound_adjective"
        procedure: "[FIX] Reword prose to eliminate the slash; preserve technical meaning."
        agent_template: "See references/finding-taxonomy.md 'Background-agent brief template'."
      - category: "F_cli_flag"
        procedure: "[FIX] Wrap the entire command in a fenced code block."
        agent_template: "See references/finding-taxonomy.md 'Background-agent brief template'."
      - category: "G_xml_template"
        procedure: "[FIX] Wrap the XML or template example in a fenced code block."
        agent_template: "See references/finding-taxonomy.md 'Background-agent brief template'."
      - category: "I_illustrative"
        procedure: "[FIX default] Add `example:` prefix to slash-form refs and `skill: \"example:...\"` literals. IMPROVE when the ref sits in a live procedure (could be a real broken instruction)."
        agent_template: "See references/finding-taxonomy.md 'Background-agent brief template'."
      - category: "J_forward_looking"
        procedure: "[FIX default] Add `proposed:` prefix to the slash-form. Optionally append a '(planned, not built)' note. IMPROVE if the cue is ambiguous."
        agent_template: "See references/finding-taxonomy.md 'Background-agent brief template'."
    discuss:
      - category: "hard-dep (no surviving mechanism)"
        procedure: "[SERIOUS] A `skill: \"...\"` invocation to a genuinely-gone skill with no replacement and no mechanical escape is a live runtime-crash path. Surface it summarized at the TOP of the report -- the unguarded invocation is the finding. Never auto-fix, never bury."
      - category: "A_renamed"
        procedure: "[IMPROVE] (Sub-case: mapping unknown.) Ask once for the whole audit: 'I see refs to `/example:old-name`. Best guess `/example:new-name`. Apply?' Batch response covers all old-name refs."
      - category: "B_retired"
        procedure: "[IMPROVE default] Offer the sub-case as a one-liner: delete section / delete clause / demote to backtick / add to allow-stale. Surrounding structural context determines the best choice; loss-free-deletion guard applies. FIX only for a purely-incidental broken clause whose removal loses nothing."
      - category: "D_scope_violating"
        procedure: "[IMPROVE] Offer as a one-liner whether the cross-reference should be deleted (shipped skill referencing personal) or kept (personal skill comparing to project). Deleting may drop true comparison content; the skill's structural intent determines the answer."
      - category: "H_harness_transcript"
        procedure: "[IMPROVE] Recommend the exclusion mechanism once as a one-liner: --ignore-dir flag in the project's wrapper, or document the recommended flags in the host project's CLAUDE.md. A batch config decision; apply to all matching files."
      - category: "shadowed"
        procedure: "[IMPROVE default] Surface an apparently-accidental shadow as a one-liner ('user skill X shadows project skill X -- intended?'). [SILENT] when confirmed an intentional personal override (an accepted structural pattern -- not surfaced)."
    special:
      procedure: "[SPECIAL] Surface the finding to the user with: the scanner's report line, the categories you attempted to match, the reasons none fit. The user proposes a strategy. If the strategy generalizes (mutually exclusive with A-J, recognizable detection signal, default remediation applies broadly), propose adding it as a new category in references/finding-taxonomy.md."
  enforcement:
    gate_kind: "merge-gate"
    gating_rule: "No ERRORs (broken hard dependencies) in any changelist submitted for merge. WARNINGs and INFOs are permissible and can be addressed in follow-up changes but should be minimized."
    appeal_process: "ERRORs in category E/F/G/H (false positives) are resolved by applying the category's default remediation (rewording, code-fencing, ignore-dir flag). ERRORs in category J (forward-looking) are resolved by adding the `proposed:` prefix. Genuine retired-skill ERRORs (category B) require either deletion or the historical-artifact `references-audit-allow-stale` allowlist with an editor's note. No appeals process bypasses the gate; remediation is always available within the taxonomy."
  gotchas:
    - "The skill pool is built from `installed_plugins.json` and the project/user `.claude/skills/` directories. A skill on disk that is not registered in installed_plugins.json will not resolve, even if a SKILL.md exists at its install path. This is correct -- the resolver mirrors what the harness does at runtime."
    - "The `references-audit-allow-stale:` frontmatter field (legacy: `audit-references-allow-stale`) silences findings *inside that file only*, for the listed names only. A new broken ref in the same file still fires. Use this for genuine historical artifacts; do not use it as a bypass for live docs."
    - "Compound-adjective false positives (category E) and harness-transcript false positives (category H) account for the majority of WARNINGs in many projects. If your warning count is in the hundreds, before investigating individual findings, check whether one of these categories applies to the whole batch."
    - "Hard-dependency edges across scanned source files are reported as a separate graph in the markdown output. Use this to spot orphaned skills (no inbound edges) or circular invocations during corpus-wide reviews."
  anti_patterns:
    - id: "per_finding_user_round_trip"
      name: "Per-finding user round-trip"
      keywords: ["round-trip", "per-finding question", "conversation friction", "batching"]
      why_it_seems_right: "Each IMPROVE finding has a genuine user decision; surely the agent should ask about each one individually so the user can give the right answer."
      why_it_is_wrong: "Per-finding round-trips multiply conversation friction and slow remediation to a crawl. The user has to context-switch into each finding individually. Cost of being wrong is one revert; cost of friction is the user abandoning the audit."
      alternative: "Batch every IMPROVE + SPECIAL finding into one foreground question round. Render as a numbered list with category, options, and a recommendation. The user answers in one pass."
    - id: "classify_then_self_remediate"
      name: "Classify and remediate in the same pass"
      keywords: ["self-remediation", "single-pass", "idempotency", "classify and fix"]
      why_it_seems_right: "Classifying a finding and applying its fix in the same step seems efficient -- one lane, fewer round trips."
      why_it_is_wrong: "Mixing classification and remediation breaks idempotency. Classification (detection) and remediation are separate phases with an interactive Q&A gate between them; conflating them mutates files before the user has decided the IMPROVE/SPECIAL cases and prevents a clean re-run from reproducing the same findings."
      alternative: "Run the CLASSIFY phase to completion (fan out one lane per file, no edits). Gather IMPROVE/SPECIAL decisions at the Q&A gate. Then run the REMEDIATE phase (fan out one lane per file). Re-run the scan to verify."
```

## What this skill does

Scan one or more sets of markdown files for broken Claude Code skill references and report them. The default scope (`skills`) is the original `skill-deps` behavior — scan every `SKILL.md` and check that each `/example:skill-name` or `skill:"example:skill-name"` reference points to a real skill. Other scopes extend the scan to additional file types so reference files and standalone docs (e.g. `references/*.md`, `CLAUDE.md`) can be audited too.

Plugin skills are discovered from `~/.claude/plugins/installed_plugins.json` and resolved by both their bare name (e.g. `/example:skill-name`) and their plugin-qualified name (e.g. `/example:plugin-skill-name`).

## Invocation

This skill is a non-user-invocable member of the md-audit union domain (its standalone slash command was collapsed into the `/md-audit` front door). Two ways in:

- **md-audit dispatch:** `/md-audit references [args]` -- md-audit routes here and feeds the flags through.
- **Skill tool (from another skill):** `Skill: "references-audit"` with `args` containing the desired flags. Pass scope decisions through `args` rather than hardcoding them at the call site so the caller stays generic.

Both resolve to the same SKILL.md and run the same `references_audit.py` script.

## Documentation Convention

When showing example skill-reference syntax in prose, use `/example:` or `/proposed:` prefixes (e.g. `/example:skill-name`, `/proposed:run-bot`). The scanner ignores any reference with one of these prefixes and never reports it as broken.

The scanner also masks inline code spans (single/double-backtick `...` runs) as well as fenced blocks, so a backticked slash-token such as a `/route` endpoint or `$/unit` notation is treated as code, not a skill reference, and never fires.

For **historical artifacts** (rollout summaries, design plans whose proposed names were later renamed or never built, postmortem docs that record past state), a per-file allowlist in YAML frontmatter declares which legacy names are expected to no longer resolve:

```yaml
---
references-audit-allow-stale: plan, designer-plan, rollback-to-preflight
---
```

The legacy field name `audit-references-allow-stale` is still recognized for backward compatibility with files written before the 0.8.0 rename; prefer the new name on any file you touch.

Listed bare names are silenced inside that file only, for both soft refs and hard deps. Any *new* broken reference in the same file still fires — the allowlist is an explicit exception list, not a file-level bypass. Prefer this over rewriting historical refs to backticks or `/proposed:` prefixes when the doc's value is the historical record itself. The allowlist is documented in the editor's note inside the doc, so a reader sees both the declared exceptions and the reason for them.

## Step 1: Pick the Scope

The scope tells the script which files to audit. The agent picks based on what the user asked for; if the user did not specify, use the default `skills`.

| User said... | Pass | Effect |
|---|---|---|
| (nothing specific, or "audit my skills") | `--scope skills` (default) | Scan every `SKILL.md` (project + user + plugin). |
| "audit my reference docs" / "check references/ files" | `--scope references` | Scan every `*.md` file that sits inside a skill directory but is NOT `SKILL.md` (i.e. `references/*.md`, `CLAUDE.md` inside a skill dir, etc.). The skill pool itself is still loaded from SKILL.md files so refs can resolve. |
| "audit every markdown file" / "check all .md" | `--scope md` | Scan every `*.md` file under the scan roots, including SKILL.md, references, and standalone docs. |
| "audit everything" | `--scope all` | Alias for `--scope md`. |
| "audit this one file: PATH" | `--path PATH` (combine with `--scope` if the skill-pool scope matters) | Scan only the file at PATH; refs still resolve against the full skill pool. May be repeated. |

Scopes can also be combined with commas: `--scope skills,references` audits SKILL.md and reference files but skips standalone docs.

## Step 2: Run the Analysis

```
uv run python "${CLAUDE_PLUGIN_ROOT}/skills/references-audit/scripts/references_audit.py" \
  --project-dir .claude/skills \
  --user-dir $HOME/.claude/skills \
  $ARGUMENTS
```

If `uv` isn't available in the host environment, fall back to whatever Python entry point fits (e.g. `python`, `python3`, `py`, or a project-bundled `python.bat`). The script depends only on the Python standard library.

Pass through any of these flags from `$ARGUMENTS`:

- `--verbose` / `-v` — full soft-reference graph and per-source-file table.
- `--ignore-dir GLOB` (repeatable) — skip files whose path contains a matching ancestor directory. Use for harness transcript dirs (e.g. `--ignore-dir 'ClaudeFeedback'`) and vendored third-party doc trees.
- `--ignore-file GLOB` (repeatable) — skip files matching the glob.
- `--json` — emit a structured JSON report instead of markdown. Use when downstream tooling (or the remediation step below) needs to consume findings programmatically.

## Step 3: Display Results

Show the script's markdown output directly to the user when remediation will be conversational. When remediation is in scope (Step 5 below), prefer `--json` and synthesize a short human summary for the user — the JSON feeds the classification step.

The output (markdown form) includes:

- **Scope summary**: which scopes ran and how many source files were scanned for each.
- **Skills discovered**: counts of project, user, and plugin skills (the resolvable name pool).
- **Issues**: each finding includes the file path, line number, and the missing ref. ERRORs (broken hard deps), WARNINGs (broken soft refs, name mismatches), INFOs (shadowed skills).
- **Hard Dependencies**: the Skill-tool invocation graph (edges across scanned source files), with line numbers.
- **Summary**: counts of each issue type.

If `--verbose` was used, also shows the full soft-reference graph (with line numbers) and a table of every scanned source file.

## Step 4: Interpret

This level only classifies the *scanner's* output type. For the **semantic categories** of broken references (the A–K taxonomy declared in the contract above), load `references/finding-taxonomy.md` in Step 5 for full detection signals, default remediations, and the background-agent brief template.

| Issue type | Meaning |
|------------|---------|
| ERROR | A Skill-tool call (e.g. `skill: "X"`) in a scanned source targets a skill that does not exist. This **will fail at runtime** when that path fires. |
| WARNING (broken ref) | A `/example:skill-name` reference in scanned prose targets a skill that does not exist. Misleading but not a runtime failure on its own. |
| WARNING (name mismatch) | The YAML frontmatter `name:` on a SKILL.md does not match what the directory structure suggests. May cause confusion. |
| INFO (shadowed) | A user-level skill has the same name as a project skill and overrides it at runtime. |

Exit code 0 = no ERRORs (warnings and infos are OK). Exit code 1 = broken hard dependencies exist.

## Step 5: Categorize and Remediate

Only run this step when the user asks to fix the findings (or it is otherwise in scope). Don't volunteer remediation if the user only wanted to see the report.

The flow is encoded in the `classify_and_dispatch` procedure of the audit-skill contract above. Operational summary:

1. Re-run the script with `--json` if you didn't already.
2. For each finding, classify into one of the categories in the contract's `taxonomy:` block (A–J, plus K for special cases). Full detection signals and remediation details live in `references/finding-taxonomy.md`.
3. Assign each finding a disposition -- **FIX** / **SERIOUS** / **IMPROVE** / **SILENT** (K -> **SPECIAL**) -- instance-level against the master razor; the taxonomy default is a starting point.
4. **CLASSIFY (fan-out)**: group findings by file. 1 file with findings → classify inline; 2+ → the Workflow tool with `workflow/classify.js`. One lane per file assigns category + disposition and computes FIX before/after. No edits.
5. **Q&A gate**: batch all IMPROVE + SPECIAL findings into one foreground round (FIX needs no decision; SERIOUS is surfaced summarized at the top; SILENT is omitted).
6. **REMEDIATE (fan-out)**: 1 file with edits → apply inline; 2+ → the Workflow tool with `workflow/remediate.js`. One lane per file applies the decided edits. Then re-run the scan and iterate only on newly-surfaced findings.

The taxonomy doc is the authoritative reference for the **finding detection signals** and the **foreground Q&A batching pattern**. The contract above declares the taxonomy structure; the reference doc supplies the operational templates.

## Workflow orchestration

Categorize-and-remediate runs in two phases split by an interactive Q&A gate, and uses the Workflow tool to fan the work out across files. **Invoking this skill authorizes the Workflow-tool calls described below** (the skill's instructions are the opt-in; do not re-prompt for permission to orchestrate). The **scan stays a single `references_audit.py` run** — it is fast, stdlib-only, and whole-corpus, so there is nothing to parallelize there; the fan-out applies to the agent work (classify + remediate).

```
scan (single script run) -> findings grouped by file
  -> CLASSIFY   (before-Q&A) : 1 file inline | 2+ files via workflow/classify.js   -> categorized findings
  -> Q&A GATE   (main loop)  : batch IMPROVE/SPECIAL decisions (FIX needs none; SERIOUS surfaced at top; SILENT omitted)
  -> REMEDIATE  (after-Q&A)  : 1 file inline | 2+ files via workflow/remediate.js  -> edits applied
  -> re-run scan to verify
```

**Multi-file threshold (the overhead equalizer).** The Workflow tool has real per-run overhead. For a single file with findings that overhead is not worth it, so it is classified / remediated inline. At 2+ files the parallel fan-out pays for itself. Classification and remediation are **always separate passes** even in workflow mode — the interactive Q&A sits between them, and a background workflow cannot ask the user anything. FIX findings need no decision, but they are applied in the REMEDIATE phase (with the decided IMPROVE edits), not during classification — this keeps the scan idempotent (`classify_then_self_remediate` anti-pattern). SERIOUS and SILENT never enter the edit lists.

**The two workflow scripts** (hand-authored, shipped as skill assets):

- `workflow/classify.js` — before-Q&A. One lane per file-with-findings: read the file around each cited line → match each finding to an A–K category + a FIX/SERIOUS/IMPROVE/SILENT disposition → compute FIX before/after. Returns `{ perFile, totals }`. No edits.
- `workflow/remediate.js` — after-Q&A. One lane per file (disjoint files, no conflicts): apply the decided edits. Returns `{ perFile, summary }`.

Both accept `args` as an object or JSON string. Pass absolute `refs` paths (they run from the session cwd, not the skill dir).

## Scope Definitions in Detail

- **skills**: every `SKILL.md` under the scan roots (project, user, installed plugins). Backward-compatible with the original `skill-deps` behavior.
- **references**: every `*.md` file that lives inside a skill directory but is not the `SKILL.md` itself. Typically `references/*.md`, but also picks up any other markdown shipped alongside a skill (a per-skill `CLAUDE.md`, design notes, etc.). The skill pool is still populated from SKILL.md frontmatter; reference files contribute references, not skill identities.
- **md**: every `*.md` file under the scan roots, including SKILL.md, references, and any standalone docs.
- **all**: alias for **md**.

## Known Limitations

- **Prose-form hard deps** like "invoke `/example:review-read-comments` using the Skill tool" are classified as soft refs, not hard deps. The slash reference is still caught, just at WARNING level instead of ERROR.
- **Frontmatter parsing is regex-based** — handles the simple `key: value` shape used by SKILL.md files but does not parse the YAML contract block inside the body. The skill pool is built from frontmatter `name:` values only.
- **NON_SKILL_WORDS exclusion list** may occasionally filter a real skill name that collides with a common path segment (e.g. if someone creates a skill named "build"). Check the exclusion list in `references_audit.py` if a reference seems missing.
- **`settings.json`, `*.py`, and other non-markdown files** are not scanned at any scope.
- **Compound-adjective prose** like `X-/Y-foo` will match the trailing token (here, `Y-foo`) as a slash reference and produce a false positive. Category E in the taxonomy doc covers the remediation: reword the prose.
