"""Alias/no-regression test for the C1 scope amendment
(``dev/tasks/cpk-session-recipients/c1-design.md`` section 5).

The worker-pack and reap assets B1 built were MOVED verbatim out of
``execution/drivers/claude_bg.py`` into the transport-neutral
``execution/workerpack.py``, and ``claude_bg.py`` now re-imports each moved
name rather than redefining it. IDENTITY, not equality, is the whole point:
a name that got re-DEFINED in ``claude_bg.py`` instead of re-IMPORTED from
``workerpack.py`` would still pass an equality check (two functions with the
same body compare unequal by identity but a naive test might compare
behavior instead) -- ``is`` is the only check that actually distinguishes
"the same object, imported" from "a re-typed copy that happens to match
today". See that design doc section 6, test 8.
"""

from __future__ import annotations

import pytest

from content_pipeline.execution import workerpack
from content_pipeline.execution.drivers import claude_bg

# Every symbol design section 5 says was MOVED verbatim, plus the private
# helper (_ENVELOPE_VERBS is intentionally excluded -- it is not imported
# into claude_bg.py at all, since nothing there references it directly).
MOVED_NAMES = (
    "ANSWER_FENCE_PREFIX",
    "AnswerFenceError",
    "AnswerFenceMismatchError",
    "MissingAnswerFenceError",
    "DEFAULT_MAX_RECLAIMS_PER_UNIT",
    "WorkerCommand",
    "_sanitize_path_component",
    "_format_argv",
    "answer_path_for",
    "envelope_path_for",
    "_envelope_payload_text",
    "format_fenced_answer",
    "parse_fenced_answer",
    "worker_envelopes_for",
    "enumerate_worker_invocations",
    "reclaimable_units",
    "reclaim_attempt_count",
    "_terminally_fail_exhausted_unit",
)


@pytest.mark.parametrize("name", MOVED_NAMES)
def test_claude_bg_alias_is_workerpack_symbol(name: str) -> None:
    """``claude_bg.X is workerpack.X`` for every moved name -- identity, not
    equality. A name re-defined instead of re-imported would fail this even
    though it might still "work"."""
    assert hasattr(workerpack, name), f"workerpack.py has no {name!r}"
    assert hasattr(claude_bg, name), f"claude_bg.py has no {name!r}"
    assert getattr(claude_bg, name) is getattr(workerpack, name), (
        f"claude_bg.{name} is not workerpack.{name} -- it was redefined "
        "instead of re-imported"
    )


def test_workerpack_never_imports_claude_bg() -> None:
    """Import-cycle guard (design section 5's import discipline): workerpack.py
    must never import claude_bg -- that module imports names OUT of
    workerpack, so the reverse edge would be a cycle. Checked against actual
    import statements, not a bare substring scan -- workerpack.py's module
    docstring legitimately mentions ``claude_bg.py`` by name."""
    import ast
    import inspect

    source = inspect.getsource(workerpack)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "claude_bg" not in alias.name
        elif isinstance(node, ast.ImportFrom):
            assert node.module is None or "claude_bg" not in node.module


def test_workerpack_and_claude_bg_import_in_both_orders() -> None:
    """Confirms no import cycle exists in practice, not just in the source
    text: both modules are already imported at collection time (via the
    module-level imports above), and re-importing them in the opposite
    order here must not raise."""
    import importlib

    import content_pipeline.execution.drivers.claude_bg as reimported_claude_bg
    import content_pipeline.execution.workerpack as reimported_workerpack

    importlib.reload(reimported_workerpack)
    importlib.reload(reimported_claude_bg)


def test_cli_run_lazy_import_still_resolves() -> None:
    """``cli/run.py``'s deferred ``from ...claude_bg import (AnswerFenceError,
    parse_fenced_answer)`` (used inside its ``submit`` handler) must still
    resolve after the move -- it imports FROM claude_bg, not workerpack, so
    the re-export is what keeps it working."""
    from content_pipeline.execution.drivers.claude_bg import (
        AnswerFenceError,
        parse_fenced_answer,
    )

    assert AnswerFenceError is workerpack.AnswerFenceError
    assert parse_fenced_answer is workerpack.parse_fenced_answer
