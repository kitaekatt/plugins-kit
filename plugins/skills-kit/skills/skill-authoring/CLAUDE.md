# skill-authoring/ insights

Per-directory insight repository for the skill-authoring domain-skill: the canonical contract surface (glossary + framework), the audit-driven evolution that shaped schema v1, and the operating norms a future agent picks up cold. The YAML block below is the load-bearing surface; this file is not narrative.

Insights here capture decision provenance -- the audits that reshaped the framework -- not the canonical content itself. The contract surface lives in `references/glossary.md`, `references/framework.md`, and `skills_kit_lib/schema_registry.py` (SSOT); this file records *why those contracts look the way they do* so a future agent can rewind the reasoning.

**Phase / finding identifier legend.** Provenance fields below cite project-side history markers. They are decision-log breadcrumbs, not active references:

- `Phase Y1`-`Y7` -- phases of the YAML contract refactor (Y1 = stdlib walker design; Y4 = local-code-review conversion; Y5 = schema v1 lock; Y7 = retired sparse-keyword RAG hook).
- `Phase 4.x` / `P5` / `P7` -- phases of the audit-prep work units (Phase 4.2 = corpus audit pass; Phase 4.6 P4-P7 = framework-extension cluster).
- `F-4-2-N` -- numbered findings from the Phase 4.2 audit (e.g. F-4-2-2 / F-4-2-3 = paired user-only technique-skill findings).
- `Dec-N` -- numbered framework decisions captured here as insights (each `id: dec_N_*` entry IS the decision record).
- `audit-prep work unit` -- the 2026-04-29 batch session that produced Dec-1 through Dec-3.
- `yaml-refactor-design-spec.md` / `project-plan.md` -- project-side planning artifacts; cited as historical sources, not as live load dependencies.

