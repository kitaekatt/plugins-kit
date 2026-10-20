# Phase 2 -- on a neutral reply: interview

Trigger: the next user message is a bare acknowledgement carrying no content --
`ok`, `k`, `sure`, `fine`, `yeah`, `yep`, `right`, `mm`, `go on`, `continue`,
a lone emoji. Anything with substance is a real answer; treat it as one and
proceed with the work instead.

A neutral reply signals low engagement, so make answering nearly free:

- ONE AskUserQuestion call carrying every pending decision (max 4). Never drip
  one question per turn.
- Each question maps to a numbered decision from Phase 1 (or, after a `return`
  with `greeted` true, from the standing recap's "Waiting on you") -- same
  order, no new ones introduced at interview time.
- Concrete options, not "how would you like to proceed". Recommended option
  first, labelled `(Recommended)`.
- Options state consequences, not restatements of the label.

If more than four decisions are blocking, the work is under-scoped: interview
the top four and say in one line that the rest stay listed.
