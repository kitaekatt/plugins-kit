# Skill-embedded enabling

This contract defines how plugin B can enable a consuming skill in plugin A.
It covers optional edges whose consumer has a `SKILL.md` that hosts the probe.
The probe discloses a current capability and adds nothing otherwise.

The audience is an author who adds this edge and a reviewer who checks it. Use
[optional-plugin-dependencies](optional-plugin-dependencies.md) for dependency
declarations, imports, frontier symbols, and the three runtime states. This
document adds the skill host, consent boundary, and disclose-or-silence rule.

## Vocabulary

This document uses the parent's terms: **owner**, **consumer**, **artifact**,
**frontier symbol**, **absent**, **too old**, and **stale after uninstall**. **Enabling**
means that B lets A do more while A remains whole without B. An **enabling script**
discloses a current capability and points to its instructions. **Consent** is the
skill invocation that admits the disclosure. To **advertise** is to answer that probe.

## Criteria

### EN-1 -- Only an optional edge with a consuming skill qualifies

The parent's Q1 must answer yes: the consumer does its job without B. The
consumer must also have a `SKILL.md` whose invocation can host the probe.

A REQUIRED edge does not qualify. A consumer without a `SKILL.md` does not
qualify. Those edges stay entirely under
[optional-plugin-dependencies](optional-plugin-dependencies.md). They never
become silence-on-absence edges.

**Detection.** Answer Q1 from the action's real no-B path. Then locate the
consuming `SKILL.md`. Reject enabling when either check fails.
**Origin.** The survey found REQUIRED edges across the shipped plugins. Job-kit
and yaml-data-editor-kit also have no consuming skill. The table below gives evidence.

### EN-2 -- The consuming skill owns the probe and the invocation grants consent

The probe lives in the consuming skill's `scripts/` directory or in a callable
that its script invokes. The skill body states when to run it and where to put
non-empty stdout. Apply the consent rules below.

**Detection.** Trace every probe caller to the consuming `SKILL.md` or an exact
child-skill invocation that it emits. Identify the command, condition, and stdout destination.
**Origin.** The review skills make their md-domain decision in the invoked flow
(`plugins/git-kit/skills/git-code-review/SKILL.md` and
`plugins/p4-kit/skills/p4-code-review/SKILL.md`). The consent rules are introduced here.

### EN-3 -- Discovery proves that the needed capability is current enough

Use one discovery shape that matches the capability:

1. For a shared library, import the module and probe the frontier symbol.
2. For a skill, find its exact catalog name and probe the needed capability
   heading or entry point.
3. For a CLI, resolve it on `PATH` and probe the needed subcommand, flag, or
   versioned entry point.

The first two shapes exist in shipped instances. The CLI shape is introduced here.

"Too old" depends on the discovery shape:

- A shared library imports but lacks the frontier symbol.
- A skill resolves but lacks the needed heading or lane entry point.
- A CLI resolves but lacks the needed subcommand, flag, or entry point.

Treat stale-after-uninstall state as not current enough. Module presence alone
is not proof. Use the parent's discovery, import, and frontier mechanics.

**Detection.** List every capability that the enabled path uses. The probe must
cover the newest one. Remove or age it in a fixture and confirm that stdout is empty.
**Origin.** Orchestrate probes llm-scripting-kit entry points
(`plugins/awesome-kit/skills/orchestrate/scripts/orchestration_guidance.py`). The
review skills probe md-domain lane files after catalog discovery
(`plugins/git-kit/skills/git-code-review/references/md-domain-review.md` and
`plugins/p4-kit/skills/p4-code-review/references/md-domain-review.md`).

### EN-4 -- Present discloses, absent and too old are silent

Apply "Permitted disclosure" and "Silence, scope, and revocation" below.
Present stdout discloses the capability and points to its instructions. Absent
and too-old stdout is empty.

**Detection.** Capture stdout for present, absent, and too-old fixtures. Count
lines, resolve the pointer, and inspect for procedures. Confirm distinct diagnostics.
**Origin.** Catalog presence activates the md-domain claim in both review skills
(`plugins/git-kit/skills/git-code-review/SKILL.md` and
`plugins/p4-kit/skills/p4-code-review/SKILL.md`). The three-line ceiling and
diagnostics location are introduced here.

### EN-5 -- Artifact truth decides whether silence is valid

