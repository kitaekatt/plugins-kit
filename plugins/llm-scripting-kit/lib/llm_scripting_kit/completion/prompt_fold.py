"""Shared prompt-folding logic for the single-stdin CLI backends.

The codex and opencode transports both expose exactly one stdin prompt rather
than a separate system channel, so both fold a system prompt and a user
prompt into one string the same way: system first, separated by the
transport's own separator, with no stray leading/trailing separator when one
half is empty. This was duplicated verbatim in both ``compose_prompt``
functions; this module is the one definition, so a change to the folding rule
cannot update one sibling and miss the other.

Each backend keeps its own ``compose_prompt(system, user) -> str`` wrapper and
its own separator constant -- those are the public per-adapter surface -- and
both now call :func:`fold_prompt` rather than repeating the logic.
"""
from __future__ import annotations


def fold_prompt(system: str, user: str, separator: str) -> str:
    """Fold a system and a user prompt into one stdin-shaped string.

    An empty ``system`` yields ``user`` verbatim (a caller that already
    folded its own instructions gets no stray leading separator); an empty
    ``user`` yields ``system`` verbatim. Otherwise the two halves are joined
    by ``separator``.
    """
    if not system:
        return user
    if not user:
        return system
    return f"{system}{separator}{user}"


__all__ = ["fold_prompt"]
