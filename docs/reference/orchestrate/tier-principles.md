# Orchestration principles

The **source of truth** for how orchestration choices are made. The decision tree
rendered into context by `orchestration_guidance.py` is DERIVED from this file.
Vocabulary is defined in
[lexicon.md](../../../plugins/awesome-kit/skills/orchestrate/references/lexicon.md)
(shipped inside the published skill); every criterion here is stated in those terms.

**Direction of authorship is one-way.** Change a criterion here first, then
re-derive the tree. Never edit the rendered tree and back-fill a principle to match
it -- that inverts the audit trail and produces criteria that exist only because a
phrasing survived.

This file is never loaded into an orchestration context. It is read when auditing or
revising the policy, which is why it can afford rationale, evidence, dated prices,
and explicit records of what is NOT known.

Every principle carries the rule, why it holds, and what would overturn it. A
principle with no evidence says so. Principles tagged **`render: required`** carry
content that MUST survive compression into the tree; the derivation has no
discretion about them.

Decision order: **shape -> backend -> tier -> agent type -> effort.**

---

## 0. Shaping the unit

Runs before everything else. Most routing defects trace to a unit that was never
shaped: the later stages then argue about model capability when the real problem is
an ill-formed brief.

### P0.1 -- Two independent axes, not three categories

- **Shape** (`known` / `open`) determines the KIND of brief: a specification for
  `known` work, a question for `open` work.
- **Sufficiency** (`sufficient`) determines whether you must AUTHOR that brief or
  can dispatch the one-sentence statement as it stands.

All four combinations occur and each is coherent. An `open` + `sufficient` unit --
*"what changed in the repo this week"* -- is discovery-shaped and needs no further
briefing; it dispatches as stated and still routes as a question.

*Why this replaced a three-category taxonomy:* the earlier form listed trivial /
specification / question as exclusive alternatives, but "trivial" was defined by
statement sufficiency while the other two were defined by whether specification is
possible. Different axes, so units satisfied two categories at once, and no rung
accepted the "trivial" one. Any future revision that reintroduces a third shape
category is reintroducing that defect.

*Overturned by:* a unit whose brief is neither a specification nor a question.

```yaml
emits:
  shape.intro: Two independent axes.
  shape.tests[brief-shape]:
    order: 1
    id: brief-shape
    principle: P0.1
    text: >-
      Shape -- {known} takes a SPECIFICATION brief; {open} takes a QUESTION
      brief. That is the only thing shape determines.
  shape.tests[brief-sufficiency]:
    order: 2
    id: brief-sufficiency
    principle: P0.1
    text: >-
      Sufficiency -- {sufficient} dispatches as stated; otherwise the unit is
      {underspecified} and you author the brief first. All four combinations
      occur.
```

### P0.2 -- Sufficiency and execution demand are independent

A unit can be `sufficient` and still demand real work. *"Summarize last week's
commits"* is an unambiguous one-sentence brief and is not `mechanical` -- it
requires judgment about what matters.

*Why it matters:* conflating them reads "no brief needed" as "cheap model will do",
which misroutes exactly the summarize-shaped units. Shape the brief in this section;
choose the rung in section 2, independently.

`render: required` -- **`sufficient` is never a rung criterion.** It decides whether
you author a brief, and nothing else. No test in section 2 may reference it.

```yaml
emits:
  shape.tests[sufficiency-is-not-a-rung-test]:
    order: 3
    id: sufficiency-is-not-a-rung-test
    principle: P0.2
    guard: true
    text: >-
      {sufficient} is never a rung criterion. It decides whether you author a
      brief, and nothing else -- no test below may reference it.
```

### P0.3 -- Whether to author a specification is driven by VOLUME  `render: principles-only`

Applies only to `known` work that is not `sufficient`.

- **Many units** -> author the specification once, then execute across them.
- **One unit** -> skip it; the overhead exceeds what it saves.

*Why volume, and why the currency is rework rather than cost:* a specification makes
N results conforming and comparable. Without one, N units return divergent results
reconciled **on the main thread** -- the exact context spend orchestration exists to
avoid. The specification buys results that compose, not a cheaper model.

*Evidence gap:* "many" is unquantified. See section 8.

```yaml
emits:
  shape.tests[specify-by-volume]:
    order: 4
    id: specify-by-volume
    principle: P0.3
    render_scope: principles-only
    text: >-
      For {known} work that is not {sufficient}: many units -- author the
      specification once, then execute across them; one unit -- skip it.
```

### P0.4 -- Who authors it  `render: principles-only`

Only reached when P0.3 says to author one. The test is NOT expected context load or
message count -- neither is estimable at decision time, and a rule keyed to them
will be applied badly.

**Does writing the specification require reading material you do not already have
in context?**

- **Yes** -> a sub-agent writes it. That reading is the whole context spend
  orchestration exists to avoid.
- **No** -> write it inline. A sub-agent round-trip costs more than typing it.

```yaml
emits:
  shape.tests[who-authors-the-specification]:
    order: 5
    id: who-authors-the-specification
    principle: P0.4
    render_scope: principles-only
    text: >-
      Does writing the specification require reading material you do not
      already have in context? Yes -- a sub-agent writes it. No -- write it
      inline.
```

### P0.5 -- Decide `fan-out` before dispatching

A mechanical fan-out is usually ONE unit: a model collapses *"check these eight
files"* into a single shell command. Work a shell one-liner can do rarely needs
delegating at all.

*Why it runs here:* it applies to both backends, and it must precede backend
selection because `fan-out` is itself a backend pull (P1.4).

```yaml
emits:
  shape.tests[fan-out-collapse]:
    order: 6
    id: fan-out-collapse
    principle: P0.5
    text: >-
      Decide {fan-out} before dispatching. One that collapses into a single
      shell command is ONE unit, and work a one-liner does rarely needs
      delegating at all.
    without_backend:
      codex: >-
        A genuine fan-out then has no other route -- sequence the units or
        handle them inline.
```

### P0.6 -- The plan is a candidate unit

The decomposition or plan the orchestrator authors is itself a unit, and it is
routed through the tree like any other -- named by the term `plan-checkpoint`.