Enabling is not a fourth branch beside REQUIRED, REFUSE, and DEGRADE. It is the
silent sub-case of DEGRADE where B's absence leaves the artifact true as read.

This is the load-bearing rule: the parent's Q2 requires an in-artifact disclosure
when B's absence makes the artifact read false. Enabling is the narrower true-artifact case.

A rendered policy that claims to list every backend must disclose a missing
registry. That case remains DEGRADE WITH DISCLOSURE under the parent contract.
A `Consult seats` section that does not appear makes no claim. That case uses
silent enabling.

**Detection.** Read the no-B artifact without outside information. If it implies
that B participated or that an incomplete list is complete, apply the parent contract.
**Origin.** Orchestrate's registry render diagnoses absent or too-old
state in its policy. The policy describes its backend set
(`plugins/awesome-kit/skills/orchestrate/scripts/orchestration_guidance.py` and
`plugins/awesome-kit/skills/orchestrate/references/configuration.md`). The silent
sub-case is introduced here and is induced from the md-domain claim probe.

### EN-6 -- Consent ends with the skill invocation

Apply "Silence, scope, and revocation" below. Run a fresh probe for each
invocation. Hooks, startup paths, and unprompted renderers cannot call it.

**Detection.** Search for every probe caller and every cache read or write.
Confirm that no hook or startup path reaches the probe. Invoke the skill twice
with B removed between calls and confirm that the second call is silent.
**Origin.** Invocation-scoped revocation and the no-cache rule are introduced here.

### EN-7 -- Every enabling edge proves all four rungs

Each enabling edge has tests for these states:

1. **Present.** Stdout has one to three non-empty lines. The capability and
   pointer are accurate.
2. **Absent.** Stdout is byte-empty. Diagnostics identify absence.
3. **Too old.** Stdout is byte-empty. Diagnostics identify the missing
   capability, not absence.
4. **Stale after uninstall.** A stale path or entry can still resolve, but
   stdout is byte-empty because the capability probe fails.

For a shared library, use the parent's `sys.modules` fixtures for absent and
too-old states. Add a stale fixture in which an old module still imports. For
a catalog skill, keep the skill name and remove its capability heading or lane
entry point. For a CLI, keep the executable on `PATH` and remove the required
CLI capability.

Also render the no-B artifact and apply EN-5. The test must show that the
artifact remains true without an absence disclosure.

**Detection.** Locate one test for each numbered state and one no-B artifact
assertion. Confirm that absent and too-old tests make distinct diagnostic
assertions while both require empty stdout.
**Origin.** The four rungs extend the parent's absent and too-old test pattern
with its documented stale-after-uninstall state. Catalog capability fixtures
come from the md-domain review fallbacks. CLI fixtures are introduced here.

## Consent

Consent is a scoped opt-in to a skill's way of operating. Installation alone
does not admit an enabling disclosure into context.

### What grants consent

- A user-invoked skill grants consent to the enabling scripts embedded in that
  skill for that invocation.
- A Claude-invoked skill also qualifies when the current request or a loaded
  instruction file permits that invocation. The invocation does not create or
  enlarge that permission.
- Consent follows an exact child-skill invocation emitted by a consented skill
  as part of the same operation. Skills emitted by `task work` qualify because
  the user selected the workflow that emits them.

### What does not grant consent

SessionStart hooks, UserPromptSubmit injections, and unprompted rendering do
not grant consent because no skill invocation selected them. Running a probe
"just in case" also does not qualify.

Bootstrap's SessionStart output does not receive consent under this model. Its
separate fix-all and notice rules remain out of scope.

### Permitted disclosure

An enabling script can advertise only after it detects the capability and the
discovery-shape evidence that it is current enough: a frontier symbol, a capability
heading or lane entry point, or a CLI subcommand, flag, or entry point.

The complete injected output must contain no more than three non-empty lines.

The output can state that the capability exists and point to its how-to. A
pointer can name a skill, a reference path, or a CLI `--help` command. The
pointer must resolve in the detected capability.

The output must contain no procedure, option catalog, example, or copied manual
text.

### Silence, scope, and revocation

An absent or too-old capability produces zero disclosure lines. The injection
never renders "B is not installed" or an equivalent message.

The probe must distinguish absent from too old in its log or through
`--explain`. That distinction never enters the injected disclosure. This rule
uses the three-state model in
[optional-plugin-dependencies](optional-plugin-dependencies.md).

This silence applies only to enabling advertisements. It does not replace
artifact disclosures required by
[optional-plugin-dependencies](optional-plugin-dependencies.md).

