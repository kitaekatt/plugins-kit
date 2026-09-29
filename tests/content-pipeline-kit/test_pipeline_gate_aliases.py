"""Alias test: ``Gate`` / ``run_gates`` live in ``pipeline/gate.py`` and
``pipeline/single_pass.py`` re-exports the SAME objects.

Identity (``is``), not equality: a class re-defined in ``single_pass`` would
fail this even though an ``isinstance``-blind behavior check might pass.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from content_pipeline.pipeline import gate, single_pass


@pytest.mark.parametrize("name", ("Gate", "run_gates"))
def test_single_pass_alias_is_gate_symbol(name: str) -> None:
    assert getattr(single_pass, name) is getattr(gate, name)
    assert name in single_pass.__all__


def test_gate_module_does_not_import_single_pass() -> None:
    tree = ast.parse(Path(gate.__file__).read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
            imported.extend(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
    assert not any("single_pass" in m for m in imported), imported


def test_controller_uses_gate_module() -> None:
    from content_pipeline.execution import controller

    assert controller.Gate is gate.Gate
    assert controller.run_gates is gate.run_gates
