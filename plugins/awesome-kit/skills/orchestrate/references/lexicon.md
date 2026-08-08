# Orchestration lexicon

The controlled vocabulary for orchestration decisions. Three artifacts share it:

- **tier-principles.md** states every criterion in these terms.
- **The rendered decision tree** carries only `[skill]` terms, as branch questions.
- **Dispatch announcements** carry only `[skill]` terms, as labels.

**A term earns its place by having a test answerable at dispatch time.** A
definition that describes rather than tests is a liability: it reads as precision
while admitting any interpretation.

**`[skill]` vs `[concept]`.** A `[skill]` term SELECTS A BRANCH. A term that
justifies or describes a choice already made is `[concept]`: it stays in this file
and never renders. The test is mechanical -- remove the term; does any decision
become undecidable? If not, it is `[concept]`.

**`render: bare` vs `render: glossed`.** The lexicon is never loaded into an
orchestration context, and every orchestration is a fresh read -- there is no
accumulated vocabulary. So a bare term works only when its NATURAL reading already
matches its test. A term that names a thing renders bare; a term that compresses a
judgment renders glossed, because the natural reading diverges from the test and
there is no surrounding prose left to correct it.

**Gloss placement (derivation rule).** Gloss at FIRST occurrence in document order,
bare thereafter -- this says it once and costs less than a glossary block, which
would pay for the term name twice. A gloss must live in a block that BOTH variants
render; a term used in both variants may not have its only gloss inside a
Codex-only block.

---

## Brief shape -- `known` / `open`

Two orthogonal axes govern briefs. This is one; `sufficient` is the other. They were
conflated in an earlier revision, which produced a taxonomy whose categories were
not mutually exclusive.

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

*Independence:* `sufficient` is about the STATEMENT, `known`/`open` about the WORK.
All four combinations occur. "What changed in the repo this week" is `open` and
`sufficient` -- discovery-shaped, and needing no further briefing. Treating
sufficiency as a third shape category is a category error and was a live defect.

---

## Execution demand

What the work does to information. Selects among the Claude rungs. Replaces the
undefined pair `shallow` / `real reasoning`.