*The observed failure this exists to prevent (2026-08-11):* the orchestrator
authored plans as coordination work and proceeded to execute them; the routing
question was never asked, because the tree routes DELEGATED units and the plan
was never one. The user had to steer "get fable to review this" by hand, and the
review found significant issues each time. No rung calibration can fix a unit
that is never classified, so the fix is upstream of every rung: the plan enters
the tree explicitly.

*Planhood implies no term.* A settled migration checklist is `known`; only an
architecture decision is `open`. And downstream units consuming a plan proves
DEPENDENCY, not propagation -- `load-bearing` asks whether the next step would
catch a wrong answer, and unit-local checks often would. Call a plan
`load-bearing` only when a wrong shared decision would survive the downstream
units' own checks and contaminate several results or a costly commitment. (An
earlier draft derived `load-bearing` "by construction" from planhood; a
gpt-5.6-sol design review killed that, correctly -- automatic `load-bearing`
plus commonplace `unverifiable` would have made the top rung routine.)

*Lifecycle and recursion boundary:* do not launch units briefed from a plan
that has not resolved; a materially revised plan re-enters as a new candidate
unit; and routing a plan is never itself a unit -- the checkpoint applies to
the plan, not to the planning of the plan.

*Overturned by:* a period in which the checkpoint fires only on plans that the
terminal rung would have handled identically -- the announcement record's
`plan-checkpoint` term makes that measurable.

```yaml
emits:
  shape.tests[plan-is-a-unit]:
    order: 7
    id: plan-is-a-unit
    principle: P0.6
    text: >-
      Route the plan itself through this tree as its own unit -- a
      {plan-checkpoint} -- before briefing anything from it. Planhood implies
      no other term: a plan is {load-bearing} only when a wrong shared
      decision would survive the downstream units' own checks. Do not launch
      units briefed from a plan still in flight; a materially revised plan
      repeats this checkpoint; routing the plan is never itself a unit.
```

### P0.7 -- Plan creation vs plan review, decided by brief transferability

Only reached for a `plan-checkpoint` that escalates to its ladder's top rung --
an unescalated plan is authored inline like any other orchestrator work.

The fork is NOT "is authoring the brief cheaper than authoring the plan": a
routine plan can be cheap to brief without deserving the top rung, and a large
plan can be expensive to restate while every constraint sits in referenceable
files. The test is information fidelity: **can a standalone brief carry or
reference every decision-relevant constraint without encoding the answer and
without losing context that exists only in this conversation?**

- **Yes** -> delegate plan CREATION (question brief -- requirements in, plan
  out).
- **No** (the constraints accumulated interactively, or the complexity was
  discovered mid-authoring) -> author the draft and delegate plan REVIEW of
  the artifact.

*Authorship independence:* a model that created a plan is not an independent
reviewer of it. When the top model created the plan, the review seat belongs to
a different family (P0.8); the two-independent-reviews shape applies to an
orchestrator-authored plan.

```yaml
emits:
  shape.tests[plan-create-or-review]:
    order: 8
    id: plan-create-or-review
    principle: P0.7
    text: >-
      For a {plan-checkpoint} that escalates to its ladder's top rung: can a
      standalone brief carry or reference every decision-relevant constraint
      without encoding the answer and without losing context that exists only
      in this conversation? Yes -- delegate plan CREATION (question brief).
      No -- author the draft and delegate plan REVIEW of it. A model that
      created a plan is not an independent reviewer of it.
```

### P0.8 -- A top-rung plan review defaults to a second family

Supersedes P3.6, whose intent could never execute (see its tombstone). Two
independent reviews of a decision everything else builds on; the disagreements
are the finding. It is a *both*, not a *versus*, so it does not contradict
P1.1.

*Why the expansion happens HERE:* one unit cannot resolve to two backends under
ordered elimination -- `cross-check` pulls to Codex and never reaches the
Claude ladder. So a qualifying review is expanded into TWO units during
shaping, before backend selection: the primary review, routed down the tree as
usual, and a `cross-check` child carrying the same brief, which the existing
backend pulls and Codex rungs route to the second family with no new mechanism.

*Why a default rather than P3.6's "never automatic":* the second seat is
per-token billed with no pool boundary, and the under-use being corrected was
observed, not hypothetical. But "no pool" is not "free" -- the child costs
latency, synthesis, and adjudication, so the default keeps an actionability
bar: it holds when unwinding the decision after downstream execution would be
expensive AND a disagreement would change the plan and be adjudicated before
anything executes. Skipping the child requires naming the reason (cheap to
unwind, or nobody would act on a disagreement) -- the burden moved from
justifying the dispatch to justifying the skip.

*Capacity fallback:* top-rung qualification is semantic, dispatchability is
not. When the top rung is marked LIMITED/UNAVAILABLE, the primary routes down a
rung and the child still dispatches -- it is then the only high-tier opinion,
not a second one.

```yaml
emits:
  shape.tests[plan-review-second-family]:
    order: 9
    id: plan-review-second-family
    principle: P0.8
    text: >-
      A top-rung {plan-checkpoint} review defaults to TWO units when unwinding
      the decision later would be expensive AND a disagreement would change
      the plan before anything executes: the primary review, routed as usual,
      plus a {cross-check} child carrying the same brief, routed by the
      backend pulls. Skipping the child needs a named reason -- cheap to
      unwind, or nobody would act on a disagreement. If the top rung is
      unavailable the primary drops a rung and the child still dispatches.
      Announce each separately.
    without_backend:
      codex: >-
        With a single model family there is no second-family child -- dispatch
        the primary review alone.
```

---

## 1. Backend selection

### P1.1 -- The backend is chosen for dispatch shape, not capability

Rungs compare within a ladder, never across. You arrive at a ladder by deciding
where the work should RUN, then choose a rung on it.

The Claude Agent tool is the default backend. Leave it only when a pull fires.

*Why:* the ladders are incommensurable on two independent axes. Cross-family
capability comparison is unreliable, and the economics differ in kind -- Claude
tiers are governed by subscription pool windows, Codex by per-token billing
(section 6). A merged ladder would invite "gpt-5.6-sol is between opus and fable"
reasoning, which is meaningless.

