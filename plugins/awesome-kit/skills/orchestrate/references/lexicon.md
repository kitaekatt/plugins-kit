# Orchestration lexicon

The controlled vocabulary for orchestration decisions. `[skill]` terms select a
branch in the rendered decision tree and are the only terms permitted in
dispatch announcements. `[concept]` terms justify or describe a choice already
made and never render.

`render: bare` terms appear as-is; `render: glossed` terms carry the
`**Gloss:**` line below as a short parenthetical at first occurrence, bare
thereafter.

---

## Brief shape -- `known` / `open`

Two orthogonal axes govern briefs. This is one; `sufficient` is the other.

### `known` `[skill]` `render: glossed`
The work can be described completely before it starts.
**Test:** can you write what *done* looks like without doing the work?
**Gloss:** "you can describe what done looks like without doing it"

### `open` `[skill]` `render: glossed`
Describing the work is doing the work. Discovery-shaped.
**Test:** would specifying it require already having the answer?
**Gloss:** "specifying it would require already having the answer"

*Consequence:* `known` work takes a SPECIFICATION brief; `open` work takes a
QUESTION brief. This is the only thing shape determines.

## Brief sufficiency -- `sufficient`

### `sufficient` `[skill]` `render: glossed`
The one-sentence statement of the task is already an unambiguous brief.
**Test:** is there any ambiguity about what *done* looks like?
**Gloss:** "the one-sentence statement is already unambiguous"

### `underspecified` `[skill]` `render: bare`
The negative pole of `sufficient`: a brief must be authored before dispatch.
**Test:** the negation of `sufficient`.
*Why it exists:* announcements are terms-only, so a unit routed by the ABSENCE of
sufficiency needs a label. Without it that branch cannot be announced.

---

## Execution demand

What the work does to information. Supplies a signal used by routing rows. Replaces the
undefined pair `shallow` / `real reasoning`.

### `mechanical` `[concept]`
Execution is checkable by inspection alone.
**Test:** can you verify the result without reading surrounding context?

### `condensation` `[skill]` `render: glossed`
The answer is present in the source; the work is retrieving and compressing it.
**Test:** does a correct answer restate what the material already says?
**Gloss:** "the answer is already in the source"

### `inference` `[skill]` `render: glossed`
The answer is not in the source; the work reaches an unstated cause, flaw, or
consequence.
**Test:** must the agent assert something no source states?
**Gloss:** "must assert something no source states"

*Note:* `condensation` and `inference` partition `open` work. Search, location and
summarization are condensation however large the corpus; debugging, review and
design critique are inference however small the input. Volume is not the axis --
what the work must produce beyond its sources is.

---

## Escalation

Why this route and not a broader one.

### `novel` `[skill]` `render: glossed`
No established pattern applies.
**Test:** can you name the pattern it follows? If yes, it is not novel.
**Gloss:** "you cannot name a pattern it follows"

### `load-bearing` `[skill]` `render: glossed`
Later work builds on the conclusion.
**Test:** would a wrong answer propagate, or would the next step catch it?
**Gloss:** "a wrong answer would propagate rather than be caught"

### `unverifiable` `[skill]` `render: glossed`
You could not spot a wrong answer by reading the summary.
**Test:** is there a cheap check -- a diff, a test run, a file you would read anyway -- that would catch the error that MATTERS here? A check catching only shallow errors does not verify.
**Gloss:** "no cheap check would catch the error that matters -- a check catching only shallow errors does not verify"

### `default` `[skill]` `render: bare`
No earlier routing row matched; the unit reaches the default row by fall-through.
**Test:** did any routing row above match? If none did, this is the term.
*Why it exists:* the default row states no shape of its own, so without this term
the most common dispatch has no legal announcement -- putting the largest hole in
the usage record at the largest population.

### `stretch` `[concept]`
The broader route below would plausibly get this wrong.
**Test:** can you say what it would get wrong?

---

## Unit class

### `plan-checkpoint` `[skill]` `render: glossed`
The unit's output is a plan, decomposition, or decision that later units will
execute against.
**Test:** will other units be briefed from this output before anything
re-derives it?
**Gloss:** "a plan or decision later units will execute against"

*Consequence:* a `plan-checkpoint` is routed through the tree like any unit --
being a plan makes nothing `open` or `novel` by itself. A decision the
orchestrator would otherwise put to the user is one: make the call, then route
the call. An `open`
`plan-checkpoint` is treated as `load-bearing` unless the downstream check that
would catch a wrong shared decision can be named: that is a rebuttable
presumption stated in the tree, not a property of planhood.

### `authored-here` `[skill]` `render: glossed`
The artifact under review was drafted inline by the orchestrator -- this
context wrote it.
**Test:** is the thing being reviewed something this context drafted itself,
rather than the output of a delegated unit?
**Gloss:** "the orchestrator drafted the artifact under review"

*Consequence:* a review is independent only when the reviewer is not the
author's model. `authored-here` names the artifacts that test applies to, so
the routing policy can send their review away from the orchestrating model by
criterion. A delegated unit's output is never `authored-here`.

---

## Dispatch shape

Supplies the shape terms that select a routing row; that row selects the model entry and its
harness.

### `conversational` `[skill]` `render: glossed`
You expect to interrogate or refine the unit mid-flight.
**Test:** is the output a deliverable you act on, or a thread you continue?
**Gloss:** "a thread you continue, not a deliverable you act on"

### `abortable` `[skill]` `render: bare`
You may need to stop it mid-run.
**Test:** is there a plausible reason to kill it before it finishes?

