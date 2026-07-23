"""Append-only projection delivery: rollback via .bak, never overwrite.

Writes generated content to a standalone projection artifact alongside (not
inside) the authored source -- the append-only counterpart to
``inplace``'s in-place mutation. A write never overwrites the previous
artifact directly; it first moves the existing file to a ``.bak`` sibling,
so rollback is a rename, never a content reconstruction.
"""


def apply_projection(artifact_path, generated: dict) -> None:
    """Write generated content to artifact_path, preserving the previous version as .bak."""
    raise NotImplementedError