*Overturned by:* a reliable cross-family benchmark AND a common cost basis.

```yaml
emits:
  default_backend: agent
  backend.requires_backend: codex
  backend.intro: >-
    Choose where the work RUNS, then pick a rung on that backend's ladder.
    Rungs compare within a ladder, never across.
  backend.default: agent
```

### P1.2 -- Gates are disqualifiers, evaluated before pulls

Two gates, both resolving to the Agent tool: `conversational`, `abortable`.

*Mechanics behind them:* `SendMessage` resumes an agent with context intact, while a
follow-up to a finished Codex session is a cold re-brief paying full orientation
again. A `codex exec` run cannot be interrupted; the sandbox is the entire safety
story.

*Why `conversational` is defined as thread-not-deliverable:* the earlier phrasing
("you will iterate or ask follow-ups") caught almost any unit whose output you might
question, silently disarming `cross-check` -- a second opinion is exactly the kind of
output you would interrogate, yet it is a one-shot deliverable.

```yaml
emits:
  backend.gates_intro: Gates -- disqualifiers, evaluated before pulls. Any one resolves to
  backend.gates[gate-conversational]:
    order: 1
    id: gate-conversational
    term: conversational
    backend: agent
  backend.gates[gate-abortable]:
    order: 2
    id: gate-abortable
    term: abortable
    backend: agent
```

### P1.3 -- The MCP / harness-tools gate was REMOVED (2026-08-07)  `render: none`

*Why:* two reasons, either sufficient. **No qualifying unit** -- a unit needing a
session-scoped capability (MCP, `Skill`, `Artifact`, sub-agent tools) was never a
plausible Codex candidate anyway, so the gate never changed an outcome. **The term
mis-routes** -- "harness tools" reads broadly enough to include `Read` and `Bash`,
which Codex has, making the backend unreachable.

*Re-add if:* a unit class needs `Skill` or `Artifact` mid-run AND would otherwise be
pulled to Codex. Phrase it as the exclusive capabilities, never as "harness tools."

*Emits nothing:* the gate was removed and this principle is retained only as history.

### P1.4 -- Four pulls, OR'd

`fan-out`, `bulk-output`, `cross-check`, `schema`. **Any one firing is enough**;
rendered order is presentation and carries no precedence.

Each names what you would OTHERWISE do, which is the argument:

- `fan-out` -- otherwise you write N briefs and take N reports into context, because
  an agent cannot spawn agents. One Codex session takes the list. *(P0.5 first: if it
  collapses to one command, it is not fan-out.)*
- `bulk-output` -- otherwise an Agent-tool report is delivered into context whole.
  Codex output lands in a file you skim and discard.
- `cross-check` -- otherwise you re-run within the same family and inherit its
  failure modes. The different failure mode IS the check.
- `schema` -- `--output-schema` validates the final message; the Agent tool has no
  per-call equivalent.

```yaml
emits:
  backend.pulls_intro: Pulls -- any one firing is enough, and their order carries no precedence. To
  backend.pulls[pull-fan-out]:
    order: 1
    id: pull-fan-out
    term: fan-out
    backend: codex
  backend.pulls[pull-bulk-output]:
    order: 2
    id: pull-bulk-output
    term: bulk-output
    backend: codex
  backend.pulls[pull-cross-check]:
    order: 3
    id: pull-cross-check
    term: cross-check
    backend: codex
  backend.pulls[pull-schema]:
    order: 4
    id: pull-schema
    term: schema
    backend: codex
```

---

## 2. Claude ladder

### P2.0 -- Resolution: ordered elimination, first match wins

Rungs are tested in the order below and the first match is the answer. There is no
comparison step and no tie-break, because a tie cannot arise.

**Order: fable -> opus -> sonnet (terminal).**

*Why this order:* most restrictive first. The top gate must precede opus, or opus's
`inference` test would swallow every unit that also meets the conjunction. sonnet is
terminal by construction -- it is the default, so it must be unreachable except by
falling through.

*Why there is no "take the lower rung" rule:* an earlier revision carried one, which
is unimplementable under elimination -- it presumes comparing two rungs that
elimination never both reaches. Correct ordering already encodes the intent: guarded
rungs are tested first with hard gates, and falling through to the default IS taking
the lower rung.

`render: required` -- state the semantics explicitly at the top of the artifact. It
is not inferable from the content, and a default-plus-exits reading picks different
models for the same unit.

```yaml
emits:
  resolution: >-
    Ordered elimination: within a block, tests are evaluated in the order given
    and the FIRST match is the answer. There is no comparison step and no
    tie-break.
  ladders.agent:
    order: 1
    id: agent
    label: Claude
```

### P2.1 -- haiku is NOT a rung (decided 2026-08-07)

`known` + `mechanical` work goes to sonnet by fall-through.

*Why removed:* the rung was carried while conceding that haiku 4.5 is an older
generation, that sonnet 5 is materially more capable, that "the saving does not carry
the rung" (one miss costs a retry at sonnet and erases it), and that any doubt
resolves to sonnet anyway. It also pre-registered its own deletion criterion -- a
period in which nothing chose it. Carrying a rung on the explicit expectation of
deleting it costs a conjunction, a debiasing guard, and a permanent asterisk, to
route a class the terminal rung handles adequately.

*What it cost to keep, concretely:* it forced a `render: required` "any doubt resolves
to sonnet" guard, and an ordering rationale about `mechanical` and `novel` co-occurring
that had to be corrected once (an earlier revision asserted mechanical work is never
novel, which is false).

*Re-add if:* the P4.5 announcement record shows a real population of `known` +
`mechanical` dispatches where sonnet was demonstrably wasteful -- which is now
measurable in a way it was not when the rung was seated. Name that population here.

`render: required` -- **there is no haiku rung.** A negative guard, same as P3.4:
without it a reader who knows the model exists will invent the dispatch.

*Stale text removed 2026-08-07:* this principle carried a `*Status:*` paragraph and a
`render: required` "any doubt resolves to sonnet" guard, both written when haiku was
a live rung and both left behind when it was deleted. A `render: required` tag on a
rung that no longer exists is worse than dead text -- it instructs the derivation to
render a guard for nothing. The guard itself was worth keeping and moved to P2.5,
where it applies to the ladder rather than to a removed rung.

