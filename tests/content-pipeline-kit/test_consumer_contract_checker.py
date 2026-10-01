"""The shipped consumer checker reports removed and deprecated names."""

import importlib.util
import json
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "content-pipeline-kit"
_spec = importlib.util.spec_from_file_location(
    "check_consumer_contract", PLUGIN / "scripts" / "check_consumer_contract.py"
)
checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(checker)


def test_guarded_sweep_import_is_reported_removed(tmp_path, capsys):
    (tmp_path / "loc.py").write_text(
        "from content_pipeline.cli.budget import guarded_sweep, BudgetStop\n", encoding="utf-8"
    )
    assert checker.main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "cli.budget.guarded_sweep" in out and "REMOVED" in out
    assert "BudgetStop --" not in out


def test_removed_kwarg_and_module_alias_uses(tmp_path, capsys):
    (tmp_path / "a.py").write_text(
        "import content_pipeline.llm.backends as b\n"
        "from content_pipeline.execution.adapter import RunAdapter\n"
        "x = b.BACKEND_ENV\nRunAdapter(reconcile=None)\n",
        encoding="utf-8",
    )
    assert checker.main([str(tmp_path / "a.py")]) == 1
    out = capsys.readouterr().out
    assert "BACKEND_ENV" in out and "RunAdapter.reconcile" in out


def test_clean_consumer_exits_zero(tmp_path):
    (tmp_path / "ok.py").write_text(
        "from content_pipeline.cli.budget import BudgetStop\n", encoding="utf-8"
    )
    assert checker.main([str(tmp_path)]) == 0


def test_deprecated_reported_and_strict_fails(tmp_path, capsys):
    contract = tmp_path / "c.json"
    contract.write_text(
        json.dumps({"entries": [{"name": "content_pipeline.x.old", "status": "deprecated",
                                 "since": "1.0.0", "remove_after": "1.2.0", "replacement": "new"}]}),
        encoding="utf-8",
    )
    src = tmp_path / "s.py"
    src.write_text("from content_pipeline.x import old\n", encoding="utf-8")
    assert checker.main([str(src), "--contract", str(contract)]) == 0
    assert "DEPRECATED" in capsys.readouterr().out
    assert checker.main([str(src), "--contract", str(contract), "--strict"]) == 1
