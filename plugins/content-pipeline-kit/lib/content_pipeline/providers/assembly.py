"""Single-owner prompt-block and slot-syntax assembly.

The one place that turns a set of registered provider labels plus a
slot-syntax template (e.g. ``${glossary}``) into an assembled prompt block.
Being the single owner structurally prevents drift between build sites --
two call sites that both need "the glossary block" get it from the same
assembly path rather than each formatting it slightly differently.
"""


def assemble(template: str, providers: dict) -> str:
    """Fill a slot-syntax template's ``${name}`` slots from resolved provider values."""
    raise NotImplementedError