*Lesson, recorded because it will recur:* deleting a rung is not a local edit. Its
criteria, its guards, its render tags, its vocabulary terms and its overturn condition
are all dependent text, and the derivation surfaces them one layer down as
"the principles did not determine this" rather than as an obvious contradiction.

```yaml
emits:
  ladders.agent.guards:
    - order: 1
      value: There is no haiku rung.
```

### P2.2 -- fable: any TWO of `novel`, `load-bearing`, `unverifiable`

Two of the three must hold, in any combination. **`open` only** -- and the
long-standing rule that fable is not an implementation tier now DERIVES from that
rather than being asserted: implementation is `known` work, and this rung takes only
`open` work.

*Why two rather than all three, and why this is a deliberate over-correction:* the
three conjuncts guard distinct axes -- `novel` that the work is LIKELY to be got
wrong, `load-bearing` that being wrong is CONSEQUENTIAL, `unverifiable` that being
wrong is INVISIBLE. Requiring all three is multiplicative, and it rejected work that
scored high on two axes and merely moderate on the third. Two-of-three keeps the
discipline that a unit must be dangerous in more than one way, while admitting the
population the conjunction was over-filtering.

Note what this deliberately does NOT do: dropping one specific conjunct was
considered and rejected, because each single drop is wasteful in a nameable way --
without `novel`, patterned work opus handles fine; without `load-bearing`, the scarce
pool goes to work whose errors do not matter; without `unverifiable`, work that a
test run would have caught anyway. Two-of-three admits none of those, since all three
fail two axes at once.

*Standing as a tuning position, not a derived truth:* this is an intentional
over-correction, chosen on the judgement that the rung was firing too rarely, against
no selection telemetry (section 7). The direction is deliberate; the magnitude is a
guess. **Revisit when P4.5 announcements provide a rung tally** -- if the top rung
then wins work that a single opus pass plus its ordinary check would have handled,
the correct response is to restore the three-way conjunction rather than to patch
around it with guards.

The gate is procedural, not predicative: write *"qualifies on &lt;criterion&gt;; opus
would plausibly get it wrong because &lt;reason&gt;."* If that sentence is hard to
write, the unit does not qualify.

*Why a procedure rather than three questions:* three self-assessed yes/no questions
about work already framed as important are a weak gate -- there is a standing pull
toward the more capable model and the cost signal is remote. A demand for written
justification is a materially stronger filter.

*Why the guard is severe when the price step is small:* fable is only 2x opus by list
price, so read against price this looks disproportionate. It is not defending
dollars. **fable draws from a different usage pool** than haiku/sonnet/opus -- the
only pool boundary on this ladder -- and a pool does not degrade gracefully when
spent, it stops.

`render: required` -- both the justification sentence AND a one-clause form of the
pool fact. A reader seeing only the conjunction and the ritual infers the guard is
overwrought, which is the exact misreading this file exists to prevent.

*Never this rung:* hard-but-patterned work; anything whose operative failure a cheap
check would catch; anything recoverable in one more pass. `render: required` -- this
list is debiasing.

*Why "operative failure" and not "a cheap check exists":* the earlier wording
disqualified on the mere EXISTENCE of a check, and almost all substantive work has
some shallow check available. That silently rejected most of the population the other
two conjuncts admit -- the conjunct was mis-stated, not merely strict. A smoke test
proving a design runs is not a check on whether the design is right. Tracks the
`unverifiable` test in lexicon.md; the two must be re-worded together or this guard
re-imports the old bar one line below the fix.

*Canonical shape:* critical review of a plan or architecture -- typically all
three fire at once, and the two-of-three gate needs only two. This is why fable
reviews the plan and opus builds against it. P0.6-P0.8 are what actually route
a plan here; before they existed the plan never entered the tree as a unit, and
the canonical shape sat in this unrendered paragraph while the rendered rung
carried only downward-pointing guards.

*Observed evidence for the direction of the two-of-three revision (2026-08-11):*
the awaited telemetry arrived as user steering -- the orchestrator repeatedly
authored and executed plans until steered "get fable to review this", and the
review found significant issues each time. That supports the rung firing MORE
on plan-shaped work (addressed structurally by P0.6-P0.8), and is not yet a
rung tally; the P4.5 announcement record, now carrying `plan-checkpoint`,
remains the instrument that settles magnitude.

```yaml
emits:
  ladders.agent.rungs[top]:
    order: 1
    id: top
    model: fable
    shape: open
    criteria:
      - [novel, load-bearing]
      - [novel, unverifiable]
      - [load-bearing, unverifiable]
    terminal: false
    gate: >-
      write "qualifies on <criterion>; opus would plausibly get it wrong
      because <reason>." If that sentence is hard to write, the unit does
      not qualify.
    notes:
      - id: pool-boundary
        text: >-
          fable draws from a different usage pool than the rungs below --
          the only pool boundary on this ladder, and a pool does not
          degrade gracefully when spent, it stops.
    guards:
      - >-
        Never this rung: hard-but-patterned work; anything whose operative
        failure a cheap check would catch; anything recoverable in one more
        pass.
```

### P2.3 -- opus: depth in either shape

`open` + `inference`; or `known` + `novel` (the interface and acceptance are settled,
so the novelty is in the building rather than the deciding).

*Note:* this rung absorbed the former standalone implementation block. Routing
implementation by specification quality is now universal (section 0), so all that
remained was "a specified build routes on how much novelty is in the building."

```yaml
emits:
  ladders.agent.rungs[high-reasoning]:
    order: 2
    id: high-reasoning
    model: opus
    criteria:
      - [open, inference]
      - [known, novel]
    terminal: false
```

### P2.4 -- sonnet: terminal default

Reached by fall-through only. It states no test of its own -- "ordinary judgment" was
never a vocabulary term, and a rung that both is terminal and carries criteria
contradicts P2.0's semantics.

Everything no rung above matched lands here.

