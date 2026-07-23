"""Content hashing for the two-tier freshness engine.

Computes the source-tier hash (over authored inputs) and the generation-tier
hash (over the generated output plus the inputs that produced it), which
``classify`` cross-references to decide staleness.
"""


def source_hash(content: str) -> str:
    """Hash of an authored input, for the source tier."""
    raise NotImplementedError


def generation_hash(output: str, source_hashes: list) -> str:
    """Hash of a generated output plus the source hashes it depended on."""
    raise NotImplementedError
