"""validate -- one-rule-set, many-call-sites.

A single Validator contract (``contract``) that both in-generation-loop and
post-hoc call sites share, so a rule can never drift between "what the agent
checks while generating" and "what the audit checks afterward." Rejection
kinds are tiered hard/soft/advisory. Validators produce deterministic fact
riders (``riders``) that downstream stages reuse instead of re-deriving.
``floor_guard`` is the opt-in, advisory-only diagnostic layer with a
known-good acceptance gate -- guidance, never a hard block a pipeline must
carry. Imports nothing from ``llm``; depends only on the standard library
(and, in ``riders``, on ``contract``).

Deviations from the two source systems' semantics
--------------------------------------------------

1. **Three severity tiers unify two different blocking models.** One model
   raises ``ValueError`` on any violation, making every rule effectively hard.
   Another carries TWO axes: a
   per-rejection ``hard_fail`` bool AND a "soft kinds" set that alone decides
   whether a rejection blocks (an advisory rule with ``hard_fail=False`` still
   blocks; only ``glossary_override_logged`` never blocks). The generic
   ``Severity`` collapses this to one tier: ``HARD`` (always blocks), ``SOFT``
   (blocks by default -- the advisory-but-enforced rule -- demotable via
   ``block_soft=False``), ``ADVISORY`` (never blocks -- the escape-valve /
   floor-guard tier). ``blocks`` / ``is_rejecting`` are the single accept/
   reject predicate every site shares.

2. **A Validator returns a list; the raise is a separate surface.** The
   generic ``Validator`` protocol returns ``list[Rejection]`` rather than
   raising. The raise-on-violation behavior is preserved as
   ``assert_valid``, which aggregates every blocking rejection into one
   ``ValidationError`` -- collecting all errors and raising once -- so the
   post-hoc hard gate reads the same rejections the in-loop
   feedback surface (``format_rejections``) renders.

3. **Riders have two producers, kept distinct.** ``compute_riders`` runs pure
   fact functions (a length, a width); ``facts_from_rejections`` /
   ``rider_from_kind`` MAP a single validation run's rejection kinds into
   ``{ok, detail}`` rider shape. Both feed one candidate rider block via
   ``attach_riders`` (duck-typed over dict and dataclass candidates). The
   rider block never forks the checking logic -- the validation-derived riders
   read the verdict the one ``Validator`` already produced, so they cannot
   drift from the hard contract.

4. **The floor guard gates per-signal on a configurable threshold.** The
   known-good acceptance gate (default 0.10, strict ``<``) is applied to each
   named guard independently (``evaluate_guards``), not to a union flag rate,
   since a union hides which signal is noisy. Population 0 yields a 0.0 rate
   (no evidence rejects a guard). The specific signals stay project-side; only
   the guard protocol and the corpus gate are generic.
"""
