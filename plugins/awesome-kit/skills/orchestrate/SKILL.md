---
name: orchestrate
description: Use when accomplishing significant multi-part work -- delegate to background agents to preserve main-agent context. Do NOT use for quick single-step tasks.
skill-type: technique-skill
---

# orchestrate

Take on the orchestrator role: accomplish significant work by delegating it to background
agents, keeping the main agent's context reserved for coordination, judgment, and synthesis.
The economics: an agent's *results* hold most of the value while *generating* them consumes
most of the context and tool calls -- so generation belongs in background agents and only the
compressed conclusions come home. Agent-tool mechanics (report visibility, follow-up via
SendMessage, parallel launch, worktree isolation) are already in the harness prompt every
session; this skill adds the economics and the procedure, not the mechanics.

```yaml
technique_skill:
  _schema_version: "1"
  identity: Orchestrate significant work through background agents so the main agent's context holds conclusions, not work product.
  scope:
    covers:
      - decomposing significant work into delegable units and running them via background agents
      - per-unit model-tier selection and the usage-pool economics behind it
      - keeping the main context clean while agents run, and synthesizing results on completion
    excludes:
      - small or single-step tasks cheaper to do inline than to delegate
      - the Workflow tool's deterministic multi-agent orchestration (use Workflow when the user opts in)
      - subagent authoring (defining new agent types)

  techniques:
    - id: orchestrate
      name: Orchestrate work through background agents
      keywords: [orchestrator, background agents, delegate, preserve context, fan-out, parallel agents, synthesize results]
      goal: Complete a significant task with the main context holding coordination state and conclusions, not raw work product.
      checklist:
        - "[ ] 1 warrants orchestration"
        - "[ ] 2 units decomposed and classified"
        - "[ ] 3 model tier picked per unit"
        - "[ ] 4 agents launched"
        - "[ ] 5 orchestrator-only work while running"
        - "[ ] 6 results synthesized"
        - "[ ] 7 substance relayed to user"
      steps:
        - n: 1
          action: Confirm the task warrants orchestration.
          detail: |
            Significant = multiple independent work units, or units whose execution burns far more
            context/tool calls than their conclusions are worth. One focused unit finishable inline
            in a few calls stays inline -- delegation overhead loses.
        - n: 2
          action: Decompose into self-contained units and classify each.
          detail: |
            Per unit note (a) dependencies -- independent units run in parallel, dependent ones
            sequence; (b) compression profile -- does the result compress to a small conclusion?
            High-generation-cost / small-conclusion units are the ideal delegations.
        - n: 3
          action: Pick a model tier per unit from the model_selection block.
        - n: 4
          action: Launch background agents -- each prompt a standalone brief (goal, paths, constraints, return shape).
        - n: 5
          action: While agents run, do orchestrator-level work only (plan synthesis, inline units, or wait).
        - n: 6
          action: Synthesize completed results; cross-check units that disagree before accepting either.
        - n: 7
          action: Relay the substance -- findings, decisions, verified-vs-reported -- in your final message.
      gotchas:
        - Delegating then redoing the work inline pays both costs; once dispatched, wait for the result.
        - Parallel agents editing the same files clobber each other -- use worktree isolation or sequence them.

  model_selection:
    keywords: [model choice, model tier, usage pool, separate pool, cost, cheap delegation]
    default: workhorse
    note: Tiers are the durable guidance; concrete names come from the Agent tool's current model enum -- the names below are examples of the current lineup, not the contract.
    tiers:
      - tier: cheapest
        example: haiku
        use_for: trivial mechanical fan-out -- bulk renames, file-by-file checks with a fixed rubric.
      - tier: workhorse
        example: sonnet
        use_for: the default -- searches, well-specified implementation, summarization, most delegated units.
      - tier: high-reasoning
        example: opus
        use_for: units where reasoning quality is the limiting factor -- tricky debugging, design judgment, nuanced review.
      - tier: top
        example: fable
        use_for: |
          only when the unit genuinely warrants top-tier judgment AND its result compresses well --
          the conclusion carries most of the value while generating it would consume heavy context
          and tool calls in the main session. Efficiency, not prestige, justifies the top tier.
    pool_economics: |
      When the orchestrating model is fable: opus/sonnet background agents draw from a SEPARATE
      usage pool than fable usage. Delegating to them is therefore particularly inexpensive --
      it both preserves top-tier context AND spends the cheaper pool. Bias strongly toward
      delegation in that configuration; work the orchestrator keeps inline should earn its
      top-tier tokens (coordination, synthesis, judgment calls).

  anti_patterns:
    - id: top_tier_everywhere
      name: Top-tier agents by default
      keywords: [fable default, model overkill, expensive fan-out]
      why_it_seems_right: The best model should give the best results on every unit.
      why_it_is_wrong: Most delegated units are workhorse-shaped; top-tier agents spend the premium pool on work that doesn't need it and forfeit the separate-pool discount.
      alternative: Default workhorse, escalate per unit; reserve the top tier for units passing the warrants-it-AND-compresses-well test.
    - id: orchestrator_does_the_work
      name: Orchestrator absorbs the work product
      keywords: [context bloat, reading everything, inline generation]
      why_it_seems_right: Reading all the raw output yourself feels more thorough than trusting summaries.
      why_it_is_wrong: It defeats the entire point -- the main context fills with generation-cost material whose value was already captured in the agents' conclusions.
      alternative: Ask agents for structured conclusions; pull raw detail only for the specific items you must verify or that agents disagreed on.
    - id: vague_dispatch
      name: Under-specified agent prompts
      keywords: [vague prompt, missing context, wrong question]
      why_it_seems_right: The task is obvious from the conversation, so a one-liner should do.
      why_it_is_wrong: The agent never saw the conversation; it fills gaps with guesses and returns polished answers to a different question.
      alternative: Write each prompt as a standalone brief -- goal, paths, constraints, and the exact shape of the answer you want back.
```
