# skills_kit_lib insights

Per-directory insight repository for the plugin-level Python library that powers audit / classify / tag / schema-validation across the skills-kit ecosystem. Insights captured during the YAML contract refactor (Phase Y1-Y4) and the library extraction (current session).

**Phase / finding identifier legend.** `Phase Y1`-`Y4` = phases of the YAML contract refactor (Y1 = stdlib walker design; Y4 = local-code-review conversion). `Phase 4.2` = corpus audit pass. `F-4-2-N` = numbered findings from Phase 4.2 (e.g. F-4-2-2 / F-4-2-3 = paired user-only technique-skill findings). For the full legend, see ../skills/skill-authoring/CLAUDE.md.

```yaml
claude_md:
  _schema_version: "1"
  scope:
    directory: plugins/skills-kit/skills_kit_lib
    covers:
      - schema_engine / schema_registry / rule_fragments design (the typed-unit DSL)
      - schemas/portable, schemas/skill_types, schemas/claude_md (the registered schemas)
      - document_walker (fenced-yaml-block extraction)
      - markdown_heuristics (the heuristic vocabulary used by the legacy markdown fallback)
      - corpus (SKILL.md discovery across user/project/plugin tiers)
      - checks (owner_doc validation and other corpus-level rules)
      - audit / classify / tag (per-skill CLI utilities, invoked via `python -m skills_kit_lib.<module>`)
      - dependency posture (stdlib + pyyaml; editable-installed via pyproject.toml)
    excludes:
      - skill content authoring (covered by ../skills/skill-authoring/references/glossary.md and framework.md)
      - bootstrap-engine internals (covered by plugins/bootstrap/skills/bootstrap/SKILL.md)
  insights:
    - id: strip_code_fences_before_heuristics
      keywords: [code fence, fenced block, narrative body, mixed-type false positive, audience-claude, yaml block, stripped body, heuristic over-fire]
      summary: Apply narrative heuristics to body text with fenced code blocks removed; structured data inside fences must not raise type-signal scores.
      detail: |
        markdown_heuristics.strip_code_fences() removes ```...``` blocks before applying type signal heuristics
        (count_ordered_steps, has_recognition_marker, etc.). The principle: structured data inside
        fenced YAML/JSON/python is reference content for machine comprehension, not narrative or
        procedure. Counting ordered list items inside a fenced ```python code block raised technique
        scores on reference-skills (bootstrap mixed-type false positive). markdown_heuristics.has_yaml_block()
        is the inverse signal: presence of a fenced YAML block is a positive reference-content marker.
      origin: Phase 4.2 audit lessons-learned (F-4-2 series); Audience-Claude principle.
      added: "2026-04-28"
    - id: user_only_via_disable_model_invocation
      keywords: [user-only, disable-model-invocation, slash-command, technique-skill carve-out, ordered-step exemption]
      summary: User-only technique-skills (disable-model-invocation true in frontmatter) skip the ordered-step body requirement; the technique IS the slash-command.
      detail: |
        markdown_heuristics.is_user_only(fm) returns true when frontmatter sets disable-model-invocation: true.
        type_signals(body, fm) adds +3 to technique-skill score for user-only skills so classify
        recognizes them as technique-skill even when the body has zero ordered-step entries.
        audit.check_technique_skill threads frontmatter so the ordered-step row reports n/a for
        user-only skills with note "user-only ... the technique IS the slash-command". The unified
        TECHNIQUE_SKILL_SCHEMA in schemas/skill_types.py enforces "techniques must have steps OR output_template"
        instead of the previous variant-separated schemas.
      origin: Phase 4.2 audit lessons-learned F-4-2-2 / F-4-2-3.
      added: "2026-04-28"
    - id: schema_walker_rule_vocabulary
      keywords: [schema, walker, validator, rule grammar, required, type, min_len, forbid_regex, items, keys, value_schema]
      summary: schema_engine.py uses a small Python-dict rule vocabulary; no external schema language.
      detail: |
        Each schema row is a dict with keys from a fixed vocabulary: required (bool), type
        (string|list|dict|int|bool), min_len/max_len (int), forbid_regex (regex with msg), items
        (subschema for list members), keys (subschema for dict children), value_schema (subschema for
        every value in a dict with arbitrary keys -- used by ACTIONS_SCHEMA). schema_engine._validate_value
        walks recursively. Cross-record rules (e.g. facts_must_include_gotcha across nested+top-level
        sources) live in audit.py as document-level checks evaluated after the walker. No jsonschema
        or pydantic dependency.
      origin: Phase Y1 design choice for the YAML contract refactor.
      added: "2026-04-28"
    - id: three_audit_states
      keywords: [audit states, yaml-validated, contract-staged, legacy fallback, pyyaml, transition]
      summary: audit.py has three runtime states; the staged middle state lets converted skills audit cleanly before pyyaml lands.
      detail: |
        State 1 (yaml-validated): pyyaml present + a recognized YAML contract block. Schema walker runs, deterministic per-row pass/fail, mixed-type signal deterministic. Legacy heuristics skipped.
        State 2 (contract-staged): pyyaml absent BUT a YAML contract block is detected by regex (root key match). Universal rows pass; YAML contract row reports judgment-required ("install pyyaml to validate"); legacy heuristics skipped (because a contract is staged, just unvalidated). Mixed-type signal deferred.
        State 3 (legacy markdown fallback): no YAML contract block at all. All legacy heuristics run as before; mixed-type via narrative scoring.
        The middle state was added so the converted skills don't report bogus markdown-heuristic failures during the transition.
      origin: Phase Y1.3 implementation; permission denial on global pip install of pyyaml in this dev env.
      added: "2026-04-28"
    - id: pyyaml_dependency_posture
      keywords: [pyyaml, dependency, stdlib, plugin venv, bootstrap, optional]
      summary: pyyaml is a runtime dependency declared in plugins/skills-kit/pyproject.toml; the audit runs without it via the contract-staged state.
      detail: |
        plugins/skills-kit/pyproject.toml declares pyyaml under dependencies. The bootstrap engine sets up a plugin venv at ~/.claude/plugins/data/plugins-kit/skills-kit/.venv/ where the audit script can be invoked with pyyaml available. Outside that venv (e.g. running with bare system Python), audit.py degrades gracefully to the contract-staged state (see three_audit_states). Do not add stdlib-only YAML parsing -- the multi-step YAML sequence pattern uses real YAML (lists, nested mappings) that a hand-rolled subset parser cannot cover.
      origin: Phase Y1.1 dependency decision; proposal section E.6.
      added: "2026-04-28"
    - id: extra_keys_allowed
      keywords: [extra keys, schema strictness, open record, narration, subagents, skill-specific structure]
      summary: The validator does not reject unknown keys; skill-specific structure (e.g. p4-code-review's narration, subagents) is preserved.
      detail: |
        validate() walks declared schema keys but does not error on additional keys present in the YAML data. This permits skill-specific structured fields the generic schema doesn't cover (e.g. p4-code-review carries narration:, subagents:, false_positive_guardrails: alongside the technique_skill: required keys). Forbidden cross-type keys (rules:, counters:, facts:, etc. inside a wrong root) DO fail. The trade: unknown keys pass silently rather than flagging for review. Y5 schema lock may revisit this if real audits surface load-bearing content hiding in extra keys.
      origin: Phase Y4 conversion of local-code-review; proposal recommendation in section E.3.
      added: "2026-04-28"
    - id: owner_doc_bidirectional_drift
      keywords: [owner_doc, schema drift, prose spec, audit, bidirectional protection, instance block]
      summary: Each registered schema declares owner_doc pointing at its canonical prose spec; corpus audit asserts the owner doc contains a valid instance of the schema's root key.
      detail: |
        Every schema in schemas/* declares an owner_doc field (plugin-root-relative path). The
        check_schema_owner_docs_validate() function in checks.py walks the registry, opens each
        owner_doc, finds <root>: blocks via document_walker.collect_yaml_units, and validates each
        instance against its schema. This catches drift in both directions: a schema change that
        adds a required field breaks the owner doc's example; an owner doc that edits to use a
        key the schema doesn't know fails validation. Schemas are Python literals (canonical);
        .md docs are prose specs that must round-trip through validation. The owner_doc field
        does NOT make .md the source of truth -- it's a back-reference, not a forward dependency.
      origin: Current session library-extraction design.
      added: "2026-05-19"
    - id: portable_units_vs_skill_type_roots
      keywords: [portable unit, skill type, registry role, mutual exclusion, mixed-type drift]
      summary: Portable units coexist freely; skill-type roots are mutually exclusive within a document.
      detail: |
        schema_registry tracks two role categories: SKILL_TYPE_ROOTS (reference_skill,
        pattern_skill, technique_skill, discipline_skill, domain_skill, capability_skill,
        audit_skill) and PORTABLE_UNIT_ROOTS (references, facts, area_config, sub_areas, actions).
        detect_mixed_type_yaml only flags drift on skill-type roots; portable units are
        first-class typed YAML primitives that can attach to any skill-type unit or stand
        alone. claude_md is a third role -- one document type, no mutual exclusion with anything
        because it identifies a CLAUDE.md not a SKILL.md.
      origin: Current session typed-unit composition design.
      added: "2026-05-19"
    - id: domain_member_resolution_check
      keywords: [member resolution, domain members, index.members, capability members, dangling ref, reorg guardrail, member ref resolve, corpus check]
      summary: checks.check_domain_members_resolve asserts every domain-skill (index.members[]) and capability-skill (members[]) member ref/name resolves to a real skill on disk; checks._cli runs it alongside the owner-doc check (python -m skills_kit_lib.checks [repo_root]).
      detail: |
        Companion corpus check to check_schema_owner_docs_validate. Globs
        plugins/*/skills/*/SKILL.md, builds the resolvable name pool from each
        skill's directory name + frontmatter name, then for every skill declaring
        members (domain_skill.index.members[] or capability_skill.members[])
        resolves each member's ref/name -- normalized by stripping a leading '/'
        and any 'plugin:' qualifier -- against the union of ALL on-disk skill names
        repo-wide, so same-plugin and cross-plugin refs both resolve. Catches the
        common reorg failure: a ref left pointing at a renamed/moved/never-created
        skill. Degrades silently without pyyaml (parse_skill_md can't populate
        body_contract), consistent with the contract-staged state (see
        three_audit_states). Codified in checks.py + tests/skills-kit/test_domain_members_resolve.py.
      origin: |
        skills-kit verb x artifact reorg, P3 make-testable step (2026-05-31). The
        reorg re-wires md-authoring / md-audit members; a script guard makes a
        mis-pointed member fail the audit instead of dangling silently.
      added: "2026-05-31"
    - id: asset_and_floor_document_checks
      keywords: [check_asset_dependencies_resolve, check_claude_md_record_floor, asset_dependencies, tools tests, union floor, document-level check, path resolution]
      summary: "Two document-level checks added 2026-07-13 (steam-analysis stress test): check_asset_dependencies_resolve resolves every declared asset_dependencies path (top-level or nested in any skill-type unit) AND every domain_skill tools[].tests path against the skill dir then the nearest project root (markers: .git/.hg/.svn/.p4config.txt), FAILing per unresolved path; check_claude_md_record_floor enforces >=1 record across the claude_md insights/conventions union (the schema no longer requires insights as a key)."
      detail: |
        Both follow the established convention: cross-key/cross-source rules live in
        audit.py after the walker, not in the schema engine. Asset resolution strips a
        ${CLAUDE_PLUGIN_ROOT}/ prefix and a leading slash before trying bases (declared
        paths are skill-dir-relative or project-root-relative by contract). Nested
        asset_dependencies are collected from inside skill-type units the same way
        check_facts_cross_rules collects nested facts (collect_yaml_units does NOT emit
        nested portable units as separate units). Both checks degrade silently without
        pyyaml, consistent with the contract-staged state.
      origin: steam-analysis stress-test feedback gaps 5/7/8 implementation (2026-07-13).
      added: "2026-07-13"
    - id: references_reachable_document_check
      keywords: [check_references_reachable_from_skill_md, orphaned reference, load graph, reachability, unlinked member directory, dangling index entry, two-hop reference, index coverage]
      summary: "Document-level check added 2026-07-15 (home-domain missing-load-graph-edge incident): check_references_reachable_from_skill_md verifies every file under references/ has an edge from SKILL.md (basename or [[stem]] mention; FAIL for a fully-orphaned .md, JUDGMENT for two-hop-only .md and orphaned non-md), every content-bearing member directory is named in SKILL.md (JUDGMENT when not), and every structured index.references[].path / members[].ref path resolves (FAIL when dangling)."
      detail: |
        Un-parks audit-framework.yaml::future_rules.references_reachable_from_skill_md,
        re-homed from references_audit to skill_md_audit (its subject is one skill
        composition -- the per-skill validator's traversal, not the corpus scanner's).
        Severity tiers are deliberate: a references/*.md nobody cites is FAIL (the
        reference_doc surface's designed inbound edge is SKILL.md; an agent with the
        skill loaded cannot discover the file); everything else is JUDGMENT because a
        legitimate counter-case exists (a lib/ dir only imported by scripts, a data
        file consumed by a script and declared via asset_dependencies, a doc reached
        by an intentional sibling citation). Member refs that are sibling skill names
        (ref: skill-audit) or slash commands (ref: /md-audit) are skipped by the
        dangling-path resolution -- only slash-qualified relative refs are treated as
        paths (checks.check_domain_members_resolve owns skill-name resolution).
        references/<name>.md-shaped index paths are also skipped there: the universal
        "references cited in body all exist" row already FAILs them (no double report).
        Mention detection is generous on purpose (any basename citation counts, YAML
        blocks included) -- a false negative is cheaper than a false FAIL across the
        fleet. Taxonomy home: skill-audit category L (L_load_graph_gap); the
        keyword-adequacy half of that category is judgment-only, not mechanical.
      origin: home-domain skill audit 2026-07-15 (tmp/home-domain-skill-audit.md LG-1/LG-3/LG-4): a pytest suite and an orphaned reference existed in the skill but nothing pointed at them; the mechanical validator passed every row.
      added: "2026-07-15"
  conventions:
    - rule: When extending heuristics, modify markdown_heuristics.py first; audit.py and classify.py both import from there.
      keywords: [SSOT, markdown_heuristics, helper extraction, drift]
      why: Duplicating heuristics between audit.py and classify.py was an early-version mistake; the refactor pulled them into a shared module so a single update reaches both consumers.
    - rule: Custom schema rules go in audit.py as document-level checks evaluated after the walker. Do not embed custom logic inside the schema engine itself.
      keywords: [walker, custom rule, schema flag, validation order]
      why: Engine stays general; custom rules are per-document and benefit from explicit audit-time invocation (check_facts_cross_rules, check_cross_block_drift). Engine should remain reusable across schema types.
    - rule: Audit output rows describe what was checked, not what was good or bad in isolation. Verdict is one of pass/fail/judgment-required/n/a.
      keywords: [verdict vocabulary, four-state output, audit row]
      why: Three-state (pass/fail) loses the conditional-not-fired case (n/a) and the agent-must-judge case (judgment-required). Both are real and deserve their own slot.
    - rule: Every new schema declares an owner_doc pointing at the canonical prose spec; the prose spec must contain a valid instance block of the schema's root key.
      keywords: [owner_doc, schema registration, prose spec, drift protection]
      why: Bidirectional drift protection. Schema changes that break the owner doc's example are caught; owner doc edits that drift from the schema are caught. Adding a schema without an owner_doc bypasses this and silently allows drift.
```