### `fan-out` `[skill]` `render: glossed`
N similar units you would otherwise brief individually.
**Test:** does it survive collapsing into one shell command? If not, it is one unit.
**Gloss:** "N similar units that resist collapsing into one command"

### `parallel-leaf` `[skill]` `render: glossed`
One implementation unit that passed every test in the rendered
parallel-development razor.
**Test:** did this implementation unit pass every test in that razor?
**Gloss:** "an independently executable implementation leaf admitted by the
parallel-development razor"

*Consequence:* the term applies to each admitted leaf, not to the collection.
Each leaf is routed and dispatched independently. The same-frontier leaves run
concurrently.

### `bulk-output` `[skill]` `render: glossed`
The unit emits substantially more than you need to keep.
**Test:** would you skim and discard most of the output?
**Gloss:** "emits much more than you need to keep"

### `cross-check` `[skill]` `render: glossed`
An independent second opinion on a conclusion Claude already reached.
**Test:** does a conclusion already exist, and is the point to have it challenged by
a different failure mode?
**Gloss:** "a second opinion on a conclusion Claude already reached"

### `schema` `[skill]` `render: bare`
The return shape must be machine-validated.
**Test:** will you parse the result rather than read it?

### `rule-applying` `[skill]` `render: glossed`
The unit applies a stated rule instead of deciding which rule applies.
**Test:** does it apply a stated rule, rather than decide which rule applies?
**Gloss:** "applies a stated rule rather than deciding which rule applies"

### `latency-tolerant` `[skill]` `render: glossed`
Nothing downstream and no user-visible result waits for this unit.
**Test:** would anything start sooner if this unit finished sooner?
**Gloss:** "nothing starts sooner if it finishes sooner"

### `mutating` `[skill]` `render: glossed`
The unit changes state that a sandbox or git checkout cannot restore.
**Test:** would any effect survive a git checkout, such as a push, deploy,
service call, or state outside the tree?
**Gloss:** "takes effects no sandbox confines and no checkout reverts"

---

## Operating context

### `user-present` `[concept]`
The user sent a message this turn or recently and has not dismissed the agent.
**Test:** would a foreground tool call now be time the user spends waiting?

### `user-afk` `[concept]`
The user dismissed the agent ("go", "get it done", a loop) or has gone quiet since their last message.
**Test:** does anyone read the reply before the work is done?

### `consult` `[concept]`
A decision put to an independent seat -- a different model from the one deciding -- before the agent takes it.
**Test:** would a wrong answer propagate (`load-bearing`), and is the seat a different model from the one deciding?

*Consequence:* Presence prices foreground work and is not a routing axis: it changes the cost of doing a unit inline, not which model does it. A `consult` routes as an `open` + `load-bearing` unit on the existing rows, adding `cross-check` when the agent already holds an answer -- no new row; the concepts belong to the consumer's own instructions where those define them, and this lexicon carries the tests either way.

## Anti-terms `[concept]`

Words that feel like criteria and are not. Each has been reached for in practice.

- **`difficult`** -- never a criterion. Hard-but-patterned work belongs on a
  broader route, not a narrower one. Say `novel` if no pattern applies, or `stretch`
  if the broader route would plausibly fail.
- **`important`** -- not `load-bearing`. Importance is about the topic;
  `load-bearing` is about whether a wrong answer propagates. Important work with a
  cheap check is not load-bearing in the sense that matters.
- **`complex`** -- usually means `inference` or `fan-out`. Say which.
- **`long`** -- replaced by `bulk-output`. Neither evaluable nor the real argument.
- **`simple`** -- ambiguous between `mechanical` (execution) and `sufficient`
  (brief). Independent axes: "summarize last week's commits" is `sufficient` and
  not `mechanical`. Say which.
- **`consequence`** (low, medium, high) -- names the shape of a call, not a test.
  Say `load-bearing` if a wrong answer propagates, `unverifiable` if no cheap check
  would catch it, `novel` if no pattern applies; the rows do the rest. A tier is
  never chosen first and matched second.
- **`the user's call`** -- almost never. A call inside sanctioned work routes like
  any unit, however product-flavoured. The user is reached only at an autonomy
  edge, and because no row can reach that seat, not because the call is important.

---

## Announcement form

One line per dispatch, terms only, no free prose:

```
delegating <what> to <target> (<the matched row's shape terms>)
```

The parenthetical carries the terms from the tree node that ACTUALLY fired -- not
the strongest available justification, not a complete characterisation. Only
`[skill]` terms are permitted, which is what makes the lines aggregate: the
transcript becomes the usage record for which routes get chosen and why.

The rendered worked examples are the `announce.examples` records in
[`../defaults/orchestration.yaml`](../defaults/orchestration.yaml). Keep that
list as the single source of truth for worked targets and matched shape terms.

The `ex-default` and `ex-migration` records illustrate the collapse test. A
rename is one `sed`, so it never becomes `fan-out` however many files it
touches; it is one `mechanical` unit. A migration needing per-file judgment
resists collapsing, so it is genuinely N units. Scale is not the discriminator
and must never be read as one.

*High-cost route:* a routing row carries one `shape` list. In the default
policy, the high-cost row is `shape: [novel, load-bearing]`, so both terms must
match; its `gate` asks for the reason the broader route is the wrong seat. The
list does not encode OR'd groups, and `plan-checkpoint` or `authored-here` do
not add alternate groups to it. Announce only the terms in the matched row.
