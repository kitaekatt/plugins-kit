"""providers -- tiered context-provider registry.

A name -> (callable, tier) registry for the pieces of context a prompt
assembles from. Tiers distinguish unit-agnostic "source" providers (the same
value regardless of which generation unit is running) from parameterized
"generation" providers (per-language, per-variant). ``assembly`` is the
single owner of prompt-block and slot-syntax assembly, so two build sites
structurally cannot drift on how a block is composed.

Submodules:

- ``registry`` -- the ``name -> (callable, tier)`` registry: ``register`` /
  ``provider`` (decorator) / ``resolve`` / ``run_tier`` (assemble a tier's
  outputs into a brief mapping, deterministically ordered).
- ``assembly`` -- the single owner: ``Block`` / ``assemble_blocks`` (ordered
  named blocks, conditional inclusion), ``SlotSyntax`` (configurable-delimiter
  ``${name}`` tokenizer), and label indirection (``assign_labels`` /
  ``invert_labels`` / ``relabel``).

Scope rule (per the plugin's dependency contract): this package may import
nothing beyond stdlib and ``freshness.hashing`` -- it never reaches into
``store``, ``vcs``, or the ``llm`` stack. Providers registered here are pure
context producers; nothing about "which model / which key" belongs in this
package.

Deviations from the two source systems' semantics
--------------------------------------------------

1. **Two tiers named ``source`` / ``generation``, not ``source`` /
   ``translation``.** loc's ``_framework`` names the parameterized tier
   ``translation`` (its variant axis is target language). The generic library
   names it ``generation`` and forwards the variant as extra positional args to
   ``run_tier`` / ``invoke`` rather than a fixed ``lang`` parameter, so a
   consumer whose variant axis is not language still fits.

2. **Uniform ``invoke`` / ``run_tier`` arity, not tier-specific invokers.** The
   source framework has separate ``invoke_source(name, cfg, line)`` and
   ``invoke_translation(name, cfg, line, lang)`` entry points with a tier
   guard. This library has one variadic ``invoke(name, *args)`` and a
   ``run_tier(tier, *args)`` that forwards the caller's args to every provider
   of the tier; the dict-return contract is preserved, the per-tier arity guard
   is the caller's responsibility (it owns what args each tier gets).

3. **Label indirection lives here, not in a request builder.** firstpass keeps
   its opaque-``item_N`` label technique inside its per-conversation
   ``request_builder``. Because the technique is generic (opaque labels defeat
   cross-item collapsing for any batched LLM request), it is promoted to
   ``assembly`` as ``assign_labels`` / ``relabel``.
"""