### `mechanical` `[concept]`
Execution is checkable by inspection alone.
**Test:** can you verify the result without reading surrounding context?
*Demoted from `[skill]` 2026-08-07:* it selected the haiku rung, and P2.1 removed
that rung. With nothing to select it fails the mechanical test for `[skill]` status --
remove it and no decision becomes undecidable, because `known` + `mechanical` work now
reaches sonnet by fall-through like everything else unescalated.
*Retained because* it is the vocabulary for the population that would justify
re-adding the rung (see P2.1's re-add condition), and because P0.5's collapse test is
still stated in terms of a "mechanical fan-out".

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

Why this rung and not the one below.

### `novel` `[skill]` `render: glossed`
No established pattern applies.
**Test:** can you name the pattern it follows? If yes, it is not novel.
**Gloss:** "you cannot name a pattern it follows"
*Why glossed:* reads naturally as "new" or "hard", neither of which is the test.

### `load-bearing` `[skill]` `render: glossed`
Later work builds on the conclusion.
**Test:** would a wrong answer propagate, or would the next step catch it?
**Gloss:** "a wrong answer would propagate rather than be caught"
*Why glossed:* the natural reading is "important" -- a listed anti-term, and the
single most over-firing misreading in the vocabulary.

### `unverifiable` `[skill]` `render: glossed`
You could not spot a wrong answer by reading the summary.
**Test:** is there a cheap check -- a diff, a test run, a file you would read anyway?
**Gloss:** "no cheap check exists -- no diff, test, or file you would read anyway"

### `default` `[skill]` `render: bare`
No escalation test matched; the unit reached the ladder's terminal rung by
fall-through.
**Test:** did any rung above match? If none did, this is the term.
*Why it exists:* the terminal rung states no criteria of its own, so without this
term the most common dispatch on the ladder has no legal announcement -- putting the
largest hole in the usage record at the largest population.

### `stretch` `[concept]`
The rung below would plausibly get this wrong.
**Test:** can you say what it would get wrong?
*Why concept:* it selects no branch. Each rung's own criteria determine the rung;
this is a sanity check on a choice already made. Retained because it is the honest
generalisation of the top rung's justification demand, and because naming it stops
`difficult` being smuggled in as a criterion.

---

## Dispatch shape

Selects the backend -- with one exception.

**`fan-out` is NOT a Codex-block term.** It is decided during shaping (both variants
render that block) and merely *consumed* as a pull. Its gloss therefore belongs in
the shape block, per the first-occurrence rule. The other five are Codex-block terms
that recur nowhere else, so their glosses live there safely.

*This correction matters:* an earlier revision grouped all six here and asserted they
were Codex-only, which collided with the shaping placement and made the lexicon's own
gloss rule unsatisfiable for `fan-out`.

### `conversational` `[skill]` `render: glossed`
You expect to interrogate or refine the unit mid-flight.
**Test:** is the output a deliverable you act on, or a thread you continue?
**Gloss:** "a thread you continue, not a deliverable you act on"
*Why glossed:* phrased as "will you ask follow-ups" this catches almost anything
and silently disarms `cross-check` -- a second opinion is a DELIVERABLE.

### `abortable` `[skill]` `render: bare`
You may need to stop it mid-run.
**Test:** is there a plausible reason to kill it before it finishes?

### `fan-out` `[skill]` `render: glossed`
N similar units you would otherwise brief individually.
**Test:** does it survive collapsing into one shell command? If not, it is one unit.
**Gloss:** "N similar units that resist collapsing into one command"
*Why glossed:* without the collapse test, any repetitive work reads as fan-out.

### `bulk-output` `[skill]` `render: glossed`
The unit emits substantially more than you need to keep.
**Test:** would you skim and discard most of the output?
**Gloss:** "emits much more than you need to keep"
*Why this replaces `long`:* duration was never the argument and is not evaluable at
dispatch time. The real asymmetry is that an Agent-tool report is DELIVERED into
context whole, while a CLI backend's output lands in a file you can skim and discard.

### `cross-check` `[skill]` `render: glossed`
An independent second opinion on a conclusion Claude already reached.
**Test:** does a conclusion already exist, and is the point to have it challenged by
a different failure mode?
**Gloss:** "a second opinion on a conclusion Claude already reached"

### `schema` `[skill]` `render: bare`
The return shape must be machine-validated.
**Test:** will you parse the result rather than read it?

---

## Anti-terms `[concept]`

Words that feel like criteria and are not. Each has been reached for in practice.

- **`difficult`** -- never a criterion. Hard-but-patterned work belongs a rung
  LOWER, not higher. Say `novel` if no pattern applies, or `stretch` if the rung
  below would plausibly fail.
- **`important`** -- not `load-bearing`. Importance is about the topic;
  `load-bearing` is about whether a wrong answer propagates. Important work with a
  cheap check is not load-bearing in the sense that matters.
- **`complex`** -- usually means `inference` or `fan-out`. Say which.
- **`long`** -- replaced by `bulk-output`. Neither evaluable nor the real argument.
- **`simple`** -- ambiguous between `mechanical` (execution) and `sufficient`
  (brief). Independent axes: "summarize last week's commits" is `sufficient` and
  not `mechanical`. Say which.

---

## Inventory

`[skill]` (16) -- render into the tree; permitted in announcements:

| term | render |
|---|---|
| `known` | glossed |
| `open` | glossed |
| `sufficient` | glossed |
| `underspecified` | bare |
| `default` | bare |
| `condensation` | glossed |
| `inference` | glossed |
| `novel` | glossed |
| `load-bearing` | glossed |
| `unverifiable` | glossed |
| `conversational` | glossed |
| `abortable` | bare |
| `fan-out` | glossed |
| `bulk-output` | glossed |
| `cross-check` | glossed |
| `schema` | bare |

`[concept]` (7) -- this file only: `stretch`, `mechanical`, `difficult`, `important`, `complex`,
`long`, `simple`.

---

## Announcement form

One line per dispatch, terms only, no free prose:

```
delegating <what> to <model> (<terms that fired>)
```

The parenthetical carries the terms from the tree node that ACTUALLY fired -- not
the strongest available justification, not a complete characterisation. Only
`[skill]` terms are permitted, which is what makes the lines aggregate: the
transcript becomes the usage record for which rungs get chosen and why, which is
the telemetry the principles file's evidence gaps currently lack.

```
delegating rename across 30 files to sonnet (known, default)
delegating log relevance sweep to sonnet (open, condensation)
delegating crash diagnosis to opus (open, inference)
delegating architecture review to fable (novel, load-bearing, unverifiable)
delegating per-file API migration to codex/gpt-5.6-luna (fan-out)
```

*The first and last lines are the same file count and route differently -- that is
the collapse test doing its job, and the pair is worth keeping as the worked
example.* A rename is one `sed`, so it never becomes `fan-out` however many files it
touches; it is one `mechanical` unit. A migration needing per-file judgment resists
collapsing, so it is genuinely N units. Scale is not the discriminator and must never
be read as one.

*Top rung:* its gate is a conjunction, so all three terms fire together by
construction -- that is the actual reason, not an inflated one. The gate's
additional demand (saying what the rung below would get wrong) stays internal and
is not rendered on the line.
