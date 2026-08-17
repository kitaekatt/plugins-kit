# skills-kit plugin orientation

Plugin-level orientation for `plugins-kit:skills-kit`. The plugin's artifact is **markdown generally** -- every md file a project accumulates: SKILL.md, CLAUDE.md, project docs, READMEs, and skill references (see "Total ownership" below). It is organized around a **verb x artifact matrix** over that `md` artifact -- **audit** and **author** crossed with the artifacts (`skill` = SKILL.md, `claude-md` = CLAUDE.md, `project-doc`, `references`), plus **generate** (claude-md only, written from analysis-produced coverage) and **analyze** (report-only, subject is CODE) -- and since 2026-07-29 that matrix is expressed as DATA inside a single skill rather than as topology. Author and generate differ by INPUT PROVENANCE, not output: see md-domain/CLAUDE.md `author_and_generate_split_on_provenance`. `skill` and `claude-md` are the *typed* specializations (the ones with a formal schema contract), not the whole surface: the domain also audits project docs and cross-references, and the framework claims ownership of every md role.

The plugin ships **four skills**:

- **`md-domain`** (`/md-domain`) -- the single front door. One dispatch table (verb x artifact -> lane record), three shared verb procedures (audit; one producing lane serving both author and generate; analyze), four per-artifact standards docs, the placement spine, and all the audit machinery.
- **`knowledge-encoding`** -- encoding a newly discovered insight into a persistent home.
- **`update-documentation`** -- end-of-session review of what the work implies for the docs.
- **`materialized-output`** -- designing a tool that produces a materialized insight.

A fresh agent landing here should read this file first; the `plugin_surface_overview` insight below is the canonical per-surface map, and `which_surface_for_which_task` points at the right file for the task at hand.

The framework the plugin advocates:

- **Total ownership (the conceptual goal).** skills-kit is an opinionated model for owning EVERY md artifact in a project: SKILL.md, CLAUDE.md (root / subsystem / directory / .local), skill references, project docs, README (the derived human brief), committed generated artifacts (provenance-only), and the out-of-band surfaces the md graph touches (in-code contract docs, runtime asset dependencies). An md file the framework cannot name a role for is a gap in the framework -- not an exempt file. Stress-test gaps are worked into the spine (`md-domain/references/cohesion-principles.md`) and the audits, never waved off.
- **Audience-Claude.** Skills are runtime context for Claude, not human documentation.
- **Form-choice bias toward structured data.** Default to YAML for LLM-facing content; prose only when content is naturally narrative.
- **Schemas are floors, not ceilings.** Each per-type schema names the required minimum; authors may add load-bearing structured keys beyond it.
- **Audits drive refinement.** Friction is discovered by running real audits, not theoretical iteration.

The YAML block below is the load-bearing surface for routing into the skills, the scripts, and the canonical references.

