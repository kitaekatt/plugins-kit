# Why delegate

Rationale behind the orchestrate procedure and its anti-pattern catalogue -- why the
procedure is shaped this way, not how to run it. Nothing here is needed to execute the
skill. See [codex-dispatch.md](codex-dispatch.md) and [configuration.md](configuration.md)
for backend mechanics and the config schema respectively.

## The economics

An agent's *results* hold most of the value while *generating* them consumes most of the
context and tool calls -- so generation belongs in a background unit and only the compressed
conclusions come home. Agent-tool mechanics are already in the harness prompt every session;
what this skill adds is the economics, the procedure, and -- through the rendered policy --
the machine's own dispatch options and their mechanics.

## Anti-patterns

```yaml
anti_patterns:
  - id: premium_model_everywhere
    name: Premium models by default
    keywords: [fable default, model overkill, expensive fan-out]
    why_it_seems_right: The best model should give the best results on every unit.
    why_it_is_wrong: Most delegated units are routine-shaped; premium models spend the premium pool on work that does not need it.
    alternative: Take the default model from the rendered routing policy and escalate per unit against its stated shape.
  - id: remembered_policy
    name: Choosing models and backends from memory
    keywords: [skipped the script, hardcoded model table, stale model names, assumed agent tool, ignored override]
    why_it_seems_right: The model lineup and dispatch mechanics feel like stable background knowledge, so running a script to restate them looks like ceremony.
    why_it_is_wrong: >-
      The policy is per-user and per-machine: a user may have retargeted models, added a
      backend this skill has never heard of, or disabled one
      because its usage is spent. Answering from memory silently ignores every one of
      those and produces confident dispatch to something that is wrong or gone.
    alternative: Run the policy script before decomposing; it is deterministic and sub-second.
  - id: orchestrator_does_the_work
    name: Orchestrator absorbs the work product
    keywords: [context bloat, reading everything, inline generation]
    why_it_seems_right: Reading all the raw output yourself feels more thorough than trusting summaries.
    why_it_is_wrong: It defeats the entire point -- the main context fills with generation-cost material whose value was already captured in the agents' conclusions.
    alternative: Ask agents for structured conclusions; pull raw detail only for the specific items you must verify or that agents disagreed on.
  - id: active_waiting
    name: Polling, sleeping, or narrating while units run
    keywords: [still running, poll result file, sleep, standing by, idle turn, cat result]
    why_it_seems_right: >-
      Checking the result file or reporting "still waiting" feels attentive, and the
      gotcha says to wait for a dispatched result.
    why_it_is_wrong: >-
      A background unit re-invokes the orchestrator when it completes. Every poll,
      sleep, or status-only turn before that is a full context read that returns
      nothing, and in the measured corpus they cluster: one empty check begets the
      next.
    alternative: >-
      End the turn when nothing is unblocked. If useful work might be unblocked, name
      the marked premise that the pending result could refute. If there is none,
      proceed.
  - id: stalling_on_a_grant
    name: Queuing for an absent user a decision that was already the agent's
    keywords: [stalling, waiting for the user, ask before proceeding, queued question, autonomy grant, claimed decision, user is afk]
    why_it_seems_right: >-
      Deferring feels careful, and putting the call to the user reads as
      respect for their authority.
    why_it_is_wrong: >-
      Where the consumer's CLAUDE.md declares the grant, it was already held --
      there the user's role is product and the agent's is trusted implementor;
      a question queued for a `user-afk` user stops the work and the user reads
      the decision line later anyway; the independence a consult buys comes
      from a DIFFERENT model, not from waiting. Declining a declared grant is
      the failure the autonomy reading exists to prevent.
    alternative: >-
      Consult an independent seat -- route the decision as an `open` +
      `load-bearing` unit, `cross-check` when you already hold an answer --
      take its answer, record `decision <id>: seat=<model>; chose=<option>;
      reason=<one clause>`, and keep going. Only a decision the user has
      CLAIMED, or product direction, waits for the user.
  - id: inline_footprint_work
    name: Doing reads-a-lot / emits-a-lot work inline in the orchestrating context
    keywords: [inline work, context footprint, quick edit, difficulty axis]
    why_it_seems_right: "It's quick, I'm already here, and dispatching an agent costs a prompt and a relay."
    why_it_is_wrong: Sessions run hundreds of messages; every file read and diff emitted inline stays in the orchestrating context for all of them. Difficulty and duration are the wrong axis -- persistent context footprint is.
    alternative: Classify by shape in one glance (step 1); reads-a-lot or emits-a-lot goes to a background agent even when it is easy.
  - id: parallelism_by_unit_count
    name: Splitting because several edits exist
    keywords: [parallelism, implementation shards, unit count, merge overhead]
    why_it_seems_right: More workers should finish any multi-file change sooner.
    why_it_is_wrong: Repeated orientation, overlapping ownership, serial dependencies, and integration can make the split slower than one implementation unit.
    alternative: Apply the rendered parallel-development razor; launch only admitted leaves on the current dependency frontier.
  - id: vague_dispatch
    name: Under-specified agent prompts
    keywords: [vague prompt, missing context, wrong question]
    why_it_seems_right: The task is obvious from the conversation, so a one-liner should do.
    why_it_is_wrong: The agent never saw the conversation; it fills gaps with guesses and returns polished answers to a different question.
    alternative: Write each prompt as a standalone brief -- goal, paths, constraints, and the exact shape of the answer you want back.
  - id: asking_a_routable_call
    name: Putting a routable decision to the user
    keywords: [user's call, confirm before proceeding, ending the turn with a question, proposing instead of doing, autonomy level, which tier]
    why_it_seems_right: The decision is nominally the user's -- their repository, their product -- and asking costs one turn while a wrong guess could cost more.
    why_it_is_wrong: >-
      The user's attention is the scarce resource the whole procedure protects, and a turn
      that ends in a question spends it on a call a seat could have made. Most such calls
      carry no escalation term and were the orchestrator's to make; the rest route to a
      model seat by the same tests as any unit. Asking also stalls every unit briefed from
      the answer. Stalling to determine the "right" tier first is the same failure at one
      remove.
    alternative: >-
      Short of an autonomy edge, make the call, route the call by its terms, and act on
      the ruling; reach the user only at an edge -- most often a directional question or a
      gated action.
  - id: overruling_the_seat
    name: Setting aside a ruling the policy routed to a seat
    keywords: [accept the risk, disagree with review, HIGH finding, overrule reviewer, re-ask another model, second-opinion shopping]
    why_it_seems_right: The orchestrator holds the whole context and the finding looks wrong or disproportionate; accepting the risk inline keeps the work moving.
    why_it_is_wrong: >-
      The routing tree sent the call to that seat because the orchestrator was the wrong
      one to make it -- `authored-here`, `load-bearing`, or both. Deciding around the
      verdict silently restores the seat the policy removed, and the join line then records
      a rejection the transcript cannot attribute. Re-asking a different seat until one
      agrees is the same move with more steps. Observed: a review returned FAIL with a HIGH
      finding, the orchestrator accepted the risk unilaterally, and the counter-argument
      was never tested by anyone but its author.
    alternative: >-
      Put the counter-argument to the seat that ruled -- follow-up channel, or a relaunch
      with the argument in the brief -- and take what comes back (step 7).
```
