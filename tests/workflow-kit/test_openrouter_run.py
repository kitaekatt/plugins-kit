"""Unit tests for the workflow-kit openrouter runner.

The runner lazy-imports llm_scripting_kit / openai inside main(), so importing the
module here needs no network and no SDK. The import-guard tests install fake
modules into sys.modules. The dispatch tests use the REAL llm_scripting_kit
(its lib/ is put on sys.path, standing in for the bootstrap shared-lib .pth)
against the shipped registry, with HOME isolated, the reachability probe
answered, and OpenRouterBackend.complete replaced -- so no network is touched
and no openai SDK is needed.
"""

import hashlib
import importlib.util
import inspect
import json
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

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


def _real_lsk(monkeypatch, tmp_path):
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
    return completion, declaration, reach


@pytest.fixture
def lsk(monkeypatch, tmp_path):
    """The real llm_scripting_kit with OpenRouterBackend.complete REPLACED.

    Valid only for dispatch and non-contract assertions: a replaced
    ``complete`` never runs prepare_contract/finalize_contract, so no
    output-contract assertion can go red under it (see ``lsk_transport``).
    """
    completion, declaration, reach = _real_lsk(monkeypatch, tmp_path)
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


# --------------------------------------------------------------------------- #
# Typed artifacts (--provides/--kind/--schema/--schema-digest/--verdict).
#
# Every assertion that touches the output contract (sent, delivered,
# validated, reported) uses ``lsk_transport``, which fakes the transport BELOW
# OpenRouterBackend.complete, so prepare_contract and finalize_contract really
# run. The ``lsk`` fixture replaces complete wholesale and could never show a
# contract failure.
# --------------------------------------------------------------------------- #
SCHEMA = {
    "type": "object",
    "required": ["lines", "words"],
    "additionalProperties": False,
    "properties": {
        "lines": {"type": "integer", "minimum": 0},
        "words": {"type": "integer", "minimum": 0},
    },
}


def _digest(schema):
    from llm_scripting_kit.completion import OutputContract

    return OutputContract(id="x", policy="validated-result", schema=schema).schema_digest


@pytest.fixture
def lsk_transport(monkeypatch, tmp_path):
    """The real llm_scripting_kit, including OpenRouterBackend.complete; only
    the OpenAI-compatible client beneath it is faked (``_ensure_client``)."""
    completion, declaration, reach = _real_lsk(monkeypatch, tmp_path)
    requests = []
    behaviour = {"text": "", "raise": None}

    class _Completions:
        def create(self, **kwargs):
            requests.append(kwargs)
            if behaviour["raise"] is not None:
                raise behaviour["raise"]
            message = SimpleNamespace(content=behaviour["text"])
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=None
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    monkeypatch.setattr(completion.OpenRouterBackend, "_ensure_client", lambda self: client)
    return {"requests": requests, "behaviour": behaviour, "reach": reach,
            "completion": completion, "declaration": declaration}


def _verdict_path(tmp_path):
    return tmp_path / ".workflow-kit" / "r1" / "classify.contract.json"


def _provider_args(tmp_path, *, kind="schema", schema=SCHEMA, digest=None, name="doc_stats"):
    args = ["--provides", name, "--kind", kind, "--verdict", str(_verdict_path(tmp_path))]
    if kind == "schema":
        args += ["--schema", json.dumps(schema),
                 "--schema-digest", digest if digest is not None else _digest(schema)]
    return args


def _verdict(tmp_path):
    path = _verdict_path(tmp_path)
    return json.loads(path.read_text(encoding="ascii")) if path.exists() else None


def _system_text(request):
    return request["messages"][0]["content"][0]["text"]


def _seed_verdict(tmp_path):
    path = _verdict_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"verdict": "satisfied"}', encoding="ascii")


