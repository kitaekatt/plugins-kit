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
import inspect
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
        resp = _Resp(f"reply from {model}", model)
        for field, value in behaviour.get("usage", {}).items():
            setattr(resp, field, value)
        return resp

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


# --------------------------------------------------------------------------- #
# Execution events (--events/--run-id/--unit-id): an Emitter + JsonlSink
# (truncate) from bootstrap_lib.execution_event, passed to run(observer=...).
# Probes run before any directory is created and before any model call.
# --------------------------------------------------------------------------- #
def _events_args(events, run_id="r1", unit_id="classify"):
    args = ["--events", str(events)]
    if run_id is not None:
        args += ["--run-id", run_id]
    if unit_id is not None:
        args += ["--unit-id", unit_id]
    return args


def _read_stream(path):
    from bootstrap_lib import execution_event

    return execution_event.validate_stream(execution_event.read_jsonl(path))


def _fake_execution_event(monkeypatch, **overrides):
    """Install a bootstrap_lib package whose execution_event is the real
    module with ``overrides`` applied (a value of None deletes the name)."""
    from bootstrap_lib import execution_event as real

    fake = types.ModuleType("bootstrap_lib.execution_event")
    fake.__dict__.update({k: v for k, v in vars(real).items() if not k.startswith("__")})
    for name, value in overrides.items():
        if value is None:
            fake.__dict__.pop(name, None)
        else:
            setattr(fake, name, value)
    pkg = types.ModuleType("bootstrap_lib")
    pkg.__path__ = []
    pkg.execution_event = fake
    monkeypatch.setitem(sys.modules, "bootstrap_lib", pkg)
    monkeypatch.setitem(sys.modules, "bootstrap_lib.execution_event", fake)


def _assert_refused_before_call(lsk, rc, events, capsys):
    assert rc == 2
    assert lsk["calls"] == []
    assert not events.exists()
    return capsys.readouterr().err


def test_events_flag_writes_conforming_stream(lsk, tmp_path):
    lsk["behaviour"]["usage"] = {"input_tokens": 11, "output_tokens": 5}
    events = tmp_path / ".workflow-kit" / "r1" / "classify.events.jsonl"
    rc, _out, _payload = _run(tmp_path, "--model", "or-qwen", *_events_args(events))
    assert rc == 0
    stream = _read_stream(events)
    assert [e["event"] for e in stream] == [
        "dispatch-selected", "call-started", "usage", "result", "terminal",
    ]
    assert {e["source"]["plugin"] for e in stream} == {"workflow-kit"}
    assert {e["identity"]["run_id"] for e in stream} == {"r1"}
    assert {e["identity"]["unit_id"] for e in stream} == {"classify"}
    assert stream[2]["payload"]["input_tokens"] == 11
    assert stream[-1]["payload"] == {"state": "completed"}


@pytest.mark.parametrize("missing", ["--run-id", "--unit-id"])
def test_events_requires_run_and_unit_ids(tmp_path, capsys, missing):
    events = tmp_path / "e" / "n.events.jsonl"
    kwargs = {"run_id": None} if missing == "--run-id" else {"unit_id": None}
    with pytest.raises(SystemExit) as caught:
        orr.main(["--prompt", "hi", "--out", str(tmp_path / "o.txt"),
                  *_events_args(events, **kwargs)])
    assert caught.value.code == 2
    assert "--events requires --run-id and --unit-id" in capsys.readouterr().err
    assert not events.parent.exists()


def test_rerun_replaces_events_file(lsk, tmp_path):
    events = tmp_path / ".workflow-kit" / "r1" / "classify.events.jsonl"
    assert _run(tmp_path, "--model", "or-qwen", *_events_args(events))[0] == 0
    assert _run(tmp_path, "--model", "or-qwen", *_events_args(events))[0] == 0
    stream = _read_stream(events)
    # the LAST execution only: one terminal, seq restarting at 0
    assert [e["event"] for e in stream].count("terminal") == 1
    assert stream[0]["seq"] == 0
    assert len(lsk["calls"]) == 2


def test_events_probe_absent_exits_2_before_call(lsk, tmp_path, capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "bootstrap_lib", None)
    events = tmp_path / "ev" / "n.events.jsonl"
    rc, _out, _payload = _run(tmp_path, "--model", "or-qwen", *_events_args(events))
    err = _assert_refused_before_call(lsk, rc, events, capsys)
    assert "claude plugin install bootstrap@plugins-kit" in err
    assert "claude plugin update" not in err


