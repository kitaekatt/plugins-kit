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


# The extensions each lane's tool reads, for the languages in EXTENSIONS. crapper
# scores the JavaScript extensions (as TypeScript); dryer ignores them.
LANE_LANGUAGES: dict[str, tuple[str, ...]] = dict(
    duplication=("python", "typescript"),
    complexity=("python", "typescript", "javascript"),
)


def lane_extensions(lane: str) -> set[str]:
    """Extensions the tool behind ``lane`` reads among the table's languages."""
    return {ext for lang in LANE_LANGUAGES[lane] for ext in EXTENSIONS[lang]}


def supported_extensions() -> set[str]:
    """Extensions at least one lane reads."""
    return {ext for lane in LANE_LANGUAGES for ext in lane_extensions(lane)}
