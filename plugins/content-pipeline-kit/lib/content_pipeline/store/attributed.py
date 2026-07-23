"""3-way attributed fields with human-always-wins precedence.

Every stored field carries three slices: ``sourced`` (the authored/original
value), ``_machine`` (the last machine-generated value), and ``_human`` (a
human correction, if any). The effective value is resolved by a fixed
precedence -- human, when present, always wins over machine, which always
wins over sourced. This is the do-no-harm boundary baked into the data model
itself: a regeneration pass can never silently clobber a human edit, because
the precedence rule is structural, not a runtime check a caller could forget.
"""


def effective_value(sourced, machine, human):
    """Resolve the 3-way attributed value: human > machine > sourced."""
    raise NotImplementedError


def merge_preserved_fields(existing: dict, incoming: dict) -> dict:
    """Merge a freshly generated record onto an existing one, preserving human fields."""
    raise NotImplementedError