*On describing that population:* it includes what was formerly haiku work, but this
principle must NOT say so in the rendered artifact -- `mechanical` is `[concept]` since
P2.1, and concept terms never render. Describing the population by fall-through
("everything no rung above matched") is the only form available, and it is also the
honest one: the terminal rung is defined by what did not match, not by a property of
its own. Naming `mechanical` here would have re-imported a term the lexicon demoted.

**Announced as `(known, default)` or `(open, condensation)`** -- `default` is the
term for "no escalation test matched", which is the honest description of a
fall-through and keeps the most common dispatch on the ladder announceable. Without
it the terminal rung produces no legal line and the usage record loses its largest
population.

*Why terminal rather than tested:* most delegated units are workhorse-shaped, and the
named anti-pattern is top-tier-everywhere. Making the default reachable only by
fall-through means every escalation is an explicit match against a stated test.

```yaml
emits:
  ladders.agent.rungs[workhorse]:
    order: 3
    id: workhorse
    model: sonnet
    criteria: []
    terminal: true
    text: >-
      terminal default, reached by fall-through only. It states no test of
      its own -- everything no rung above matched lands here,
      {condensation} work included.
    announce_as:
      - [known, default]
      - [open, condensation]
```

### P2.5 -- Cross-cutting rules for this ladder

- **Any doubt resolves to the rung below.** `render: required` -- debiasing, not
  rationale. Moved here from P2.1 when the haiku rung was deleted; it was never
  specific to that rung, and stating it ladder-wide is what it always meant.
- Never down-tier a unit that meets the fable bar to harvest the discount.
- Judge by total tokens to a VERIFIED result, not by apparent difficulty. `render: principles-only`

```yaml
emits:
  ladders.agent.guards:
    - order: 2
      value: Any doubt resolves to the rung below.
    - order: 3
      value: Never down-tier a unit that meets the fable bar to harvest the discount.
```

---

## 3. Codex ladder

### P3.0 -- Resolution: ordered elimination, gpt-5.6-sol -> gpt-5.6-luna (terminal)

```yaml
emits:
  ladders.codex:
    order: 2
    id: codex
    label: Codex
```

### P3.0a -- Model identifiers must be FULLY QUALIFIED, and validated by dispatch

`-m` takes `gpt-5.6-luna`, `gpt-5.6-terra`, `gpt-5.6-sol`. The bare codenames are
**not dispatchable**.

*Evidence (2026-08-07, codex-cli 0.146.0, ChatGPT-account auth):* a knock-knock
dispatch on each name. Bare `luna` / `terra` / `sol` each produced
`Model metadata for '<name>' not found` followed by HTTP 400 *"not supported when
using Codex with a ChatGPT account"*, and wrote no `-o` file. The qualified names
produced zero warnings and populated output.

*Why this is a principle and not a footnote:* the superseded policy carried
`model: terra` and `model: sol` while instructing the reader to select the model with
`-m`. Following it verbatim fails every time. The defect survived because the policy
is prose nobody executes -- so **any change to a model identifier must be validated by
an actual dispatch before it ships**, not by inspection.

*Not established by that test:* which model answers. The three runs self-reported
"GPT-5", "GPT-5" and "GPT-5.4"; models are unreliable self-identifiers. The test
proves the identifier is accepted, nothing more.

```yaml
emits:
  ladders.codex.guards:
    - order: 1
      value: >-
        Model identifiers are FULLY QUALIFIED as written above; the bare
        codenames are not dispatchable by `-m`.
```

### P3.1 -- gpt-5.6-sol at max effort, or do not run it here

`cross-check`; or `novel` + `unverifiable` **where raising gpt-5.6-luna to `max`
would not resolve it**.

*Why the effort clause is part of the criterion:* effort IS dialable on this backend,
so "try more thinking on the cheap rung first" is a real move here -- but under
ordered elimination it cannot be stated as a sequencing instruction, because a unit
matching this rung never reaches the one below to have its effort raised. Folding it
into the criterion is the only form that executes.

*Why these terms:* the inherited criterion was "hard, ambiguous, long-horizon work",
none of which are vocabulary terms -- and `difficult` is an explicit anti-term whose
reasoning the Claude ladder's own never-list forbids one rung over. A unit routed
here on that phrasing could not be announced at all, which put a hole in the usage
record at the expensive rung. The escalation logic is the same one the Claude ladder
uses, minus `load-bearing` (that criterion carries a pool argument specific to the
Claude ladder, and there is no pool boundary here).

*Not evidenced:* this is the same decision-under-uncertainty as P3.4. Nothing
measured says these two terms are where the gpt-5.6-luna/gpt-5.6-sol line belongs; they are the
defensible restatement of an unevidenced inherited criterion, in vocabulary that can
at least be audited. See section 7.

*Why `cross-check` resolves here unconditionally:* an earlier revision hedged that a
routine second opinion "may legitimately sit on gpt-5.6-luna", which elimination cannot
express -- gpt-5.6-sol's criterion matches first and gpt-5.6-luna becomes unreachable for those
units. The hedge is removed rather than the semantics bent.

```yaml
emits:
  ladders.codex.rungs[codex-top]:
    order: 1
    id: codex-top
    model: gpt-5.6-sol
    criteria:
      - [cross-check]
      - terms: [novel, unverifiable]
        where: raising gpt-5.6-luna to `max` would not resolve it
    terminal: false
```

### P3.2 -- gpt-5.6-luna at high effort is the terminal default

*Why the cheap rung is the default:* you are on this backend for dispatch shape, not
difficulty (P1.1), so arriving units are workhorse-difficulty units that happen to be
awkwardly shaped. Seating an expensive rung as the default for shape-selected work
inverts the reasoning used on the Claude ladder.

```yaml
emits:
  ladders.codex.rungs[codex-workhorse]:
    order: 2
    id: codex-workhorse
    model: gpt-5.6-luna
    criteria: []
    terminal: true
    text: >-
      terminal default. You are on this backend for dispatch shape, not
      difficulty, so arriving units are workhorse-difficulty units that
      happen to be awkwardly shaped.
```

### P3.3 -- DELETED (2026-08-07)  `render: none`

Formerly: "raise gpt-5.6-luna's effort before reaching for gpt-5.6-sol."

