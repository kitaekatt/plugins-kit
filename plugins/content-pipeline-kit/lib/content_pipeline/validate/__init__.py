"""validate -- one-rule-set, many-call-sites.

A single Validator contract (``contract``) that both in-generation-loop and
post-hoc call sites share, so a rule can never drift between "what the agent
checks while generating" and "what the audit checks afterward." Rejection
kinds are tiered hard/soft/advisory. Validators produce deterministic fact
riders (``riders``) that downstream stages reuse instead of re-deriving.
``floor_guard`` is the opt-in, advisory-only diagnostic layer with a
known-good <10% acceptance gate -- guidance, never a hard block a pipeline
must carry.
"""
