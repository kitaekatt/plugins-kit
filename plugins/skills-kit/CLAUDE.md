# skills-kit plugin orientation

Plugin-level orientation for `plugins-kit:skills-kit`. The plugin's artifact is **markdown generally** -- every md file a project accumulates: SKILL.md, CLAUDE.md, project docs, READMEs, and skill references (see "Total ownership" below). It is organized around a **verb x artifact matrix** over that `md` artifact: two **union domains** -- `md-authoring` (`/md-authoring`) and `md-audit` (`/md-audit`) -- plus standalones `cohesion-principles`, `knowledge-encoding`, `materialized-output`, and `update-documentation`. `skill` = SKILL.md and `claude-md` = CLAUDE.md are the *typed* specializations (the ones with a formal schema contract), not the whole surface -- md-audit also audits project docs and cross-references, and the framework claims ownership of every md role. A fresh agent landing here should read this file first; the `plugin_surface_overview` insight below is the canonical per-skill / per-surface map (what each domain unions, what tooling and references it owns), and this file points at the right surface for the task at hand.

The framework the plugin advocates:

- **Total ownership (the conceptual goal).** skills-kit is an opinionated model for owning EVERY md artifact in a project: SKILL.md, CLAUDE.md (root / subsystem / directory / .local), skill references, project docs, README (the derived human brief), committed generated artifacts (provenance-only), and the out-of-band surfaces the md graph touches (in-code contract docs, runtime asset dependencies). An md file the framework cannot name a role for is a gap in the framework -- not an exempt file. Stress-test gaps are worked into the spine (cohesion-principles) and the audits, never waved off.
- **Audience-Claude.** Skills are runtime context for Claude, not human documentation.
- **Form-choice bias toward structured data.** Default to YAML for LLM-facing content; prose only when content is naturally narrative.
- **Schemas are floors, not ceilings.** Each per-type schema names the required minimum; authors may add load-bearing structured keys beyond it.
- **Audits drive refinement.** Friction is discovered by running real audits, not theoretical iteration.

The YAML block below is the load-bearing surface for routing into the skill, the scripts, and the canonical references.

