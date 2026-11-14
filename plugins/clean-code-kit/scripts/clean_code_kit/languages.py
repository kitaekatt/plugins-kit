"""The one table mapping a configurable language to the file extensions it owns.

Pruning deletes, from the analysis snapshot, every file whose extension belongs
to a language that the target's config does not list. Extensions that no
language owns are left alone.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

_PY = (".py",)
_TS = (".ts", ".tsx", ".mts", ".cts")
_JS = (".js", ".jsx", ".mjs", ".cjs")
_GD = (".gd",)

# Per lane, per configurable language, the extensions the lane's tool reads.
# The tools differ: dryer ignores .c++ and .hxx, which crapper scores.
LANE_EXTENSIONS: dict[str, dict[str, tuple[str, ...]]] = dict(
    duplication=dict(
        python=_PY, typescript=_TS, javascript=_JS, gdscript=_GD,
        cpp=(".cpp", ".cc", ".cxx", ".hpp", ".hh", ".h")),
    complexity=dict(
        python=_PY, typescript=_TS, javascript=_JS, gdscript=_GD,
        cpp=(".cpp", ".cc", ".cxx", ".c++", ".h", ".hpp", ".hh", ".hxx")),
)

# A language's pruning extensions: the union over the lanes that read it.
EXTENSIONS: dict[str, tuple[str, ...]] = {}
for _by_lang in LANE_EXTENSIONS.values():
    for _lang, _exts in _by_lang.items():
        EXTENSIONS[_lang] = tuple(dict.fromkeys(EXTENSIONS.get(_lang, ()) + _exts))


def unconfigured_extensions(languages: list[str]) -> set[str]:
    """Extensions owned by a language that is not in ``languages``."""
    return {ext for lang, exts in EXTENSIONS.items() if lang not in languages for ext in exts}


LANE_LANGUAGES: dict[str, tuple[str, ...]] = {
    lane: tuple(by_lang) for lane, by_lang in LANE_EXTENSIONS.items()}

# dryer skips test and spec JavaScript files on its own; they are not measured.
LANE_SKIPPED_NAME = dict(duplication=(re.compile(r"\.(test|spec)\.[^.]+$"), frozenset(_JS)))


def lane_extensions(lane: str) -> set[str]:
    """Extensions the tool behind ``lane`` reads among the table's languages."""
    return {ext for exts in LANE_EXTENSIONS[lane].values() for ext in exts}


def lane_skips(lane: str, rel: str) -> bool:
    """True when the lane's tool skips ``rel`` by name although it reads the extension."""
    rule = LANE_SKIPPED_NAME.get(lane)
    if rule is None:
        return False
    pattern, exts = rule
    return PurePosixPath(rel).suffix in exts and pattern.search(PurePosixPath(rel).name) is not None


def supported_extensions() -> set[str]:
    """Extensions at least one lane reads."""
    return {ext for lane in LANE_EXTENSIONS for ext in lane_extensions(lane)}


# Extensions of source languages, whether or not any lane reads them. The coverage
# table and init show the ones no lane reads, so an unread language is not buried
# among asset extensions. Compared in lower case.
SOURCE_EXTENSIONS: frozenset[str] = frozenset(
    "." + name for name in (
        "py js jsx mjs cjs ts tsx mts cts gd c h cc cpp hpp cs go rs java kt swift rb php lua "
        "scala clj cljs ex exs sh zig dart m mm "
        "gdshader glsl hlsl shader vert frag comp wgsl metal").split())

KIND_MEASURED = "measured"
KIND_SOURCE_UNMEASURED = "source-unmeasured"
KIND_OTHER = "other"


def kind_of(ext: str) -> str:
    """``measured`` when some lane reads ``ext``, ``source-unmeasured`` for another source language, else ``other``."""
    if ext in supported_extensions():
        return KIND_MEASURED
    return KIND_SOURCE_UNMEASURED if ext in SOURCE_EXTENSIONS else KIND_OTHER
