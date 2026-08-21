# Lexicon derivation notes

Maintainer-only. This file holds the authoring rules and per-term derivation
history for `plugins/awesome-kit/skills/orchestrate/references/lexicon.md` --
the published controlled vocabulary that `scripts/generate_orchestration.py`
reads to build the lexicon block of `orchestration.yaml`. Nothing here ships
to a consumer; a consumer's plugin cache has no `docs/` tree, and none of this
content is a generator input (the generator parses only the `###` headings,
`**Test:**` / `**Gloss:**` lines, and the one-line definition under each
heading -- see `is_derivation_input` and `parse_lexicon` in
`scripts/generate_orchestration.py`).

Kept out of the published file per the plugin-opinion razor's OP-1 (no
maintainer-only material on a published surface,
[../plugin-opinion-razor.md](../plugin-opinion-razor.md)): this doc cites
`tier-principles.md` criteria by number (P2.1, P0.5) and records design
history (demotions, corrections, retired categories) that a consumer who
never reads `tier-principles.md` has no use for. `lexicon.md` itself stays in
the skill's `references/` -- it is genuine vocabulary a consumer reads
alongside their rendered decision tree, not build plumbing.

## Authoring rules

**A term earns its place by having a test answerable at dispatch time.** A
definition that describes rather than tests is a liability: it reads as
precision while admitting any interpretation.

**`[skill]` vs `[concept]`.** A `[skill]` term SELECTS A BRANCH. A term that
justifies or describes a choice already made is `[concept]`: it stays in the
lexicon and never renders. The test is mechanical -- remove the term; does any
decision become undecidable? If not, it is `[concept]`.

**`render: bare` vs `render: glossed`.** The lexicon is never loaded into an
orchestration context, and every orchestration is a fresh read -- there is no
accumulated vocabulary. So a bare term works only when its NATURAL reading
already matches its test. A term that names a thing renders bare; a term that
compresses a judgment renders glossed, because the natural reading diverges
from the test and there is no surrounding prose left to correct it.

**Gloss placement.** Gloss at FIRST occurrence in document order, bare
thereafter -- this says it once and costs less than a glossary block, which
would pay for the term name twice. A gloss must live in a block that BOTH
variants render; a term used in both variants may not have its only gloss
inside a Codex-only block.

**`fan-out` gloss placement, specifically.** `fan-out` is NOT a Codex-block
term -- it is decided during shaping (both variants render that block) and
merely *consumed* as a pull during dispatch. Its gloss therefore belongs in
the "Dispatch shape" block, per the first-occurrence rule above, not in a
Codex-only block. The other five dispatch-shape terms are Codex-block terms
that recur nowhere else, so their glosses live there safely. (An earlier
revision grouped all six as Codex-only, which collided with the shaping
placement and made the gloss-placement rule unsatisfiable for `fan-out`.)

## Per-term derivation history

Design history and principle citations for terms in the published lexicon, by
section. Each entry names the published term; edit the published file's
one-line definition/Test/Gloss first, then update this history, per the
one-way-authorship rule in [CLAUDE.md](CLAUDE.md).

- **Brief shape (`known` / `open`).** The two brief axes -- shape
  (`known`/`open`) and sufficiency (`sufficient`/`underspecified`) -- were
  conflated in an earlier revision, which produced a taxonomy whose
  categories were not mutually exclusive. They are now kept strictly
  orthogonal: `sufficient` is about the STATEMENT, `known`/`open` about the
  WORK. All four combinations occur -- "what changed in the repo this week"
  is `open` and `sufficient`, discovery-shaped but needing no further
  briefing. Treating sufficiency as a third shape category was a live defect
  in that earlier revision.

- **`mechanical` `[concept]`.** Demoted from `[skill]` 2026-08-07: it used to
  select the haiku rung, and P2.1 removed that rung. With nothing left to
  select, it fails the mechanical test for `[skill]` status -- removing it as
  a skill term leaves no decision undecidable, because `known` + `mechanical`
  work now reaches sonnet by fall-through like everything else unescalated.
  Retained as a `[concept]` because it is still the vocabulary for the
  population that would justify re-adding the rung (see P2.1's re-add
  condition), and because P0.5's collapse test is still stated in terms of a
  "mechanical fan-out".