```yaml
claude_md:
  _schema_version: "1"
  scope:
    directory: plugins/skills-kit
    covers:
      - what the skills-kit plugin ships and how to use it
      - the four canonical surfaces (glossary, framework, schemas, scripts) and which to load when
      - the merge-gate convention for any change touching schemas or canonical references
      - dependency posture (pyyaml via bootstrap.json + pyproject.toml, never manual pip)
    excludes:
      - audit-driven framework decision provenance (covered by skills/skill-authoring/CLAUDE.md)
      - validator and script internals (covered by skills_kit_lib/CLAUDE.md)
      - per-plugin dependency posture for other plugins (covered by plugins-kit/CLAUDE.md)
  insights:
    - id: plugin_surface_overview
      keywords:
        - skills-kit overview
        - what is this plugin
        - skill-authoring domain-skill
        - audit script
        - classify script
        - tag script
        - schema validator
        - configure standards
        - rule catalog
        - standards resolver
        - disable optional rule
      origin: Phase 4.6 P5 plugin-level orientation surface (2026-04-30).
      added: "2026-04-30"
      summary: skills-kit ships the verb x artifact matrix -- md-authoring + md-audit union domains over the md artifact (skill / claude-md specializations) -- plus standalones (cohesion-principles, knowledge-encoding, materialized-output, update-documentation) and the skill-authoring tooling (audit / classify / tag / schema_registry). skill-authoring is the skill-authoring sub-domain, kept whole.
      detail: |
        Plugin layout:
        - skills/skill-authoring/SKILL.md -- the domain-skill itself; aggregates the
          framework as content and exposes audit/classify/tag as capabilities.
        - skills/skill-authoring/references/glossary.md -- canonical vocabulary
          (Audience-Claude principle, CRP/CCP/ADP/SSOT, types, patterns, attributes).
          Embedded YAML under root key glossary: with 63 records.
        - skills/skill-authoring/references/framework.md -- type contracts (5 markdown
          tables for human review) plus structured framework records (description
          requirements, content-form choice, audit procedure, schemas-as-floors,
          conditional-requirement grammar) embedded as YAML under root key framework:.
          schema_registry.py is authoritative on divergence with the markdown tables.
        - skills/skill-authoring/references/scripts.md -- script reference (purpose,
          usage, output verdicts, gotchas).
        - skills/skill-authoring/references/example-audit.md -- worked audit example.
        - skills_kit_lib/schema_registry.py -- canonical machine-readable
          per-type contract. SCHEMAS_BY_ROOT dispatches by YAML root key.
        - skills_kit_lib/audit.py -- per-skill or per-CLAUDE.md audit;
          three states (yaml-validated / contract-staged / legacy-fallback).
        - skills_kit_lib/classify.py -- type inference; YAML root key
          is deterministic, heuristic scoring is the legacy fallback.
        - skills_kit_lib/tag.py -- idempotent skill-type: frontmatter
          writer; refuses to overwrite existing differing values without --force;
          refuses missing-frontmatter cases (never invents).

        Standards-configuration surface (configurable optional rules + layered
        user/project standards):
        - skills_kit_lib/rule_catalog.py -- SSOT mapping every audit rule id to
          one bucket (architectural / optional / inoffensive); consumed by the
          resolver's reject-un-tunable-rule check and by the M4 config docs.
        - skills_kit_lib/standards_resolve.py -- self-contained (stdlib+pyyaml,
          no bootstrap_lib) resolver of the layered config: shipped -> user_dir
          skills-kit/ -> its config.local.yaml -> project .claude/skills-kit/ ->
          its config.local.yaml; config.yaml carries rules:{<id>: off} +
          thresholds:; *-standards.md files union across layers. Loud errors
          (StandardsConfigError) on an un-tunable id, unknown threshold, or
          malformed config; degrades to defaults + note without pyyaml.
        - skills/md-audit/references/configuring-standards.md -- user-and-Claude
          configuration reference (layer model, config.yaml format, the full
          rule-id catalog, thresholds, troubleshooting).
        - skills/md-audit/references/authoring-standards.md -- authoring spec for
          an additive standards_set file.
    - id: which_surface_for_which_task
      keywords:
        - reading order
        - which file
        - vocabulary lookup
        - contract lookup
        - schema lookup
        - audit operation
        - classify operation
        - tag operation
      origin: Phase 4.6 P5 plugin-level orientation surface (2026-04-30).
      added: "2026-04-30"
      summary: Vocabulary -> glossary.md. Contract floor -> schema_registry.py (or framework.md tables for human review). Audit-driven decisions and provenance -> skill-authoring/CLAUDE.md. Validator internals -> skills_kit_lib/CLAUDE.md.
      detail: |
        - "What does <term> mean?" -> glossary.md, search the appropriate sub-grouping
          (files / conventions / external_binding / principles / patterns / skill_types
          / attributes / sources). Every record has a keywords cluster for routing.
        - "Does this skill satisfy its type contract?" -> run audit.py against the
          SKILL.md path. Zero FAILs is well-formed.
        - "What are the required keys for type X?" -> schema_registry.py (canonical) or
          framework.md type contract tables (human-review surface; schemas wins on
          divergence).
        - "Why does the framework forbid Y / require Z?" -> skill-authoring/CLAUDE.md
          insights. Each Dec-N entry cites surface / finding / follow-up.
        - "How does the validator decide between yaml-validated / contract-staged /
          legacy-fallback?" -> skills_kit_lib/CLAUDE.md three_audit_states insight.
    - id: merge_gate_convention
      keywords:
        - re-audit gate
        - merge criterion
        - schema change discipline
        - framework change discipline
        - zero fails
      origin: P1 convention (skill-authoring/CLAUDE.md) generalized to plugin level during P5 (2026-04-30).
      added: "2026-04-30"
      summary: Any change touching schema_registry.py, glossary.md, or framework.md must re-audit every plugins-kit SKILL.md (the plugins/*/skills/*/SKILL.md glob -- the count grows; do not hardcode it) plus the three CLAUDE.md files (skill-authoring, skills_kit_lib, plugins-kit root) to zero FAILs before shipping.
      detail: |
        The plugin advocates schema validation as the audit substrate. Shipping a
        contract change that breaks the plugin's own skills would violate the
        principle the plugin teaches.

        Re-audit invocation pattern:

          for f in plugins/*/skills/*/SKILL.md \\
                   plugins/skills-kit/skills/skill-authoring/CLAUDE.md \\
                   plugins/skills-kit/skills_kit_lib/CLAUDE.md \\
                   plugins/skills-kit/CLAUDE.md \\
                   CLAUDE.md; do
            (cd plugins/skills-kit && python -m skills_kit_lib.audit "../../$f")
          done

        Catch second-order effects: a tightened technique-skill row may force one or
        more SKILL.md files to gain steps: blocks (this is what happened during the
        audit-prep work unit -- cache-report and test-greeting both gained 1-step
        bodies after Dec-2 was codified).
    - id: dependency_posture
      keywords:
        - pyyaml dependency
        - skills-kit venv path
        - audit graceful degradation
        - HAVE_YAML
      origin: User directive 2026-04-28 codified in plugins-kit/CLAUDE.md; surfaced at plugin level during P5 (2026-04-30).
      added: "2026-04-30"
      summary: skills-kit's only Python dependency is pyyaml; the plugin venv lives at ~/.claude/plugins/data/plugins-kit/skills-kit/.venv. audit.py degrades gracefully when pyyaml is unavailable (contract-staged state). For the cross-plugin dep-management rule, see plugins-kit/CLAUDE.md.
      detail: |
        skills-kit-specific facts (the cross-plugin rule lives in plugins-kit/CLAUDE.md):

        - bootstrap.json declares venv.check_imports = ["yaml"]; pyproject.toml
          declares pyyaml in [project] dependencies.
        - The plugin venv path is ~/.claude/plugins/data/plugins-kit/skills-kit/.venv.
        - audit.py degrades gracefully when pyyaml is unavailable (HAVE_YAML False):
          universal rows still pass, the YAML contract row reports judgment-required
          ("install pyyaml to validate"), and legacy markdown heuristics are skipped
          (the contract is staged but unvalidated).
    - id: invocation_paths
      keywords:
        - invoke skill-authoring
        - run audit
        - run classify
        - run tag
        - bootstrap-installed venv python
      origin: Phase 4.6 P5 plugin-level orientation surface (2026-04-30).
      added: "2026-04-30"
      summary: The skill-authoring domain-skill loads on its trigger (skill design / authoring / audit / classification context). Scripts run via the plugin venv's Python.
      detail: |
        - Domain-skill: trigger fires on skill-design vocabulary (audit, classify,
          contract, schema, framework, type-contract, etc.). Per the
          conditional-loading index in SKILL.md, references load on demand.
        - Scripts: invoke via the plugin venv directly. The bootstrap engine ensures
          the venv exists at ~/.claude/plugins/data/plugins-kit/skills-kit/.venv;
          calling its python.exe runs audit.py / classify.py / tag.py with pyyaml
          available.

          Example (Windows; analogous on Mac/Linux with .venv/bin/python):

          (cd plugins/skills-kit && \\
            ~/.claude/plugins/data/plugins-kit/skills-kit/.venv/Scripts/python.exe \\
            -m skills_kit_lib.audit \\
            <path-to-SKILL.md-or-CLAUDE.md>)

        - Outside the venv (bare system Python): audit.py runs but reports
          judgment-required on the YAML contract row. classify.py and tag.py operate
          on frontmatter and a regex-detected YAML root key; they do not need pyyaml.
    - id: audit_framework_paths_are_cross_plugin_api
      keywords: [audit-framework.md, audit-framework.yaml, cross-plugin consumers, breaking rename, md-audit references, awesome-kit, prototypes, path contract]
      summary: skills/md-audit/references/audit-framework.{md,yaml} are consumed BY PATH from awesome-kit and prototypes -- renaming or moving them is a breaking cross-plugin change requiring consumer version bumps.
      detail: |
        awesome-kit and prototypes reference
        plugins/skills-kit/skills/md-audit/references/audit-framework.md and
        audit-framework.yaml by literal path (the shared audit framework is a
        cross-plugin API surface, not a private reference). Treat any
        rename/move/restructure of those two files like a breaking library
        change: update every consumer in the same release and bump the
        consumers' plugin versions, or do not move the files. Grep
        plugins/awesome-kit and plugins/prototypes for "audit-framework"
        before touching them.
      origin: Arch-review finding S19 (2026-06-09).
      added: "2026-06-10"
  conventions:
    - rule: "Audit workflow lanes pin an explicit model AND effort -- never inherit either from the session: detect/classify lanes set model 'opus' + effort 'high'; remediate lanes set model 'sonnet' + effort 'low'. The remediate defaults live once in scripts/gen_workflow_js.py (the template); the detect/classify defaults live in each hand-authored detect.js/classify.js agent() call. A new audit workflow script must follow the same split."
      keywords:
        - workflow lane model
        - opus detect high effort
        - sonnet remediate low effort
        - no inherited effort
        - token cost
        - agent() model default
      why: "Without explicit tiers, every fan-out lane inherits the main-loop session model and effort -- a 20-file audit on a top-tier session is 20 top-tier lanes, mostly wasted, while a low-effort session would silently under-power detection. Each lane declares the RIGHT tier for its work instead: remediation applies already-decided edits (the judgment happened at the Q&A gate), so sonnet at low effort suffices; detection/classification IS the audits' judgment core (CCP/CRP/ADP criteria application), the judge stage that warrants opus at high effort. User directive 2026-07-13 (explicitly: pin the right effort, do not inherit)."
    - rule: Surface a framework decision as a lessons-learned entry with surface / finding / follow-up provenance before the contract change ships. Land it in skill-authoring/CLAUDE.md (framework decisions) or scripts/CLAUDE.md (validator-side decisions).
      keywords:
        - provenance
        - decision log
        - lessons-learned
        - surface finding follow-up
      why: A contract change without provenance cannot be rewound. A future agent must be able to reconstruct what audit surface revealed the friction; outcomes alone (the new schema) do not carry that signal.
```