def test_provider_schema_writes_validated_json(lsk_transport, tmp_path, capsys):
    reply = '  {"words": 5,\n   "lines": 3}  '
    lsk_transport["behaviour"]["text"] = reply
    rc, out, payload = _run(tmp_path, "--model", "or-qwen", *_provider_args(tmp_path))
    assert rc == 0, capsys.readouterr().err
    written = out.read_bytes()
    assert written == b'{"words": 5, "lines": 3}'
    assert written.decode("ascii") != reply
    # the schema instruction was delivered on the system message
    sent = _system_text(lsk_transport["requests"][0])
    assert sent.startswith("be terse") and sent != "be terse"
    verdict = _verdict(tmp_path)
    assert verdict["verdict"] == "satisfied"
    assert verdict["kind"] == "schema" and verdict["artifact"] == "doc_stats"
    assert verdict["schema_digest"] == _digest(SCHEMA)
    assert verdict["bytes"] == len(written)
    assert verdict["sha256"] == hashlib.sha256(written).hexdigest()
    assert verdict["path"] == str(out)
    assert payload["ok"] is True


def test_provider_schema_violation_exits_1_verdict_violated_no_out(lsk_transport, tmp_path):
    lsk_transport["behaviour"]["text"] = '{"lines": "three", "words": 5}'
    rc, out, payload = _run(tmp_path, "--model", "or-qwen", *_provider_args(tmp_path))
    assert rc == 1
    assert not out.exists()
    assert len(lsk_transport["requests"]) == 1
    verdict = _verdict(tmp_path)
    assert verdict["verdict"] == "violated"
    assert verdict["errors"] == [["/lines", "type"]]
    assert verdict["schema_digest"] == _digest(SCHEMA)
    assert verdict["bytes"] is None and verdict["sha256"] is None
    assert payload["ok"] is False


def test_provider_unparseable_reply_is_violated(lsk_transport, tmp_path):
    lsk_transport["behaviour"]["text"] = "three lines, five words"
    rc, out, _payload = _run(tmp_path, "--model", "or-qwen", *_provider_args(tmp_path))
    assert rc == 1
    assert not out.exists()
    verdict = _verdict(tmp_path)
    assert verdict["verdict"] == "violated"
    assert verdict["errors"] == [["", "unparseable"]]


