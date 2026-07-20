---
name: cohesion-principles
author: christina
skill-type: reference-skill
description: Use when deciding where a fact should live across CLAUDE.md / SKILL.md / references (placement). Do NOT use for content shape (use md-authoring).
---

# Cohesion Principles (Content Allocation)

The canonical framework for **where a fact, rule, or doc-section should live** across the project's load
graph — CLAUDE.md (and which one), SKILL.md, or `references/*.md`. Every placement decision reduces to
CRP / CCP / ADP applied to the load graph; the L1/L2/L3 load levels are a derived consequence, not the
primary frame.

This is the shared spine: the `md-audit` domain (the artifact audits, reached via `/md-audit skill|claude-md|project-doc|references`)
and the `md-authoring` domain all defer to it. `content-authoring` (a reference under `md-authoring`) is the companion
that answers the orthogonal question — *how* a fact should be shaped (YAML vs prose vs frontmatter) — not where it lives.

The `facts:` block below is a routable index; the `content_allocation:` block beneath it is the load-bearing
framework the audits derive from.

## Index

```yaml
reference_skill:
  _schema_version: "1"
  identity: The canonical framework for where a fact, rule, or doc-section should live across the project's load graph (CLAUDE.md / SKILL.md / references), via CCP / CRP / ADP.
  scope:
    covers:
      - placing a fact, rule, or doc-section across CLAUDE.md / SKILL.md / references
      - applying CCP (write-together) / CRP (read-together) / ADP (link-forward-only) to the load graph
      - the per-artifact-role audit rules each surface must satisfy
      - the size-is-a-signal CRP test for SKILL.md splits
      - the skill-maturation pipeline (inline -> project reference -> skill)
    excludes:
      - content shape / form choice -- YAML vs prose vs frontmatter (use md-authoring)
      - skill-type contracts and per-type schemas (use skill-authoring)
      - running the audits that enforce these rules (use the md-audit domain)
  facts:
    - id: placement_algorithm
      summary: Place a fact by applying CCP (change cadence) -> CRP (reader set) -> ADP (load order) -> frequency tiebreak, in that order.
      keywords: [placement algorithm, where does this go, ccp crp adp order, change cadence, reader set]
      detail: CCP narrows by what changes force the fact to update; CRP narrows to the smallest scope whose readers all need it; ADP rejects forward load-graph edges; frequency breaks CRP ties. See placement_algorithm in the framework block below.
      example:
        input: "Where does the fact 'the validator has three states' go?"
        output: "CCP: it changes with audit.py -> scripts/CLAUDE.md; CRP: only validator editors need it; ADP: valid (no upward edge). Verdict: scripts/CLAUDE.md."
    - id: load_graph_dag
      summary: The files Claude loads form a DAG in load order; references run forward-only -- later-loaded may cite earlier-loaded, never the reverse.
      keywords: [load graph, dag, load order, forward-only, l1 l2 l3, surfaces]
      detail: Surfaces are root/subsystem/directory CLAUDE.md + .local (L1, loading on cwd descent OR file access beneath the directory), SKILL.md (L2), references/ (L3), project references (L3-equivalent), and the out-of-band surfaces -- scripts/assets, in-code contract docs, README. An edge must not reverse load order (e.g. a reference must not cite SKILL.md sections). See load_graph in the framework block.
    - id: total_ownership_roles
      summary: Every md (and md-adjacent) file in a project has a named role -- including README (derived human brief), generated artifacts (provenance-only audit), and in-code contract docs (SSOT for code-enforced schemas). No file is outside the model.
      keywords: [readme role, generated artifact, code-enforced contract, docstring ssot, out-of-band surface, asset dependency, total ownership]
      detail: "README: the agent-facing copy is SSOT, README is the derived brief; identity-grain overlap tolerated; agent-relevant facts stranded README-only are a FAIL. Generated artifacts (identified by a generation-record sidecar or in-file marker): audits check only that provenance is named. Code-enforced contracts: the validator's in-code doc owns the fields; md states existence/invariants/change-discipline. Runtime asset consumption is declared via asset_dependencies: so moves/renames FAIL the audit instead of breaking silently. See readme_md / generated_artifact / in_code_contract_doc in the framework block."
    - id: three_principles
      summary: Every placement reduces to CCP (write-together), CRP (read-together), ADP (link-forward-only); the L1/L2/L3 levels are a derived consequence.
      keywords: [ccp, crp, adp, common closure, common reuse, acyclic dependencies]
      detail: CCP co-locates facts that change together; CRP places a fact in the smallest scope whose readers all need it; ADP forbids depending on a later-loaded surface. See principles_applied_to_placement.
      gotchas:
        - Splitting same-change-cadence facts across files breaks SSOT and causes drift (CCP-fail).
        - A SKILL.md trimmed to a stub that always co-loads its only reference is a CRP-fail tool-call doubling, not progressive disclosure.
    - id: size_is_signal
      summary: The 500-line / 3000-token threshold is a SIGNAL to evaluate a CRP split, not a verdict; split only when a CRP-passing decomposition exists.
      keywords: [size threshold, signal not verdict, crp split, 500 lines 3000 tokens, progressive disclosure]
      detail: Over-threshold prompts evaluation; CRP is the test (do sections serve different reading tasks?). If no decomposition passes CRP, keep the monolith. See crp_reader_set.note_on_size_threshold and skill-authoring framework.md "CRP is the test for L2 -> L3 splits".
    - id: skill_maturation_pipeline
      summary: Knowledge matures out of an inline CLAUDE.md tip into a structured home once it stabilizes; the mature home follows the trigger shape (task-shaped -> skill, location-shaped -> directory CLAUDE.md), NOT "always a skill". Project references are the escape-hatch / nursery for still-emerging content.
      keywords: [skill maturation, project reference, nursery, escape hatch, graduate to skill, trigger shape]
      detail: An inline tip grows into a project reference (cited from CLAUDE.md) while the concept is still emerging, then matures into its trigger-appropriate home -- a skill when the trigger is task-shaped (a verb), or a directory CLAUDE.md reference when the trigger is location-shaped (working under a directory). Not all mature reference content belongs in a skill. See skill_maturation_pipeline and placement_follows_trigger_shape.
    - id: placement_follows_trigger_shape
      summary: The home for mature knowledge follows its natural trigger. TASK-shaped trigger (a verb -- "authoring a .fbs", "classifying a CL") -> skill. LOCATION-shaped trigger (working under a directory) -> referenced from that directory's CLAUDE.md, which auto-loads when files beneath it are touched. Both shapes -> prefer the location home; a skill may point at it.
      keywords: [trigger shape, task-shaped, location-shaped, graduate to skill, directory-scoped knowledge, when to make a skill, not everything is a skill, claude.md home]
      detail: |
        NOT all mature documentation graduates to a skill -- that stance is too opinionated. The
        placement question for mature knowledge is: what is the natural trigger for needing it?
        A TASK-shaped trigger is a verb the session performs (authoring a schema, running an
        evaluator, classifying a changelist); its content is loaded by matching the session-wide
        skill list, so a SKILL.md is the right home. A LOCATION-shaped trigger is "working under
        this directory" (a config subtree, quest source dirs); a directory CLAUDE.md auto-loads
        when ANY file beneath it is touched -- the exactly-right trigger, at zero session-wide
        context cost -- so directory-scoped knowledge belongs referenced from (or moved adjacent
        to) that CLAUDE.md, NOT graduated to a skill. Knowledge with BOTH shapes: prefer the
        location home and let a skill point at it via summarize-and-reference. See
        placement_follows_trigger_shape in the framework block.
    - id: summarize_and_reference
      summary: Facts live in one place (SSOT); elsewhere you reference. The canonical compact form is REMINDER PLUS REFERENCE together -- an inline summary that spares the reader loading the target, plus the reference that names the SSOT and makes it checkable.
      keywords: [summarize and reference, reminder plus reference, ssot pointer, dedup, cross-reference budget, collapse to pointer, guardrail plus pointer]
      detail: |
        When the same fact would otherwise be restated across files, keep it in ONE place and
        reference it from the others. The compact form is a reminder PLUS a reference:
        "Never p4 submit without user approval -- see /submit." Rule: summarize-and-reference is
        OK when the referenced fact fits in roughly a dozen tokens or less, so the reader does not
        need to load the target. Beyond that budget: reference only, no restatement (a longer
        restatement just re-creates the drift the SSOT was meant to prevent). This is the authoring
        side of the same convention the md-audit skills enforce as a FIX (dedup under the
        summarize-and-reference rule); the loss-free-deletion guard applies -- fold any local delta
        into the SSOT or the summary line before deleting the duplicate. See summarize_and_reference
        in the framework block.
```

