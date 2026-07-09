# Orchestration

How the orchestrating session dispatches task work to background agents: the
delegation rule and the model-routing table. This is the canonical home for
both; the task skill's dispatch touchpoints (`work` step 2's `agent_hint`
dispatch, `status` step 2's background summarizer) apply it rather than
restating it.

## The delegation rule

**The orchestrator does no task work. Every task is delegated to a background
agent.**

The main context's job is orchestration only: decompose the work, dispatch
agents, monitor their results, and synthesize/relay the outcome to the user.
Task work executed inline -- edits, analysis, research, test runs -- pays its
full token cost in the orchestrating context and serializes work that agents
could run in parallel. This is the same main-context-preservation rationale
the `status` verb is built on, applied to all work rather than just
summarization.

Not task work (stays in the main context): invoking task.py verbs, launching
and messaging agents, reading agent return values, answering the user, and
writing the final synthesis.

### Rationalizations

| Excuse | Reality |
|---|---|
| "This is just a quick edit -- faster to do it inline." | Quick edits compound; ten inline "quick" tasks fill the orchestrating context with diffs and file reads. The dispatch costs seconds; the context is gone for the session. |
| "I need to read these files anyway to know what to dispatch." | Scoping a dispatch needs the work-list, not the content. Delegate the reading (information gathering -> sonnet) and dispatch from the returned summary. |
| "One agent is enough; I'll do the rest myself while it runs." | The rest is also task work. Dispatch it too -- independent agents launched in parallel finish sooner than orchestrator-plus-one. |

## Model routing

Every agent dispatch names a `model` explicitly. Route by task shape:

| Model | Route when the task is |
|---|---|
| `fable` | deep analysis or complex coding |
| `sonnet` | information gathering or simple analysis |
| `haiku` | trivial operations (mechanical renames, single-file lookups, formatting) |
| `opus` | everything else (the default when no row above clearly fits) |

Omitting `model` silently inherits the session model, which skips the routing
-- treat a model-less dispatch as a red flag, not a neutral default.

## Dispatch mechanics

- **Background by default.** Agents run in the background; the orchestrator is
  notified on completion. Stay foreground (`run_in_background: false`) only
  when the very next orchestration decision needs the result.
- **Parallel launches in one message.** Independent agents go out as multiple
  Agent calls in a single message, not one per turn.
- **Relay results.** An agent's final message returns to the orchestrator; the
  user never sees it. Restate what matters in the reply to the user.
- **Continue, don't respawn.** To follow up with an agent that already has the
  context, use SendMessage with its ID; a new Agent call starts cold.
