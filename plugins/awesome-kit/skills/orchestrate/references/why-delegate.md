# Why delegate

Rationale behind the orchestrate procedure and its anti-pattern catalogue. Not needed to
execute the skill -- see [SKILL.md](../SKILL.md) for the procedure itself, and
[codex-dispatch.md](codex-dispatch.md) / [configuration.md](configuration.md) for backend
mechanics and config schema respectively.

## The economics

An agent's *results* hold most of the value while *generating* them consumes most of the
context and tool calls -- so generation belongs in a background unit and only the compressed
conclusions come home. Agent-tool mechanics are already in the harness prompt every session;
what this skill adds is the economics, the procedure, and -- through the rendered policy --
the machine's own dispatch options and their mechanics.

## Anti-patterns

```yaml
anti_patterns:
  - id: top_tier_everywhere
    name: Top-tier agents by default
    keywords: [fable default, model overkill, expensive fan-out]
    why_it_seems_right: The best model should give the best results on every unit.
    why_it_is_wrong: Most delegated units are workhorse-shaped; top-tier agents spend the premium pool on work that doesn't need it.
    alternative: Take the default tier from the rendered policy and escalate per unit against its stated criteria.
  - id: remembered_policy
    name: Choosing models and backends from memory
    keywords: [skipped the script, hardcoded tier table, stale model names, assumed agent tool, ignored override]
    why_it_seems_right: The tier lineup and dispatch mechanics feel like stable background knowledge, so running a script to restate them looks like ceremony.
    why_it_is_wrong: >-
      The policy is per-user and per-machine: a user may have retargeted tiers, added a
      backend this skill has never heard of, disabled one, or marked a tier unavailable
      because its usage is spent. Answering from memory silently ignores every one of
      those and produces confident dispatch to something that is wrong or gone.
    alternative: Run the policy script at step 3; it is deterministic and sub-second.
  - id: orchestrator_does_the_work
    name: Orchestrator absorbs the work product
    keywords: [context bloat, reading everything, inline generation]
    why_it_seems_right: Reading all the raw output yourself feels more thorough than trusting summaries.
    why_it_is_wrong: It defeats the entire point -- the main context fills with generation-cost material whose value was already captured in the agents' conclusions.
    alternative: Ask agents for structured conclusions; pull raw detail only for the specific items you must verify or that agents disagreed on.
  - id: inline_footprint_work
    name: Doing reads-a-lot / emits-a-lot work inline in the orchestrating context
    keywords: [inline work, context footprint, quick edit, difficulty axis]
    why_it_seems_right: "It's quick, I'm already here, and dispatching an agent costs a prompt and a relay."
    why_it_is_wrong: Sessions run hundreds of messages; every file read and diff emitted inline stays in the orchestrating context for all of them. Difficulty and duration are the wrong axis -- persistent context footprint is.
    alternative: Classify by shape in one glance (step 1); reads-a-lot or emits-a-lot goes to a background agent even when it is easy.
  - id: vague_dispatch
    name: Under-specified agent prompts
    keywords: [vague prompt, missing context, wrong question]
    why_it_seems_right: The task is obvious from the conversation, so a one-liner should do.
    why_it_is_wrong: The agent never saw the conversation; it fills gaps with guesses and returns polished answers to a different question.
    alternative: Write each prompt as a standalone brief -- goal, paths, constraints, and the exact shape of the answer you want back.
```
