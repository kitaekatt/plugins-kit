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

# What each tool skips on its own, per lane, so the audit counts those files as
# skipped and not as measured. Both tools share one rule set. Each rule is
# (what it matches, the extensions it applies to or None for all, the tool source).
#   dir  -- a path component (directory) equal to one of the names
#   name -- a regex searched in the file name
_TEST_DIRS = frozenset({"test", "tests", "spec", "specs", "__tests__"})
_JS_TS_NAME = re.compile(r"\.(test|spec)\.(ts|tsx|mts|cts|js|jsx|mjs|cjs)$")
_PY_NAME = re.compile(r"(^conftest\.py$|_test\.py$|^test_.*\.py$)")
_TS_JS_EXTS = frozenset(_TS + _JS)
LANE_SKIP_RULES: dict[str, tuple[tuple[str, object, frozenset[str] | None, str], ...]] = dict(
    # dryer src/dryer/discover.py @ e8bca626: TEST_DIRS line 57, pruned at 103-104 and 110-117,
    # is_test_file 68-100 (JS/TS suffixes 75-95, conftest.py 96, python names 98, dirs 100).
    duplication=(
        ("dir", _TEST_DIRS, None, "dryer discover.py:57,100,104"),
        ("name", _JS_TS_NAME, _TS_JS_EXTS, "dryer discover.py:75-95"),
        ("name", _PY_NAME, frozenset(_PY), "dryer discover.py:96-98"),
    ),
    # crapper src/crapper/discover.py @ 3b7d4b4a: TEST_DIRS line 54, pruned at 110,
    # JS_TEST_SUFFIXES 58-75, is_test_file 86-99 (JS/TS 93, conftest.py 95, python names 97, dirs 99).
    complexity=(
        ("dir", _TEST_DIRS, None, "crapper discover.py:54,99,110"),
        ("name", _JS_TS_NAME, _TS_JS_EXTS, "crapper discover.py:58-75,93"),
        ("name", _PY_NAME, frozenset(_PY), "crapper discover.py:95-97"),
    ),
)

def lane_extensions(lane: str) -> set[str]:
    """Extensions the tool behind ``lane`` reads among the table's languages."""
    return {ext for exts in LANE_EXTENSIONS[lane].values() for ext in exts}


def lane_skips(lane: str, rel: str) -> bool:
    """True when the lane's tool skips ``rel`` on its own (a test directory or test file name)."""
    path = PurePosixPath(rel)
    for kind, rule, exts, _source in LANE_SKIP_RULES[lane]:
        if exts is not None and path.suffix not in exts:
            continue
        if kind == "dir" and any(part in rule for part in path.parts):
            return True
        if kind == "name" and rule.search(path.name) is not None:
            return True
    return False


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