```yaml
claude_md:
  _schema_version: "1"
  scope:
    directory: plugins/skills-kit/skills/skill-authoring
    covers:
      - canonical contract surface (glossary, framework, schemas) and how it evolved
      - audit-driven framework evolution decisions (Dec-1 / Dec-2 / Dec-3)
      - form-choice in practice on the framework's own content
      - SSOT discipline between glossary, framework, schemas, and the scripts directory
    excludes:
      - validator / audit / classify / tag script internals (covered by skills_kit_lib/CLAUDE.md)
      - per-plugin-dependency posture (covered by plugins-kit/CLAUDE.md and plugins/skills-kit/CLAUDE.md)
      - per-skill SKILL.md insights for other skills
  insights:
    - id: dec_1_form_choice_bias_structured
      keywords:
        - form choice
        - audience-claude
        - structured by default
        - prose exception
        - bias toward structured
        - yaml default
        - anti-pattern list assertion
        - llm-facing content
      summary: The default for LLM-facing skill content is structured YAML; prose is the documented exception. Earlier framing ("if unclear, prose is the default") was wrong direction for an Audience-Claude framework and was reversed.
      detail: |
        Original glossary phrasing said "if unclear, prose is the default." The audit-prep
        review of the eleven advocated principles surfaced that this was the wrong default
        for LLM-facing content: skills are runtime context for Claude, structure aids
        Claude's comprehension, prose is what Claude generates on demand for the user.
        The form-choice principle was rewritten: default to structured YAML; use prose only
        when (a) the content is naturally narrative -- an identity sentence, an orientation
        paragraph, a single-paragraph explanation that does not decompose into discrete
        records -- or (b) hierarchy carries no meaning. When in doubt, bias toward
        structured data. The bar for prose is "I can articulate why this would be worse as
        YAML"; if that articulation is hard, default to YAML.

        Why this matters operationally: structure carries assertions prose cannot. An
        anti_patterns: list with each entry as a record asserts implicitly that every item
        is genuinely an anti-pattern; a markdown bullet list carries no such assertion.
        Records are routable, keyword-able, and validatable; prose is none of those.

        Codified in: glossary.md (Audience-Claude entry, Form choice subsection);
        framework.md (Content-form choice section).
      origin: |
        Audit-prep work unit 2026-04-29. Surface: review of plugins-kit's advocated
        principles against its own canonical content (framework advocated
        bias-toward-structure but its glossary defaulted to prose). PR #12
        (mergeCommit 5d11b93) landed the contract change.
      added: "2026-04-29"
    - id: dec_2_ordered_steps_universal
      keywords:
        - technique-skill
        - ordered steps
        - steps required
        - user-only carveout removed
        - output_template optional
        - trigger model metadata
        - schema tightening
      summary: "Every technique-skill requires an ordered steps: list (min 1) regardless of trigger model. The previous user-only carveout was dropped after auditing real user-only skills."
      detail: |
        Phase 4.2 paired finding F-4-2-2 / F-4-2-3 surfaced that the framework exempted
        user-only technique-skills (disable-model-invocation: true) from the ordered-step
        body requirement, and classify.py could not detect ordered steps in the user-only
        branch. Verification against actual user-only technique-skills (cache-report,
        p4-code-review, test-greeting) showed every technique reduces to ordered steps,
        even cache-report's "render script stdout verbatim" which maps to a 1-2 step
        procedure. Steps are a sufficient body; output_template: is a separate concept
        (output-shape contract) that stays as an optional companion field, not a
        substitute.

        Implementation: schemas.py TECHNIQUE_SKILL_SCHEMA now requires steps: with min_len
        1 on every technique. trigger_model is metadata. output_template: is optional
        with a note clarifying it's a companion to steps. The trigger-model conditional
        was dropped from framework.md technique-skill row; audit.py and classify.py
        heuristic paths updated; cache-report and test-greeting gained 1-step steps:
        blocks during the audit-prep work unit.
      origin: |
        Audit-prep work unit 2026-04-29. Surface: F-4-2-2 / F-4-2-3 paired finding from
        Phase 4.2 audit of plugins-kit skills. PR #12 landed the schema tightening.
      added: "2026-04-29"
    - id: dec_3_schemas_are_floors
      keywords:
        - schemas are floors
        - extras allowed
        - schema strictness
        - load-bearing extras
        - extension keys
        - structure-friendly
        - mixed-type forbidden_keys
      summary: Per-type YAML schemas validate the required minimum; authors may add load-bearing structured keys beyond the schema. Mixed-type drift is detected via explicit forbidden_keys, not via blanket strictness.
      detail: |
        The yaml-refactor design open question Q3 ("extra-key strictness") was resolved:
        always allow extras. Disallowing extras would push authors toward unstructured
        prose when they want to add legitimate load-bearing structure (an exceptions:
        list inside an anti-pattern entry; narration: and subagents: blocks inside
        p4-code-review's technique). That contradicts the bias-toward-structured-data
        default codified by Dec-1.

        The schema validates the floor: required keys, required item shapes, required
        list lengths. Authors layer additional structured content on top freely.
        Cross-type drift is still caught -- each schema names a forbidden_keys list
        (e.g. rules: inside reference_skill: is forbidden because rules belong to
        discipline-skills). Unknown keys not in the forbidden list pass silently.

        Codified in: framework.md "Schemas are floors, not ceilings" section;
        schemas.py module docstring; skills_kit_lib/CLAUDE.md insight extra_keys_allowed
        which is the validator-side detail of the same decision. yaml-refactor-design-spec
        Q3 marked resolved.
      origin: |
        Audit-prep work unit 2026-04-29. Surface: yaml-refactor-design-spec Open
        Questions section 3. PR #12 landed the documentation.
      added: "2026-04-29"
    - id: dec_4_anti_patterns_promoted_field
      keywords:
        - anti-patterns
        - structured field
        - structure asserts
        - lookalike-but-wrong
        - technique-skill optional
        - discipline-skill optional
        - schema extension
      summary: "anti_patterns: was promoted from a load-bearing extra (schemas-are-floors territory) to a first-class optional field on technique_skill and discipline_skill. The structured record shape implicitly asserts every entry is a genuine anti-pattern; a markdown bullet list carries no such assertion."
      detail: |
        Schemas-as-floors (Dec-3) said extras are always allowed; it did not say extras
        cannot be promoted to documented optional fields. anti_patterns: is the first
        such promotion. Authors can still add an inline anti-patterns list under any
        skill type as an extra (per Dec-3), but technique_skill and discipline_skill
        now carry a recommended structured shape with the keys id / name / keywords /
        why_it_seems_right / why_it_is_wrong / alternative.

        The promotion exists because anti-patterns are the canonical example of "structure
        asserts." Containment in a list of records with these specific keys implicitly
        asserts every item is a genuine anti-pattern; a markdown bullet list under an
        H3 carries no such assertion. The five-key record shape forces authors to name
        why the lookalike seems right (the rationalization), why it is actually wrong
        (the failure mode), and what to do instead (the alternative pointer). That
        decomposition is what makes the entry useful at audit time -- a list of "don't
        do X" bullets without rationalization counters cannot survive a pressure test.

        Codified in: schemas.py ANTI_PATTERNS_RULE (constant, reused by both schemas);
        framework.md schemas_are_floors.promoted_extensions block listing the new field
        and its rationale.
      origin: |
        Phase 4.6 P4 work (2026-04-30). Surface: the session dialog that named four new
        value propositions on top of the embodiment-closure gap fixes; anti_patterns:
        as a first-class field was one of the four.
      added: "2026-04-30"
    - id: dec_5_hook_killed_keyword_surface_retained
      keywords:
        - y7 hook killed
        - sparse keyword rag
        - retrieval optimization deferred
        - chat-term relevance hints
        - keyword surface as documentation
        - intelligent yaml navigation
        - post-rollout optimization
      origin: |
        Phase 4.6 P7 framing review 2026-04-30. Surface: explanation of the proposed
        hook mechanic to the user against worked examples from real records, followed
        by the user's "is this a simplified RAG?" framing question.
      added: "2026-04-30"
      summary: "Y7 hook killed before implementation. The YAML keyword clusters remain on every record as documentation and as a navigation aid for direct reads; the hook that consumes them does not ship. Post-rollout optimization will revisit teaching Claude to search the YAML structure intelligently rather than inject hand-tagged keyword matches."
      detail: |
        The proposed Y7 hook was a sparse-keyword agentic RAG (walk registered YAML
        files, score prompt/record keyword overlap, inject top-N as additionalContext
        on UserPromptSubmit), with three documented failure modes and a 50/50 net-harm
        risk.

        User reasoning for killing it (verbatim summary):
        1. RAG-as-such is not necessary for the current workflow; injecting a
           simplified RAG to optimize a non-bottleneck creates new failure modes
           without solving a real problem.
        2. Keyword-score relevance matching tends to hit the same over-tagged
           records repeatedly -- precision degrades exactly when retrieval would
           need to be smartest.
        3. The higher-value optimization is teaching Claude to navigate the YAML
           structure intelligently (selectively reading by sub-grouping, by
           keyword cluster, by record id), which is best revisited as a
           post-rollout optimization once real usage surfaces what kinds of
           navigation actually matter.

        What stays: the keyword: clusters on every record (glossary, framework,
        CLAUDE.md insights), the chat-term relevance hints principle in the
        glossary, the schema requirement that every load-bearing record carries
        keywords (>=3). These remain valuable as (a) human-readable navigation
        signals when a reader scans a record, (b) inputs to direct-reading agents
        who use the cluster as a navigation aid, (c) the index a future
        intelligent-search runtime would consume.

        What goes: Y7.1-Y7.5 hook implementation steps; the UserPromptSubmit hook
        wiring; the 50-prompt validation corpus (drafted but never run). The test
        plan is parked as a reference artifact, not a live workstream.

        Codified in: yaml-refactor-design-spec.md Y7 section marked superseded;
        project-plan.md P7 row marked killed and Phase 4.6 closed; new "Phase 6
        -- Post-rollout optimizations" section in project-plan.md queues the
        intelligent-YAML-navigation work.
    - id: dec_6_capability_skill_type_and_layering
      keywords:
        - capability-skill
        - wrapper skill
        - external capability provider
        - tool / mcp / api / service / ide / framework
        - L1 L2 L3 layering
        - CLAUDE.md SKILL.md references content allocation
        - frequency of need test
      summary: "Capability-skill added as the 6th skill type. A capability-skill wraps an external capability provider (tool / MCP server / API / service / IDE / framework) with the project's setup and conventions. Conceptually inherits technique-skill (capabilities are techniques+); schema requires capabilities: at root, external_capability declaration, layering manifest (L1/L2/L3 content allocation), and capability-skill-level gotchas. Member skills + Conditional Loading fire conditionally when capabilities grow."
      detail: |
        Surface: a corpus reference-shape audit (Bucket C) flagged three
        tool-wrapper skills for re-bucket -- mixed-type drift,
        "reference-skill that's actually mixed." The deeper finding:
        tool-shape and wrapper-shape skills don't fit reference-skill or
        technique-skill or domain-skill cleanly. Their content shape is
        "structured capabilities wrapping an external thing" -- which is
        its own coherent shape.

        Three options were considered:
        - Use subset (force reference-skill): bad, loses domain structure
          for rich tool skills.
        - Use superset (force domain-skill): bad, fabricates members for
          flat tool skills.
        - New type via inheritance (capability-skill IS-A technique-skill
          + capability-specific structure): best fit; conditional rows
          support the growth curve from flat to rich.

        Naming: "tool-skill" was too narrow (MCP servers, APIs, services
        aren't tools). "Wrapper-skill" too generic. "Capability-skill"
        composes with the existing Capability pattern (a capability-skill
        is a skill of capabilities). The naming layers cleanly: capability
        (unit) -> capability-skill (type that aggregates them).

        L1/L2/L3 content layering principle ships in the same change as a
        new framework section. The principle is general (applies to any
        skill with multi-load-level potential) but most load-bearing for
        capability-skills (which have significant L1 territory because
        the wrapped thing is used widely). The schema's required
        layering manifest enforces it for capability-skills; for other
        types it is authoring guidance.
      origin: |
        Corpus mixed-type-drift audit on tool-shape skills + a domain-skill
        build that demonstrated the layering principle in practice (worked
        example for the principle). User framing question pushed past
        "tool" to the broader "wrapper around external capability provider"
        framing, which generalizes cleanly.
      added: "2026-04-30"
    - id: dec_7_visibility_rubric_for_examples_and_anti_patterns
      keywords:
        - visibility rubric
        - L1 L2 L3 visibility
        - common trigger-relevant esoteric
        - example-grain content allocation
        - anti-pattern grain
        - frequency vs trigger-relevance
        - tool-wrapper layering audit
        - example/anti-pattern visibility criterion
      summary: "L1/L2/L3 visibility criterion at the example/anti-pattern grain: L1 if COMMON, L2 if DIRECTLY RELATED to why the agent invokes the skill (trigger-relevance dominates frequency when both fire), L3 if ESOTERIC. Codifies the decision rule the framework had been silent on at the example/anti-pattern grain."
      detail: |
        The framework's L1/L2/L3 content-allocation section names what each
        level holds (CLAUDE.md ambient, SKILL.md triggered, references/ on-
        demand) but was silent on which examples or anti-patterns belong
        where -- the visibility decision recurs at the example/anti-pattern
        grain (a single gotcha, a single anti-pattern record, a single
        escaping example), not just at the major-content grain.

        The canonical criterion (apply verbatim):
        - L1 (CLAUDE.md): if they are COMMON. Frequency dominates -- the
          example/anti-pattern fires in most sessions touching the area;
          ambient cost is justified.
        - L2 (SKILL.md): if they are DIRECTLY RELATED to why the agent
          invokes the skill. Stays in SKILL.md regardless of frequency
          when this fires. Trigger-relevance dominates frequency when both
          fire (e.g. a PowerShell -Command escaping gotcha that's
          literally why the skill exists belongs in SKILL.md, not
          CLAUDE.md, even if also common).
        - L3 (references/): if they are ESOTERIC. One-in-a-hundred edge
          cases, third-party-tool-specific quirks, environment-specific
          footguns most invocations never hit. Specificity dominates --
          ambient cost is not justified, but the content must be
          reachable when the rare situation fires.

        Codified in: framework.md "Visibility criterion for examples and
        anti-patterns" sub-section, under the existing L1/L2/L3 content
        allocation block.
      origin: |
        Surface: a tool-wrapper skill layering audit (April 2026) found 5
        of 7 SKILL.md gotchas should have been in CLAUDE.md (frequency
        criterion fired). The user clarified the rubric mid-audit.
      added: "2026-04-30"
    - id: dec_8_step_tracker_or_checklist_for_explicit_step_tracking
      keywords:
        - step tracking
        - workflow checklist OR
        - TaskCreate invocation
        - tickbox checklist alternative
        - workflow-checklist refinement
        - explicit step-tracking discipline
        - markdown syntax incidental
        - step tracker scratch file
      summary: "Workflow-checklist conditional row restated as OR-form: paste-able `- [ ]` checklist OR explicit step-tracker invocation (TaskCreate, scratch file, etc.) at procedure start. The underlying goal is the discipline of explicit step-tracking, not the specific markdown syntax."
      detail: |
        The original workflow-checklist requirement treated `- [ ]` lists as
        required when a technique-skill has >3 steps. User question
        (2026-04-30) sharpened the rule: the goal is explicit step-
        tracking, not the specific markdown syntax. If the skill already
        invokes TaskCreate (or another step tracker) at the start of the
        procedure, parallel `- [ ]` markdown adds no information.

        The refined rule (apply verbatim):
        Ship a paste-able `- [ ]` checklist OR explicitly invoke a step
        tracker (TaskCreate, scratch file, etc.) at the start of the
        procedure. Either satisfies the underlying goal of preventing
        premature completion claims; the markdown syntax is one path, not
        the only one.

        Implementation:
        - framework.md technique-skill table row + conditional_requirements
          example restated as the OR-form, with a note that the goal is
          the discipline of explicit step-tracking.
        - _shared.py adds has_step_tracker_invocation() detector
          (recognizes TaskCreate / TaskWrite / TodoWrite invocations and
          explicit prose markers like "track steps in", "step tracker",
          "scratch file for steps").
        - audit.py technique-skill row "explicit step-tracking
          (conditional, IF >3 steps): checklist OR tracker invocation"
          passes when EITHER signal is present.
        - schemas.py keeps `checklist:` as an optional field with a note
          that the OR-form is enforced at audit.py level on the rendered
          SKILL.md body, not in the YAML schema (the schema cannot detect
          a step-tracker invocation in step text).
        - tests/skills-kit/ extended to demonstrate a technique-skill with
          a TaskCreate invocation in step body but no `- [ ]` markdown
          still passes the conditional row.
      origin: |
        Surface: user question on workflow-checklist purpose during a
        tool-wrapper bundle session (2026-04-30).
      added: "2026-04-30"
    - id: dec_9_yaml_steps_canonical_for_yaml_contract_skills
      keywords:
        - yaml steps canonical
        - markdown checklist legacy
        - two-source-of-truth drift
        - shape hierarchy
        - workflow checklist YAML preference
        - contract skill canonical surface
        - mixed-shape promote to yaml
        - dec-8 follow-up
      summary: "When a skill is on the YAML contract, the `steps:` list IS the canonical step-tracking surface. Markdown `- [ ]` checklists are the right form for legacy / non-YAML-contract skills only; pairing them with YAML `steps:` creates two-source-of-truth drift. The hierarchy: YAML `steps:` for contract skills; markdown `- [ ]` for legacy; mixed shapes promote to YAML and drop the markdown."
      detail: |
        Dec-8 made the workflow-checklist row OR-form (paste-able `- [ ]`
        checklist OR step-tracker invocation). It did not say which form
        is preferred when a skill has a choice. The unspoken default after
        Dec-8 left it ambiguous whether YAML-contract skills should also
        carry a parallel markdown checklist.

        The clarification: a YAML `steps:` block already satisfies the
        explicit-step-tracking discipline. It is structured, schema-
        validated, keyword-able, and authoritative. Adding a parallel
        markdown `- [ ]` checklist alongside the YAML duplicates the
        information in two places; the two surfaces drift independently
        as the skill evolves, and audit tooling has to reason about which
        is the source of truth. The right hierarchy:

        - YAML-contract skills: `steps:` only. The schema is the
          authority; do not pair with markdown `- [ ]`.
        - Legacy / non-YAML-contract skills: markdown `- [ ]` is the
          right form. The OR-form Dec-8 produced is what carries the
          explicit-step-tracking discipline for skills not yet on the
          contract.
        - Mixed shapes: promote to YAML and drop the markdown rather
          than maintaining both. Avoid blanket-converting a corpus's
          existing markdown checklists to YAML pre-emptively -- the
          conversion happens as the skill is brought onto the YAML
          contract.

        Codified in: framework.md conditional_requirements row note
        (canonical_form_when_on_yaml_contract sub-clause), framework.md
        technique-skill table row parenthetical, this insight.
      origin: |
        Surface: user question 2026-04-30 after a corpus-wide application
        of the workflow-checklist requirement that added 26 markdown
        `## Workflow Checklist` sections to technique-skills. The question
        surfaced the unresolved hierarchy: which form is canonical when
        a skill is on the YAML contract?
      added: "2026-04-30"
    - id: dec_10_harness_targeted_capability_skill_eligibility
      keywords:
        - harness-targeted
        - capability-skill kind
        - kind harness
        - claude code harness wrapper
        - hooks settings status line MCP config wrapper
        - harness-targeted attribute orthogonal to type
        - borderline resolution
      summary: "Harness-targeted skills (those whose primary content wraps the Claude Code harness's exposed surfaces -- hooks, settings, MCP config, status line, slash-command runtime) are eligible to be capability-skills when their content shape is capabilities-wrapping-an-external-thing. The capability_skill schema gains `harness` as an explicit kind value. Harness-targeted skills with rules / lookup tables / techniques content shape stay in their respective non-capability types and use the `harness-targeted: true` frontmatter flag as orthogonal cross-cutting categorization."
      detail: |
        Dec-6 introduced capability-skill as the wrapper-shape type and named
        kinds tool / mcp_server / api / service / ide / framework. The Claude
        Code harness was not explicitly named, leaving the question open
        whether a skill that wraps `the project's hook conventions` or
        `the project's settings.json patterns` (e.g. a hooks-skill) is a
        capability-skill or a different type.

        The resolution: yes -- harness-targeted skills CAN be capability-
        skills when their content shape is capabilities-wrapping-an-
        external-thing. The harness IS the external thing they wrap; its
        exposed surfaces (hook trigger model, settings.json key surface,
        MCP config schema, status line API, slash-command runtime contract)
        are no different in structure from the surfaces a tool / MCP / API
        / service / IDE / framework exposes. The capability_skill schema
        gains `harness` as an explicit kind value; framework.md kind list
        and Examples row updated accordingly.

        Two crucial distinctions remain:
        1. The `harness-targeted: true` frontmatter flag (shipped as a
           cross-cutting categorization marker) is orthogonal to skill
           type. A skill can be `skill-type: reference-skill` AND
           `harness-targeted: true` (lookup table about hook syntax);
           or `skill-type: capability-skill` AND
           `harness-targeted: true` (wraps the hook system as
           kind: harness). The flag is taxonomic; the type is structural.
        2. Not every harness-targeted skill is a capability-skill.
           Harness-targeted skills whose content is rules / lookup tables
           / techniques (not capabilities-wrapping-an-external-thing)
           stay in their respective non-capability types -- e.g. a
           reference-skill that documents harness permission rules stays
           reference-skill, even though it's harness-targeted.

        Codified in: schemas.py CAPABILITY_SKILL_SCHEMA module docstring
        + external_capability.kind note (adds "harness");
        framework.md capability-skill required-blocks row (adds harness
        to the kinds list); framework.md capability-skill Examples row
        (adds harness wrapper as a worked example with the discrimination
        criterion).
      origin: |
        Surface: a corpus audit BORDERLINE finding. After a change shipped
        the `harness-targeted: true` frontmatter flag for two
        permissions-audit skills, the question remained whether
        harness-targeted skills with capability-wrapping content shape
        qualify as capability-skills.
      added: "2026-04-30"
    - id: dec_11_size_threshold_is_signal_crp_is_the_test
      keywords:
        - size threshold signal not verdict
        - crp is the test
        - 500 lines 3000 tokens prompt
        - splits require decomposition
        - tool-call doubling anti-pattern
        - stub plus always co-loaded reference
        - common reuse principle gates split
        - decomposition test
      summary: "The 500-line / 3000-token threshold is a SIGNAL that a SKILL.md deserves evaluation for splitting; it is NOT a verdict that splitting is correct. CRP is the test that decides whether the split is legitimate. A split that creates a stub-plus-always-co-loaded-reference is a tool-call doubling, not a context-efficiency win, and should be reverted."
      detail: |
        The three-principle tension (context efficiency vs tool-call efficiency vs
        size threshold) and the worked CRP-pass / CRP-fail split shapes are owned by
        framework.md -- see "CRP is the test for L2 -> L3 splits". The decision-specific
        delta: the >500-line / >3000-token threshold is a SIGNAL that triggers
        evaluation, not a verdict that mandates a split; CRP gates the split.

        Operationally:
        - Threshold breach triggers evaluation, not auto-split.
        - Enumerate the proposed sections and check whether each fires on
          the same trigger or independent sub-triggers.
        - Split only when at least one section can be omitted on a typical
          invocation, and the remaining SKILL.md is a viable standalone in
          that case.
        - When no decomposition passes CRP, keep the larger SKILL.md. An
          over-threshold SKILL.md that costs one tool call is preferable
          to a stub-plus-always-co-loaded reference that costs two -- revert
          a CRP-fail split by inlining the reference back into SKILL.md.

        Codified in: framework.md conditional_requirements row (rule restated
        as CONSIDERED-when-over-threshold + REQUIRED-only-if-CRP-passes, with
        the anti-pattern named); framework.md "CRP is the test for L2 -> L3
        splits" sub-section under the L1/L2/L3 content allocation block;
        per-type table rows updated to "progressive disclosure CONSIDERED if
        ..., REQUIRED only if a CRP-passing decomposition exists".
      origin: |
        Surface: April 2026 progressive-disclosure split execution across
        a corpus of skills. Background agents triggered splits on every
        SKILL.md exceeding the size threshold. User pushback: "right so
        you feel all these splits won't just turn into double tool calls".
        Inspection of the split shapes confirmed several were CRP-fails --
        e.g. a discipline-skill went from 423 lines to a 37-line stub
        pointing at one always-co-loaded reference. Tool-call doubling
        without context-efficiency win.
      added: "2026-05-01"
    - id: dec_12_subdomain_config_schema_extension
      keywords:
        - subdomain config
        - capability-skill schema extension
        - state terms
        - operations
        - scope axes
        - canonical phrasing
        - dependency order
        - sub-area vocabulary contract
      summary: "CAPABILITY_SKILL_SCHEMA gains an optional list field `subdomain_config` carrying per-sub-area structural records (name + optional state_terms / operations / scope_axes / canonical_phrasing / llm_dependent_content / dependency_order). The schema gives audit tooling a mechanical floor for verifying a sub-area's vocabulary contract."
      detail: |
        Capability-skills that decompose into sub-areas frequently carry
        repeated structural metadata per sub-area: a canonical state
        vocabulary the agent must use verbatim, a verb list the sub-area
        supports, scope axes that decompose the capability space, a
        readback rule for restating user requests, fields populated by an
        LLM rather than retrieved deterministically, and capability
        ordering constraints. Without a schema, this metadata floats as
        ad-hoc YAML in reference docs, unauditable for consistency.
        
        The extension is conservative:
        - The field is optional. Single-sub-area capability-skills omit
          it; multi-sub-area capability-skills declare one record per
          sub-area.
        - Within a record, only `name` is required. The other six fields
          are independent options; each sub-area declares only the fields
          it actually uses.
        - The schema validates field shapes (state_terms is a list,
          canonical_phrasing is a string) without enforcing semantic
          content (the schema does not check that state_terms are
          uppercase or that operations are imperative verbs).
        
        Audit hooks documented in subdomain-schema.md cover the next-level
        invariants (no duplicate state terms, dependency_order references
        existing capabilities). Those checks are downstream of the schema
        and live in subdomain-schema.md as auditable conditions rather
        than schema rules.
        
        Codified in: schemas.py CAPABILITY_SKILL_SCHEMA `subdomain_config`
        block; framework.md capability-skill required-blocks row
        (subdomain_config: noted as optional with cross-reference);
        glossary.md new `subdomain_config` record under Patterns >
        Procedural composition; references/subdomain-schema.md (new) with
        per-field definitions, two worked examples, and audit hooks.
      origin: |
        Surface: dialog-domain extraction audit (2026-05-01) flagged the
        sub-area config schema as a generalizable pattern.
      added: "2026-05-01"
    - id: dec_13_domain_consolidation_criterion
      keywords:
        - domain consolidation
        - merge skills into domain
        - when to make a domain
        - doer types merge
        - reference pattern discipline fold in
        - co-location is not a domain
        - shared pattern not shared subject
        - one doer plus references is a skill
        - never nest domains
        - retroactive merge direction
      summary: "A domain-skill is justified only when 2+ skills share a SUBJECT and are doer types (technique / capability / audit, which merge as members). Reference / pattern / discipline skills fold in as references/guardrails, not members; domains never nest. Co-location in a plugin, topical adjacency, and a shared pattern are NOT a shared subject. One doer + N references is a skill, not a domain."
      detail: |
        The framework already covered two boundary-discipline directions:
        building a NEW domain bottom-up (compositional_order) and SPLITTING
        one skill that outgrew its type (auditing_procedure.mixed_type_check,
        plus CRP-is-the-test for L2->L3). It was silent on the third,
        retroactive direction: looking across the corpus at N existing
        standalone skills and deciding whether they should MERGE into one
        domain.

        The merge criterion (apply verbatim):
        - Justified only when BOTH hold: (1) 2+ skills share a subject --
          not co-location, not topical adjacency, not a shared pattern;
          (2) the skills are doer types.
        - Mergeable-vs-foldable by type: technique / capability / audit
          merge as MEMBERS (they are operations over the subject; multiple
          operations on one subject IS the domain). reference / pattern /
          discipline FOLD IN as supporting content (reference -> L3 doc;
          pattern -> stays standalone, it applies across many subjects;
          discipline -> the domain's guardrails) -- none needs its own
          member sub-trigger. domain NEVER nests (fails top-level CRP).
        - Consequences: one doer + N references is a skill with references,
          not a domain; skills sharing a pattern but different subjects are
          not a domain (they reference one pattern-skill); a plugin is a
          packaging unit, a domain is a subject unit -- they can diverge or
          a subject can span two plugins.
        - CRP gate (same as L2->L3 splits): each member must fire on a
          distinct sub-trigger so a typical invocation loads container + one
          member. If every candidate co-loads, it is a CRP-fail merge --
          keep them separate, exactly as a CRP-fail split gets reverted.

        Audit hook codified for /skill-audit hierarchy: cluster skills by
        subject, flag any subject owning 2+ doer-type skills as a
        consolidation candidate; flag any domain whose members all co-load
        as a CRP-fail to revert.

        Codified in: framework.md new section "When to consolidate skills
        into a domain (the merge direction)" under the L1/L2/L3 content
        allocation block, parallel to "CRP is the test for L2 -> L3 splits".
      origin: |
        Surface: a repository domain-structure design session (2026-05-30)
        that pulled back from "should these specific skills merge" to "what
        should the whole repo's domain structure be." The corpus survey
        showed one real domain (skill-authoring), one obvious missing one
        (a cohesion-audit domain over claude-md-audit + skill-audit +
        references-audit), and a content/authoring cluster the user had
        eyed for merging that actually FAILS the bar (one doer +
        references). The user supplied the two load-bearing constraints --
        "a domain makes sense only when you can group multiple skills" and
        "only certain skill types make sense to merge into domains."
        Scope held to documentation (the /skill-audit detector and the
        concrete repo redesign deferred).
      added: "2026-05-30"
    - id: dec_14_shared_reference_across_sibling_domains
      keywords:
        - shared reference standalone
        - cross-verb base
        - sibling domains cite one reference
        - fold-in assumes single domain
        - cohesion-principles standalone
        - reference acts like pattern
        - domain boundary orphan
      summary: "A reference cited by 2+ sibling domains stays STANDALONE (every domain cites it), not folded into one. The merge table's 'reference -> fold in as L3 doc' disposition assumes a single consuming domain; folding a shared reference into domain A severs domain B's edge to it. Such a reference behaves like a pattern-skill ('applies across many subjects') even though typed reference-skill."
      detail: |
        The dec_13 merge table classifies a reference-skill as "fold in, don't
        merge -> becomes an L3 doc" of the domain it supports. That disposition
        is correct only for a reference with ONE consuming domain. The gap: a
        reference that is the shared substrate of two sibling domains cannot fold
        into either without orphaning the other.

        The rule (apply verbatim): a reference cited by 2+ sibling domains stays
        standalone, exactly as a pattern-skill does, and every consuming domain
        cites it. Test: if folding the reference into one domain would force a
        sibling domain to reach across a domain boundary to read it, keep it
        standalone. This is the cross-verb base case -- structurally a pattern in
        the merge table's sense.

        Live example: cohesion-principles (placement CCP/CRP/ADP) is cited by both
        md-authoring (placement while authoring) and md-audit (placement criteria
        while auditing). Folding it into either orphans the other; it stays
        standalone.

        Codified in: framework.md new section "Shared references across sibling
        domains stay standalone" under the merge-direction block.
      origin: |
        Surface: the skills-kit verb x artifact reorg design session (2026-05-31);
        P1 re-read of the framework merge table (lines ~269-291) found it did not
        cover a reference shared by two sibling domains.
      added: "2026-05-31"
    - id: dec_15_specialization_by_artifact_merge_axis
      keywords:
        - specialization by artifact
        - second merge axis
        - md skill claude-md is-a
        - artifact dimension
        - general case narrower case
        - composes with merge-by-subject
        - md-authoring md-audit members
      summary: "A domain may group members along a SECOND axis besides shared-subject: artifact specialization. A general artifact (md) has specializations that are still that artifact + a contract (skill is-a md + SKILL.md contract; claude_md is-a md + CLAUDE.md contract). A domain can route by artifact specialization (md-authoring -> skill-authoring + claude-md-authoring) instead of by operation. Composes with merge-by-subject; the CRP gate (distinct sub-trigger per member) is identical."
      detail: |
        The dec_13 merge direction groups DOERS that share a subject (N operations
        on one subject = a domain). It is silent on a second, orthogonal axis: one
        subject specialized along an artifact dimension. Merge-by-subject asks "do
        these operate on the same thing?"; specialization-by-artifact asks "is this
        the general case and that a narrower case of the same artifact?"

        The shape: a general artifact (md = any LLM-facing markdown document) has
        specializations that are still that artifact plus a contract -- skill is-a
        md + the SKILL.md contract; claude_md is-a md + the CLAUDE.md contract. A
        domain organized along this axis routes by artifact specialization rather
        than by operation: md-authoring indexes skill-authoring (the skill
        specialization) and claude-md-authoring (the claude-md specialization); the
        shared general-md content lives as the domain's own reference, inherited by
        every member.

        This COMPOSES with merge-by-subject, it does not compete. A domain may group
        members along either axis -- by operation (technique/audit doers over one
        subject) or by artifact specialization (the same verb applied to narrower
        artifacts). The CRP gate is identical: each member fires on a distinct
        sub-trigger (here "which artifact") so a typical invocation loads the domain
        plus one member.

        Codified in: framework.md new section "Specialization by artifact (a second
        merge axis)" under the merge-direction block.
      origin: |
        Surface: the skills-kit verb x artifact reorg design session (2026-05-31) --
        the target tree was a specialization hierarchy the merge table did not name.
      added: "2026-05-31"
    - id: dec_16_broader_union_domain_vs_nest
      keywords:
        - union domain
        - broader domain
        - domains never nest refined
        - sub-domain member
        - selective dispatch vs co-load
        - member type domain-skill
        - domain-layering member skill
        - roof over domains
      summary: "'A domain never nests' is a CRP rule, not a topology ban. A domain MEMBER may itself be a domain when the parent is a BROADER UNION DOMAIN -- a thin greeting + argument-dispatch router that loads the parent plus exactly one sub-domain. The discriminator is selective-dispatch (union, allowed) vs force-co-load (nest, prohibited), NOT whether the member is a domain. Sub-domains may be backed by reference sub-areas (sub_domains: + reference:) OR by member skills (index.members[], type may be domain-skill)."
      detail: |
        Surface: the verb x artifact reorg hit an apparent block -- skill-authoring
        is a domain-skill, and the target tree makes it a member of a new
        md-authoring domain, which dec_13's "domain | Never nest" row appeared to
        forbid. The user corrected the framing: "domains do not nest, but we can
        have a broader domain so it's a union not a nest," and pointed at
        spiritcrossing's dialog-domain as a live sub-domain example.

        The refinement (apply verbatim):
        - The "never nest" rule is the top-level CRP test, not a ban on a domain
          appearing under a domain. A BROADER UNION DOMAIN is a thin router that
          greets + argument-dispatches into ONE sub-domain at a time (the
          domain-layering pattern). Invoking it loads the router plus the single
          selected sub-domain, so CRP holds -- exactly the gate every member merge
          already passes (each member fires on a distinct sub-trigger).
        - UNION (allowed) = selective dispatch. NEST (prohibited) = the parent
          force-co-loads its member domains' full content on every invocation
          (wanted one sub-area, got all of them, two domain-indexes deep).
        - The test is selective-dispatch vs co-load, NOT "is the member a domain."
          A union domain's sub-domain member MAY itself be a full domain-skill;
          member `type: domain-skill` is legitimate in that case.
        - Two backings for a sub-domain (domain-layering.md): a reference sub-area
          (sub_domains: index with reference: path, e.g. dialog-domain's first_pass)
          OR a member skill (index.members[], for a substantial standalone skill or
          a pre-existing domain). Same routing behavior; different backing.

        This is what licenses md-authoring (broader union domain) over skill-authoring
        (kept whole as a domain-skill) + claude-md-authoring via index.members[], and
        the parallel md-audit. skill-authoring is NOT retyped; it stays a domain and
        becomes a sub-domain member.

        Codified in: framework.md merge-table `domain` row (rewritten union-vs-nest)
        + new section "Broader union domains over sub-domains (union vs nest)";
        domain-layering.md new subsection "Two ways to back a sub-domain: reference
        sub-area vs member skill". Refines (does not delete) dec_13's "domain never
        nests" cell.
      origin: |
        User correction during the reorg (2026-05-31): "domains do not nest, but we
        can have a broader domain so it's a union not a nest" + "domains can have
        sub-domains as well see .../dialog-domain/SKILL.md" + "you may need to update
        the docs so they enable navigating this type of situation better in the future."
      added: "2026-05-31"
    - id: dec_17_subdomain_single_declaration
      keywords:
        - sub_domains retired
        - index.references is the sub-domain index
        - single declaration
        - domain layering registration
        - drift twin blocks
        - one declaration two authorities
      summary: "Sub-domains of a contract domain-skill are declared ONCE, in the domain_skill schema's index: block (index.references[] for reference-backed, index.members[] for member-skill-backed). The bespoke sub_domains: index in domain-layering.md is retired for contract skills -- it demanded the same records twice (name/description/keyword_cues/reference vs id/summary/keywords/path), and the two blocks drift. The portable sub_areas: unit (same shape) remains the declaration for generic md documents NOT on the domain_skill contract."
      detail: |
        The stress test authored a real domain-skill against both authorities:
        DOMAIN_SKILL_SCHEMA requires index.references (id/path/keywords/summary),
        domain-layering.md required a sub_domains: index (name/reference/
        keyword_cues/description) -- near-synonymous field pairs over the same
        files, authored twice to satisfy both, guaranteed to drift. Resolution:
        the schema is the single authority (consistent with ssot_canonical_split
        -- schemas win); domain-layering.md's routing behaviors (greeting menu,
        argument dispatch, overview detection) now consume the index: block
        directly (id = identifier, summary = menu line, keywords = routing cues,
        path = deeper doc). A parallel sub_domains: block is itself an audit
        finding. The layering audit hooks and both worked examples were rewritten
        against index:. framework.md's "Two ways to back a sub-domain" and the
        domain-skill conditional row updated to match.
      origin: |
        steam-analysis stress-test feedback gap 4 (docs/skills-kit-feedback.md,
        worked 2026-07-13): game-analysis declared both blocks over the same two
        files to pass both authorities.
      added: "2026-07-13"
    - id: dec_18_asset_dependencies_and_script_contracts
      keywords:
        - asset_dependencies portable unit
        - runtime asset edge
        - mirrors invariant
        - script contract surface
        - tools tests path
        - skill-shipped tests convention
        - resolve check
      summary: "New portable unit asset_dependencies: ({path, consumer?, purpose, invariant?}) declares repo files a skill consumes at RUNTIME (bundled scripts, tool-args examples) -- an edge invisible to md-citation checks. audit.py resolves every declared path (skill dir, then project root); a move/rename FAILs the consumer's audit instead of breaking silently. The invariant field carries the mirrors-style sync contract a bundled script must uphold against a doc section. domain_skill tools[] gains an optional tests path (resolved the same way); convention: skill-shipped script tests live next to the script under scripts/, stdlib-only, and the project's test inventory names skill test roots alongside the main suite."
      detail: |
        Two stress-test gaps, one mechanism. Gap 5: a workflow script consumed a
        reference owned by a SIBLING skill (right design -- the asset stays with
        its CCP siblings) but a rename would break it with no audit signal. Gap 7:
        a skill whose operative core is a bundled workflow.js had its load-bearing
        sync invariant ("output records mirror the funnel's output-schema
        section") only as a code comment, and its companion test joined no test
        inventory. Direction REJECTED: extending the mechanical resolve-check to
        path literals embedded in scripts/examples -- heuristic path-scanning
        false-positives (placeholders, examples, generated strings) and cannot
        distinguish consumption from mention. Direction taken: an explicit,
        schema-validated declaration, consistent with the framework's
        structure-asserts stance. Nested-or-top-level placement follows the
        portable-unit convention; audit.py collects both and also resolves
        tools[].tests. ${CLAUDE_PLUGIN_ROOT}/ prefixes are stripped before
        resolution. Codified in: schemas/portable.py ASSET_DEPENDENCIES_SCHEMA;
        audit.py check_asset_dependencies_resolve; framework.md "Runtime asset
        dependencies and script contracts" + portable-units table + domain-skill
        conditional row; cohesion-principles asset_dependency edge +
        runtime_asset_dependencies_declared rule.
      origin: |
        steam-analysis stress-test feedback gaps 5 + 7 (docs/skills-kit-feedback.md,
        worked 2026-07-13): the candidate-screening-funnel runtime dependency and
        the workflow.js mirrors-comment with no auditable surface.
      added: "2026-07-13"
    - id: dec_19_claude_md_union_floor
      keywords:
        - claude_md schema
        - conventions-only valid
        - insights optional
        - union floor
        - non-empty block
        - document-level check
      summary: "CLAUDE_MD_SCHEMA no longer requires insights: specifically -- the floor is >=1 record across the insights/conventions UNION, enforced as a document-level check (audit.py check_claude_md_record_floor). A conventions-only CLAUDE.md (rules + why) validates; an empty block (neither insights nor conventions) still fails."
      detail: |
        The old floor ("insights required, min 1") aimed at the wrong invariant:
        it forced a conventions-only CLAUDE.md to manufacture a filler insight
        record to validate, when the real invariant is "the block is non-empty".
        The schema engine validates per-key shapes and cannot express a
        cross-key union floor, so per the existing convention (custom rules go
        in audit.py as document-level checks evaluated after the walker), the
        union floor lives in audit.py and runs on every claude_md audit. The
        insights: key keeps min_len 1 WHEN PRESENT (an empty insights list is
        still a shape error); it is simply no longer required as a key.
        knowledge-encoding (the owner doc) documents the union floor beside its
        instance example.
      origin: |
        steam-analysis stress-test feedback gap 8 (docs/skills-kit-feedback.md,
        worked 2026-07-13): a conventions-only root CLAUDE.md would have had to
        invent an insight to validate.
      added: "2026-07-13"
    - id: ssot_canonical_split
      keywords:
        - SSOT
        - glossary canonical
        - framework canonical
        - schemas authoritative
        - markdown table review
        - divergence rule
      summary: Vocabulary lives in glossary.md, contracts in framework.md, machine-readable schemas in skills_kit_lib/schema_registry.py. Schemas win on divergence with framework.md tables; framework.md tables stay for human review.
      detail: |
        The three artifacts have different jobs and one canonical owner per fact:
        - glossary.md owns vocabulary (terms, principles, patterns, type names,
          attributes). Other documents reference terms by name without redefining.
        - framework.md owns contracts (per-type required/conditional/prohibited rows,
          description requirements, content-form choice, schemas-as-floors section).
          The markdown contract tables are kept for human review clarity.
        - skills_kit_lib/schema_registry.py owns the machine-readable contract. When schemas.py and
          framework.md tables diverge, schemas.py wins. Framework tables get updated
          to match; the schema does not get loosened to match an out-of-date table.

        This split is what makes audits deterministic: audit.py validates against
        schemas.py, not against markdown heuristics. The markdown surface is for the
        human reviewer; the YAML schema is for the agent.
      origin: |
        Phase Y5 schema v1 lock 2026-04-28. Codified in framework.md "Canonical
        contract surface (schema v1, locked 2026-04-28)" section.
      added: "2026-04-28"
    - id: audit_driven_refinement
      keywords:
        - audit-driven evolution
        - lessons-learned
        - friction log
        - re-audit gate
        - strict by default
        - real audits over theoretical iteration
      summary: Framework friction is discovered by running real audits, not by theoretical iteration. Strict-by-default is safer than loose; real audits surface over-strictness, while under-strictness is silent.
      detail: |
        The user methodology is "by actually performing audits we can make it more
        crisp." The framework refines through audit cycles rather than abstract
        polishing. Each audit captures friction in a lessons-learned log
        (project-side notes plus this CLAUDE.md for the plugins-kit-resident
        decisions); each subsequent contract change cites the finding that
        triggered it.

        Operating consequence: when the framework feels under-specified or
        over-specified, do not iterate the framework in isolation. Run an audit on
        a real skill, capture the friction with surface / finding / follow-up
        provenance, and refine the contract from concrete evidence. Default toward
        stricter rules; real audits will surface over-strictness, while
        under-strictness fails silently.
      origin: |
        User methodology captured 2026-04-28 during framework v1 sign-off; reinforced
        through the audit-prep work unit (Dec-1/Dec-2/Dec-3 are themselves
        audit-driven decisions, not theoretical ones).
      added: "2026-04-28"
  conventions:
    - rule: When changing a schema in skills_kit_lib/schema_registry.py, update framework.md table rows in the same change so the human-review surface stays in sync.
      keywords:
        - schema change
        - framework sync
        - SSOT discipline
        - paired update
      why: schemas.py is authoritative on divergence, but a stale framework.md table is the most common drift source and confuses reviewers. Pair the updates so the divergence window is zero.
```