- **`novel` `render: glossed`.** Glossed because the bare term reads
  naturally as "new" or "hard", neither of which is the test.

- **`load-bearing` `render: glossed`.** Glossed because the natural reading
  is "important" -- a listed anti-term, and the single most over-firing
  misreading in the vocabulary.

- **`stretch` `[concept]`.** Selects no branch; each rung's own criteria
  determine the rung, and `stretch` is a sanity check on a choice already
  made. Retained as the honest generalisation of the top rung's justification
  demand, and because naming it stops `difficult` being smuggled in as a
  criterion.

- **`authored-here` `render: glossed`.** Added 2026-08-21 with the P0.7
  revision (review independence at every rung). A gpt-5.6-sol cross-check of
  the first draft refuted "route the review to the rung above the author" as
  unimplementable: under ordered elimination a rung is reached only through
  its criteria, and authorship was not one. The term exists so that P2.2 can
  carry `[authored-here, load-bearing]` as a real criterion, and so that the
  announcement record can show how often review independence -- rather than
  the escalation terms -- put a review on the top rung. Scoped deliberately to
  artifacts the orchestrator drafted inline: a delegated unit's output is
  never `authored-here`, which is what keeps "review of own work" from
  reading as "review of every agent's output".

- **`plan-checkpoint` `render: glossed`.** Added 2026-08-11 with P0.6-P0.8
  (the plan-is-a-unit shaping principles). A `[skill]` term rather than a
  criteria alias because the announcement telemetry must be able to show
  whether those principles caused a dispatch -- without a term of its own,
  a plan review announces identically to any other fable review and
  over-firing cannot be measured (the gpt-5.6-sol design review of the
  proposal flagged exactly this). Glossed because the bare term reads as
  "an important plan"; the test is narrower -- other units will be BRIEFED
  from the output. Deliberately does NOT imply `open`, `novel`, or
  `load-bearing`: an earlier draft derived `load-bearing` "by construction"
  from planhood, which conflates dependency with propagation (`load-bearing`
  asks whether the next step would CATCH a wrong answer). The published
  *Consequence* line carries that negative so the term cannot be read as an
  escalation shortcut. A companion `brief-transferable` term for the
  creation-vs-review fork was considered and skipped: the fork is visible in
  the dispatch line's `<what>` ("plan creation" vs "plan review"), so a term
  would duplicate telemetry the line already carries.

- **`conversational` `render: glossed`.** Glossed because "will you ask
  follow-ups" catches almost anything and silently disarms `cross-check` -- a
  second opinion is a DELIVERABLE, not a thread.

- **`fan-out` `render: glossed`.** Glossed because without the collapse test,
  any repetitive work reads as fan-out.

- **`bulk-output` `render: glossed`, replaces `long`.** Duration was never
  the argument and is not evaluable at dispatch time. The real asymmetry is
  that an Agent-tool report is DELIVERED into context whole, while a CLI
  backend's output lands in a file you can skim and discard.

## Term inventory (duplicate of the generated list)

Hand-maintained cross-check against the generated policy; keep in sync when
adding or removing a term, but treat `orchestration.yaml`'s generated
`lexicon` block as authoritative if the two ever disagree.

`[skill]` (17) -- render into the tree; permitted in announcements:

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
| `plan-checkpoint` | glossed |
| `authored-here` | glossed |
| `conversational` | glossed |
| `abortable` | bare |
| `fan-out` | glossed |
| `bulk-output` | glossed |
| `cross-check` | glossed |
| `schema` | bare |

`[concept]` (7) -- lexicon.md only: `stretch`, `mechanical`, `difficult`,
`important`, `complex`, `long`, `simple`.

## Announcement telemetry

The announcement line's parenthetical -- terms from the tree node that
actually fired -- is also the usage record: which rungs get chosen and why.
That transcript is the telemetry the principles file's evidence gaps
currently lack (see `tier-principles.md`'s evidence-gap notes).
