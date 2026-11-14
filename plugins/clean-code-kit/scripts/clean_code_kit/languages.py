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


# Extensions of source languages, whether or not any lane reads them. The coverage
# table and init show the ones no lane reads, so an unread language is not buried
# among asset extensions. Compared in lower case.
SOURCE_EXTENSIONS: frozenset[str] = frozenset(
    "." + name for name in (
        "py js jsx mjs cjs ts tsx mts cts gd c h cc cpp hpp cs go rs java kt swift rb php lua "
        "scala clj cljs ex exs sh zig dart m mm").split())

KIND_MEASURED = "measured"
KIND_SOURCE_UNMEASURED = "source-unmeasured"
KIND_OTHER = "other"


def kind_of(ext: str) -> str:
    """``measured`` when some lane reads ``ext``, ``source-unmeasured`` for another source language, else ``other``."""
    if ext in supported_extensions():
        return KIND_MEASURED
    return KIND_SOURCE_UNMEASURED if ext in SOURCE_EXTENSIONS else KIND_OTHER
