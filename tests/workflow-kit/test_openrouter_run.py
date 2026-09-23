"""Unit tests for the workflow-kit openrouter runner.

The runner lazy-imports llm_scripting_kit / openai inside main(), so importing the
module here needs no network and no SDK. The import-guard tests install fake
modules into sys.modules. The dispatch tests use the REAL llm_scripting_kit
(its lib/ is put on sys.path, standing in for the bootstrap shared-lib .pth)
against the shipped registry, with HOME isolated, the reachability probe
answered, and OpenRouterBackend.complete replaced -- so no network is touched
and no openai SDK is needed.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

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


def test_llm_scripting_kit_too_old_exits_2_with_a_distinct_message(monkeypatch, capsys, tmp_path):
    # llm_scripting_kit is present (unlike the absent-package case above) but
    # predates the declaration API the node dispatches through
    # (create_transport_backend is its newest symbol) -- a venv linked against
    # an older shared lib. The message must name a minimum version and must not
    # be the same text as the absent-package message.
    fake_pkg = types.ModuleType("llm_scripting_kit")
    fake_pkg.ModelResolveError = Exception
    fake_pkg.resolve_model = lambda *a, **k: "m"
    fake_completion = types.ModuleType("llm_scripting_kit.completion")
    fake_completion.BackendOptions = object
    fake_completion.OpenRouterBackend = object  # create_transport_backend deliberately absent

    monkeypatch.setitem(sys.modules, "llm_scripting_kit", fake_pkg)
    monkeypatch.setitem(sys.modules, "llm_scripting_kit.completion", fake_completion)

    rc = orr.main(["--prompt", "hi", "--out", str(tmp_path / "o.txt")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "0.46.0" in err
    assert "claude plugin update llm-scripting-kit@plugins-kit" in err
    assert "llm_scripting_kit not importable" not in err


# --------------------------------------------------------------------------- #
# real seam: BackendOptions leaves temperature unset so the provider picks it
# --------------------------------------------------------------------------- #
def test_real_seam_backend_options_temperature_defaults_to_none(lsk):
    assert lsk["completion"].BackendOptions().temperature is None


# --------------------------------------------------------------------------- #
# Migration step 9 (W2): the node's `--model` is a model declaration of
# transport ENTRIES, dispatched through llm_scripting_kit.declaration.run. A
# non-transport id is skipped silently; nothing usable is the typed floor,
# propagated as exit 2 with the itemised dispositions in the status file.
# --------------------------------------------------------------------------- #
_LSK_LIB = Path(__file__).resolve().parents[2] / "plugins" / "llm-scripting-kit" / "lib"


class _Resp:
    def __init__(self, text, model):
        self.text = text
        self.model = model


@pytest.fixture
def lsk(monkeypatch, tmp_path):
    """The real llm_scripting_kit on an isolated HOME, probing nothing."""
    monkeypatch.syspath_prepend(str(_LSK_LIB))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("MODEL_ENDPOINTS_REGISTRY", raising=False)
    monkeypatch.chdir(tmp_path)
    from llm_scripting_kit import completion, declaration
    from llm_scripting_kit.reachability import STATUS_REACHABLE, Reachability

    reach = {"status": STATUS_REACHABLE}
    monkeypatch.setattr(
        declaration, "check_many",
        lambda entries, **_kw: {n: Reachability(reach["status"], "models-probe", "x") for n in entries},
    )
    calls = []
    behaviour = {"raise": {}, "halt": None}

    def complete(self, system, user, *, model, options=None):
        calls.append({"endpoint": self.endpoint, "model": model, "system": system, "user": user})
        exc = behaviour["raise"].get(model)
        if exc is not None:
            raise exc
        return _Resp(f"reply from {model}", model)

    monkeypatch.setattr(completion.OpenRouterBackend, "complete", complete)
    monkeypatch.setattr(
        completion.OpenRouterBackend, "classify_halt",
        lambda self, exc: behaviour["halt"] if behaviour["raise"] else None,
    )
    return {"calls": calls, "behaviour": behaviour, "reach": reach,
            "completion": completion, "declaration": declaration}


def _run(tmp_path, *extra):
    out = tmp_path / "out.txt"
    status = tmp_path / "status.json"
    rc = orr.main(["--prompt", "hi there", "--system", "be terse",
                   "--out", str(out), "--status", str(status), *extra])
    payload = json.loads(status.read_text(encoding="utf-8")) if status.exists() else None
    return rc, out, payload


def test_a_transport_entry_runs(lsk, tmp_path, capsys):
    rc, out, payload = _run(tmp_path, "--model", "or-qwen")
    assert rc == 0
    assert lsk["calls"] == [{"endpoint": "or-qwen", "model": "qwen/qwen3-32b",
                             "system": "be terse", "user": "hi there"}]
    assert out.read_text(encoding="utf-8") == "reply from qwen/qwen3-32b"
    assert payload == {"ok": True, "entry": "or-qwen", "model": "qwen/qwen3-32b",
                       "bytes": len("reply from qwen/qwen3-32b")}
    assert capsys.readouterr().err == ""


def test_no_model_uses_the_default_declaration_and_cheap_selects_within_it(lsk, tmp_path):
    assert _run(tmp_path)[0] == 0
    assert _run(tmp_path, "--cheap")[0] == 0
    assert [(c["endpoint"], c["model"]) for c in lsk["calls"]] == [
        ("openrouter", "openai/gpt-4o-mini"),
        ("openrouter", "qwen/qwen3-32b"),
    ]


def test_a_harness_id_is_skipped_silently(lsk, tmp_path, capsys):
    rc, _out, payload = _run(tmp_path, "--model", "sol,or-gpt-mini")
    assert rc == 0
    assert payload["entry"] == "or-gpt-mini"
    assert capsys.readouterr().err == ""


def test_nothing_usable_propagates_the_itemised_floor(lsk, tmp_path, capsys):
    rc, out, payload = _run(tmp_path, "--model", "sol,typo")
    assert rc == 2
    assert not out.exists()
    assert lsk["calls"] == []
    assert payload["ok"] is False and payload["error"] == "NoUsableRoutingTarget"
    assert [(d["id"], d["disposition"]) for d in payload["dispositions"]] == [
        ("sol", "unroutable"), ("typo", "unresolved"),
    ]
    err = capsys.readouterr().err
    assert "no usable routing target" in err and err.index("sol") < err.index("typo")


def test_an_unreachable_only_declaration_reaches_the_floor(lsk, tmp_path):
    from llm_scripting_kit.reachability import STATUS_UNREACHABLE

    lsk["reach"]["status"] = STATUS_UNREACHABLE
    rc, _out, payload = _run(tmp_path, "--model", "or-qwen")
    assert rc == 2
    assert payload["dispositions"][0]["disposition"] == "unreachable"


def test_a_classified_halt_moves_to_the_next_entry(lsk, tmp_path):
    lsk["behaviour"]["raise"] = {"qwen/qwen3-32b": Exception("401 unauthorized")}
    lsk["behaviour"]["halt"] = "auth"
    rc, _out, payload = _run(tmp_path, "--model", "or-qwen,or-gpt-mini")
    assert rc == 0
    assert payload["entry"] == "or-gpt-mini"
    assert [c["endpoint"] for c in lsk["calls"]] == ["or-qwen", "or-gpt-mini"]


def test_a_halt_on_the_only_entry_is_an_attempt_limit_with_its_halt(lsk, tmp_path):
    lsk["behaviour"]["raise"] = {"qwen/qwen3-32b": Exception("401 unauthorized")}
    lsk["behaviour"]["halt"] = "auth"
    rc, out, payload = _run(tmp_path, "--model", "or-qwen")
    assert rc == 1
    assert not out.exists()
    assert payload["ok"] is False
    assert payload["error"] == "attempt-limit"
    assert payload["entry"] == "or-qwen"
    assert payload["halt"] == "auth"


def test_a_task_error_is_a_failed_node_without_a_halt(lsk, tmp_path):
    lsk["behaviour"]["raise"] = {"qwen/qwen3-32b": RuntimeError("no API key resolved")}
    rc, _out, payload = _run(tmp_path, "--model", "or-qwen")
    assert rc == 1
    assert payload["ok"] is False and payload["error"] == "failed"
    assert "halt" not in payload
    assert "no API key resolved" in payload["detail"]


@pytest.mark.parametrize("legacy", ["qwen", "qwen/qwen3-32b"])
def test_a_model_alias_or_raw_slug_is_not_an_entry(lsk, tmp_path, capsys, legacy):
    """Migration step 12: an alias or raw slug no longer runs on the default
    entry. It is not a transport entry id, so the node dispatches nothing and
    exits 2; the entry id (``or-qwen``) is the only accepted form."""
    rc, out, payload = _run(tmp_path, "--model", legacy)
    assert rc == 2
    assert not out.exists()
    assert lsk["calls"] == []
    assert payload["ok"] is False
    assert "deprecated" not in capsys.readouterr().err


def test_a_structurally_invalid_declaration_exits_2(lsk, tmp_path, capsys):
    rc, _out, _payload = _run(tmp_path, "--model", "or-qwen,or-qwen")
    assert rc == 2
    assert "duplicate" in capsys.readouterr().err
    assert lsk["calls"] == []
