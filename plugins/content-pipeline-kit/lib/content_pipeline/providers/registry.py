"""Name -> (callable, tier) provider registry.

Tiers: ``source`` (unit-agnostic -- the same value no matter which
generation unit is running) and ``generation`` (parameterized per-language,
per-variant, or similar). Registering a provider under a name lets prompt
assembly reference it by label rather than importing the provider function
directly, which is what lets ``providers.assembly`` be the single owner of
how those labels compose into a prompt block.
"""


def register(name: str, callable_, tier: str) -> None:
    """Register a provider callable under a name and tier ('source' | 'generation')."""
    raise NotImplementedError


def resolve(name: str):
    """Look up a registered provider callable by name."""
    raise NotImplementedError
