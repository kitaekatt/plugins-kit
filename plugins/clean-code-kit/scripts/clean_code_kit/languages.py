"""The one table mapping a configurable language to the file extensions it owns.

Pruning deletes, from the analysis snapshot, every file whose extension belongs
to a language that the target's config does not list. Extensions that no
language owns are left alone.
"""

from __future__ import annotations

EXTENSIONS: dict[str, tuple[str, ...]] = dict(
    python=(".py",),
    typescript=(".ts", ".tsx", ".mts", ".cts"),
    javascript=(".js", ".jsx", ".mjs", ".cjs"),
)


def unconfigured_extensions(languages: list[str]) -> set[str]:
    """Extensions owned by a language that is not in ``languages``."""
    return {ext for lang, exts in EXTENSIONS.items() if lang not in languages for ext in exts}
