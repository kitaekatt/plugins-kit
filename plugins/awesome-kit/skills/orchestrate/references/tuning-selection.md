# Tuning how often a rung gets selected

You have read the rendered policy, and some rung is firing more or less often
than you want -- the top rung almost never wins, or the terminal rung swallows
everything. This document is the tuning guide: which keys move selection
frequency, which way, and how far.

It is not the schema. [configuration.md](configuration.md) documents every key,
the layer model, and merge semantics; read it for *what a key is*. Read this for
*which key to turn*.

## The mental model

A rung's selection rate is not set anywhere. It falls out of how hard its
criteria are to satisfy, evaluated by ordered elimination -- first match wins. So
there is no "use fable more" setting, and adding one would be a lie: the only way
to move the rate is to change what the rung asks for, or what the rungs above it
take first.

Two consequences worth holding onto:

- **Criteria compose multiplicatively.** A rung requiring three criteria at once
  is not three times harder to reach than a one-criterion rung -- it is harder by
  the product of each criterion's rejection rate. Adding or removing a single
  conjunct is the largest single move available.
- **Rungs compete.** Loosening a rung takes work away from the rung below it, not
  from nowhere. Every knob below is zero-sum across the ladder.

## The knobs, ranked by travel per risk

Travel = how much it moves the rate. Risk = how bad it is if you overshoot.
The best knob has real travel and reverses cleanly.

| Knob | Travel | Risk | Reverses |
|---|---|---|---|
| `lexicon[].test` / `.gloss` -- reword a criterion | high | low | cleanly |
| `rungs[].criteria` -- add/drop a conjunct | very high | high | cleanly |
| `rungs[].guards` -- the never-list | medium | low | cleanly |
| `rungs[].gate` -- the justification ritual | medium | low | cleanly |
| `rungs[].shape` -- `open` / `known` restriction | high | medium | cleanly |
| `rungs[].disabled: true` | total | total | cleanly |

All of them reverse cleanly, because overrides are sparse: delete the key and the
default returns. That is what makes tuning by observation safe -- you are never
one edit away from an unrecoverable policy.

### Rewording a criterion (start here)

The highest-value knob, because it is usually not a *calibration* problem at all.
A criterion can reject far more than intended because its wording does not say
what it means, and that reads as "the bar is too high" when the real defect is
that the bar is aimed wrong.

The tell: the criterion disqualifies on the existence of some condition rather
than on whether that condition is *relevant*. Compare a check on whether any
cheap verification exists against a check on whether cheap verification would
catch the error that actually matters -- nearly all substantive work has some
shallow check available, so the first form rejects almost everything while the
second rejects only what is genuinely covered.

Before assuming a rung is too strict, read its criteria and ask whether each one
means what you want it to mean. Re-stating is a smaller, safer change than moving
a bar, and it usually explains the symptom better.

```yaml
lexicon:
- id: unverifiable
  test: >-
    <your wording -- what makes an error invisible in YOUR work>
  gloss: >-
    <the short form the rendered tree shows inline>
```

`lexicon[]` merges by `id`, so this patches that one term and leaves every other
untouched.

### Adding or dropping a conjunct

The biggest hammer. Dropping one conjunct from a three-way requirement can move a
rung from rare to common in a single edit, which is exactly why it is the highest
risk on the table.

```yaml
ladders:
- id: agent
  rungs:
  - id: top
    criteria:
    - [novel, load-bearing]
```

Before doing this, satisfy yourself that the conjunct you are dropping is not
guarding a distinct failure. A conjunction is usually three different questions,
not one question asked three ways; dropping the one that guards *consequence*
gets you a rung that fires on interesting-but-harmless work.

### The gate and the guards

Softer than criteria and often enough on their own. A gate demanding a written
justification is a real filter -- the friction is the mechanism, and removing it
raises the rate without changing a single criterion. A never-list is pure
debiasing: it names work that satisfies the criteria on paper but should not go
there anyway.

Reach for these when the criteria are right but the rung is being reached for too
eagerly, or too timidly, in practice.

### Disabling a rung

`disabled: true` removes it from the ladder entirely and sends its work to the
next rung down. Use it when a tier is unavailable to you, not to express a
preference -- a disabled rung renders as absent rather than as declined, so
readers cannot tell you made a choice.

## Measuring what you changed

Turning a knob by feel and turning it by measurement are different activities,
and it is worth knowing which one you are doing. Selection frequency is not
recorded anywhere by default.

The announcement line is the instrument. Every dispatch announces its rung and
the terms that fired, so a period of announcements is a tally: which rungs won,
and which criteria were doing the rejecting. Collect a few weeks, then tune
against the tally rather than the impression.

A felt frequency is a legitimate reason to *start* tuning. It is a poor reason to
stop, because the thing that changed may be the work you happened to do.

## What not to do

- **Do not tune by overriding `capacity.tier_overrides`.** That is a manual
  pin for a specific situation, not a policy control; it will not survive the
  situation it was set for.
- **Do not edit the shipped defaults file in place.** It lives inside the plugin
  and is replaced on every update. Overrides are the supported path, and they
  keep tracking upstream defaults for every key you did not touch.
- **Do not compensate for a wrong rung by loosening the one above it.** Work
  arriving at the wrong rung usually means a criterion is mis-stated; loosening
  the neighbour moves the symptom and leaves the cause.