*Why deleted:* unimplementable under ordered elimination, for exactly the reason
P2.0's "take the lower rung" rule was deleted from the Claude ladder and P3.1's
`cross-check` hedge was removed -- a unit matching the upper rung never reaches the
lower one, so there is no moment at which its effort could be raised first. This was
the same defect in a third location.

Its intent now lives inside P3.1's criterion, where it can actually fire. The
concrete misroute it failed to prevent: a `fan-out` of 30 items each `novel` +
`unverifiable` matched the top rung and dispatched 30 times at `max`, while the
principle written to stop that could never execute.

*Emits nothing:* this deleted rule survives only as history; P3.1 emits its remaining intent.

### P3.4 -- gpt-5.6-terra is NOT a rung (decided 2026-08-07)

*Why:* no unit class has been named that gpt-5.6-terra wins. The policy's own standard --
*"add a rung only with a case that names units it wins"* -- applies symmetrically,
and gpt-5.6-terra never met it; it was seated as the default by assertion.

*Its only defensible niche:* work we are confident gpt-5.6-luna cannot handle and for which
gpt-5.6-sol is overkill. Add it back when such a class is named, and name it here.

*Supporting but NOT load-bearing:* gpt-5.6-terra is 10x gpt-5.6-luna while gpt-5.6-sol is only 2.5x gpt-5.6-terra, so
the escalation cliff is leaving gpt-5.6-luna at all. Cost is explicitly not the argument; the
missing case is.

```yaml
emits:
  ladders.codex.guards:
    - order: 2
      value: There is no gpt-5.6-terra rung.
```

### P3.5 -- Section 0 applies on this ladder too  `render: none`

Shape, sufficiency, `fan-out` and who-authors are backend-independent.

*Emits nothing:* it reuses section 0's decisions and adds no Codex-specific leaf.

### P3.6 -- SUPERSEDED (2026-08-11) by P0.8  `render: none`

Formerly: "Running BOTH families on plan review is never automatic", requiring
all of fable qualification, unwinding cost, and willingness to act on a
disagreement -- rendered as a `render_scope: principles-only` note on the Codex
ladder.

*Why superseded -- two defects, both surfaced by a gpt-5.6-sol design review
(2026-08-11, itself a P0.8-shaped dispatch):* (1) it could not execute --
tagged principles-only it never rendered, so the runtime could not know the
move existed; and a Codex-ladder note is appended after the rungs by the
renderer, so it is not a branch and cannot create a second dispatch. (2) It was
housed after backend selection, where one unit cannot resolve to two backends
under ordered elimination -- `cross-check` pulls to Codex and never reaches the
Claude ladder at all. The expansion has to happen during shaping, which is
where P0.8 now puts it. Its surviving intent -- an actionability bar on the
second review -- lives in P0.8's default-plus-named-skip.

*Emits nothing:* retained as history only.

---

## 4. Agent type, effort, announcements

### P4.1 -- Agent type is a separate dimension, Claude-side only

Tier decides which model; agent type decides which dispatch. Renders after tier,
before effort. Codex has no agent types, so this block is Claude-side even in the
with-Codex variant.

- **`Explore`** -- work over many files where you want the conclusion and not the
  file dumps. **The default for every `open` + `condensation` unit.** Takes a breadth
  argument; state "medium" or "very thorough" explicitly, defaulting to "medium".

  *Two scope corrections, both from units it wrongly excluded:* restricting it to
  answers of the form *where* or *whether* excluded summarization; requiring that it
  "need not run anything" excluded every repo question, since reading a repo means
  running `git log`. Explore is read-only, not command-free. `condensation` is the
  whole criterion.
- **`Plan`** -- implementation-strategy design.
- **general-purpose** -- everything else, including anything that must run commands
  **that change state**. Read-only inspection does not disqualify `Explore`.

  *Why the qualifier:* without it this bullet silently re-imports the exclusion the
  `Explore` correction above was written to delete -- every repo question "must run
  commands" (`git log`), so general-purpose reclaimed the exact case that correction
  exists to protect.

*Status:* added 2026-08-07, unvalidated. See section 8.

```yaml
emits:
  agent_types.intro: Claude-side only.
  agent_types.items[explore]:
    order: 1
    id: explore
    name: Explore
    text: >-
      the default for every {open} + {condensation} unit. Read-only, not
      command-free -- it may run commands that do not change state. Give it a
      breadth argument, "medium" or "very thorough"; default "medium".
  agent_types.items[plan]:
    order: 2
    id: plan
    name: Plan
    text: implementation-strategy design.
  agent_types.items[general-purpose]:
    order: 3
    id: general-purpose
    name: general-purpose
    text: >-
      everything else, including anything that must run commands that CHANGE
      state. Read-only inspection does not disqualify Explore.
```

### P4.2 -- Effort is orthogonal to tier

Decided separately, after the tier. Deliberately not a tree node: its criteria
partly overlap tier criteria (`unverifiable` appears in both) with different
consequences, and forcing it in would require a cross-edge.

Scale, low to high: `low`, `medium`, `high`, `xhigh`, `max`.

**Codex-side, effort is a real dial** -- set per dispatch. Defaults: gpt-5.6-luna `high`,
gpt-5.6-sol `max`.

**Claude-side, effort is NOT dialable per call.** The Agent tool has no effort
parameter; agents inherit from their type or the session, and it is settable per call
only through Workflow's `agent()` `opts.effort`. So a Claude-side effort decision is
an input to *tier* selection, not a knob to turn afterwards -- which is exactly what
P4.3 means by trying up-effort before up-tier when the mechanism allows it.
`render: required`.

*Why this is stated as an asymmetry rather than a table of six defaults:* an earlier
revision prescribed a default for every rung and then said in the next sentence that
four of them could not be applied. Six inert numbers read as a control surface that
does not exist.

*fable is the one Claude-side case worth naming:* its gate requires `unverifiable`,
so on every unit reaching it, deliberation is the only control over the error that
MATTERS -- a shallow check may well exist and will not catch that error. Where effort
IS settable there, set it to `max`. An earlier revision defaulted it to `high` and
separately licensed `max` "when unverifiable" -- which is always -- giving two answers
with no tie-break.

