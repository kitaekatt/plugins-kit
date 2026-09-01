"""Unit tests for the workflow-kit openrouter runner.

The runner lazy-imports llm_scripting_kit / openai inside main(), so importing the
module here needs no network and no SDK. The completion-seam tests below install a
fake `llm_scripting_kit` / `llm_scripting_kit.completion` pair into sys.modules
(mirroring the import-guard test's monkeypatch style) rather than reaching for the
real shared lib or the openai SDK.
"""

import importlib.util
import json
import sys
import types
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


def _install_fake_llm_scripting_kit(monkeypatch, *, complete_impl, classify_halt_impl=None):
    """Install a fake llm_scripting_kit + llm_scripting_kit.completion pair.

    Stands in for the real shared lib the same way the import-guard test above
    stands in for its absence (monkeypatch.setitem(sys.modules, ...)), so these
    tests need neither llm_scripting_kit nor the openai SDK installed.
    """
    if classify_halt_impl is None:
        classify_halt_impl = lambda self, exc: None  # noqa: E731

    class ModelResolveError(Exception):
        pass

    def resolve_model(model, *, cheap=False, project_root=None):
        if model:
            return model
        return "cheap-default" if cheap else "default-model"

    fake_pkg = types.ModuleType("llm_scripting_kit")
    fake_pkg.ModelResolveError = ModelResolveError
    fake_pkg.resolve_model = resolve_model

    class BackendOptions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class OpenRouterBackend:
        def __init__(self, *args, **kwargs):
            pass

        def complete(self, system, user, *, model, options=None):
            return complete_impl(system, user, model=model, options=options)

        def classify_halt(self, exc):
            return classify_halt_impl(self, exc)

    fake_completion = types.ModuleType("llm_scripting_kit.completion")
    fake_completion.BackendOptions = BackendOptions
    fake_completion.OpenRouterBackend = OpenRouterBackend

    monkeypatch.setitem(sys.modules, "llm_scripting_kit", fake_pkg)
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.completion", fake_completion)


def test_success_writes_reply_and_status_via_seam(monkeypatch, tmp_path):
    class _Resp:
        text = "hello from seam"
        model = "qwen/qwen3-32b"

    seen = {}

    def complete_impl(system, user, *, model, options=None):
        seen["system"] = system
        seen["user"] = user
        seen["model"] = model
        return _Resp()

    _install_fake_llm_scripting_kit(monkeypatch, complete_impl=complete_impl)

    out = tmp_path / "out.txt"
    status = tmp_path / "status.json"
    rc = orr.main([
        "--prompt", "hi there",
        "--system", "be terse",
        "--out", str(out),
        "--status", str(status),
        "--model", "qwen/qwen3-32b",
    ])

    assert rc == 0
    assert seen == {"system": "be terse", "user": "hi there", "model": "qwen/qwen3-32b"}
    assert out.read_text(encoding="utf-8") == "hello from seam"
    payload = json.loads(status.read_text(encoding="utf-8"))
    assert payload == {
        "ok": True,
        "model": "qwen/qwen3-32b",
        "bytes": len("hello from seam".encode("utf-8")),
    }


def test_halt_error_from_seam_sets_status_and_exit_1(monkeypatch, tmp_path):
    class _FakeAuthError(Exception):
        pass

    def complete_impl(system, user, *, model, options=None):
        raise _FakeAuthError("401 unauthorized")

    def classify_halt_impl(self, exc):
        return "auth" if isinstance(exc, _FakeAuthError) else None

    _install_fake_llm_scripting_kit(
        monkeypatch, complete_impl=complete_impl, classify_halt_impl=classify_halt_impl
    )

    out = tmp_path / "out.txt"
    status = tmp_path / "status.json"
    rc = orr.main(["--prompt", "hi", "--out", str(out), "--status", str(status)])

    assert rc == 1
    assert not out.exists()
    payload = json.loads(status.read_text(encoding="utf-8"))
    assert payload == {"ok": False, "error": "_FakeAuthError", "halt": "auth"}


def test_runtime_error_from_seam_sets_status_with_no_halt_key(monkeypatch, tmp_path):
    def complete_impl(system, user, *, model, options=None):
        raise RuntimeError("no API key resolved")

    _install_fake_llm_scripting_kit(monkeypatch, complete_impl=complete_impl)

    out = tmp_path / "out.txt"
    status = tmp_path / "status.json"
    rc = orr.main(["--prompt", "hi", "--out", str(out), "--status", str(status)])

    assert rc == 1
    assert not out.exists()
    payload = json.loads(status.read_text(encoding="utf-8"))
    assert payload == {"ok": False, "error": "RuntimeError"}