Consent ends when the invocation that granted it ends. A later invocation must
run its own probe.

Probe output must not be cached for reuse across invocations or sessions. When
a skill stops being invoked, its enabling scripts stop admitting disclosures.

### Reviewer checks for consent

- Trace every probe caller. A hook, startup path, or unprompted renderer that
  reaches the probe is a consent violation.
- Test present, absent, and too-old states. Absent and too-old stdout must be
  empty, while diagnostics must distinguish them.
- Count non-empty output lines and inspect their content. More than three
  lines, embedded how-to, or reused output violates this contract.

## Worked instance: consult seats

The first deliberate design uses llm-scripting-kit as the owner and
`awesome-kit:orchestrate` as the consuming skill. Its owner contract is a
`discover_seats` callable. Each classified entry supplies tier and family.

The enabling probe emits no more than three lines that disclose the callable and
point to its use. The consuming script can then call it and render a `Consult
seats` section. It classifies entries as UP or BESIDE relative to the current
agent. An unclassified entry is never BESIDE.

For absent, too-old, or stale owner state, the probe emits no disclosure and
the section does not appear. The remaining orchestration policy stays true
because it does not claim that a missing `Consult seats` section lists every
available backend.

This instance evolves the script-side registry detection without
depending on its exact implementation
(`plugins/awesome-kit/skills/orchestrate/scripts/orchestration_guidance.py`).
The registry renderer's disclosed degradation remains the contrast case under
EN-5
(`plugins/awesome-kit/skills/orchestrate/references/configuration.md`).

## Applying the contract to the shipped instances

| Surveyed instance | Restatement in contract terms | Finding |
|---|---|---|
| skills-kit -> git-kit md-domain claim | The owner skill is discovered by exact catalog name. The consuming review skill conditionally claims Markdown and uses md-domain lane entry points. (`plugins/git-kit/skills/git-code-review/SKILL.md`, `plugins/git-kit/skills/git-code-review/references/md-domain-review.md`) | Closest exact probe fit. The enabling advertisement is silent when absent. Any review-artifact degradation note remains under the parent's Q2. |
| skills-kit -> p4-kit md-domain claim | The owner skill is discovered by exact catalog name. The consuming review skill conditionally claims Markdown and uses md-domain lane entry points. (`plugins/p4-kit/skills/p4-code-review/SKILL.md`, `plugins/p4-kit/skills/p4-code-review/references/md-domain-review.md`) | Closest exact probe fit. It has the same two-layer contract shape as git-kit. |
| llm-scripting-kit -> awesome-kit orchestrate registry render | The consumer script probes owner symbols before it adds registry rows. Agent-tool rows remain without the owner. (`plugins/awesome-kit/skills/orchestrate/scripts/orchestration_guidance.py`, `plugins/awesome-kit/skills/orchestrate/references/configuration.md`) | Near-fit only. The rendered policy claims completeness, so absent and too-old states need an in-artifact disclosure under the parent's Q2. |
| llm-scripting-kit -> git-kit and p4-kit endpoint lanes | Each review runner distinguishes owner absence from missing frontier symbols and REFUSES the configured lane. (`plugins/git-kit/scripts/run_review_lane.py`, `plugins/p4-kit/scripts/run_review_lane.py`) | Does not fit. A requested lane cannot disappear silently while the review remains true as read. The parent REFUSE branch applies. |
| llm-scripting-kit -> workflow-kit OpenRouter node | The runner directly imports the owner for the requested node. (`plugins/workflow-kit/bootstrap.json`, `plugins/workflow-kit/scripts/openrouter_run.py`, `plugins/workflow-kit/skills/workflow-kit/SKILL.md`) | Does not fit. The requested node cannot be omitted, and the runner has no frontier probe. |
| llm-scripting-kit -> job-kit | The package has a required owner import at its front door. Its selector diagnoses missing frontier symbols. (`plugins/job-kit/bootstrap.json`, `plugins/job-kit/lib/job_kit/select.py`, `plugins/job-kit/lib/job_kit/__init__.py`) | Does not fit. The edge is REQUIRED and job-kit has no consuming skill host. |
| llm-scripting-kit and bootstrap -> content-pipeline-kit live backends | Live backend call sites import required owner APIs. The skill contains unconditional preflight pointers. (`plugins/content-pipeline-kit/bootstrap.json`, `plugins/content-pipeline-kit/skills/content-pipeline-domain/SKILL.md`, `plugins/content-pipeline-kit/lib/content_pipeline/llm/backends.py`) | Does not fit. The live backend unit is required, and the pointers are not disclose-or-silence probes. |
| skills-kit and bootstrap -> awesome-kit task and helper scripts | The task system hard-imports skills-kit APIs. Awesome-kit also ships bootstrap guard modules. (`plugins/awesome-kit/bootstrap.json`, `plugins/awesome-kit/skills/task/scripts/task_system/task_items.py`, `plugins/awesome-kit/skills/orchestrate/scripts/bootstrap_guard.py`) | Does not fit. These are REQUIRED support edges, not optional skill disclosures. |
| content-pipeline-kit -> yaml-data-editor-kit | Editor dispatch code hard-imports content-pipeline APIs. (`plugins/yaml-data-editor-kit/bootstrap.json`, `plugins/yaml-data-editor-kit/lib/yaml_data_editor_kit/dispatch/background.py`, `plugins/yaml-data-editor-kit/lib/yaml_data_editor_kit/dispatch/worker_mount.py`) | Does not fit. The edge is REQUIRED and the consumer has no `SKILL.md`. |
| bootstrap -> git-kit and p4-kit review infrastructure | Both review systems use provisioning guards before required review scripts. (`plugins/git-kit/bootstrap.json`, `plugins/git-kit/scripts/prepare_review.py`, `plugins/p4-kit/bootstrap.json`, `plugins/p4-kit/scripts/prepare_review.py`) | Does not fit. Bootstrap is required review infrastructure. |
| bootstrap -> llm-scripting-kit and unreal-kit | Llm-scripting-kit imports bootstrap configuration helpers. Unreal helpers import bootstrap configuration and path helpers. (`plugins/llm-scripting-kit/bootstrap.json`, `plugins/llm-scripting-kit/lib/llm_scripting_kit/models.py`, `plugins/unreal-kit/bootstrap.json`, `plugins/unreal-kit/lib/unreal_stub.py`, `plugins/unreal-kit/custom_bootstrap.py`) | Does not fit. These are required infrastructure edges, not optional consuming-skill probes. |