*This premise is coupled to `unverifiable`'s wording and survived its re-statement
deliberately.* When that conjunct disqualified on the mere existence of a cheap check,
"deliberation is the ONLY control left" was literally true. It no longer is, so the
claim is narrowed to the operative error rather than dropped: the reason for `max` was
never that no check exists, but that no check reaches the failure worth escalating for.

```yaml
emits:
  ladders.codex.rungs[codex-top].effort: max
  ladders.codex.rungs[codex-workhorse].effort: high
  effort.intro: >-
    Decided separately, after the tier. Scale, low to high: `low`, `medium`,
    `high`, `xhigh`, `max`.
  effort.backend_notes[codex-dial]:
    order: 1
    id: codex-dial
    backend: codex
    text: >-
      Codex-side, effort is a real dial set per dispatch -- the rung defaults
      are above.
  effort.note: >-
    Claude-side, effort is NOT dialable per call: the Agent tool has no effort
    parameter, agents inherit from their type or the session, and it is
    settable per call only through Workflow's `agent()` `opts.effort`. So a
    Claude-side effort decision is an input to TIER selection, not a knob to
    turn afterwards. Where it IS settable on fable, set `max` -- that rung
    requires {unverifiable}, so deliberation is the only quality control left.
```

### P4.3 -- Try up-effort before up-tier

Stays in the same pool and clears most units that look like they need a better model.
Concretely: opus at `xhigh` before reaching for fable at all.

*Sequencing against P2.2:* this applies to units that do NOT meet the top-rung
bar. A unit that already meets the two-of-three conjunction goes to fable
directly; up-effort is the move for units that merely feel like they need more.
(An earlier wording said "meeting all three", written when the gate was a
three-way conjunction and left behind by the two-of-three revision.)

```yaml
emits:
  effort.up_effort_note: >-
    Try up-effort before up-tier -- opus at `xhigh` before reaching for fable at
    all. That is the move for units that do NOT meet fable's conjunction; a
    unit already meeting the two-of-three bar goes there directly.
```

### P4.4 -- Effort tests

- *Raise:* ambiguous, or several plausible approaches where picking wrong is
  expensive; cannot verify cheaply; rework propagates.
- *Lower:* applies a stated rule; cheaply checkable; retry beats deliberation.

```yaml
emits:
  effort.raise_when:
    - order: 1
      value: ambiguous, or several plausible approaches where picking wrong is expensive
    - order: 2
      value: you cannot verify the result cheaply
    - order: 3
      value: rework propagates rather than being caught by the next step
  effort.lower_when:
    - order: 1
      value: it applies a stated rule rather than deciding which rule applies
    - order: 2
      value: the result is cheaply checkable
    - order: 3
      value: retry beats deliberation
```

### P4.5 -- Announce every dispatch, in one line

```
delegating <what> to <model> (<terms that fired>)
```

Only `[skill]` terms; no free prose. The parenthetical carries **the shape term plus
the deciding term(s)** -- and where the tier fell through to a terminal rung after a
backend pull, the pull term instead. Not the strongest available justification, not a
complete characterisation.

*Why stated mechanically:* "the terms from the node that actually fired" did not
describe four of its own five worked examples -- `known` in `(known, mechanical)`
comes from the shaping block, and `condensation` in `(open, condensation)` comes from
no node at all, since the terminal rung states no test. A renderer cannot derive the
rule from a description that its own examples contradict.

*Why:* it makes routing auditable at dispatch time, and the transcript becomes the
usage record that section 8's evidence gaps currently lack -- including the data that
settles P2.1. A closed vocabulary is what makes the lines aggregate; free prose would
not.

`render: required` -- the form and the terms-only rule.

```yaml
emits:
  announce.form: delegating <what> to <model> (<terms that fired>)
  announce.rule: >-
    Terms only, no free prose. The parenthetical carries the shape term plus
    the deciding term(s) -- not the strongest available justification, not a
    complete characterisation.
  announce.backend_notes[pull-term]:
    order: 1
    id: pull-term
    backend: codex
    text: >-
      Where the tier fell through to a terminal rung after a backend pull,
      the parenthetical carries the pull term instead.
  announce.examples[ex-rename]:
    order: 1
    id: ex-rename
    text: delegating rename across 30 files to sonnet (known, default)
  announce.examples[ex-sweep]:
    order: 2
    id: ex-sweep
    text: delegating log relevance sweep to sonnet (open, condensation)
  announce.examples[ex-crash]:
    order: 3
    id: ex-crash
    text: delegating crash diagnosis to opus (open, inference)
  announce.examples[ex-review]:
    order: 4
    id: ex-review
    text: delegating architecture review to fable (novel, load-bearing, unverifiable)
  announce.examples[ex-migration]:
    order: 5
    id: ex-migration
    requires_backend: codex
    text: delegating per-file API migration to codex/gpt-5.6-luna (fan-out)
  announce.examples[ex-plan-review]:
    order: 6
    id: ex-plan-review
    text: delegating plan review to fable (plan-checkpoint, novel, unverifiable)
  announce.examples[ex-plan-crosscheck]:
    order: 7
    id: ex-plan-crosscheck
    requires_backend: codex
    text: delegating plan review second opinion to codex/gpt-5.6-sol (cross-check)
```

---

## 5. How to derive the tree

Fixes the SHAPE and conventions of the rendered artifact so derivation is
reproducible. **It must not restate criteria, and it must not decide outcomes.** If a
sentence here could change which model gets chosen, it is a principle in the wrong
place and belongs in sections 0-4. Resolution semantics and render-required content
were moved out of this section for exactly that reason.

### P5.1 -- Structure

1. **Shape** (section 0)
2. **Backend: Claude or Codex?** (section 1)
3. **Tier** -- one subtree per backend
4. **Agent type** (P4.1)
5. **Effort** (section 4)
6. **Announcement form** (P4.5)

**Within a block, render in the order the principles are numbered.** That makes
intra-block layout mechanical rather than a judgment call.