```yaml
claude_md:
  _schema_version: "1"
  scope:
    directory: plugins/skills-kit
    covers:
      - what the skills-kit plugin ships and how to use it
      - the canonical surfaces (standards docs, cohesion-principles, glossary, framework, schemas, scripts) and which to load when
      - the merge-gate convention for any change touching schemas or canonical references
      - dependency posture (pyyaml via bootstrap.json + pyproject.toml, never manual pip)
    excludes:
      - shape decisions about md-domain itself (covered by skills/md-domain/CLAUDE.md)
      - audit-driven framework decision provenance (covered by skills/md-domain/references/provenance/)
      - validator and script internals (covered by skills_kit_lib/CLAUDE.md)
      - per-plugin dependency posture for other plugins (covered by plugins-kit/CLAUDE.md)
  insights:
    - id: plugin_surface_overview
      keywords:
        - skills-kit overview
        - what is this plugin
        - md-domain front door
        - four skills
        - standards docs
        - audit script
        - classify script
        - tag script
        - schema validator
        - configure standards
        - rule catalog
        - standards resolver
      origin: Phase 4.6 P5 plugin-level orientation surface (2026-04-30); rewritten for the md-domain fold (2026-07-29).
      added: "2026-04-30"
      summary: skills-kit ships four skills -- md-domain (the single verb x artifact front door, owning the standards docs, both verb lanes, the placement spine, the audit framework, and the discover/workflow machinery) plus knowledge-encoding, update-documentation, and materialized-output. The audit/classify/tag validators and the standards-configuration surface live in skills_kit_lib.
      detail: |
        Plugin layout (post-fold), one line per surface. md-domain's own SKILL.md
        references block is the file-by-file index.

        md-domain -- the single front door.
        - skills/md-domain/SKILL.md -- the ONE dispatch table (verb x artifact ->
          lane record); read for routing, the lane roster, and argument grammar.
        - skills/md-domain/CLAUDE.md -- shape decisions about the skill itself.
        - references/standards/ -- the per-artifact "what good looks like", each
          read in BOTH directions (detecting for audit, producing for generation).
        - references/lanes/ -- the three shared verb procedures (audit-lane,
          generation-lane serving BOTH author and generate, coverage-lane which
          is the analyze procedure named for its output).
        - references/cohesion-principles.md -- the placement spine; read for WHERE
          a fact lives. Every lane and standards doc defers here.
        - references/audit-framework.{md,yaml} -- audit-family glossary + registry
          (see audit_framework_paths_are_cross_plugin_api below).
        - references/references-finding-taxonomy.md -- the references lane's taxonomy.
        - references/configuring-standards.md, references/authoring-standards.md --
          standards configuration and additive standards files.
        - references/authoring-patterns/ -- the verb-generic content-shape cluster.
        - references/skill-domain/ -- skill-artifact deep references (glossary,
          framework tables, scripts, examples); schema_registry.py wins on divergence.
        - references/provenance/ -- inherited decision logs (the dec_N framework
          decisions and the folded skills' CLAUDE.md histories).
        - scripts/ -- the lanes' discover / scan / report entry points.
        - workflow/ -- the detect/classify lanes and the generated remediate lanes.

        The other three skills: knowledge-encoding (encode a discovered insight),
        update-documentation (end-of-session doc review), materialized-output (the
        insight-view tool pattern). None of them own placement or the standards.

        Validators (plugin-level Python, invoked by md-domain's capabilities):
        - skills_kit_lib/schema_registry.py -- canonical machine-readable
          per-type contract. SCHEMAS_BY_ROOT dispatches by YAML root key.
        - skills_kit_lib/audit.py -- per-skill or per-CLAUDE.md audit;
          three states (yaml-validated / contract-staged / legacy-fallback).
        - skills_kit_lib/classify.py -- type inference; YAML root key
          is deterministic, heuristic scoring is the legacy fallback.
        - skills_kit_lib/tag.py -- idempotent skill-type: frontmatter
          writer; refuses to overwrite existing differing values without --force;
          refuses missing-frontmatter cases (never invents).

        Standards-configuration surface:
        - skills_kit_lib/rule_catalog.py -- the RULES catalog, SSOT for rule id ->
          bucket / description.
        - skills_kit_lib/standards_resolve.py -- self-contained (stdlib+pyyaml,
          no bootstrap_lib) resolver of the layered standards config.
        - Layer model, config.yaml format, thresholds, rule catalog, and error
          behavior: skills/md-domain/references/configuring-standards.md.

        The fold (2026-07-29): md-audit and md-authoring (the two routers), their
        six member skills, and the standalone cohesion-principles skill were
        dissolved into md-domain; cohesion-principles survives as a reference doc.
        The fold was a relocation, not a behavior change -- rationale and the
        verbatim-preservation list: skills/md-domain/CLAUDE.md (`md_domain_fold`,
        `contracts_preserved_verbatim_through_the_fold`).
    - id: which_surface_for_which_task
      keywords:
        - reading order
        - which file
        - vocabulary lookup
        - contract lookup
        - schema lookup
        - standards lookup
        - placement question
        - audit operation
        - classify operation
        - tag operation
      origin: Phase 4.6 P5 plugin-level orientation surface (2026-04-30); re-pointed at the md-domain surfaces (2026-07-29).
      added: "2026-04-30"
      summary: Vocabulary -> md-domain/references/skill-domain/glossary.md. Contract floor -> skills_kit_lib/schema_registry.py. Framework tables -> md-domain/references/skill-domain/framework.md. What good looks like -> md-domain/references/standards/. Decision provenance -> md-domain/references/provenance/ + md-domain/CLAUDE.md. Validator internals -> skills_kit_lib/CLAUDE.md.
      detail: |
        - "What does <term> mean?" -> md-domain/references/skill-domain/glossary.md,
          search the appropriate sub-grouping (files / conventions /
          external_binding / principles / patterns / skill_types / attributes /
          sources). Every record has a keywords cluster for routing.
        - "What are the required keys for type X?" -> skills_kit_lib/schema_registry.py
          (canonical, machine-readable contract floor).
        - "What do the type contracts look like for human review?" ->
          md-domain/references/skill-domain/framework.md tables; schema_registry.py
          wins on divergence.
        - "What does a good SKILL.md / CLAUDE.md / project doc / cross-reference
          look like?" -> md-domain/references/standards/<artifact>-standards.md.
          Same doc for both verbs -- read detecting for an audit, producing for
          generation.
        - "Where should this fact live?" -> md-domain/references/cohesion-principles.md.
          Never re-derive the placement algorithm from memory.
        - "Does this skill satisfy its type contract?" -> run audit.py against the
          SKILL.md path. Zero FAILs is well-formed.
        - "Why does the framework forbid Y / require Z?" ->
          md-domain/references/provenance/ (the dec_N framework decisions and the
          folded skills' histories) for contract rationale;
          md-domain/CLAUDE.md for why the SKILL tree has this shape.
        - "How does the validator decide between yaml-validated / contract-staged /
          legacy-fallback?" -> skills_kit_lib/CLAUDE.md three_audit_states insight.
    - id: merge_gate_convention
      keywords:
        - re-audit gate
        - merge criterion
        - schema change discipline
        - framework change discipline
        - standards change discipline
        - zero fails
      origin: P1 convention generalized to plugin level during P5 (2026-04-30); CLAUDE.md roster updated for the md-domain fold (2026-07-29).
      added: "2026-04-30"
      summary: Any change touching schema_registry.py, the glossary, framework.md, or a standards doc must re-audit every plugins-kit SKILL.md (the plugins/*/skills/*/SKILL.md glob -- the count grows; do not hardcode it) plus the CLAUDE.md files enumerated below, to zero FAILs before shipping.
      detail: |
        The plugin advocates schema validation as the audit substrate. Shipping a
        contract change that breaks the plugin's own skills would violate the
        principle the plugin teaches.

        Re-audit invocation pattern:

          for f in plugins/*/skills/*/SKILL.md \\
                   plugins/skills-kit/skills/md-domain/CLAUDE.md \\
                   plugins/skills-kit/skills_kit_lib/CLAUDE.md \\
                   plugins/skills-kit/CLAUDE.md \\
                   CLAUDE.md; do
            (cd plugins/skills-kit && uv run python -m skills_kit_lib.audit --config "../../$f")
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
        - audit.py degrades gracefully when pyyaml is unavailable (HAVE_YAML False) --
          the contract-staged state; mechanics in skills_kit_lib/CLAUDE.md
          three_audit_states.
    - id: invocation_paths
      keywords:
        - invoke md-domain
        - run audit
        - run classify
        - run tag
        - discover script
        - bootstrap-installed venv python
      origin: Phase 4.6 P5 plugin-level orientation surface (2026-04-30); re-pointed at md-domain (2026-07-29).
      added: "2026-04-30"
      summary: md-domain loads on its trigger (audit or generation intent over project markdown) or via /md-domain; there is no /md-audit or /md-authoring alias. Scripts run via the plugin venv's Python.
      detail: |
        - Skill: /md-domain bare shows the verb x artifact menu; argument dispatch
          is `/md-domain <verb> <artifact> [selector] [flags]`. Natural language
          routes by the verb and artifact named -- each lane record declares its
          invocation_phrasings. The former /md-audit and /md-authoring commands do
          NOT exist as aliases (clean break, 2026-07-29).
        - Scripts: invoke via the plugin venv directly. The bootstrap engine ensures
          the venv exists at ~/.claude/plugins/data/plugins-kit/skills-kit/.venv;
          calling its python.exe runs audit.py / classify.py / tag.py with pyyaml
          available.

          Example (Windows; analogous on Mac/Linux with .venv/bin/python):

          (cd plugins/skills-kit && \\
            ~/.claude/plugins/data/plugins-kit/skills-kit/.venv/Scripts/python.exe \\
            -m skills_kit_lib.audit \\
            <path-to-SKILL.md-or-CLAUDE.md>)

        - The md-domain lane scripts (scripts/discover_*.py, references_audit.py,
          report.py) are stdlib-only entry points invoked by the lanes themselves.
        - Outside the venv (bare system Python): audit.py runs but reports
          judgment-required on the YAML contract row. classify.py and tag.py operate
          on frontmatter and a regex-detected YAML root key; they do not need pyyaml.
    - id: audit_framework_paths_are_cross_plugin_api
      keywords: [audit-framework.md, audit-framework.yaml, cross-plugin consumers, breaking rename, md-domain references, awesome-kit, prototypes, path contract]
      summary: skills/md-domain/references/audit-framework.{md,yaml} are consumed BY PATH from awesome-kit and prototypes -- renaming or moving them is a breaking cross-plugin change requiring consumer version bumps.
      detail: |
        awesome-kit and prototypes reference
        plugins/skills-kit/skills/md-domain/references/audit-framework.md and
        audit-framework.yaml by literal path (the shared audit framework is a
        cross-plugin API surface, not a private reference). Treat any
        rename/move/restructure of those two files like a breaking library
        change: update every consumer in the same release and bump the
        consumers' plugin versions, or do not move the files. Grep
        plugins/awesome-kit and plugins/prototypes for "audit-framework"
        before touching them.

        The md-domain fold is the worked example: the files moved from
        skills/md-audit/references/ to skills/md-domain/references/, and the
        consumers were re-pointed and version-bumped in the same change. No stub
        was left at the old path -- a stub would be the half-migration
        anti-pattern.
      origin: Arch-review finding S19 (2026-06-09); path updated for the md-domain fold (2026-07-29).
      added: "2026-06-10"
  conventions:
    - rule: "Audit workflow lanes pin an explicit model AND effort -- never inherit either from the session: detect/classify lanes set model 'opus' + effort 'high'; remediate lanes set model 'sonnet' + effort 'low'. The remediate defaults live once in scripts/gen_workflow_js.py (the canonical template that generates skills/md-domain/workflow/*-remediate.js); the detect/classify defaults live in each hand-authored skills/md-domain/workflow/*-detect.js and references-classify.js agent() call. A new audit workflow script must follow the same split."
      keywords:
        - workflow lane model
        - opus detect high effort
        - sonnet remediate low effort
        - no inherited effort
        - token cost
        - agent() model default
      why: "Without explicit tiers, every fan-out lane inherits the main-loop session model and effort -- a 20-file audit on a top-tier session is 20 top-tier lanes, mostly wasted, while a low-effort session would silently under-power detection. Each lane declares the RIGHT tier for its work instead: remediation applies already-decided edits (the judgment happened at the Q&A gate), so sonnet at low effort suffices; detection/classification IS the audits' judgment core (CCP/CRP/ADP criteria application), the judge stage that warrants opus at high effort. User directive 2026-07-13 (explicitly: pin the right effort, do not inherit)."
    - rule: "A workflow lane is shipped only after ONE REAL DISPATCH through the Workflow tool. Reading it is not testing it, `node --check` is not testing it, and the text-substring contract tests in tests/skills-kit are not testing it -- all three pass on a lane that dispatches zero agents. Two known unrunnable shapes, each of which defeated the checks that caught the other: (a) a bare `input` instead of `let input = args` (with the JSON.parse guard for the string form), which throws `Error: input is not defined` on the first line; (b) a markdown-style backtick-quoted word in prompt prose inside a template literal, which ends the template early -- node accepts the result as a tagged template on an undefined identifier while the Workflow parser rejects the whole script. Do not treat either enumerated shape as the rule; the rule is the dispatch."
      keywords:
        - input is not defined
        - workflow args binding
        - let input = args
        - lane never ran
        - written not working
        - verify by running
        - one real dispatch
        - backtick in prompt prose
        - tagged template
        - unexpected token
        - node --check passes
        - parse error workflow
        - JSON string args
      why: "coverage-detect.js and hierarchy-detect.js both shipped without the binding and were unrunnable from the day they were published -- every call died before the criteria guard and before the preflight, so no failure surfaced in any report. The defect survived the 0.46.0 publish because the campaign using coverage had worked around it with a plain opus agent handed the same refs.criteria path, which was recorded as an off-pin shortcut rather than as evidence the pinned path was broken. Fixed in 0.46.1; the eight older workflows all had the binding (skill-detect.js:118-123). THEN IT HAPPENED AGAIN: coverage-detect.js shipped unrunnable in 0.47.0 on the backtick shape and was caught only by the first real dispatch (fixed 0.47.1, with a regression test in tests/skills-kit/test_workflow_js_drift.py). That recurrence is why this rule now names the DISPATCH rather than a line to check: the 0.46.1 wording was followed to the letter -- the file did bind `let input = args`, and a session verified exactly that by reading -- which is precisely what made it look tested. The general lesson is the repo's never_hand_make_a_plugins_output rule applied to a lane: a plugin change is verified by the plugin doing the thing."
    - rule: Surface a framework decision as a lessons-learned entry with surface / finding / follow-up provenance before the contract change ships. Land it in skills/md-domain/references/provenance/ (framework and standards decisions), skills/md-domain/CLAUDE.md (decisions about the skill's own shape), or skills_kit_lib/CLAUDE.md (validator-side decisions).
      keywords:
        - provenance
        - decision log
        - lessons-learned
        - surface finding follow-up
      why: A contract change without provenance cannot be rewound. A future agent must be able to reconstruct what audit surface revealed the friction; outcomes alone (the new schema) do not carry that signal.
```
