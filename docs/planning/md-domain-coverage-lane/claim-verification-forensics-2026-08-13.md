# Why the ten false claims passed their own verification (2026-08-13)

Companion to `sol-adjudication-2026-08-13.md`, which established WHAT was false.
This document establishes WHY the generation lane did not catch it, from the
recovered run transcripts, and records the acceptance corpus any proposed fix
must be tested against.

## The finding, in one breath

Eight of the ten adjudicated false claims HAVE a `verifications` entry in the
writing agent's own report, and six of those entries record a command that was
CORRECT and that RETURNED WHAT THE WRITER SAID IT RETURNED. The claims are false
anyway, because the sentence written into the document asserts more than the
evidence establishes.

The dominant failure is therefore NOT unchecked assertion. It is a true check
paired with a broader sentence.

## Evidence recovery

The lane persists no per-subject results (it contains no `writeFileSync`). The
final `StructuredOutput` of every writing agent survives in the harness subagent
transcripts for the 2026-08-13 run:

```
~/.claude/projects/D--dev-plugins-kit/049ec0e8-47f0-401c-89f6-1384d5c5efb5/
  subagents/workflows/wf_ec9bf97e-f39/agent-*.jsonl        (12 writers)
  subagents/workflows/wf_25adb0df-c26/agent-aa2c52cd151563b2d.jsonl:35  (root)
```

13 subjects, 133 verification entries -- matching the adjudication's 13
documents. Two entries were re-verified by hand rather than taken on the
analysing agent's report:

- Claim 1 (`.claude/CLAUDE.md`): the verification entry itself contains
  ``_ToolCtx.__init__: os_spec = install.get(current_os) feeds skip (2058),
  scoop_pkg (2059) ...`` -- a line-cited reading of a class that does not exist
  (it is `_ToolEntryCtx`). The falsehood is INSIDE the verification record.
- Claim 2 (`src/effects/CLAUDE.md`): the entry records a real `TABLE_Y` grep
  returning `TableSaw.js` and `MiterSaw.js`. That grep cannot by construction
  see `LatheTool`'s `-maxRadius - 0.3`. The writer's own prose even states that
  the effects copy "is invisible to a `TABLE_Y` grep".

## The two failure modes

**(a) False as executed -- 2 claims (1, 6).** The writer misreported what its
own command returned, or ran a command that could not establish the claim
(claim 6 asserts a module "cannot load under Node" on the evidence of reading
its import lines).

**(b) True as executed, narrower than the sentence -- 6 claims (2, 3, 4, 5, 7,
10).** The command ran, returned correctly, and does not entail the sentence.
Claim 5 is the sharpest instance: it is the 0.53.0 restated-rule discipline
VISIBLY EXECUTING -- the entry reads "the prohibition I wrote is satisfied by
the code in THIS directory, not merely by the candidate" -- and returning true
for a claim about what WOULD happen.

**(c) No entry at all -- 1 claim (9).** Nine entries, none about geometry
disposal.

**(d) No surface at all -- 1 claim (8).** `godot/assets/CLAUDE.md` was produced
by the apply step's create path, whose `APPLY_SCHEMA` has no `verifications`
field.

## What this refutes

- **"Make the writer verify by executing."** Shipped in 0.53.0, executed in this
  run, and six of the ten falsehoods passed a correct execution.
- **Re-running the recorded commands after the write** (queued in
  `generation-deficiencies-and-plan.md:1011-1022`). Refuted twice: many
  `command` values are natural-language descriptions of `Glob`/`Read` tool calls
  rather than runnable shell, and re-running the runnable ones CONFIRMS six of
  the ten falsehoods, because those commands already returned true.
- **Binding each document sentence to a self-reported verification entry.**
  8/10 already have one. It would catch claim 9 and nothing else.
- **A marker scan used as a gate.** Six of ten carry a universal or prohibition
  marker; as a gate it licenses skipping claims 1, 3, 5 and 9. Sound only as an
  escalator deciding how hard a sentence is checked, never whether it is.

## The acceptance corpus

Any proposed fix is credible only if tested claim by claim against this table.
Refutation mode is the adjudication's own classification for 3, 6 and 8; the
rest is downstream judgment, and the adjudication's `checked:` field is recorded
per document rather than per claim -- do not over-fit to the split.

| # | Document | Claim | Verification state | Mode | Marker |
|---|---|---|---|---|---|
| 1 | `.claude` | `_ToolCtx.__init__` exists; `install.<os>` field list exhaustive | entry, false as executed | reading | none |
| 2 | `src/effects` | particle clamp -1 is ALWAYS the shop-table plane | entry, true, narrower | reading | always |
| 3 | `test/fixtures` | raising resolution inherently puts bins on the 0.1 floor | entry, true, narrower | execution | none |
| 4 | `test/fixtures` | EACH `TEST_PIECES` description restates the four thresholds | entry, other predicate | reading | each |
| 5 | `test/fixtures` | a top-level `require` would leave npm tests green | entry, 0.53.0 check, wrong predicate | reading | none |
| 6 | `bots/lib` | `apiClient.js` CANNOT load under Node | entry, false as executed | execution | cannot |
| 7 | `bots/lib` | losing bot credentials makes EVERY submission 429 | entry, true, narrower | reading | every |
| 8 | `godot/assets` | asset without its `.import` sidecar gives the consumer NOTHING | no schema surface | execution | nothing |
| 9 | `src/debug` | a JS reference keeps WebGL resources alive past `dispose()` | no entry | reading | none |
| 10 | `src/devtools` | refreshing one finish arrives as a diff of EVERY thumbnail | entry, true, narrower | reading | every |

## The design constraint this imposes

A fix must judge ENTAILMENT -- does this evidence establish this sentence, or
something narrower -- and it must be judged by a party that did not write the
sentence. Execution capability is a secondary need (3 of 10), not the primary
one; the primary need is an independent reader comparing the evidence's
predicate against the sentence's.

The `src/bench`, `src/cuts` and `src/sim` documents (the adjudication's three
KEEPs) are the only available FALSE-POSITIVE control: this corpus measures a
fix's recall and says nothing about how often it would weaken a true claim.
