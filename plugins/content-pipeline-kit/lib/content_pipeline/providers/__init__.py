"""providers -- tiered context-provider registry.

A name -> (callable, tier) registry for the pieces of context a prompt
assembles from. Tiers distinguish unit-agnostic "source" providers (the same
value regardless of which generation unit is running) from parameterized
"generation" providers (per-language, per-variant). ``assembly`` is the
single owner of prompt-block and slot-syntax assembly, so two build sites
structurally cannot drift on how a block is composed.
"""
