"""Regression: normalize() must strip both the lexical and the resolved
staged root (macOS tempdirs resolve through /private)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import corpus_runner  # noqa: E402


def test_normalize_handles_lexical_and_resolved_root(tmp_path):
    real = tmp_path / "real" / "fixtures"
    real.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real")
    lexical = link / "fixtures"
    assert lexical != lexical.resolve()
    obj = {"a": f"{lexical}/x.md", "b": [f"{lexical.resolve()}/y.md"]}
    out = corpus_runner.normalize(obj, lexical)
    assert out == {"a": "<CORPUS>/x.md", "b": ["<CORPUS>/y.md"]}