`awesome-kit:task` emitting `Skill(awesome-kit:orchestrate)` is a skill handoff
inside one plugin. It is not a cross-plugin enabling edge.

## Anti-patterns

- **Manual injection.** Keep stdout to the disclosure and pointer. Put all
  procedure text in the referenced owner material.
- **Rendered absence.** Emit zero stdout for absent and too-old capabilities.
  Put their distinct diagnoses in `--explain` or a log.
- **Hook probe.** Call the probe only from the consented skill invocation.
- **Cached probe.** Run it again for each invocation.
- **Hard dependency for simplicity.** Keep Q1-optional owners out of the
  consumer's required plugin dependencies.

## Reviewer checklist

For every enabling edge that a change adds or edits:

- [ ] EN-1 passes: Q1 answers yes and a consuming `SKILL.md` hosts the probe.
- [ ] The probe lives in the skill's `scripts/` directory, or that script calls
      the probe callable.
- [ ] The skill body states when to run the probe and where to place stdout.
- [ ] Every caller is downstream of a consented skill invocation.
- [ ] Discovery uses the parent mechanics and probes the newest required
      symbol, heading, entry point, subcommand, or flag.
- [ ] Present stdout contains only a disclosure and a resolvable pointer.
- [ ] Present stdout has no more than three non-empty lines.
- [ ] Absent and too-old stdout are byte-empty. No absence message enters the
      skill context.
- [ ] `--explain` or a log distinguishes absent from too old.
- [ ] The no-B artifact passes EN-5. If it does not, apply the parent's REFUSE
      or DEGRADE WITH DISCLOSURE branch.
- [ ] Tests cover present, absent, too old, and stale after uninstall.
- [ ] No probe output is cached across invocations or sessions.
- [ ] No hook, startup path, or unprompted renderer reaches the probe.

## Relationship to optional plugin dependencies

[Optional plugin dependencies](optional-plugin-dependencies.md) remains the
source of truth for REQUIRED, REFUSE, DEGRADE, shared-library mechanics,
frontier probing, remedies, and artifact disclosure. Enabling adds no manifest
and no owner-published advertisement layer. It defines only the skill-embedded,
silent sub-case of DEGRADE that passes Q2.