def test_events_probe_without_schema_v1_exits_2_before_call(lsk, tmp_path, capsys, monkeypatch):
    _fake_execution_event(
        monkeypatch, SUPPORTED_SCHEMAS=frozenset({"plugins-kit.execution-event/v0"})
    )
    events = tmp_path / "ev" / "n.events.jsonl"
    rc, _out, _payload = _run(tmp_path, "--model", "or-qwen", *_events_args(events))
    err = _assert_refused_before_call(lsk, rc, events, capsys)
    assert "claude plugin update bootstrap@plugins-kit" in err
    assert "bootstrap >= 0.135.0" in err
    assert "claude plugin install" not in err


def test_events_probe_unbindable_sink_exits_2_before_call(lsk, tmp_path, capsys, monkeypatch):
    class OldJsonlSink:  # predates mode=
        def __init__(self, path):
            self.path = path

        def write(self, event):
            raise AssertionError("never constructed")

    _fake_execution_event(monkeypatch, JsonlSink=OldJsonlSink)
    events = tmp_path / "ev" / "n.events.jsonl"
    rc, _out, _payload = _run(tmp_path, "--model", "or-qwen", *_events_args(events))
    err = _assert_refused_before_call(lsk, rc, events, capsys)
    assert "claude plugin update bootstrap@plugins-kit" in err


def _fake_run(lsk, monkeypatch, fn):
    ran = []

    def wrapper(*args, **kwargs):
        ran.append(1)
        return fn(*args, **kwargs)

    wrapper.__signature__ = inspect.signature(fn)
    monkeypatch.setattr(lsk["declaration"], "run", wrapper)
    return ran


def test_events_probe_run_without_observer_exits_2_before_call(lsk, tmp_path, capsys, monkeypatch):
    def old_run(names, request, *, project_root=None, backend_factory=None, max_attempts=None):
        raise AssertionError("never called")

    ran = _fake_run(lsk, monkeypatch, old_run)
    events = tmp_path / "ev" / "n.events.jsonl"
    rc, _out, _payload = _run(tmp_path, "--model", "or-qwen", *_events_args(events))
    err = _assert_refused_before_call(lsk, rc, events, capsys)
    assert ran == []
    assert "claude plugin update llm-scripting-kit@plugins-kit" in err
    assert "llm-scripting-kit >= 0.56.0" in err


def test_events_probe_positional_only_observer_exits_2_before_call(
    lsk, tmp_path, capsys, monkeypatch
):
    def odd_run(names, request, observer=None, /, *, project_root=None,
                backend_factory=None, max_attempts=None):
        raise AssertionError("never called")

    ran = _fake_run(lsk, monkeypatch, odd_run)
    events = tmp_path / "ev" / "n.events.jsonl"
    rc, _out, _payload = _run(tmp_path, "--model", "or-qwen", *_events_args(events))
    err = _assert_refused_before_call(lsk, rc, events, capsys)
    assert ran == []
    assert "llm-scripting-kit >= 0.56.0" in err


def test_events_invalid_ids_exit_2_before_call(lsk, tmp_path, capsys):
    events = tmp_path / "ev" / "n.events.jsonl"
    rc, _out, _payload = _run(
        tmp_path, "--model", "or-qwen", *_events_args(events, run_id="r\x01")
    )
    err = _assert_refused_before_call(lsk, rc, events, capsys)
    assert not events.parent.exists()
    assert "not valid event identities" in err


def test_fresh_run_creates_events_directory(lsk, tmp_path):
    run_dir = tmp_path / ".workflow-kit" / "fresh"
    events = run_dir / "classify.events.jsonl"
    assert not run_dir.exists()
    rc, _out, _payload = _run(tmp_path, "--model", "or-qwen", *_events_args(events))
    assert rc == 0
    assert _read_stream(events)[-1]["event"] == "terminal"


def test_refused_probe_creates_no_events_directory(lsk, tmp_path, capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "bootstrap_lib", None)
    run_dir = tmp_path / ".workflow-kit" / "refused"
    rc, _out, _payload = _run(
        tmp_path, "--model", "or-qwen", *_events_args(run_dir / "n.events.jsonl")
    )
    assert rc == 2
    assert not run_dir.exists()
    assert not run_dir.parent.exists()


def test_no_events_flag_passes_no_observer(lsk, tmp_path, monkeypatch):
    seen = []
    real_run = lsk["declaration"].run

    def spy(*args, **kwargs):
        seen.append("observer" in kwargs)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(lsk["declaration"], "run", spy)
    assert _run(tmp_path, "--model", "or-qwen")[0] == 0
    assert seen == [False]
