# Tuning routing-row selection

You have read the rendered policy and a routing row is matching more or less often than you
want. This document describes which configuration keys change that frequency. It is not the
schema; [configuration.md](configuration.md) documents every key, layer, and merge rule.

## The mental model

Selection is the result of ordered rows. A row matches when every shape term in its `shape`
list applies to the unit. The first matching row wins. There is no separate frequency setting.
To change frequency, change the shape test, row order, or the model list attached to a row.

Two consequences matter:

- A row with more shape terms is narrower because all of its terms must match.
- Row order is substantive. Moving a broad row above a narrow row diverts work from the narrow
  row; moving it below makes it a fallback.

## The knobs, ranked by travel per risk

| Knob | Travel | Risk | Reverses |
|---|---|---|---|
| `lexicon[].test` / `.gloss` -- reword a shape term | high | low | cleanly |
| `routing[].shape` -- add or remove a shape term | very high | high | cleanly |
| `routing` row order -- move a row | very high | high | cleanly |
| `routing[].models` -- change the declared models or their order | medium | medium | cleanly |
| `routing[].gate` / `.guards` -- refine the decision ritual | medium | low | cleanly |
| `routing[].shape: []` -- change the default row | total | total | cleanly |

Overrides are sparse. A routing override replaces the complete list, so copy every row you
intend to retain before editing it. Delete the override to restore the shipped list.

### Rewording a shape term

Start here when a row rejects too much or too little. The issue may be a test that describes a
different question from the one the row needs to answer. Change one lexicon record by id:

```yaml
lexicon:
- id: novel
  test: >-
    <your wording -- what makes the unit lack an established pattern>
  gloss: >-
    <the short form the rendered policy shows inline>
```

The routing shape name stays stable while its dispatch-time test becomes clearer.

### Adding or dropping a shape term

This is the strongest routing adjustment. A row containing `[novel, load-bearing]` requires
both signals; `[novel]` is broader. Dropping a term can divert a large population, so name the
failure mode that the term had been excluding before removing it.

```yaml
routing:
- shape: [novel, load-bearing]
  models: [fable, sol]
```

### Moving a row

Rows are an ordered list, not an unordered set. Keep narrow rows above broad rows when the
narrow case should win. Keep the empty-shape row last unless it is intentionally the only row.

### Changing model priority

A row's `models` is a model declaration, in preference order. Entries with a pace reading are
re-sorted by pace in the rendered menu, entries without one keep their places, and the first
usable entry is the default. Later entries are the other choices and the re-selection targets
after a failed dispatch. An id is a core id (`fable`, `opus`, `sonnet`, `haiku`) or an entry
exposed by `llm_scripting_kit`.

```yaml
routing:
- shape: [fan-out]
  models: [luna, sonnet]
```

The renderer leaves out, without comment, an id that resolves to nothing or that this policy
cannot drive. A row with no usable entry shows the floor instead of a menu: it names every
declared id and why it cannot run here.

### The gate and guards

A gate is a required justification before dispatch. A guard names a case that must not take the
row. Use these when the shape terms are correct but the decision needs an explicit check or a
debiasing instruction. They do not change matching by themselves; they make the decision
visible to the orchestrator.

## Measuring what changed

The announcement line is the instrument. It records the target and the shape terms from the
matched row. A choice from a multi-entry row also records a `route:` line naming the entry and
the reason. Collect a sample of
announcements, compare row frequencies, and adjust one key at a time.

A felt frequency is a legitimate reason to start tuning. It is a poor reason to stop, because
the work mix may be the thing that changed.

## What not to do

- Do not tune by changing `capacity`. Capacity reports account-wide windows and does not select
  a model.
- Do not add command text to a routing row. Command construction belongs to the machine
  backend's provider seam.
- Do not edit the shipped defaults file as a user. Put the complete replacement list in the
  user or project layer.
- Do not fix a wrong row by broadening a different row. Correct the shape test or row order
  that caused the mismatch.
