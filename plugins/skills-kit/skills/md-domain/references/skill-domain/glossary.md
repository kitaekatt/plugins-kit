# Skill Glossary

Canonical terms for skill authoring in this project. Use these terms verbatim in skill documentation, framework references, and conversation about skill design. When a contributor uses different language, re-phrase in these terms to keep usage consistent.

This glossary defines vocabulary only. The contracts that bind these terms into rules -- what a given skill type must contain, recommend, or prohibit -- live in `framework.md`. Schemas are floors, not ceilings: authors may add load-bearing keys beyond the schema names below.

Sections are organized by what they describe: Material (physical artifacts), Method (design vocabulary), Skill types (auditable contracts), and Orthogonal attributes (independent flags). The first two sections preserve their internal sub-groupings (Files/Conventions/External binding, then Principles/Patterns). All records carry `keywords:` for routing and structured routing.

```yaml
glossary:
  _schema_version: "1"
  scope:
    covers:
      - Material foundations (files, conventions, external binding)
      - Design method (principles and patterns)
      - Skill type contracts
      - Orthogonal attributes across types
    excludes:
      - Type contracts and audit rules (see framework.md)
      - Validators and codegen (see scripts/)
      - Integration specifics and platform features

  files:
    - term: SKILL.md file
      keywords: [skill.md, frontmatter, body, l2 layer, progressive disclosure, required main file]
      definition: |
        The required main file of every skill. Holds the YAML frontmatter and the markdown body. The L2 layer of progressive disclosure: loaded only when a skill's trigger matches.

    - term: Reference file
      keywords: [reference file, supplementary markdown, l3 layer, one-hop-deep, on demand]
      definition: |
        Supplementary markdown in the skill directory, linked from SKILL.md and loaded on demand when its link is followed. The L3 layer of progressive disclosure. Reference files should be one hop deep from SKILL.md, not nested -- Claude tends to read partial content from deeply-nested references.

    - term: Script
      keywords: [script, executable, python, shell, stdout, bundled, user-only distinction]
      definition: |
        An executable file (Python, shell, etc.) shipped inside a skill. The agent runs the script via bash but never loads its contents into the conversation; only the script's stdout/stderr enters context. Do not confuse the *script* file block with the `trigger: user-only` attribute -- a script is a file you ship; user-only is how a skill is invoked.

    - term: Template / asset
      keywords: [template, asset, template.docx, config.json, lookup table, csv, non-executable resource]
      definition: |
        A non-executable resource: a `template.docx`, a `config.json`, a lookup table CSV. Loaded only when SKILL.md instructs the agent to read it.

  conventions:
    - term: Frontmatter
      keywords: [frontmatter, yaml header, skill.md top, name, description, allowed-tools, metadata]
      definition: |
        The YAML header at the top of a SKILL.md file. Holds `name` (<=64 chars, lowercase letters/numbers/hyphens, no reserved words "anthropic" or "claude"), `description` (<=160 chars; see Trigger entry for content rules), and optionally `allowed-tools` and `metadata`.

    - term: Trigger
      keywords: [trigger, directive clause, description field, use when, invoke when, cost-justified]
      definition: |
        The directive clause inside the `description` field. The single signal Claude uses to decide whether to load the skill, and the only purpose the description serves. A trigger must:

        - Be **directive**: "Use when..." or "Invoke when...". Capability summaries ("Enables...", "Provides...", "Manages...") cause Claude to follow the description instead of reading the body.
        - Name a **clear, unambiguous condition** for invocation. If the condition isn't crisp, the skill's design probably has a deeper flaw -- the skill is doing too much or doesn't have a real role.
        - Be **cost-justified, not over-aggressive**. Every skill load is tokens and a tool-call boundary; the trigger condition must justify that cost. A description that fires on topical adjacency ("...for any Python work...", "...whenever you read code...") violates the user's trust by burning tool calls without bringing value.
        - Stay within the **length budget** (<=160 chars). A description that doesn't fit in 160 chars is summarizing capability, not naming a trigger.

    - term: Exclusion
      keywords: [exclusion, do not use for, clause, overtriggering, adjacent topics]
      definition: |
        A "Do NOT use for..." clause appended to `description`. Prevents overtriggering on adjacent topics. Cited in the literature as the single most important sentence in many skill descriptions because positive triggers alone do not bound the activation surface.

    - term: Rule
      keywords: [rule, must, must not, discipline-skill, statement]
      definition: |
        A single MUST/MUST NOT statement in the body of a discipline-skill. A rule without a counter is incomplete.

    - term: Counter
      keywords: [counter, rationalization, rebuttal, loophole, excuse, reality, table]
      definition: |
        The rationalization rebuttal that closes a loophole on a rule. Lives next to the rule it protects, typically in an `excuse -> reality` table. A discipline-skill needs a counter for every rationalization observed in baseline testing.

    - term: Step
      keywords: [step, ordered instruction, procedure, technique-skill, ordered]
      definition: |
        One ordered instruction in a procedure. Steps live in technique-skills and in the procedural sections of script-bundled domain-skills.

    - term: Checklist
      keywords: [checklist, copyable, tickable, list, tick-off, procedure]
      definition: |
        A copyable, tickable list the agent pastes into its response and ticks off as it progresses. Used for procedures with more than three steps. Each turn's unchecked items are what raise the bar against premature completion claims.

    - term: Example
      keywords: [example, input output pair, tone, format, detail level, embedded]
      definition: |
        An input/output pair embedded in the skill body. Shows tone, format, and detail level more clearly than prose can. Strong enough that style bias in examples reproduces across all invocations -- the example set should span the variation the skill needs to support.

    - term: Gotcha
      keywords: [gotcha, failure mode, documented, real runs, happy path]
      definition: |
        A documented failure mode observed in real runs. Often the most valuable content in a mature reference-skill or technique-skill -- happy paths Claude can usually figure out; gotchas it cannot.

    - term: Index
      keywords: [index, member skills, triggers, container skills, domain-skills]
      definition: |
        A list of member skills with their triggers, used by container skills to declare what they aggregate. The only convention specific to domain-skills.

  external_binding:
    - term: Sub-agent binding
      keywords: [sub-agent binding, paired agent, auto-load, agent-bundled, session start]
      definition: |
        A paired agent definition that auto-loads its companion skill on session start. The convention is typically to name the agent after the skill it wraps (e.g. an `unreal-kit-a` agent for a `/ue-python-api` domain-skill). The mechanism by which a domain-skill becomes ambient context for a specialized agent. Carried as the `agent-bundled` attribute on the skill.

  principles:
    - id: audience_claude
      term: Audience-Claude (skills are written for Claude, not users)
      keywords: [audience-claude, llm-facing, structured data, form choice, bias toward structured, prose exception, schemas as floors]
      definition: |
        Skills are runtime context for Claude, not documentation for humans. Claude is the translator: when a user asks a question, Claude loads the skill, picks the relevant data, and presents it to the user in the user's natural language. This shapes authoring choices -- structured data (YAML, tables, code-fenced blocks) is appropriate because Claude consumes it; user-friendly prose is unnecessary because Claude generates it on demand. The user is the audience of Claude's reply, not of the skill itself.

        Form choice (bias toward structured data): the default for LLM-facing content is structured YAML. Use prose only when (a) the content is naturally narrative -- an identity sentence, an orientation paragraph, a single-paragraph explanation that does not decompose into discrete records; or (b) structure carries no meaning over prose. **When in doubt, bias toward structured data.** Structure carries assertions prose cannot: an `anti_patterns:` list with each entry as a record asserts implicitly that every item is genuinely an anti-pattern; a markdown bullet list carries no such assertion. Records are routable, keyword-able, and validatable; prose is none of those. The test is "does this structure aid Claude's comprehension better than prose would?" -- the default answer for LLM-facing content is yes.

        Schemas are floors, not ceilings. The per-type schema names the required minimum; authors may add load-bearing structured keys beyond the schema (an `exceptions:` list inside an anti-pattern entry, a `narration:` sub-block inside a technique). Forbidding extras would push authors toward unstructured prose when they want to add legitimate structure, which contradicts the bias-toward-structured-data default.
      audit_consequence: Structural choices (YAML data blocks, dense tables, condensed lists) are evaluated for whether they aid Claude's comprehension, not for whether they read smoothly as prose.
      realized_by: [chat-term relevance hints]

    - id: crp
      term: CRP -- Common Reuse Principle (read-together)
      keywords: [crp, common reuse principle, read-together, cohesion, split for reading, package cohesion]
      definition: |
        If a reader loads one section of a SKILL.md or reference file, they should plausibly need the rest. When sections serve different reading tasks, split them into separate files.
      why: Forcing readers through irrelevant content burns tokens and dilutes signal.
      realized_by: [progressive disclosure, domain-specific organization, conditional details]

    - id: ccp
      term: CCP -- Common Closure Principle (write-together)
      keywords: [ccp, common closure principle, write-together, content changes, same reason]
      definition: |
        Content that changes for the same reason belongs in the same file. When a system updates, only one file should need updating.
      why: A single conceptual change should be a single edit, not a hunt across files.
      realized_by: []

    - id: adp
      term: ADP -- Acyclic Dependencies Principle (link-forward-only)
      keywords: [adp, acyclic dependencies principle, link-forward-only, dag, cycles]
      definition: |
        References between files form a directed acyclic graph. No cycles.
      why: Cycles cause partial reads and ambiguous resolution order; a DAG guarantees the reader can always tell what comes first.
      realized_by: [progressive disclosure]

    - id: ssot
      term: SSOT -- Single Source of Truth
      keywords: [ssot, single source of truth, canonical, duplicates, drift]
      definition: |
        Every fact, term, or definition has exactly one canonical location. Other places reference it by name; they do not redefine or duplicate.

        Drift between duplicates is the most common decay mode in skill documentation; one source means no drift. Orientation-level summaries are not duplicates. A short summary that primes a reader to load the canonical reference is allowed when (a) the summary names the reference and (b) the canonical wins on divergence. A summary that has accumulated detail or examples beyond priming level has decayed into duplication; collapse it to a pointer.
      why: Drift between duplicates is the most common decay mode in skill documentation.
      realized_by: []

    - id: bottom_up
      term: Bottom-up composition
      keywords: [bottom-up composition, atomic primitives, dependency order, composed concepts]
      definition: |
        Atomic primitives are introduced before composed concepts that depend on them. Within a document, sections appear in dependency order; within the type system, atomic skill types (reference, pattern) come before composed ones (technique, discipline, domain).
      why: The reader understands each section using only what they've already seen -- this is also what makes ADP visible.
      realized_by: []

    - id: context_efficiency
      term: Context efficiency
      keywords: [context efficiency, footprint, context window, structure, writing, progressive disclosure]
      definition: |
        Skills minimize their footprint in the context window. Two faces, both required:

        Structure -- defer optional material to reference files; keep scripts as executables (their code never enters context); pull content into the always-loaded SKILL.md body only when the agent needs it every time.

        Writing -- every loaded paragraph justifies its tokens; assume Claude is capable; remove explanatory prose Claude doesn't need.

        Content the agent doesn't need wastes shared context that the user's task could use.
      why: Content the agent doesn't need wastes shared context that the user's task could use.
      realized_by: [progressive disclosure, domain-specific organization, conditional details, utility bundle, context budget]

    - id: tool_call_efficiency
      term: Tool-call efficiency
      keywords: [tool-call efficiency, latency, context cost, bundle, single script invocation]
      definition: |
        Bundle repeated multi-call sequences into single script invocations. Every tool call carries latency, context cost, and a turn boundary; collapsing N calls into one is faster and cheaper.
      why: Every tool call carries latency, context cost, and a turn boundary.
      realized_by: [utility bundle]
      audit_consequence: Audit by observing actual tool-call sequences in transcripts and identifying repeated multi-call patterns that could become a single script invocation.

    - id: inference_efficiency
      term: Inference efficiency
      keywords: [inference efficiency, deterministic scripts, expensive, non-deterministic, cheap, predictable]
      definition: |
        Replace inference with deterministic scripts for operations the agent performs repeatedly. Inference is expensive and non-deterministic; scripts are cheap and predictable.
      why: Inference is expensive and non-deterministic; scripts are cheap and predictable.
      realized_by: [utility bundle, template scaffold, self-correcting loop, plan-validate-execute]
      audit_consequence: Audit by reviewing inference-cost retrospectives and identifying operations that could move to scripts.

    - id: investigate_before_answering
      term: Investigate before answering
      keywords: [investigate before answering, no speculation, multi-source data, ground truth, runtime vs source, data location, do not assume, grep before assume]
      definition: |
        When a domain spans multiple data sources (config files, code, database, CSVs, external services), never speculate about which source holds a particular fact. Investigate (grep, read, query) before answering. Multiple sources create ambiguity; speculation manufactures bugs downstream when the agent edits the wrong file, misses a value cached elsewhere, or applies an update that runtime overwrites. The discipline is: list candidate sources from memory, query each, report the actual location, note any conflicts. When the question is "what's the current value", query runtime state; when the question is "how do I change it permanently", find the authoritative source rather than the runtime cache.
      why: Speculation about data location feels fast but produces wrong answers when the data is multi-source; the cost of grep is small relative to the cost of editing the wrong file.
      realized_by: [behavioral guardrail in domain-skill orientation]
      audit_consequence: A domain-skill with 2+ data sources cited in its scope or references should declare an investigate-before-answering guardrail in its `behavioral_guardrails:` block.

  patterns:
    - id: activation_metadata
      term: Activation metadata
      sub_grouping: Discovery and selection
      keywords: [activation metadata, trigger, key terms, description field, third person, use when, specific]
      definition: |
        The trigger and key terms packed into the `description` field. Third person, "use when...", specific. The single signal Claude uses to select which skill to load.
      citation: Anthropic best-practices doc

    - id: exclusion_clause
      term: Exclusion clause
      sub_grouping: Discovery and selection
      keywords: [exclusion clause, do not use for, activation surface, bounds]
      definition: |
        A "Do NOT use for..." appended to `description`. Bounds the activation surface that activation metadata alone leaves open.
      citation: Generative Programmer 14-pattern synthesis (cites Ruben Hassid)

    - id: chat_term_relevance_hints
      term: Chat-term relevance hints
      sub_grouping: Discovery and selection
      keywords: [chat-term relevance hints, keywords, summaries, structured data, yaml blocks, table rows, conditional loading]
      definition: |
        Keywords or short summaries embedded inside a skill's structured data (YAML blocks, table rows, capability entries) that match user-language phrasing, so Claude can route the user's words to the right data piece without needing to read full reference text. The conditional-loading block is the domain-skill version; in-record `keywords:` lists are the same pattern applied per-entry. Without these hints, Claude has to read body content top-to-bottom hunting for relevance; with them, Claude jumps to the right slot directly.
      fulfills: [Audience-Claude, Tool-call efficiency, Inference efficiency]

    - id: subdomain_layering
      term: Sub-domain layering
      sub_grouping: Discovery and selection
      keywords: [sub-domain, layering, greeting menu, argument dispatch, overview detection, sub-domain registration, capability menu, bare invocation]
      definition: |
        The user-facing surface mechanics of a domain-skill that decomposes into 2+ sub-domains: a bare-invocation greeting menu listing every sub-domain, argument dispatch that jumps directly into a named sub-domain, overview-request detection that returns only the matching sub-domain's capability table when the user asks "what can I do here", and a machine-readable sub-domain registration index that drives the menu and dispatch. Use only when a domain has 2+ sub-areas; single-area domains carry the cost without the orientation benefit.
      realized_by: [domain-layering.md]

    - id: subagent_dispatch
      term: Sub-agent dispatch
      sub_grouping: Discovery and selection
      keywords: [sub-agent dispatch, agent-bundled, paired agent, parent skill invocation, agent name suffix, dispatch convention]
      definition: |
        The convention that a domain-skill paired with a `<skill-name>-a` sub-agent is always invoked via that bundled agent rather than a generic agent that loads the skill manually. The bundled agent invokes its companion skill on session start, so it always has the domain's vocabulary loaded; this preserves the agent-bundled invariant the skill was designed against. The skill declares the convention explicitly when a paired agent exists, so any future caller composing with sub-agents knows the parent-spawns-bundled rule.
      realized_by: [domain-layering.md]

    - id: context_budget
      term: Context budget
      sub_grouping: Context economy
      keywords: [context budget, paragraph justify, tokens, capable, explanatory prose]
      definition: |
        The discipline of making every paragraph in a SKILL.md justify its tokens. Assume Claude is capable; remove explanatory prose Claude doesn't need.
      citation: Generative Programmer 14-pattern synthesis (Concise is key)

    - id: progressive_disclosure
      term: Progressive disclosure
      sub_grouping: Context economy
      keywords: [progressive disclosure, load levels, l1, l2, l3, metadata, body, reference files, 500 lines, one-hop-deep]
      definition: |
        Splitting content across the three load levels -- L1 metadata always loaded, L2 SKILL.md body loaded on trigger, L3 reference files loaded on demand. SKILL.md should stay under 500 lines, and references should be one hop deep, not nested.
      fulfills: [CRP, ADP, Context efficiency]
      citation: Anthropic best-practices doc

    - id: domain_specific_organization
      term: Domain-specific organization
      sub_grouping: Context economy
      keywords: [domain-specific organization, reference content, sub-domain, mutually exclusive, relevant, task]
      definition: |
        Splitting reference content by sub-domain (e.g. `finance.md`, `sales.md`) when sub-domains are mutually exclusive, so Claude reads only what's relevant to the current task.
      fulfills: [CRP, Context efficiency]
      citation: Anthropic best-practices doc

    - id: conditional_details
      term: Conditional details
      sub_grouping: Context economy
      keywords: [conditional details, basic content, inline, advanced material, reference files, linking]
      definition: |
        Keeping basic content inline in SKILL.md and linking to advanced material in reference files.
      fulfills: [CRP, Context efficiency]
      citation: Anthropic best-practices doc

    - id: control_tuning
      term: Control tuning
      sub_grouping: Instruction calibration
      keywords: [control tuning, instruction freedom, fragility, consistency, over-constrain, rigid]
      definition: |
        Matching instruction freedom to task fragility. High freedom for open-field work where multiple approaches are valid; low freedom for fragile sequences where consistency is critical. Authors reliably over-constrain because rigid feels safer.
      citation: Anthropic best-practices doc

    - id: explain_the_why
      term: Explain-the-why
      sub_grouping: Instruction calibration
      keywords: [explain-the-why, imperatives, reasoning, generalize, unanticipated cases]
      definition: |
        Replacing imperatives ("MUST do X") with reasoning ("do X because Y") so Claude can generalize the rule to unanticipated cases.
      citation: Generative Programmer 14-pattern synthesis

    - id: template_scaffold
      term: Template scaffold
      sub_grouping: Instruction calibration
      keywords: [template scaffold, output skeleton, placeholders, machine-parsed, human-reviewed]
      definition: |
        Embedding an output skeleton with placeholders that Claude fills. Strict ("ALWAYS use this template") for machine-parsed output; flexible ("sensible default; adapt as needed") for human-reviewed work.
      fulfills: [Inference efficiency]
      citation: Anthropic best-practices doc

    - id: in_skill_examples
      term: In-skill examples
      sub_grouping: Instruction calibration
      keywords: [in-skill examples, input output pairs, convey, tone, format, detail level, style]
      definition: |
        Two or three input/output pairs that convey tone, format, and detail level. Templates show the skeleton; examples show populated instances with style.
      citation: Anthropic best-practices doc

    - id: known_gotchas
      term: Known gotchas
      sub_grouping: Instruction calibration
      keywords: [known gotchas, concrete failure modes, real runs, documented, happy path, valuable]
      definition: |
        Concrete failure modes from real runs, documented alongside the happy path. Often the most valuable content in a mature skill.
      citation: Generative Programmer 14-pattern synthesis

    - id: workflow_checklist
      term: Workflow checklist
      sub_grouping: Workflow control
      keywords: [workflow checklist, copyable, tickable, list, pastes, ticks, procedures, steps]
      definition: |
        A copyable, tickable list provided for procedures with more than three steps. The agent pastes the checklist into its response and ticks off items as it works.
      citation: Anthropic best-practices doc

    - id: self_correcting_loop
      term: Self-correcting loop
      sub_grouping: Workflow control
      keywords: [self-correcting loop, output, validate, fix, revalidate, cycle, repeated, validator]
      definition: |
        The output -> validate -> fix -> revalidate cycle, repeated until the validator passes. The validator can be a script or a documented checklist.
      fulfills: [Inference efficiency]
      citation: Anthropic best-practices doc

    - id: plan_validate_execute
      term: Plan-validate-execute
      sub_grouping: Workflow control
      keywords: [plan-validate-execute, three-step, batch, destructive operations, structured artifact, plan, validate, execute]
      definition: |
        A three-step pattern for batch and destructive operations: produce a plan as a structured artifact, validate the plan, then execute against it. Catches errors before side effects rather than after.
      fulfills: [Inference efficiency]
      citation: Anthropic best-practices doc

    - id: technique
      term: Technique
      sub_grouping: Procedural composition
      keywords: [technique, self-contained, how-to, ordered sequence, defined goal, bundled script, gotchas]
      definition: |
        A self-contained how-to: an ordered sequence of steps for accomplishing a defined goal, optionally with a bundled script and known gotchas. A technique is the unit of action-oriented content in a skill. A skill may contain one or more techniques -- the technique-skill type does not imply 1-to-1.

    - id: capability
      term: Capability
      sub_grouping: Procedural composition
      keywords: [capability, technique, structural metadata, user-objective, operation, tool reference, sub-cases, scope axes]
      definition: |
        A technique extended with structural metadata: a user-objective description, an operation/tool reference, optional sub-cases, scope axes, and a pointer to a reference section that owns full mechanics. Capabilities surface action units as discrete, auditable entries with consistent vocabulary and scope. Most commonly used inside domain-skills (where multiple capabilities aggregate under one container), but applicable in any skill that benefits from structured procedural surfaces -- e.g. a technique-skill exposing several variants of an action with shared scope semantics. Capabilities are techniques+.

    - id: actions
      term: Actions pattern
      sub_grouping: Procedural composition
      keywords: [actions, multi-step, recipe, deterministic, capture, tell_user, facade script, narration, ordered sequence]
      definition: |
        A YAML step sequence attached to a sub-domain or capability that encodes a deterministic multi-step recipe. Each step is either a `tool` invocation or a `tell_user` message; the agent runs every step in order rather than improvising. Steps may declare `capture` to extract output fields used as `<field>` placeholders in later steps. When a recipe has 3+ tool calls that always co-occur, pair the action with a facade script (a single subcommand wrapping the sequence and emitting YAML) to avoid tool-call doubling. Single tool calls in or out of an action carry a one-line narration before the call.
      realized_by: [patterns-actions.md]

    - id: subdomain_config
      term: Sub-domain config schema
      sub_grouping: Procedural composition
      keywords: [subdomain config, state terms, operations, scope axes, canonical phrasing, llm-dependent content, dependency order, sub-area record, capability-skill subdomain]
      definition: |
        An optional structured record on a capability-skill that declares a sub-area's runtime configuration: canonical state vocabulary (`state_terms`), supported verbs (`operations`), capability-space axes (`scope_axes`), readback rules (`canonical_phrasing`), LLM-produced fields (`llm_dependent_content`), and capability ordering constraints (`dependency_order`). The schema gives audit tooling a mechanical floor for verifying a sub-area's vocabulary; a capability-skill with multiple sub-areas declares one record per sub-area. Single-area capability-skills omit the field entirely.
      realized_by: [subdomain-schema.md]

    - id: utility_bundle
      term: Utility bundle
      sub_grouping: Executable code
      keywords: [utility bundle, purpose-built scripts, scripts directory, deterministic operations, helper]
      definition: |
        Purpose-built scripts shipped in a `scripts/` subdirectory of the skill, used for deterministic operations rather than asking Claude to regenerate the same helper each time.
      fulfills: [Tool-call efficiency, Inference efficiency, Context efficiency]
      citation: Anthropic best-practices doc

    - id: autonomy_calibration
      term: Autonomy calibration
      sub_grouping: Executable code
      keywords: [autonomy calibration, allowed-tools, frontmatter, pre-approved, hard restriction, over-broad]
      definition: |
        Declaring `allowed-tools` in the SKILL.md frontmatter so the skill is pre-approved to use only the tools it needs. Pre-approval is not the same as a hard restriction; over-broad lists silently grant unintended autonomy.
      citation: Generative Programmer 14-pattern synthesis

    - id: query_tool_facade
      term: Query-tool facade
      sub_grouping: Executable code
      keywords: [query tool, facade, gazetteer, lookup, did-you-mean, YAML output, spelling discovery, catalog index, exact match, substring filter, structured catalog]
      definition: |
        A single facade script with multiple modes wrapping a structured catalog (gazetteer, inventory, directory, configuration tree). Modes provide exact-match lookups by ID/name and substring enumeration; misses return a `did_you_mean:` list of close suggestions rather than silently resolving. Output is YAML on every path. The agent's discipline: when uncertain about canonical spelling, run `list --substring <fragment>` first or accept did-you-mean suggestions before grepping the codebase. Replaces ad-hoc greps and spelling-from-memory with a deterministic, fast lookup surface.
      realized_by: [../authoring-patterns/query-tool-pattern.md]

    - id: adversarial_pressure_testing
      term: Adversarial pressure testing
      sub_grouping: Discipline
      keywords: [adversarial pressure testing, red, green, refactor, baseline failure, subagent, complies, loopholes]
      definition: |
        The RED -> GREEN -> REFACTOR cycle for discipline-skills. RED is the baseline failure: a subagent without the skill violates the rule. GREEN is the skill written so the same subagent now complies. REFACTOR closes loopholes the agent finds under combined pressures (time + sunk cost + fatigue).
      citation: obra/superpowers

    - id: rationalization_counter_table
      term: Rationalization counter table
      sub_grouping: Discipline
      keywords: [rationalization counter table, excuse, reality table, rationalization, baseline agent, refutes]
      definition: |
        An explicit `excuse -> reality` table that names every rationalization the baseline agent produced and refutes it directly.
      citation: obra/superpowers

    - id: red_flags_list
      term: Red flags list
      sub_grouping: Discipline
      keywords: [red flags list, stop signals, self-checking, phrases, code before test, just this once]
      definition: |
        A short list of STOP signals the agent uses for self-checking -- phrases like "code before test", "just this once", "tests after achieve the same purpose."
      citation: obra/superpowers

  skill_types:
    - id: reference_skill
      term: Reference-skill
      keywords: [reference-skill, facts, lookup, api specs, syntax guides, vocabulary, glossaries, atomic, no procedure]
      definition: |
        A skill whose content is pure facts and lookup -- API specs, syntax guides, project conventions, vocabulary glossaries, error and symptom tables. No procedure, no rule, no workflow. Atomic; composes from nothing.
      source: obra/superpowers (line 69)

    - id: pattern_skill
      term: Pattern-skill
      keywords: [pattern-skill, mental model, way of thinking, problem class, teaches recognition, atomic, peer]
      definition: |
        A skill that teaches a mental model -- a way of thinking about a class of problem. Teaches recognition, not procedure. Atomic; peer of reference-skill.
      source: obra/superpowers (line 66)

    - id: technique_skill
      term: Technique-skill
      keywords: [technique-skill, procedure, ordered steps, techniques, user-only attribute, script-skill]
      definition: |
        A skill whose primary content is one or more *techniques* (the procedural pattern). Composes pattern + reference into procedure(s), and may bundle scripts as canonical executable forms. A technique-skill invoked only by user slash command (rather than by LLM auto-discovery) carries the `trigger: user-only` attribute; this replaces what the project previously called `script-skill`.
      source: obra/superpowers (line 63)

    - id: capability_skill
      term: Capability-skill
      keywords: [capability-skill, wrapper, external capability, tool wrapper, mcp wrapper, api wrapper, service wrapper, layering manifest, capabilities at root]
      definition: |
        A skill whose primary content is a structured set of *capabilities* (techniques+ per the Capability pattern) wrapping an external capability provider -- a tool, MCP server, API, service, IDE, or framework. The skill brings the external thing into the project's workflow with project-specific setup, conventions, and gotchas. Conceptually IS-A technique-skill: capabilities are techniques+, so capability-skill is a technique-skill with stronger structural requirements on each technique. The schema requires `capabilities:` at root (replacing technique-skill's `techniques:`), an `external_capability` declaration naming what is wrapped, a `layering` manifest declaring how content is allocated across CLAUDE.md / SKILL.md / references/, and >=1 capability-skill-level gotcha. Member skills + Conditional Loading + aggregated capability surface fire conditionally when capabilities grow into separate skills.
      source: project addition (no upstream canonical reference; follows the project's audit-driven type evolution -- mixed-type drift in tool-shape skills surfaced the contract gap)

    - id: discipline_skill
      term: Discipline-skill
      keywords: [discipline-skill, enforces rule, pressure, wraps, target, rules, counters, compliance]
      definition: |
        A skill that enforces a rule under pressure. Wraps a target technique-skill or pattern-skill with rules + counters and demands compliance even when faster alternatives exist.
      source: obra/superpowers (line 399, Discipline-Enforcing Skills)

    - id: domain_skill
      term: Domain-skill
      keywords: [domain-skill, container, leaf skills, references, patterns, techniques, disciplines, knowledge area, ambient context]
      definition: |
        A container skill that gathers leaf skills (references, patterns, techniques, disciplines, scripts) for a knowledge area. Loading a domain-skill primes the LLM with awareness of the whole set; specific members load via their own triggers when relevant. Often paired with a sub-agent that auto-loads the domain-skill on session start.
      source: project addition (no upstream canonical reference)

  attributes:
    - term: Trigger-model
      keywords: [trigger-model, auto, user-only, auto-invoked, slash command, invocation contract]
      definition: |
        Whether a skill is invoked automatically by the LLM (`trigger: auto`, the default and obra-canonical model) or only by the user via slash command (`trigger: user-only`). The user-only attribute is the project's renaming of what was previously a separate `script-skill` type -- a slash-command workflow is structurally a technique-skill with a different invocation contract.

    - term: Agent-bundled
      keywords: [agent-bundled, paired agent, auto-load, session start, domain-skill, domain-specific agent]
      definition: |
        The attribute on a skill that is auto-loaded by a paired sub-agent on session start. Most commonly seen on domain-skills paired with a domain-specific agent that wraps the skill's vocabulary and member set. Example: `/ue-python-api` (unreal-kit domain-skill).

    - term: Harness-targeted
      keywords: [harness-targeted, claude code harness, agent, audit criterion, project work, harness reflects]
      definition: |
        The attribute on a skill that operates on the Claude Code harness or agent itself rather than on project work. Harness-targeted skills change their audit criterion: success is verified by checking that the harness reflects the change, not by running a project task. Typical examples: `/bootstrap` (interpreting SessionStart bootstrap messages); `/cache-report` (displaying session cache metrics); skills that audit permission rules in settings.

    - term: Integration
      keywords: [integration, third-party system, talks to, submits, returns, perforce, plugin, remote git]
      definition: |
        The attribute on a skill that talks to a third-party system. Typical examples: `/p4-code-review` and `/git-code-review` (run multi-agent reviews against Perforce / Git); `/bootstrap` (manages plugin marketplaces and remote dependencies); skills syncing files to a remote git host.

    - term: Bootstrap
      keywords: [bootstrap, one-time setup, initializes, dependencies, configuration, session start, allowlist]
      definition: |
        The attribute on a skill that performs one-time setup. Typical examples: `/bootstrap` (initializes plugin dependencies and configuration on session start); a skill that scans recent transcripts to seed an allowlist.

    - term: Scheduled / autonomous
      keywords: [scheduled, autonomous, loop, schedule, interactive use, recurring interval]
      definition: |
        The attribute on a skill designed to run via `/loop` or `/schedule` rather than through interactive use.

    - term: Script-bundling
      keywords: [script-bundling, ships executable resources, python, shell, markdown, procedural, technique-skills, utility-bundle]
      definition: |
        The attribute on a skill that ships executable resources (Python, shell) alongside its markdown. Common for technique-skills, the procedural portions of domain-skills, and any skill applying the utility-bundle pattern.

  sources:
    - id: anthropic_best_practices
      name: Anthropic best-practices doc
      url: https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices
      notes: |
        Skill authoring best practices from Anthropic Claude Docs. Source of principles CRP/CCP/ADP applied to skill documentation, tool-call efficiency, and pattern citations for progressive disclosure, domain-specific organization, conditional details, control tuning, template scaffold, in-skill examples, workflow checklist, self-correcting loop, plan-validate-execute, and utility bundle.

    - id: generative_programmer_14_patterns
      name: Generative Programmer 14-pattern synthesis
      url: https://generativeprogrammer.com/p/skill-authoring-patterns-from-anthropics
      notes: |
        Skill Authoring Patterns from Anthropic's Best Practices. Cites Ruben Hassid for the exclusion-clause pattern. Source of pattern citations for context budget (Concise is key), explain-the-why, known gotchas, and autonomy calibration.

    - id: obra_superpowers
      name: obra/superpowers
      url: https://github.com/obra/superpowers/blob/main/skills/writing-skills/SKILL.md
      notes: |
        skills/writing-skills/SKILL.md by Jesse Vincent. Source of the four canonical skill type names (reference, pattern, technique, discipline-enforcing) and the discipline patterns (adversarial pressure testing, rationalization counter table, red flags list).

    - id: martin_package_cohesion
      name: Robert C. Martin package-cohesion principles (CRP, CCP, ADP)
      notes: |
        Package-cohesion principles from Agile Software Development: Principles, Patterns, and Practices (2002), applied here to skill documentation.
```