## The placement framework

The load-bearing content. The audit skills derive their criteria from this block; `audit-criteria.md` (in
`claude-md-audit`) is the self-contained, audit-path summary derived from it.

```yaml
content_allocation:
  _schema_version: "1"
  identity: Every fact placement reduces to CRP / CCP / ADP applied to the load graph; the load levels (L1/L2/L3) are a derived consequence.

  load_graph:
    description: |
      The set of files Claude loads, in load order. Each surface has a defined load
      trigger; the order is what makes the graph a DAG. References between surfaces
      must run in the load-order direction (later-loaded may cite earlier-loaded;
      earlier-loaded must not cite later-loaded by name, since the later surface
      may not load).
    surfaces:
      - id: root_claude_md
        path_pattern: "<project>/CLAUDE.md"
        load_trigger: any cwd inside the project
        load_level: L1 (always loaded for any session in this project)
      - id: subsystem_claude_md
        path_pattern: "<project>/<subsystem>/CLAUDE.md (e.g. plugins/skills-kit/CLAUDE.md)"
        load_trigger: cwd inside the subsystem, OR file access (Read/Edit/Write) beneath it
        load_level: L1 (always loaded when working within the subsystem)
        notes: Multiple levels may exist (subsystem -> sub-subsystem). Each loads when the session descends into it -- by cwd or by touching files beneath it.
      - id: directory_claude_md
        path_pattern: "<any-directory>/CLAUDE.md (e.g. plugins/skills-kit/skills/skill-authoring/CLAUDE.md)"
        load_trigger: cwd inside this directory, OR file access (Read/Edit/Write) beneath it
        load_level: L1 (lazy-loaded; ambient whenever the session touches this directory's files)
        notes: |
          BOTH triggers are real: the harness loads a directory's CLAUDE.md when the cwd
          descends into it AND when files beneath it are read or edited by path. Most
          data directories and leaf code packages are worked BY PATH from a repo-root
          cwd -- a cwd-only model would mark their CLAUDE.md dead weight and force every
          fact to bubble to root, which is wrong. CRP consequence: the reader set of a
          directory CLAUDE.md is "sessions touching files beneath this directory"
          (by cwd or by path), not "sessions cd'd into it". A CLAUDE.md in a path-worked
          data or leaf-package directory (review rails for committed-data diffs, package
          review notes) is reachable and correctly placed.
      - id: claude_local_md
        path_pattern: "<any-directory>/CLAUDE.local.md"
        load_trigger: same as the co-located CLAUDE.md, but loaded only on the author's machine (p4ignored / gitignored)
        load_level: L1 (peer of CLAUDE.md at the same directory; personal scope)
      - id: skill_md
        path_pattern: "<plugin>/skills/<skill>/SKILL.md"
        load_trigger: skill's frontmatter description matches the user's request
        load_level: L2 (loaded on trigger, regardless of cwd)
      - id: skill_reference_md
        path_pattern: "<skill-dir>/references/*.md"
        load_trigger: SKILL.md instructs the agent to load this file by name
        load_level: L3 (loaded on demand from SKILL.md; one hop deep)
        notes: A reference document scoped to one skill, owned by that skill. Default home for reference content.
      - id: project_reference_md
        path_pattern: "<project>/docs/*.md, <project>/.claude/docs/*.md, <subsystem>/docs/*.md (any markdown doc NOT inside a <skill-dir>/references/ folder)"
        load_trigger: a CLAUDE.md (or another project reference doc) instructs the agent to load this file by name
        load_level: L3-equivalent (loaded on demand from CLAUDE.md; same load mechanism as a skill reference, but the consumer is CLAUDE.md, not SKILL.md)
        notes: |
          A reference document NOT contained within a skill. Two legitimate uses:
          (a) escape hatch when a CLAUDE.md is too large -- specific tips can be moved out so the
              CLAUDE.md stays small, while the information is still accessible (though less
              accessible -- the reader has to follow the pointer).
          (b) knowledge-maturation nursery -- a place to gather knowledge about an emerging
              concept that is not yet structured enough to ship as a skill. Project references
              may graduate into skills as the concept matures (see "Skill-maturation pipeline"
              below).
          Default preference: put reference content in a skill (skill_reference_md), not as a
          project_reference_md. Project references are the escape hatch / nursery, not the
          primary mode.
      - id: script_or_asset
        path_pattern: "<skill-dir>/scripts/*.py, <skill-dir>/templates/*, <skill-dir>/*.js"
        load_trigger: agent invokes via Bash / Read at runtime
        load_level: out-of-band (executables and assets do NOT enter context as text; only their stdout / read-content does)
        notes: |
          A script (or a SKILL.md/reference tool-argument example) may consume a repo file
          at runtime -- a reference owned by another skill, a data file, a template. That
          consumption is a real load-graph edge invisible to md-citation scanning; it must
          be declared in the skill's asset_dependencies: block (see the asset_dependency
          edge below) so the audits can resolve it mechanically.
      - id: in_code_contract_doc
        path_pattern: "module docstring or contract comment of the code that VALIDATES a machine contract (e.g. the schema module, the validator, the parser)"
        load_trigger: agent Reads the module on demand
        load_level: out-of-band (in-code documentation; enters context only when the module is read)
        notes: |
          For a CODE-ENFORCED contract (a schema the code validates, a wire format the
          code parses), the validator's in-code doc is the SSOT -- it changes in the
          same diff as the validator (perfect CCP). md surfaces state the contract's
          EXISTENCE, its INVARIANTS, and its CHANGE-DISCIPLINE, and cite the module for
          the field-level detail. An md file that enumerates every field of a
          code-validated schema is a drift hazard, not documentation (see the
          md_restates_code_enforced_contract anti-pattern).
      - id: readme_md
        path_pattern: "<project>/README.md (and <subsystem>/README.md)"
        load_trigger: never ambient for the agent; read only on explicit demand
        load_level: out-of-band (human-facing; readers are humans and web crawlers, effectively never the agent)
        notes: |
          The GitHub-facing brief. The agent-facing copy (the CLAUDE.md / skill graph)
          is the SSOT; README is the DERIVED brief -- identity, one architecture
          paragraph, layout pointers. Overlap with the root CLAUDE.md is tolerated at
          the identity-sentence grain (both may open with the same project identity).
          Commands, conventions, and schemas are never duplicated into README; any such
          fact present in README must also be reachable through the CLAUDE.md / skill
          graph, or agents can never see it.
    edges:
      - from: child_claude_md
        to: parent_claude_md
        rule: child may cite parent (parent already loaded first); the citation does not create a load dependency.
      - from: skill_md
        to: skill_reference_md
        rule: SKILL.md cites references/ (loads on demand). One hop deep.
      - from: claude_md
        to: project_reference_md
        rule: a CLAUDE.md may cite a project reference doc as an "if you're working on X, see docs/Y.md" pointer. The citation must be informational -- CLAUDE.md instructions remain complete without the project reference being loaded.
      - from: claude_md
        to: skill_md
        rule: |
          a CLAUDE.md may declare that a skill should be loaded ambient when the CLAUDE.md is in scope.
          Two expression forms:
          (a) a YAML header field on the CLAUDE.md (e.g. `required-skills: [python-coding]`) that
              the harness consumes to auto-load named skills. (Convention; check whether the
              harness in use today supports the field before relying on it.)
          (b) a prose pointer (e.g. "for any Python work, invoke /python-coding") in CLAUDE.md
              body. Less reliable -- the agent must remember to invoke. Acceptable when the
              skill load is conditional on the user's task within the scope.
          The edge is forward-only: CLAUDE.md may name a skill; SKILL.md must not depend on a
          specific CLAUDE.md being loaded (the skill must be self-contained against any cwd
          where it could trigger).
      - from: skill_reference_md
        to: sibling_skill_reference_md
        rule: one-hop-deep cross-citation only; a reference must not chain through multiple references.
      - from: project_reference_md
        to: sibling_project_reference_md
        rule: one-hop-deep cross-citation only; same constraint as skill references.
      - from: skill (its scripts, or tool-argument examples in its SKILL.md / references)
        to: any_repo_file (asset dependency)
        rule: |
          A skill may consume a repo file at RUNTIME -- a reference owned by another skill
          (correct when the file's CCP home is the other skill), a data file, a committed
          template. This is the asset_dependency edge. It is legitimate and often the right
          design (the asset stays with its CCP siblings; the consumer Reads it out-of-band),
          but it is invisible to md-citation checks -- so it MUST be declared in the
          consuming skill's asset_dependencies: block (a portable typed unit; see
          skill-authoring framework.md "Runtime asset dependencies"). The audit resolves
          each declared path mechanically: moving or renaming the asset then FAILs the
          consuming skill's audit instead of breaking silently at runtime.
      - prohibited:
          - from: parent_claude_md
            to: child_claude_md
            why: child loads conditionally on cwd; the parent is loaded for sessions outside the child's directory and would dangle.
          - from: skill_reference_md
            to: skill_md
            why: SKILL.md is upstream in load order; the reference runs after SKILL.md loads, so citing SKILL.md sections from the reference reverses the load direction.
          - from: project_reference_md
            to: claude_md
            why: CLAUDE.md is upstream in load order; the project reference is loaded after CLAUDE.md cites it, so citing back into CLAUDE.md sections reverses the load direction. The reference may reference CLAUDE.md as the orientation surface but must not depend on CLAUDE.md content the reader has already passed.
          - from: subsystem_claude_md
            to: deeper_subsystem_claude_md
            why: same dangling-load reason as parent->child.

  principles_applied_to_placement:
    - id: ccp_change_cadence
      principle: CCP (Common Closure Principle) -- write-together
      placement_question: "What set of changes would force this fact to update?"
      placement_rule: "The fact lives co-located with whatever drives those changes. Files have implicit change cadences; a fact belongs in the file whose cadence matches its own."
      bubble_test: |
        If editing system X always forces this fact to update, the fact lives near X.
        If editing X never forces this fact to update, the fact does not belong in X's CLAUDE.md.
      worked_examples:
        - fact: "audit.py supports three states: yaml-validated, contract-staged, legacy-fallback"
          change_driver: audit.py logic
          correct: scripts/CLAUDE.md (co-located with audit.py)
          wrong: framework.md (would force framework re-version on every script-internal change)
        - fact: "the merge-gate convention is zero FAILs across all SKILL.md and CLAUDE.md files"
          change_driver: plugin's release discipline
          correct: plugins/skills-kit/CLAUDE.md (plugin scope)
          wrong: skill-authoring/CLAUDE.md (too narrow), root CLAUDE.md (too broad)
        - fact: "the project uses Perforce, not git"
          change_driver: project tooling
          correct: root CLAUDE.md (entire project bound by this)
          wrong: any subsystem CLAUDE.md (would imply other subsystems use a different VCS)
      audit_violations:
        - name: same-cadence facts split across files
          test: scan for facts that update together; if they live in different files, CCP is violated.
        - name: facts placed where the change driver is absent
          test: ask "what does this scope's directory contain that would force this fact to update?" If the answer is "nothing in this directory", bubble up.

    - id: crp_reader_set
      principle: CRP (Common Reuse Principle) -- read-together
      placement_question: "Who reads this scope, and does every reader of this scope need this fact?"
      placement_rule: "The fact lives in the smallest scope whose readers all need it. If only some readers need it, push down. If readers in sibling scopes also need it, push up."
      bubble_test: |
        Bubble down: if a typical reader of this scope does NOT need the fact, push it to a child scope.
        Bubble up: if readers of sibling scopes also need the fact, push it to the common parent.
      worked_examples:
        - fact: "UE prefixes (U/A/F/I/T/E/S) and PascalCase variables"
          reader_set: any session writing UE C++ across the project
          correct: root CLAUDE.md (universal across the project's UE code)
          wrong: a single subsystem CLAUDE.md (sibling subsystems also write UE C++)
        - fact: "the Phase 4.6 P5 plugin-level orientation surface decisions"
          reader_set: only sessions working inside plugins/skills-kit
          correct: plugins/skills-kit/CLAUDE.md
          wrong: root CLAUDE.md (irrelevant to non-skills-kit work)
        - fact: "the validator's three-state output (yaml-validated/contract-staged/legacy-fallback) decision history"
          reader_set: only sessions editing the validator scripts
          correct: scripts/CLAUDE.md
          wrong: skill-authoring/CLAUDE.md (broader than the validator)
      audit_violations:
        - name: fact in too-broad scope
          test: would a typical reader of this scope NOT need it? If yes, bubble down.
        - name: fact in too-narrow scope
          test: do readers of sibling scopes also need it? If yes, bubble up.
        - name: fact in too-large file requiring CRP-decomposition
          test: does the file exceed 500 lines / 3000 tokens AND admit a CRP-passing decomposition (sub-sections fire on independent triggers)? If yes, split. If no decomposition passes CRP, keep monolithic.
      note_on_size_threshold: |
        The 500-line / 3000-token threshold is a SIGNAL for CRP evaluation, not a verdict.
        See framework.md "CRP is the test for L2 -> L3 splits" for the operational rule.
        A stub-with-always-co-loaded-reference is CRP-fail; revert and keep the monolith.

    - id: adp_load_order
      principle: ADP (Acyclic Dependencies Principle) -- link-forward-only
      placement_question: "Where does this file sit in the load graph, and what does this fact reference?"
      placement_rule: "Files reference downward in load order (later-loaded may cite earlier-loaded). A file must not depend on content that loads after it, because the dependency may dangle."
      operational_rules:
        - id: no_parent_to_child_citation
          rule: Parent CLAUDE.md must not cite a child CLAUDE.md by name as a load dependency.
          why: child loads conditionally on cwd; the parent is loaded for sessions where the child does not load.
          permitted: parent may say "for X-specific work, see <X>/CLAUDE.md" as a content pointer. The parent's correctness must not depend on the child being loaded.
          test: scan parent CLAUDE.md for `see <subdir>/CLAUDE.md`-style references; for each, check whether the parent's instruction is incomplete without the child. If yes, ADP-fail.
        - id: skill_to_reference_one_hop
          rule: SKILL.md cites references/*.md; a reference may cite a sibling reference at most one hop deep.
          why: deeper chains read partial content (Claude tends to stop at the second hop).
          test: scan references/ for citations to other references/; verify each is one hop. (audit.py already enforces this.)
        - id: reference_must_not_cite_skill_sections
          rule: A reference doc must not cite SKILL.md sections by name.
          why: load order is SKILL.md -> reference; the reference runs after SKILL.md and citing back into SKILL.md reverses the direction.
          permitted: a reference may name the SKILL.md as the orientation surface; it must not depend on SKILL.md content the reader has already passed.
        - id: stale_references_break_dag
          rule: Every cross-file reference must resolve.
          why: a broken edge breaks the DAG; the agent is left with no path to the cited content.
          test: for every "see X" citation, verify X exists. (Mechanical check.)
        - id: runtime_asset_dependencies_declared
          rule: Every repo file a skill consumes at runtime (via its scripts or its tool-argument examples) is declared in the skill's asset_dependencies: block, and every declared path resolves.
          why: the consumption edge is invisible to md-citation scanning ("see X" checks); undeclared, a rename/move of the asset breaks the consumer silently at runtime with no audit signal.
          test: for every declared asset_dependencies path, verify it resolves against the skill dir or the project root. (Mechanical check -- skills_kit_lib audit enforces it.) The judgment half -- "is every runtime consumption actually declared?" -- is checked by reading the skill's scripts and tool-args examples for embedded repo paths.
        - id: skills_must_not_gate_load_bearing_facts_for_common_errors
          rule: A common agent error pattern must live in a CLAUDE.md (always loaded for the relevant scope), not behind a skill's trigger.
          why: skill invocation is conditional on the trigger firing; common errors must be reachable on every session that could hit them. Gating a common error behind a skill creates a load-graph dependency on a trigger that may not fire.
          test: scan CLAUDE.md content for "for X, invoke /Y skill" pointers; for each, judge whether the underlying fact is a common agent error. If it is, the fact (or at least its trigger fingerprint) must be inline in CLAUDE.md.

  placement_algorithm:
    description: |
      When placing a new fact, apply the principles in this order. CCP narrows by
      change cadence; CRP narrows by reader set; ADP filters out load-graph
      violations; frequency is the tiebreaker when CRP is ambiguous.
    steps:
      - n: 1
        question: CCP - what changes force this fact to update?
        action: Identify the file whose change cadence matches. The fact lives there.
        narrows_to: a small set of candidate scopes (typically 1-3).
      - n: 2
        question: CRP - who reads each candidate scope, and does every reader need this fact?
        action: Choose the smallest scope whose readers all need it. Bubble down if some readers do not; bubble up if sibling readers also do.
        narrows_to: a single scope.
      - n: 3
        question: ADP - does this placement create a forward-only edge?
        action: Verify the fact does not require the file's reader to have loaded a downstream surface. Adjust if it does.
        narrows_to: confirms the placement, or surfaces a load-graph violation requiring restructure.
      - n: 4
        question: Frequency tiebreak - if CRP was ambiguous (the fact is "kind of" universal), how often does the fact fire?
        action: A fact load-bearing in MOST sessions justifies pushing UP into ambient L1; a rare fact justifies pushing DOWN into L2/L3.
        notes: Frequency is the tiebreak, not the primary frame. CCP and CRP usually settle the question first.

  per_artifact_role:
    - id: root_project_claude_md
      ccp_role: changes when project-wide conventions change (build commands, VCS choice, top-level directory layout, project terminology, language standards across the project).
      crp_role: every session in the project needs every fact in this file.
      adp_role: top of the load graph; cites no later-loaded file by name.
      audit_rules:
        - id: project_identity
          rule: includes a brief project description (1-3 lines).
          test: first 10 lines describe what the project IS.
          severity: FAIL if missing.
        - id: essential_commands
          rule: build / test / lint commands are present as exact runnable commands.
          test: scan for ```bash blocks containing build/test/lint invocations.
          severity: FAIL if missing.
        - id: directory_structure
          rule: high-level directory map showing where major components live.
          test: scan for a section with a directory listing or tree.
          severity: FAIL if missing for a multi-subsystem project.
        - id: ccp_change_cadence
          rule: every fact's change driver is project-wide.
          test: for each fact, ask "would editing only one subsystem's code force this fact to update?" If yes, the fact belongs in that subsystem's CLAUDE.md (CCP violation here).
          severity: FAIL on subsystem-cadence facts placed at root.
        - id: crp_reader_set
          rule: every fact is needed by every project session.
          test: ask "would a session working only in subsystem X need this?" If for some X the answer is no, bubble down.
          severity: INFO (migration opportunity; not always wrong).
        - id: adp_no_forward_dependencies
          rule: no `see <subdir>/CLAUDE.md` citation that creates a load dependency.
          test: scan for child-CLAUDE.md references; for each, judge whether root content is incomplete without the child.
          severity: FAIL on incomplete-without-child references.
      notes: |
        Externally-expected root facts vs CRP: when a fact externally expected at the root
        (a build command specific to one subsystem, a subsystem's directory detail) has a
        CRP-correct home in a child scope, CRP wins -- push the detail down to the child
        CLAUDE.md and keep a one-line pointer at the root. The pointer satisfies the
        root-level expectation (essential_commands / directory_structure) without inflating
        the root for readers who do not need the detail.

    - id: subsystem_claude_md
      ccp_role: changes when subsystem conventions / decisions change.
      crp_role: every session inside the subsystem needs every fact.
      adp_role: cites parent CLAUDE.md (already loaded); does not cite deeper child CLAUDE.md by name as a dependency.
      audit_rules:
        - id: ccp_change_cadence
          rule: every fact's change driver is subsystem-scoped.
          test: ask "would editing project conventions outside this subsystem force this fact to update?" If yes, bubble up.
          severity: FAIL on project-cadence facts placed in subsystem.
        - id: crp_reader_set
          rule: every fact is needed by every session inside this subsystem.
          test: ask "would a session in a sub-area of this subsystem (e.g. one specific skill within the plugin) NOT need this?" If yes, bubble down.
          severity: INFO (bubble-down opportunity).
        - id: not_duplicated_from_parent
          rule: no fact present here is also present in any ancestor CLAUDE.md.
          test: diff against ancestor CLAUDE.md content; flag duplications.
          severity: FAIL (CCP+SSOT violation).
        - id: adp_no_forward_dependencies
          rule: no citation of deeper child CLAUDE.md as a load dependency.
          test: same as root.
          severity: FAIL on incomplete-without-child references.

    - id: directory_claude_md
      ccp_role: changes when local conventions or decision history in this directory changes.
      crp_role: every session TOUCHING FILES BENEATH this directory -- by cwd descent or by path access -- needs every fact (this is a narrower set than "every session triggering a skill in this directory" - SKILL.md serves that audience). Do not treat a path-worked directory's CLAUDE.md as unreachable; the file-access trigger makes it load exactly when its readers appear.
      adp_role: cites ancestor CLAUDE.md content freely; does not cite SKILL.md or references/ as a dependency (those are downstream and triggered by different load events).
      audit_rules:
        - id: directory_specific_content
          rule: every fact is specific to this directory; not generic to the parent.
          test: ask "would removing this fact and pushing it to the parent CLAUDE.md inflate the parent with content most readers don't need?" If no (it would not inflate), the fact belongs at the parent.
          severity: FAIL on parent-cadence content.
        - id: decision_provenance_lives_here
          rule: decision history (origin/finding/follow-up records) lives in CLAUDE.md, not in SKILL.md or framework.md.
          test: scan SKILL.md for "Dec-N" / "decided 2026-..." style entries; if present, they belong here.
          severity: FAIL on decision history in SKILL.md.

    - id: claude_local_md
      ccp_role: changes when personal preferences / machine-specific paths change.
      crp_role: read by the same agent in the same scope as the co-located CLAUDE.md, but contains personal-only content.
      adp_role: peer of CLAUDE.md; loaded alongside.
      audit_rules:
        - id: personal_only
          rule: every fact is machine-specific or personal preference.
          test: would another team member benefit from this fact? If yes, move to shared CLAUDE.md.
          severity: FAIL on team-useful content.
        - id: not_duplicated_from_shared
          rule: no fact present here is also in the shared CLAUDE.md.
          test: diff against shared CLAUDE.md; flag duplications.
          severity: FAIL on duplications.

    - id: skill_md
      ccp_role: changes when the skill's contract or trigger changes.
      crp_role: every invocation of the skill needs every section.
      adp_role: cites references/ (downstream); does not cite parent CLAUDE.md content as a dependency.
      audit_rules:
        - id: trigger_not_summary
          rule: description is a directive trigger ("Use when..."), not a capability summary.
          test: see framework.md description requirements.
          severity: FAIL on capability-summary descriptions.
        - id: crp_within_file
          rule: every section is needed by every invocation; sections fired by different sub-triggers split into references/.
          test: identify sections; for each, judge whether it fires on the same trigger as the others. If sub-triggers differ, split per CRP.
          severity: FAIL on must-split content above the size threshold; INFO on borderline cases.
        - id: no_decision_provenance
          rule: decision history lives in the co-located CLAUDE.md, not here.
          test: scan for Dec-N entries.
          severity: FAIL on decision history in SKILL.md.
        - id: no_project_convention_facts
          rule: facts that change with project conventions live in CLAUDE.md, not in SKILL.md.
          test: scan for facts whose change driver is the project (e.g. "this project uses Perforce" in a SKILL.md not specifically about P4).
          severity: FAIL on project-convention facts.

    - id: skill_reference_md
      ccp_role: changes when one specific advanced situation's mechanics change.
      crp_role: every reader who lands here needs everything in the file (or it should split).
      adp_role: bottom of the load graph; one-hop-deep cross-references only; does not cite SKILL.md by section.
      audit_rules:
        - id: crp_unitary_reading_task
          rule: all sections fire on the same sub-trigger.
          test: enumerate sections; for each, judge whether it loads in the same situation as the others. If multiple sub-triggers fire, split.
          severity: FAIL on multi-trigger references.
        - id: one_hop_deep
          rule: cross-references to other references/ are one hop, not chained.
          test: scan for citations of other references/; verify no chains. (audit.py enforces this mechanically.)
          severity: FAIL on chained references.
        - id: no_skill_md_back_reference
          rule: no citation of SKILL.md sections by name.
          test: scan for `SKILL.md` mentions citing section content.
          severity: FAIL on back-citations.
        - id: no_code_contract_field_restatement
          rule: the reference does not enumerate field-by-field a machine contract that code validates; it states the contract's existence, invariants, and change-discipline and cites the validating module (the in_code_contract_doc surface) for the fields.
          test: for each schema/format the reference describes, check whether code validates it; if yes, check whether the reference restates the field list rather than citing the module.
          severity: FAIL on live field-by-field restatement; INFO when the restatement is flagged in-doc as an accepted drift risk.

    - id: project_reference_md
      ccp_role: changes when the specific topic the document covers changes. Same as skill_reference_md, but the change driver is whatever the topic is rather than a particular skill's contract.
      crp_role: every reader who lands here needs everything in the file. Same multi-trigger split test as skill references.
      adp_role: cited by name from a CLAUDE.md (or another project reference). Must not cite back into CLAUDE.md sections; the citation reverses load order. May cite a sibling project reference or a SKILL.md by name (informational pointer only -- the SKILL.md will load on its own trigger if invoked).
      audit_rules:
        - id: not_in_skill_directory
          rule: file path is NOT inside any <skill-dir>/references/ folder. Project references live at project-level paths (<project>/docs/, <project>/.claude/docs/, <subsystem>/docs/, etc.).
          test: scan path; if it matches `*/skills/*/references/*`, this is a skill reference, not a project reference. Mis-classified file -- update the audit role.
          severity: FAIL if mis-classified (a fix to the audit, not a fix to the file).
        - id: prefer_mature_home_by_trigger_shape
          rule: project references are the escape hatch / nursery for still-emerging content, not a permanent home. When content stabilizes, it moves to its TRIGGER-APPROPRIATE mature home -- a skill (task-shaped trigger) or a directory CLAUDE.md reference (location-shaped trigger). Skills are NOT the default home for all reference content; see placement_follows_trigger_shape.
          test: ask "what is the natural trigger for this content?" If a VERB the session performs (task-shaped), it fits a skill. If WORKING UNDER A DIRECTORY (location-shaped), it belongs referenced from that directory's CLAUDE.md, not a skill. Both shapes -> prefer the directory CLAUDE.md home and let a skill point at it.
          severity: INFO -- migration opportunity toward the trigger-appropriate home. Do not recommend skill-graduation for location-scoped knowledge (see placement_follows_trigger_shape and skill_maturation_pipeline).
        - id: crp_unitary_reading_task
          rule: all sections fire on the same sub-trigger (same as skill_reference_md).
          severity: FAIL on multi-trigger references.
        - id: one_hop_deep
          rule: cross-references to other project references are one hop, not chained.
          severity: FAIL on chained references.
        - id: no_claude_md_back_reference
          rule: no citation of CLAUDE.md sections by name.
          test: scan for `CLAUDE.md` mentions citing section content from CLAUDE.md.
          severity: FAIL on back-citations.
        - id: no_code_contract_field_restatement
          rule: the doc does not enumerate field-by-field a machine contract that code validates; it states the contract's existence, invariants, and change-discipline and cites the validating module (the in_code_contract_doc surface) for the fields.
          test: for each schema/format the doc describes, check whether code validates it; if yes, check whether the doc restates the field list rather than citing the module.
          severity: FAIL on live field-by-field restatement; INFO when the restatement is flagged in-doc as an accepted drift risk.

    - id: readme_md
      ccp_role: changes when the project's public identity or high-level architecture changes -- the same drivers as the root CLAUDE.md's identity content, which is why the identity-grain overlap is tolerated.
      crp_role: readers are humans and web crawlers landing on the repo; effectively never the agent. Content is for that audience -- identity, one architecture paragraph, layout pointers, badges, install/usage for outsiders.
      adp_role: out-of-band; nothing in the agent load graph depends on README. README may point INTO the graph ("see CLAUDE.md", "see docs/"); nothing points out to it as a load dependency.
      audit_rules:
        - id: derived_brief_not_ssot
          rule: the agent-facing copy (CLAUDE.md / skill graph) is the SSOT; README is the derived brief. Identity/architecture overlap with the root CLAUDE.md is tolerated at the identity-sentence grain and is NOT a duplication finding.
          test: for overlapping content, confirm the grain -- an identity sentence or a single architecture paragraph passes; a synchronized multi-section restatement fails (derive, don't mirror).
          severity: INFO on grain overflow (recommend trimming README to the brief).
        - id: no_agent_only_facts_stranded_in_readme
          rule: every command, convention, or schema present in README is also reachable through the CLAUDE.md / skill graph.
          test: for each command block, convention statement, or schema description in README, verify the same fact (or its SSOT) is reachable from a CLAUDE.md or skill surface.
          severity: FAIL on facts stranded in README only -- agents never load README, so a README-only fact is invisible to every session.

    - id: generated_artifact
      ccp_role: changes when REGENERATED -- the change driver is the generator plus its inputs, never a hand edit. Committed outputs of a tool or an analysis session (renderings, generated reports, session-analysis documents).
      crp_role: readers are humans reviewing the output, or sessions regenerating it. The content is not hand-maintained knowledge; placement/maturation rules for authored docs do not apply.
      adp_role: out-of-band leaf; nothing may depend on a generated artifact as a load-bearing knowledge surface (regeneration may rewrite it wholesale).
      identification: |
        A path convention or provenance signal identifies the role: a machine-readable
        generation-record sidecar next to the artifact (e.g. <name>.params.json recording
        exactly how to regenerate), OR an in-file marker in the first ~20 lines naming the
        generator or the generating session ("generated by X", "generated analysis").
      audit_rules:
        - id: provenance_named
          rule: the generator or session provenance is named -- via the sidecar or the in-file marker. This is the ONLY audit rule for a generated artifact; the authored-doc criteria (maturation, CRP split, orphan, duplication) are skipped.
          test: check for a generation-record sidecar or an in-file generation marker.
          severity: FAIL when a file is claimed/committed as generated output but carries neither signal (unverifiable provenance); everything else about the file is exempt.

  skill_maturation_pipeline:
    description: |
      Project reference documents are not just an escape hatch -- they are also a deliberate
      stage in the knowledge-maturation pipeline. Concepts often start unstructured (a few
      tips inline in CLAUDE.md), accumulate enough material to spill into a project
      reference (still unstructured but no longer inline), and eventually mature into a
      skill (with a structured contract, a trigger, and an audit surface).
    stages:
      - n: 1
        location: inline in CLAUDE.md
        when: knowledge is small (a tip, a single fact, a one-line guardrail). The content is load-bearing in a way that justifies ambient cost.
        example: 'a single fact like "this project uses Perforce, not git" (root CLAUDE.md).'
      - n: 2
        location: project reference doc, cited from CLAUDE.md
        when: knowledge has grown beyond what fits inline. Multiple tips, an emerging pattern, or a topic with sub-points. Not yet structured enough to fit a skill type.
        example: ".claude/docs/python-tips.md collecting Python gotchas the team has hit."
        graduation_signal: when the content stabilizes into a procedure (technique-skill candidate), a rule + counter pattern (discipline-skill candidate), a lookup table (reference-skill candidate), or a wrapper around an external tool (capability-skill candidate), it is ready to graduate.
      - n: 3
        location: the trigger-appropriate mature home -- a skill (task-shaped trigger) OR a directory CLAUDE.md reference (location-shaped trigger). See placement_follows_trigger_shape; "always a skill" is the wrong default.
        when: the concept has stabilized and its natural trigger is clear. If the trigger is a VERB the session performs, it graduates to a skill (SKILL.md + references/, migrated or replaced from the project reference). If the trigger is WORKING UNDER A DIRECTORY, it homes to that directory's CLAUDE.md (moved adjacent, or referenced), which auto-loads on file access beneath it -- no skill.
        example: 'task-shaped: ".claude/docs/python-tips.md graduates into a /python-coding skill". location-shaped: quest-source review notes move into quests/CLAUDE.md rather than a skill.'
    audit_implication: |
      A project reference doc that has stabilized is INFO-flagged for its trigger-appropriate
      home by the audit: skill-graduation ONLY when the trigger is task-shaped; a directory
      CLAUDE.md reference when it is location-shaped (see placement_follows_trigger_shape). The
      flag is not a FAIL -- the doc is doing useful work where it sits -- and the framework does
      NOT prefer skills for directory-scoped knowledge; it prefers the location home.

  placement_follows_trigger_shape:
    description: |
      The razor for where MATURE knowledge lives. Skills-kit is NOT of the view that all mature
      documentation graduates to a skill -- that stance is too opinionated. The placement
      question is: what is the natural TRIGGER for needing this knowledge?
    razor:
      - shape: task-shaped
        trigger: a VERB the session performs -- "authoring a .fbs", "classifying a CL", "running the Lens evaluator".
        home: a skill (SKILL.md). Loaded by matching the session-wide skill list; the right home when the need is triggered by an activity, wherever in the tree it happens.
      - shape: location-shaped
        trigger: WORKING UNDER A DIRECTORY -- a config subtree, quest source dirs, a package.
        home: referenced from (or moved adjacent to) that directory's CLAUDE.md. A directory CLAUDE.md auto-loads when ANY file beneath it is touched (by cwd or by path) -- the exactly-right trigger for directory-scoped knowledge, at ZERO session-wide context cost. Do NOT graduate location-scoped knowledge to a skill.
      - shape: both
        trigger: the knowledge is needed both by an activity AND when working in a place.
        home: prefer the LOCATION home (the directory CLAUDE.md); a skill may point at it via summarize-and-reference rather than duplicating it.
    why: |
      A skill spends session-wide context budget (its description sits in every session's skill
      list) and fires by description match. A directory CLAUDE.md spends nothing until the
      session touches files beneath it, then loads exactly when its readers appear. Matching the
      home to the trigger shape puts directory-scoped knowledge where it costs nothing and loads
      precisely, and reserves skills for activity-triggered knowledge that must be reachable from
      anywhere.
    audit_link: |
      project-doc-audit routes its maturation recommendations by this razor: recommend
      skill-graduation (taxonomy B) ONLY for task-shaped knowledge; recommend folding into /
      referencing from a directory CLAUDE.md (taxonomy C) for location-shaped knowledge -- the
      PREFERRED destination for directory-scoped content, not a lesser fallback.

  summarize_and_reference:
    description: |
      The compact form of a cross-file reference. Facts live in one place (the SSOT); every
      other surface REFERENCES rather than restates. The canonical form is REMINDER PLUS
      REFERENCE together: the inline reminder spares the reader from loading the target for a
      fact they can hold in a phrase; the reference names the SSOT and keeps the claim
      checkable. This is the mechanism CCP's "collapse to a pointer" and "use cross-references
      for terminology, not content" alternatives invoke; this convention gives it a budget.
    rule: |
      Summarize-and-reference (keep the inline reminder) is OK when the referenced fact fits in
      roughly a dozen tokens or less -- short enough that the reader need not load the target.
      Beyond that budget: reference ONLY, no restatement, because a longer inline copy re-creates
      the drift the SSOT exists to prevent.
    example: '"Never p4 submit without user approval -- see /submit." (reminder + reference, within budget)'
    loss_free_guard: |
      Before collapsing a duplicate to a reference, fold any local delta -- an extra fact, a
      directory-specific anchor the surviving copy lacks -- into the SSOT (or the reminder line)
      FIRST. Only then delete the proven-redundant remainder.
    audit_link: |
      The md-audit skills enforce this convention from the audit side: cross-file / skill-content
      duplication is a FIX (dedup under the summarize-and-reference rule), applied with the same
      loss-free-deletion guard. Authoring to this rule is what keeps that audit green.

  worked_examples:
    - id: claude_md_placement
      scenario: Adding a fact about the validator's three-state output (yaml-validated / contract-staged / legacy-fallback).
      ccp_step: change driver is audit.py logic; the fact updates when the validator's branching changes.
      crp_step: only readers editing or reasoning about the validator scripts need this; readers writing skills do not.
      adp_step: scripts/CLAUDE.md is downstream of skill-authoring/CLAUDE.md, which is downstream of plugins/skills-kit/CLAUDE.md. Placement at scripts/CLAUDE.md is valid (no upward references created).
      verdict: scripts/CLAUDE.md.
    - id: skill_md_split
      scenario: A SKILL.md has grown to 600 lines with sections on (a) common operations and (b) edge-case handling.
      ccp_step: both sections change when the skill's contract changes; same cadence.
      crp_step: common operations fire on every invocation; edge-case handling fires only when the user reports a specific failure. Different sub-triggers; CRP says split.
      adp_step: SKILL.md cites the edge-cases reference under references/ (one hop, downstream). Forward-only.
      verdict: split edge-case handling into a references/ doc (an edge-cases reference).
    - id: parent_to_child_temptation
      scenario: Adding "for skill-authoring decisions, see skills/skill-authoring/CLAUDE.md" to plugins/skills-kit/CLAUDE.md.
      ccp_step: this is a content pointer, not a placement decision. It is acceptable as orientation IF the parent's correctness does not depend on the child being loaded.
      crp_step: the parent is loaded for sessions in any sub-area of skills-kit; some of those sessions never enter skill-authoring/. The pointer must not gate parent correctness on the child.
      adp_step: the citation passes ADP only if it is informational. If the parent says "see child for the details on X" and X is critical, ADP-fail.
      verdict: keep the pointer; do not move critical content into the child while leaving an incomplete reference at the parent.

  anti_patterns:
    - id: ccp_fail_same_cadence_split
      name: same-change-cadence facts split across files
      keywords: [ccp violation, same cadence split, drift, hunt across files]
      why_it_seems_right: "splitting feels organized; one fact per file feels minimal."
      why_it_is_wrong: "drift -- updating one fact requires hunting for and updating the other; SSOT is broken; the change is spread across multiple commits."
      alternative: collapse to a single file. Identify the change driver; place all same-driver facts together. Use cross-references for terminology, not for content.

    - id: crp_fail_stub_with_always_co_loaded_reference
      name: SKILL.md stub trimmed to a pointer that always loads its only reference
      keywords: [crp violation, tool-call doubling, stub with co-loaded reference, two loads one read]
      why_it_seems_right: "the SKILL.md is now under the size threshold; progressive disclosure looks satisfied."
      why_it_is_wrong: "the reader pays two file loads for one reading task; CRP fails because the 'sections' do not serve different reading tasks; the second load is mandatory, not conditional."
      alternative: revert the split. Inline the reference back into SKILL.md and accept the over-threshold size, OR find a genuine sub-trigger decomposition where the reference loads only sometimes.

    - id: adp_fail_parent_to_child_citation
      name: parent CLAUDE.md depending on child CLAUDE.md content
      keywords: [adp violation, dangling load, child not loaded, parent incomplete]
      why_it_seems_right: "structuring 'see child for details' looks like clean delegation."
      why_it_is_wrong: "the child loads conditionally on cwd; sessions where the child does not load see an incomplete parent. The parent's correctness depends on a downstream load that may not fire."
      alternative: inline the load-bearing fact at the parent. Keep child-CLAUDE.md citations as orientation pointers only, never as content delegation.

    - id: ccp_fail_skill_md_carrying_decision_history
      name: SKILL.md accumulates decision provenance (Dec-N entries)
      keywords: [ccp violation, decision history in skill, change cadence mismatch]
      why_it_seems_right: "the decision concerns the skill; logging it next to the skill's content feels co-located."
      why_it_is_wrong: "SKILL.md changes when the skill's contract changes; decision provenance changes when audits surface new findings. Different cadences. The provenance bloats SKILL.md and forces the contract file to update on every audit."
      alternative: decision history goes in the co-located CLAUDE.md (e.g. skill-authoring/CLAUDE.md). SKILL.md stays focused on the contract.

    - id: adp_fail_skill_gating_common_errors
      name: a common agent error documented only behind a skill's trigger
      keywords: [adp violation, skill gating common error, conditional reachability]
      why_it_seems_right: "the skill is the natural home for the topic; the trigger should fire when the error is relevant."
      why_it_is_wrong: "skill invocation is conditional on the description matching the user's request; agents make common errors in many contexts, not just when the skill's trigger fires. The error must be reachable in the always-loaded layer."
      alternative: keep a one-line guardrail in CLAUDE.md naming the error and pointing at the skill for full guidance. The CLAUDE.md line ensures reachability; the skill carries the depth.

    - id: project_reference_should_have_been_skill
      name: project reference doc has matured into a structured concept but never graduated
      keywords: [skill maturation pipeline, project reference proliferation, ungraduated concept, structured content not in skill]
      why_it_seems_right: "the doc is doing useful work where it sits; readers know to look there; migration is friction."
      why_it_is_wrong: "structured procedural / rule / lookup / wrapper content has more leverage as a skill -- a discoverable trigger, an audit surface, a typed contract. Leaving structured content as an unstructured project reference forfeits those benefits and makes the content less reachable than it could be."
      alternative: graduate the project reference into a skill. Identify the matching skill type (technique / discipline / reference / capability), restructure the content into the type's contract, ship a SKILL.md with the appropriate frontmatter and trigger. Migrate the project reference content into the skill's references/ folder if it serves a sub-trigger, or replace it entirely with structured SKILL.md content.

    - id: md_restates_code_enforced_contract
      name: md surface enumerates every field of a schema that code validates
      keywords: [code-enforced contract, schema restatement, docstring ssot, field-by-field drift, validator doc]
      why_it_seems_right: "the reference doc feels incomplete without the schema's fields; readers shouldn't have to open the code."
      why_it_is_wrong: "the validator's in-code doc changes in the same diff as the validator (perfect CCP); an md copy of the field list changes in a DIFFERENT diff and drifts. Two enumerations of one code-enforced contract is an SSOT violation with a silent failure mode -- the md copy keeps looking authoritative after the code moves on."
      alternative: the module docstring (or contract comment) of the validating code is the SSOT for the fields. md surfaces state the contract's existence, its invariants, and its change-discipline ("the schema changes in the same diff as metadata.py's validator"), and cite the module for the field-level detail.

    - id: project_reference_proliferation_when_skill_exists
      name: a project reference doc duplicates content already in a skill
      keywords: [project reference duplication, skill content escape, ssot violation, parallel reference]
      why_it_seems_right: "the project reference is more discoverable from CLAUDE.md than the skill is from a description match; duplicating ensures the reader finds it."
      why_it_is_wrong: "duplication is SSOT violation; the two copies drift independently. CLAUDE.md should point at the skill, not duplicate the skill's content into a parallel project reference."
      alternative: collapse to a pointer. CLAUDE.md says "for X, invoke /skill-name" (or declares it required-skills); the skill's references/ holds the canonical content.
```

## Cross-references

- **Vocabulary** -- `glossary.md` (in skills-kit:skill-authoring): CRP, CCP, ADP, SSOT, progressive disclosure, conditional details.
- **CRP for SKILL.md size splits** -- `framework.md` (in skills-kit:skill-authoring) "CRP is the test for L2 -> L3 splits" section. The placement algorithm here generalizes the CRP test to all placement decisions.
- **Content shape (the orthogonal question)** -- the `content-authoring` reference under `/md-authoring`: how a fact should be shaped once you know where it lives.
- **The audits that enforce these rules** -- the `md-audit` domain (`/md-audit skill`, `/md-audit claude-md`, `/md-audit project-doc`, `/md-audit references`). The `project_reference_md` per-artifact role + `skill_maturation_pipeline` here are operationalized by `project-doc-audit` (`/md-audit project-doc`).
