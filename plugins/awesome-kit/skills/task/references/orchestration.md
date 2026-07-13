# Orchestration

How the orchestrating session dispatches task work to background agents: the
delegation rule and the model-routing table. This is the canonical home for
both; the task skill's dispatch touchpoints (`work` step 2's `agent_hint`
dispatch, `status` step 2's background summarizer) apply it rather than
restating it.

## The delegation rule

**Delegate by context footprint, not difficulty. Work that reads a lot or
emits a lot goes to a background agent; a small self-contained op whose
result the next orchestration decision needs stays inline.**

The orchestrating context lives for hundreds of messages; every file read,
diff, and command dump done inline stays in it for all of them. That
persistent footprint is the deciding axis -- not difficulty or duration. A
hard one-line decision is fine inline; an easy grep across the tree is not.
This is the `status` verb's main-context-preservation rationale generalized
to all work.

Classify by shape, in one glance:

- **Delegate** -- the work reads content you will not need verbatim later, or
  emits bulk (diffs, logs, long listings, drafts). Anything shaped "read a
  bunch, conclude a little" or "produce a bunch."
- **Inline** -- the op is small AND self-contained AND its result feeds the
  very next orchestration decision (a task.py verb, a one-line file check, a
  single field edit). Dispatching these costs about as much context (prompt
  plus relay) as doing them, and adds latency.
- **Do not deliberate.** If classifying takes more thought than the two
  bullets above, treat it as delegate-shaped and dispatch. A per-action "how
  expensive will this be" estimate is itself context spend this rule exists
  to avoid.

Orchestration proper always stays in the main context: invoking task.py
verbs, launching and messaging agents, reading agent return values, answering
the user, and writing the final synthesis.

### Rationalizations

| Excuse | Reality |
|---|---|
| "This is just a quick edit -- faster to do it inline." | Quick is not small. If it means opening files and emitting diffs, that output sits in the context for the hundreds of messages that follow. Shape says delegate. |
| "I need to read these files anyway to know what to dispatch." | Scoping a dispatch needs the work-list, not the content. Delegate the reading (information gathering -> sonnet) and dispatch from the returned summary. |
| "One agent is enough; I'll do the rest myself while it runs." | The rest is also footprint. Independent delegate-shaped work goes out as parallel agents in one message, not orchestrator-plus-one. |
| "The rule says delegate, so this one-line check goes to an agent too." | The opposite failure. An agent round-trip for a small self-contained op costs as much context as doing it inline -- and you wait for it. Inline it. |
| "Let me work out how heavy this will be first." | That estimate is the cost. One glance at the shape (reads-a-lot / emits-a-lot vs small-and-self-contained), then move. |

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