def test_provider_opaque_records_satisfied(lsk_transport, tmp_path):
    lsk_transport["behaviour"]["text"] = "a free-form reply"
    rc, out, _payload = _run(
        tmp_path, "--model", "or-qwen", *_provider_args(tmp_path, kind="opaque-file")
    )
    assert rc == 0
    assert out.read_text(encoding="utf-8") == "a free-form reply"
    assert _system_text(lsk_transport["requests"][0]) == "be terse"  # no contract sent
    verdict = _verdict(tmp_path)
    assert verdict["verdict"] == "satisfied"
    assert verdict["kind"] == "opaque-file"
    assert "schema_digest" not in verdict
    assert verdict["sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()


def test_provider_task_error_records_missing(lsk_transport, tmp_path):
    lsk_transport["behaviour"]["raise"] = RuntimeError("no API key resolved")
    rc, out, payload = _run(tmp_path, "--model", "or-qwen", *_provider_args(tmp_path))
    assert rc == 1
    assert not out.exists()
    assert payload["error"] == "failed"
    verdict = _verdict(tmp_path)
    assert verdict["verdict"] == "missing"
    assert verdict["errors"] == []


def test_provider_floor_records_missing(lsk_transport, tmp_path):
    rc, out, payload = _run(tmp_path, "--model", "sol,typo", *_provider_args(tmp_path))
    assert rc == 2
    assert not out.exists()
    assert lsk_transport["requests"] == []
    assert payload["error"] == "NoUsableRoutingTarget"
    assert _verdict(tmp_path)["verdict"] == "missing"


def test_provider_default_declaration_failure_records_missing(lsk_transport, tmp_path, monkeypatch):
    import llm_scripting_kit

    def unconfigured(project_root=None):
        raise ValueError("no default declaration is configured")

    monkeypatch.setattr(llm_scripting_kit, "default_declaration", unconfigured)
    rc, out, payload = _run(tmp_path, *_provider_args(tmp_path))
    assert rc == 2
    assert not out.exists()
    assert lsk_transport["requests"] == []
    assert payload["error"] == "ValueError"
    assert _verdict(tmp_path)["verdict"] == "missing"


def test_provider_unexpected_exception_records_missing(lsk_transport, tmp_path, monkeypatch):
    def exploding_run(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(lsk_transport["declaration"], "run", exploding_run)
    with pytest.raises(RuntimeError, match="unexpected"):
        _run(tmp_path, "--model", "or-qwen", *_provider_args(tmp_path))
    assert _verdict(tmp_path)["verdict"] == "missing"


def test_provider_completed_without_contract_report_is_missing(lsk, tmp_path, capsys):
    # ``lsk`` replaces complete wholesale: the "backend" returns a completed
    # response carrying no contract report, as a backend that ignored the
    # contract would. That is never written as a validated artifact.
    rc, out, payload = _run(tmp_path, "--model", "or-qwen", *_provider_args(tmp_path))
    assert rc == 1
    assert not out.exists()
    assert payload == {"ok": False, "error": "output-contract", "entry": "or-qwen"}
    assert _verdict(tmp_path)["verdict"] == "missing"
    assert "completed without a validated result" in capsys.readouterr().err


def test_no_provides_sends_no_output_contract(lsk_transport, tmp_path, monkeypatch):
    seen = []
    real_complete = lsk_transport["completion"].OpenRouterBackend.complete

    def spy(self, system, user, *, model, options=None):
        seen.append(getattr(options, "output_contract", None))
        return real_complete(self, system, user, model=model, options=options)

    monkeypatch.setattr(lsk_transport["completion"].OpenRouterBackend, "complete", spy)
    lsk_transport["behaviour"]["text"] = '{"lines": 1}'
    rc, out, _payload = _run(tmp_path, "--model", "or-qwen")
    assert rc == 0
    assert seen == [None]
    assert _system_text(lsk_transport["requests"][0]) == "be terse"
    assert out.read_text(encoding="utf-8") == '{"lines": 1}'
    assert not (tmp_path / ".workflow-kit").exists()


# --------------------------------------------------------------------------- #
# refusals before any call, and invalidation before the probes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("drop", ["--kind", "--verdict"])
def test_provides_requires_kind_and_verdict(tmp_path, capsys, drop):
    args = ["--provides", "a", "--kind", "opaque-file", "--verdict", str(tmp_path / "v.json")]
    i = args.index(drop)
    del args[i:i + 2]
    with pytest.raises(SystemExit) as caught:
        orr.main(["--prompt", "hi", "--out", str(tmp_path / "o.txt"), *args])
    assert caught.value.code == 2
    assert "--provides requires --kind and --verdict" in capsys.readouterr().err


@pytest.mark.parametrize("extra,message", [
    (["--kind", "opaque-file"], "require --provides"),
    (["--verdict", "v.json"], "require --provides"),
    (["--provides", "a", "--kind", "schema", "--verdict", "v.json"],
     "--kind schema requires --schema and --schema-digest"),
    (["--provides", "a", "--kind", "opaque-file", "--verdict", "v.json", "--schema", "{}"],
     "--kind opaque-file takes no --schema or --schema-digest"),
])
def test_provider_flag_pairing_is_a_usage_error(tmp_path, capsys, extra, message):
    with pytest.raises(SystemExit) as caught:
        orr.main(["--prompt", "hi", "--out", str(tmp_path / "o.txt"), *extra])
    assert caught.value.code == 2
    assert message in capsys.readouterr().err


def test_provider_flags_parse_in_any_position(lsk_transport, tmp_path):
    # TC2 appends the provider flags to wkOpenRouter's runner prefix, so they
    # precede the node's own flags.
    lsk_transport["behaviour"]["text"] = '{"lines": 1, "words": 2}'
    out = tmp_path / "out.txt"
    rc = orr.main([*_provider_args(tmp_path), "--model", "or-qwen", "--prompt", "hi",
                   "--out", str(out)])
    assert rc == 0
    assert _verdict(tmp_path)["verdict"] == "satisfied"


def test_provider_schema_digest_mismatch_exits_2_before_call(lsk_transport, tmp_path, capsys):
    rc, out, payload = _run(
        tmp_path, "--model", "or-qwen", *_provider_args(tmp_path, digest="0" * 64)
    )
    assert rc == 2
    assert lsk_transport["requests"] == []
    assert _verdict(tmp_path) is None
    assert not _verdict_path(tmp_path).parent.exists()
    assert payload is None and not out.exists()
    assert "changed in transit" in capsys.readouterr().err


def test_provider_contract_probe_too_old_exits_2_before_call(
    lsk_transport, tmp_path, capsys, monkeypatch
):
    args = _provider_args(tmp_path)
    monkeypatch.delattr(lsk_transport["completion"], "OutputContract")
    rc, _out, _payload = _run(tmp_path, "--model", "or-qwen", *args)
    assert rc == 2
    assert lsk_transport["requests"] == []
    assert _verdict(tmp_path) is None
    err = capsys.readouterr().err
    assert "llm-scripting-kit >= 0.56.0" in err
    assert "not importable" not in err


def test_provider_backend_options_without_output_contract_exits_2(
    lsk_transport, tmp_path, capsys, monkeypatch
):
    class OldBackendOptions:  # predates output_contract
        def __init__(self, max_tokens=4096, temperature=None):
            self.max_tokens = max_tokens
            self.temperature = temperature

    args = _provider_args(tmp_path)
    monkeypatch.setattr(lsk_transport["completion"], "BackendOptions", OldBackendOptions)
    rc, _out, _payload = _run(tmp_path, "--model", "or-qwen", *args)
    assert rc == 2
    assert lsk_transport["requests"] == []
    err = capsys.readouterr().err
    assert "BackendOptions takes no `output_contract`" in err
    assert "llm-scripting-kit >= 0.56.0" in err


def test_provider_digest_mismatch_removes_seeded_verdict(lsk_transport, tmp_path):
    _seed_verdict(tmp_path)
    rc, _out, _payload = _run(
        tmp_path, "--model", "or-qwen", *_provider_args(tmp_path, digest="0" * 64)
    )
    assert rc == 2
    assert not _verdict_path(tmp_path).exists()


def test_provider_probe_failure_removes_seeded_verdict(lsk_transport, tmp_path, monkeypatch, capsys):
    args = _provider_args(tmp_path)
    _seed_verdict(tmp_path)
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", None)
    rc, _out, _payload = _run(tmp_path, "--model", "or-qwen", *args)
    assert rc == 2
    assert "llm_scripting_kit not importable" in capsys.readouterr().err
    assert not _verdict_path(tmp_path).exists()


def test_provider_undeletable_verdict_exits_2_before_call(lsk_transport, tmp_path, capsys):
    _verdict_path(tmp_path).mkdir(parents=True)  # a directory where the verdict goes
    rc, out, payload = _run(tmp_path, "--model", "or-qwen", *_provider_args(tmp_path))
    assert rc == 2
    assert lsk_transport["requests"] == []
    assert _verdict_path(tmp_path).is_dir()
    assert payload is None and not out.exists()
    assert "cannot invalidate the previous verdict at" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# cleanup-layer failures and exception precedence
# --------------------------------------------------------------------------- #
def _fail_verdict_replace(monkeypatch, tmp_path):
    real_replace = os.replace
    target = str(_verdict_path(tmp_path))

    def failing(src, dst):
        if str(dst) == target:
            raise OSError("verdict replace refused")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", failing)


def test_provider_verdict_write_failure_is_reported_exit_1(
    lsk_transport, tmp_path, monkeypatch, capsys
):
    lsk_transport["behaviour"]["text"] = '{"lines": 1, "words": 2}'
    _fail_verdict_replace(monkeypatch, tmp_path)
    rc, out, _payload = _run(tmp_path, "--model", "or-qwen", *_provider_args(tmp_path))
    assert rc == 1
    assert out.exists()  # the call itself succeeded
    assert not _verdict_path(tmp_path).exists()
    assert list(_verdict_path(tmp_path).parent.iterdir()) == []  # no temp sibling
    err = capsys.readouterr().err
    assert "the verdict cleanup layer failed" in err
    assert "verdict replace refused" in err


@pytest.mark.parametrize("body_error,expected_rc,message", [
    ("task", 1, "openrouter node failed on or-qwen"),
    ("floor", 2, "no usable routing target"),
])
def test_provider_body_error_wins_over_verdict_write_failure(
    lsk_transport, tmp_path, monkeypatch, capsys, body_error, expected_rc, message
):
    if body_error == "task":
        lsk_transport["behaviour"]["raise"] = RuntimeError("no API key resolved")
        model = "or-qwen"
    else:
        model = "sol,typo"
    _fail_verdict_replace(monkeypatch, tmp_path)
    rc, _out, _payload = _run(tmp_path, "--model", model, *_provider_args(tmp_path))
    assert rc == expected_rc
    err = capsys.readouterr().err
    assert message in err
    assert "the verdict cleanup layer failed" in err



# --------------------------------------------------------------------------- #
# TC4: the contract event. A provider node with --events writes its whole
# stream under schema v3 and records one ``contract`` event, which always
# precedes the unit's ``terminal`` (``_TerminalLast`` holds it). Every
# assertion about the contract path uses ``lsk_transport``.
# --------------------------------------------------------------------------- #
_V1 = "plugins-kit.execution-event/v1"
_V2 = "plugins-kit.execution-event/v2"
_V3 = "plugins-kit.execution-event/v3"
_VALID = '{"lines": 3, "words": 5}'


def _events_path(tmp_path):
    return tmp_path / ".workflow-kit" / "r1" / "classify.events.jsonl"


def _run_provider(tmp_path, *extra, kind="schema", model="or-qwen"):
    args = ["--model", model] if model else []
    return _run(tmp_path, *args, *_provider_args(tmp_path, kind=kind),
                *_events_args(_events_path(tmp_path)), *extra)


def _names(stream):
    return [e["event"] for e in stream]


def _contracts(stream):
    return [e for e in stream if e["event"] == "contract"]


def _raising_run(error):
    """A ``run`` that emits the unit's terminal through its observer, then raises."""
    def run(names, request, *, project_root=None, backend_factory=None, max_attempts=1,
            observer=None):
        observer.emit("terminal", payload={"state": "failed"})
        raise error

    return run


def _failing_sink(monkeypatch, event_name):
    """Make the real JsonlSink raise while writing ``event_name`` events only."""
    from bootstrap_lib import execution_event

    real_write = execution_event.JsonlSink.write

    def write(self, event):
        if event["event"] == event_name:
            raise OSError(f"{event_name} sink refused")
        return real_write(self, event)

    monkeypatch.setattr(execution_event.JsonlSink, "write", write)


def test_provider_stream_has_contract_before_terminal(lsk_transport, tmp_path, capsys):
    lsk_transport["behaviour"]["text"] = _VALID
    rc, _out, _payload = _run_provider(tmp_path)
    assert rc == 0, capsys.readouterr().err
    stream = _read_stream(_events_path(tmp_path))  # validate_stream passes
    names = _names(stream)
    assert names[:2] == ["dispatch-selected", "call-started"]
    assert names[-2:] == ["contract", "terminal"]
    assert names.count("contract") == 1
    assert {e["schema"] for e in stream} == {_V3}
    contract = _contracts(stream)[0]
    assert contract["payload"] == {
        "artifact": "doc_stats", "kind": "schema", "verdict": "satisfied",
        "schema_digest": _digest(SCHEMA),
    }
    assert contract["identity"] == {"run_id": "r1", "unit_id": "classify"}
    assert contract["source"]["plugin"] == "workflow-kit"
    assert stream[-1]["payload"] == {"state": "completed"}
    assert [e["seq"] for e in stream] == sorted(e["seq"] for e in stream)


def test_held_terminal_is_forwarded_when_run_raises(lsk_transport, tmp_path, monkeypatch):
    monkeypatch.setattr(lsk_transport["declaration"], "run", _raising_run(RuntimeError("late")))
    with pytest.raises(RuntimeError, match="late"):
        _run_provider(tmp_path)
    stream = _read_stream(_events_path(tmp_path))
    terminals = [e for e in stream if e["event"] == "terminal"]
    assert len(terminals) == 1
    assert terminals[0]["payload"] == {"state": "failed"}
    assert terminals[0]["seq"] == stream[-1]["seq"]


def test_unexpected_exception_emits_contract_then_terminal(lsk_transport, tmp_path, monkeypatch):
    monkeypatch.setattr(lsk_transport["declaration"], "run", _raising_run(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        _run_provider(tmp_path)
    stream = _read_stream(_events_path(tmp_path))
    assert _names(stream) == ["contract", "terminal"]
    assert stream[0]["payload"]["verdict"] == "missing"
    assert _verdict(tmp_path)["verdict"] == "missing"


@pytest.mark.parametrize("verdict", ["satisfied", "violated", "missing"])
def test_provider_contract_event_verdict(lsk_transport, tmp_path, verdict):
    if verdict == "satisfied":
        lsk_transport["behaviour"]["text"] = _VALID
    elif verdict == "violated":
        lsk_transport["behaviour"]["text"] = '{"lines": "three", "words": 5}'
    else:
        lsk_transport["behaviour"]["raise"] = RuntimeError("no API key resolved")
    rc, _out, _payload = _run_provider(tmp_path)
    assert rc == (0 if verdict == "satisfied" else 1)
    (contract,) = _contracts(_read_stream(_events_path(tmp_path)))
    expected = {"artifact": "doc_stats", "kind": "schema", "verdict": verdict,
                "schema_digest": _digest(SCHEMA)}
    if verdict == "violated":
        expected["error_count"] = 1
    assert contract["payload"] == expected
    assert _verdict(tmp_path)["verdict"] == verdict


def test_provider_contract_event_opaque_has_no_digest(lsk_transport, tmp_path):
    lsk_transport["behaviour"]["text"] = "free text"
    assert _run_provider(tmp_path, kind="opaque-file")[0] == 0
    (contract,) = _contracts(_read_stream(_events_path(tmp_path)))
    assert contract["payload"] == {"artifact": "doc_stats", "kind": "opaque-file",
                                   "verdict": "satisfied"}


@pytest.mark.parametrize("reply,count", [
    ('{"lines": "three", "words": -1, "extra": 1}', 3),
    ("three lines, five words", 1),
])
def test_provider_contract_event_error_count(lsk_transport, tmp_path, reply, count):
    lsk_transport["behaviour"]["text"] = reply
    assert _run_provider(tmp_path)[0] == 1
    (contract,) = _contracts(_read_stream(_events_path(tmp_path)))
    assert contract["payload"]["error_count"] == count == len(_verdict(tmp_path)["errors"])


def test_non_provider_stream_stays_v1(lsk, tmp_path):
    events = tmp_path / ".workflow-kit" / "r1" / "classify.events.jsonl"
    assert _run(tmp_path, "--model", "or-qwen", *_events_args(events))[0] == 0
    stream = _read_stream(events)
    assert {e["schema"] for e in stream} == {_V1}
    assert _names(stream) == ["dispatch-selected", "call-started", "result", "terminal"]


def test_provider_events_probe_without_v3_exits_2_before_call(
    lsk_transport, tmp_path, capsys, monkeypatch
):
    args = [*_provider_args(tmp_path), *_events_args(_events_path(tmp_path))]
    _fake_execution_event(monkeypatch, SUPPORTED_SCHEMAS=frozenset({_V1, _V2}))
    rc, out, payload = _run(tmp_path, "--model", "or-qwen", *args)
    assert rc == 2
    assert lsk_transport["requests"] == []
    assert not (tmp_path / ".workflow-kit").exists()  # no stream, no verdict, no directory
    assert payload is None and not out.exists()
    err = capsys.readouterr().err
    assert "supports plugins-kit.execution-event/v1 but not /v3" in err
    assert "contract events" in err and ">= 0.137.0" in err
    assert "claude plugin update bootstrap@plugins-kit" in err
    assert "does not bind" not in err


def test_provider_events_probe_emitter_without_schema_exits_2(
    lsk_transport, tmp_path, capsys, monkeypatch
):
    class OldEmitter:  # predates schema=
        def __init__(self, plugin, run_id, *, unit_id=None, sinks=(), start_seq=0):
            raise AssertionError("never constructed")

    args = [*_provider_args(tmp_path), *_events_args(_events_path(tmp_path))]
    _fake_execution_event(monkeypatch, Emitter=OldEmitter)
    rc, _out, _payload = _run(tmp_path, "--model", "or-qwen", *args)
    assert rc == 2
    assert lsk_transport["requests"] == []
    assert not (tmp_path / ".workflow-kit").exists()
    err = capsys.readouterr().err
    assert "does not bind" in err and "bootstrap >= 0.137.0" in err
    assert "but not /v3" not in err


def test_default_declaration_failure_emits_contract_only_missing_stream(
    lsk_transport, tmp_path, monkeypatch
):
    import llm_scripting_kit

    def unconfigured(project_root=None):
        raise ValueError("no default declaration is configured")

    monkeypatch.setattr(llm_scripting_kit, "default_declaration", unconfigured)
    rc, _out, _payload = _run_provider(tmp_path, model=None)
    assert rc == 2
    stream = _read_stream(_events_path(tmp_path))
    assert _names(stream) == ["contract"]
    assert stream[0]["payload"]["verdict"] == "missing"
    assert stream[0]["schema"] == _V3


def test_routing_floor_emits_contract_before_unroutable_terminal(lsk_transport, tmp_path):
    rc, _out, _payload = _run_provider(tmp_path, model="sol,typo")  # the real run
    assert rc == 2
    assert lsk_transport["requests"] == []
    stream = _read_stream(_events_path(tmp_path))  # validate_stream passes
    assert _names(stream) == ["contract", "terminal"]
    assert stream[0]["payload"]["verdict"] == "missing"
    assert stream[1]["payload"] == {"state": "unroutable"}


def test_contract_sink_error_still_releases_terminal(lsk_transport, tmp_path, monkeypatch, capsys):
    lsk_transport["behaviour"]["text"] = _VALID
    _failing_sink(monkeypatch, "contract")
    rc, _out, _payload = _run_provider(tmp_path)
    assert rc == 1
    stream = _read_stream(_events_path(tmp_path))
    assert "contract" not in _names(stream)
    assert _names(stream)[-1] == "terminal"
    assert _verdict(tmp_path)["verdict"] == "satisfied"
    err = capsys.readouterr().err
    assert "the contract cleanup layer failed" in err and "contract sink refused" in err


def test_verdict_write_failure_still_emits_contract_and_terminal(
    lsk_transport, tmp_path, monkeypatch, capsys
):
    lsk_transport["behaviour"]["text"] = _VALID
    _fail_verdict_replace(monkeypatch, tmp_path)
    rc, _out, _payload = _run_provider(tmp_path)
    assert rc == 1
    assert not _verdict_path(tmp_path).exists()
    stream = _read_stream(_events_path(tmp_path))
    assert _names(stream)[-2:] == ["contract", "terminal"]
    assert _contracts(stream)[0]["payload"]["verdict"] == "satisfied"
    assert "the verdict cleanup layer failed" in capsys.readouterr().err


def test_terminal_sink_failure_is_reported_exit_1(lsk_transport, tmp_path, monkeypatch, capsys):
    lsk_transport["behaviour"]["text"] = _VALID
    _failing_sink(monkeypatch, "terminal")
    rc, _out, _payload = _run_provider(tmp_path)
    assert rc == 1
    assert _verdict(tmp_path)["verdict"] == "satisfied"
    stream = _read_stream(_events_path(tmp_path))
    assert _names(stream)[-1] == "contract"
    assert "terminal" not in _names(stream)
    err = capsys.readouterr().err
    assert "the terminal cleanup layer failed" in err and "terminal sink refused" in err


class _RecordingEmitter:
    def __init__(self, fail_on=None):
        self.events = []
        self.fail_on = fail_on

    def emit(self, event, **fields):
        self.events.append((event, fields))
        if event == self.fail_on:
            raise OSError(f"{event} refused")
        return {"event": event}


def test_release_once_forwards_at_most_once():
    emitter = _RecordingEmitter()
    held = orr._TerminalLast(emitter)
    forwarded = held.emit("result", attempt_id="1", payload={"status": "completed"})
    assert forwarded == {"event": "result"}
    assert held.emit("terminal", payload={"state": "completed"}) is None
    assert [e for e, _f in emitter.events] == ["result"]  # terminal held
    held.release_once()
    held.release_once()  # a second call is a no-op
    assert emitter.events[1:] == [("terminal", {"payload": {"state": "completed"}})]

    failing = _RecordingEmitter(fail_on="terminal")
    held = orr._TerminalLast(failing)
    held.emit("terminal", payload={"state": "failed"})
    with pytest.raises(OSError, match="terminal refused"):
        held.release_once()
    held.release_once()  # a forward that raised is not retried
    assert [e for e, _f in failing.events] == ["terminal"]


def test_body_exception_wins_over_cleanup_errors(lsk_transport, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(lsk_transport["declaration"], "run", _raising_run(RuntimeError("body")))
    _failing_sink(monkeypatch, "contract")
    with pytest.raises(RuntimeError, match="body"):
        _run_provider(tmp_path)
    err = capsys.readouterr().err
    assert "the contract cleanup layer failed" in err and "contract sink refused" in err
    assert _names(_read_stream(_events_path(tmp_path))) == ["terminal"]


@pytest.mark.parametrize("path", [
    "completed", "violated", "task_error", "floor", "default_declaration", "exception",
])
def test_provider_emits_exactly_one_contract(lsk_transport, tmp_path, monkeypatch, path):
    model = "or-qwen"
    if path == "completed":
        lsk_transport["behaviour"]["text"] = _VALID
    elif path == "violated":
        lsk_transport["behaviour"]["text"] = '{"lines": -1, "words": 5}'
    elif path == "task_error":
        lsk_transport["behaviour"]["raise"] = RuntimeError("no API key resolved")
    elif path == "floor":
        model = "sol,typo"
    elif path == "default_declaration":
        import llm_scripting_kit

        def unconfigured(project_root=None):
            raise ValueError("no default declaration is configured")

        monkeypatch.setattr(llm_scripting_kit, "default_declaration", unconfigured)
        model = None
    else:
        monkeypatch.setattr(lsk_transport["declaration"], "run",
                            _raising_run(RuntimeError("boom")))
    if path == "exception":
        with pytest.raises(RuntimeError):
            _run_provider(tmp_path, model=model)
    else:
        _run_provider(tmp_path, model=model)
    stream = _read_stream(_events_path(tmp_path))  # validate_stream passes
    names = _names(stream)
    assert names.count("contract") == 1
    assert names.count("terminal") <= 1
    if "terminal" in names:
        assert names.index("contract") < names.index("terminal")


def _seed_events(tmp_path):
    path = _events_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("stale\n", encoding="ascii")
    return path


def test_provider_probe_failure_removes_seeded_events_file(lsk_transport, tmp_path, monkeypatch):
    args = [*_provider_args(tmp_path), *_events_args(_events_path(tmp_path))]
    events = _seed_events(tmp_path)
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", None)
    rc, _out, _payload = _run(tmp_path, "--model", "or-qwen", *args)
    assert rc == 2
    assert not events.exists()
    assert not _verdict_path(tmp_path).exists()


def test_provider_undeletable_events_file_exits_2_before_call(lsk_transport, tmp_path, capsys):
    _events_path(tmp_path).mkdir(parents=True)  # a directory where the stream goes
    rc, out, payload = _run_provider(tmp_path)
    assert rc == 2
    assert lsk_transport["requests"] == []
    assert _events_path(tmp_path).is_dir()
    assert payload is None and not out.exists()
    assert "cannot invalidate the previous events file at" in capsys.readouterr().err


def test_non_provider_probe_failure_keeps_events_file(lsk, tmp_path, monkeypatch):
    events = _seed_events(tmp_path)
    monkeypatch.setitem(sys.modules, "llm_scripting_kit", None)
    rc, _out, _payload = _run(tmp_path, "--model", "or-qwen", *_events_args(events))
    assert rc == 2
    assert events.read_text(encoding="ascii") == "stale\n"