**The rule orders each SLOT, not the whole block.** Rungs order among rungs,
guards among guards, tests among tests. It is not a licence to interleave slots:
P2.1 is a ladder guard numbered before the rung principles P2.2-P2.4, and strict
whole-block numbering would try to emit a guard between two rungs, which the
schema has no shape for. Slot membership is decided by the emit target
(`ladders.agent.guards` vs `ladders.agent.rungs[...]`); `order:` then sequences
within it.

**What renders:** every decision in sections 0-4 -- the tests, the routes, the
defaults -- plus **every negative guard** (a rung something must NOT be used for, a
rung that does not exist). Rationale does not: the *why*, the *overturned by*, the
correction notes, and the history of removed criteria. Worked examples render only
for the announcement form, where the form is the content.

`render: required` marks decision content that might read as rationale and be
compressed away. It is a backstop, not the rule -- an untagged negative guard still
renders. An earlier revision made the tag the sole mechanism, which meant guards
survived or died by whether an author remembered to tag them: fable's never-list
rendered while terra-is-not-a-rung did not, on no principled distinction.

**`render: principles-only`** marks the converse: a principle that is genuine policy
but is NOT a routing decision, so it does not belong in an artifact loaded once per
orchestration. Sections 0-4 contain both routing decisions and procedural ones, and
only the first kind earns rendered tokens.

*Why this tag exists:* "everything in sections 0-4 renders" put briefing procedure --
including a threshold the file admits is unquantified -- in the first screen the agent
reads, and gave forty-five tokens to a rule that by its own construction should almost
never fire. Procedure the orchestrator applies once is not the same artifact as tests
it evaluates per unit. Tagged today: P0.3, P0.4, P2.5 bullet 3. (P3.6 carried the
tag until its 2026-08-11 supersession -- on a rule whose entire job was runtime
behaviour, the tag was itself the defect.)

```yaml
generator:
  blocks:
    - order: 1
      path: shape
      label: Shape the unit
    - order: 2
      path: backend
      label: Backend
    - order: 3
      path: ladders
      label: Tier
    - order: 4
      path: agent_types
      label: Agent type
    - order: 5
      path: effort
      label: Effort
    - order: 6
      path: announce
      label: Announce every dispatch, in one line
  intra_block_order: principle-number
  intra_block_order_scope: slot
```

### P5.2 -- Two variants, one source

- **Codex available** -- render everything.
- **Codex absent** -- omit block 2 entirely (nothing to choose with one backend) and
  the Codex tier subtree. Principles that depend on a Codex rung say so themselves
  and drop with it. Everything else is backend-independent.

Never author the variants separately; both derive from this file.

**`fan-out` still renders in block 1 without Codex.** Its collapse test is what stops
a one-command job being dispatched as N units, which is useful on either backend --
it is only the *pull* that disappears.

A genuine high-fan-out unit then has no backend answer, and the tree must **say so in
one clause**: "no Codex-side route -- sequence the units or handle them inline."

*Why say it rather than stay silent:* an earlier revision forbade both an invented
answer and any acknowledgement, which is the worst of the three options -- the tree
teaches a test, makes the reader run it, and goes quiet on a positive result. Silence
about a known hole reads as an oversight and invites the reader to invent the answer
the principle was trying to prevent. Naming the absence costs about eight tokens and
invents nothing. This is still a gap in the principles (section 7); disclosing it is
not closing it.

### P5.3 -- Glossing

Per lexicon.md: gloss `render: glossed` terms at first occurrence in document order,
bare thereafter. A gloss must live in a block both variants render.

### P5.4 -- Out of scope

Execution mechanics (launch commands, gotchas, capability bullets), detected
versions, and capacity come from machine data. They render as their own section and
are not derived.

### P5.5 -- Compression

Render decisions, not rationale. No prices, no dates, no now-relative phrasing.

---

## 6. Prices (as of 2026-08-07)

**Not rendered into the tree.** They expire, and a stale price in context is wrong
silently where nobody re-reads it.

| Ladder | Model | Price (in/out per 1M) | Relative |
|---|---|---|---|
| Claude | haiku 4.5 | $1 / $5 | 1x |
| Claude | sonnet 5 | $3 / $15 | 3x |
| Claude | opus 4.8 | $5 / $25 | 5x |
| Claude | fable 5 | $10 / $50 | 10x |
| Codex | gpt-5.6-luna | $0.20 / $1.20 | 1x |
| Codex | gpt-5.6-terra | $2 / $12 | 10x |
| Codex | gpt-5.6-sol | $5 / $30 | 25x |

sonnet 5 carries an introductory rate of $2 / $10 through 2026-08-31. **Revisit P2.1
after that date** -- haiku's margin returns from 2x to 3x.

- The Claude ladder is FLAT: 10x end to end, largest step at the bottom
  (haiku->sonnet, 3x), smallest at the top (opus->fable, 2x). The most heavily
  guarded steps are the cheapest -- coherent only because the top guard defends a
  POOL, not a price (P2.2).
- The Codex ladder is STEEP: 25x across two rungs.
- These are API list prices. For a subscription the operative Claude constraint is
  pool windows, so Claude-side ratios are indicative only and must not be used to
  re-seat rungs.

**Cost is not a primary driver anywhere in this file.** Where a price appears in an
argument it is marked supporting rather than load-bearing.

---

## 7. Known evidence gaps

- **No capability benchmarks for gpt-5.6-luna / gpt-5.6-sol.** The Codex ladder is seated on dispatch
  shape and the absence of a counter-case, not measured capability. P3.4 is a
  decision under uncertainty.
- **No measured pool-consumption data.** P2.2's pool argument is structural, not
  quantified.
- **`multi_agent_v2` is narrow** -- N=8 trivial items on codex-cli 0.146.0 (65s vs
  48s), token accounting inconclusive. Says nothing about independent long-running
  items.
- **No usage telemetry** on which rungs get chosen -- what would settle P2.1. P4.5's
  announcements are designed to produce it.
- **P4.1 (agent type) is unvalidated** -- no data on whether `Explore` outperforms a
  general-purpose agent at the same rung.
- **P0.3's volume threshold is unquantified.** "Many" is undefined and nothing
  establishes where specify-then-execute starts winning.
- **`fan-out` has no Claude-side answer** (P5.2), so the Codex-absent variant is
  silent on high-fan-out work.
