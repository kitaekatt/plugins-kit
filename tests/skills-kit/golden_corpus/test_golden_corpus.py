"""Golden-corpus regression test for the mechanical audit surfaces.

Each case runs its tool TWICE against the staged fixtures: the two live runs
must match each other (idempotency) and must match the committed golden in
expected/. A diff here means audit behavior changed -- either fix the
regression or, if the change is intended, re-record with record.py and
review the expected/ diff as part of the change.

This corpus is the regression harness for the planned md-domain restructure:
the folded lanes must produce these same verdicts on these same fixtures.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_RUNNER_PATH = Path(__file__).resolve().parent / "corpus_runner.py"
_spec = importlib.util.spec_from_file_location("corpus_runner", _RUNNER_PATH)
corpus_runner = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("corpus_runner", corpus_runner)
_spec.loader.exec_module(corpus_runner)


@pytest.fixture(scope="module")
def staged(tmp_path_factory):
    return corpus_runner.stage(tmp_path_factory.mktemp("golden"))


def test_every_case_has_a_golden():
    missing = [c for c in corpus_runner.CASES
               if not (corpus_runner.EXPECTED / f"{c}.json").exists()]
    assert not missing, f"cases without recorded goldens: {missing} (run record.py)"


def test_no_orphan_goldens():
    orphans = [p.name for p in corpus_runner.EXPECTED.glob("*.json")
               if p.stem not in corpus_runner.CASES]
    assert not orphans, f"goldens without cases: {orphans}"


@pytest.mark.parametrize("case_id", sorted(corpus_runner.CASES))
def test_golden(case_id, staged):
    golden_path = corpus_runner.EXPECTED / f"{case_id}.json"
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    run1 = corpus_runner.run_case(case_id, staged)
    run2 = corpus_runner.run_case(case_id, staged)
    assert run1 == run2, f"{case_id}: two identical runs disagree (non-determinism)"
    assert run1 == golden, (
        f"{case_id}: live output diverges from expected/{case_id}.json -- "
        "regression, or an intended change that needs record.py + diff review"
    )
