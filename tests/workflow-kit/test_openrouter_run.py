"""Unit tests for the workflow-kit openrouter runner's pure helpers.

The runner lazy-imports llm_scripting_kit / openai inside main(), so importing the
module here needs no network and no SDK.
"""

import importlib.util
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "workflow-kit" / "scripts" / "openrouter_run.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("workflow_kit_openrouter_run", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


orr = _load()


def test_build_messages_user_only():
    assert orr.build_messages("hi") == [{"role": "user", "content": "hi"}]


def test_build_messages_with_system():
    assert orr.build_messages("hi", "sys") == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]


def test_missing_llm_scripting_kit_exits_2_via_single_guard(monkeypatch, capsys, tmp_path):
    # W11: the two duplicated import guards are merged into one. Force the
    # import to fail (None in sys.modules raises ImportError) and check the
    # single actionable message; pin the dedup structurally.
    import sys

    monkeypatch.setitem(sys.modules, "llm_scripting_kit", None)
    rc = orr.main(["--prompt", "hi", "--out", str(tmp_path / "o.txt")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "llm_scripting_kit not importable" in err
    assert "shared-libs .pth" in err
    assert _SCRIPT.read_text(encoding="utf-8").count("from llm_scripting_kit import") == 1
