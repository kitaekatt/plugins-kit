"""Tests for the ``swapper`` CLI facade in llm_scripting_kit.cli.

The CLI holds no logic beyond parsing, calling llm_scripting_kit.swapper, and
formatting output -- every swapper exception carries its own ``exit_code``
(1, 2 or 5), so these tests monkeypatch the library seam
(``resolve_swapper_url``, ``SwapperClient``, ``terminate_model``,
``find_strays``, ``check_launch_allowed``) exactly as ``cli.py`` imports it,
and never touch a real swapper or process table.
"""

from __future__ import annotations

import json

import pytest

from llm_scripting_kit import cli
from llm_scripting_kit._swapper_process import ProcessRecord
from llm_scripting_kit.swapper import (
    InspectionIndeterminate,
    LaunchRefused,
    ModelNotRunningError,
    NonLocalTargetError,
    RunningModel,
    SafetyRefusal,
    SwapperUsageError,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeClient:
    """Stand-in for SwapperClient: canned `.running()` / `.unload_all()`."""

    def __init__(self, base_url, running=(), unload=None, unload_error=None):
        self.base_url = base_url
        self._running = tuple(running)
        self._unload = tuple(unload) if unload is not None else self._running
        self._unload_error = unload_error

    def running(self):
        return self._running

    def unload_all(self):
        if self._unload_error is not None:
            raise self._unload_error
        return self._unload


def _model(model="qwen38", state="ready", proxy="http://127.0.0.1:5800", cmd="ninfer-serve"):
    return RunningModel(model=model, state=state, proxy=proxy, cmd=cmd)


def _patch_target(monkeypatch, client):
    """resolve_swapper_url -> a fixed URL; SwapperClient(url) -> client."""
    monkeypatch.setattr(cli, "resolve_swapper_url", lambda **_kw: client.base_url)
    monkeypatch.setattr(cli, "SwapperClient", lambda url: client)


# ---------------------------------------------------------------------------
# Target: exactly one of --endpoint / --url
# ---------------------------------------------------------------------------


def test_running_requires_a_target(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["swapper", "running"])
    assert exc.value.code == 2


def test_running_refuses_both_endpoint_and_url(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["swapper", "running", "--endpoint", "local", "--url", "http://x"])
    assert exc.value.code == 2


def test_endpoint_and_url_both_reach_resolve_swapper_url(monkeypatch, capsys):
    client = FakeClient("http://127.0.0.1:8081", running=[_model()])
    seen = {}

    def fake_resolve(*, endpoint, url):
        seen["endpoint"], seen["url"] = endpoint, url
        return client.base_url

    monkeypatch.setattr(cli, "resolve_swapper_url", fake_resolve)
    monkeypatch.setattr(cli, "SwapperClient", lambda url: client)

    assert cli.main(["swapper", "running", "--endpoint", "local"]) == cli.EXIT_OK
    assert seen == {"endpoint": "local", "url": None}
    capsys.readouterr()

    assert cli.main(["swapper", "running", "--url", "http://127.0.0.1:8081"]) == cli.EXIT_OK
    assert seen == {"endpoint": None, "url": "http://127.0.0.1:8081"}


# ---------------------------------------------------------------------------
# running
# ---------------------------------------------------------------------------


def test_running_emits_versioned_json(monkeypatch, capsys):
    client = FakeClient("http://127.0.0.1:8081", running=[_model(), _model(model="other")])
    _patch_target(monkeypatch, client)

    assert cli.main(["swapper", "running", "--url", "http://127.0.0.1:8081"]) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["protocol"] == cli.SWAPPER_PROTOCOL_VERSION
    assert payload["endpoint"] == "http://127.0.0.1:8081"
    assert [m["model"] for m in payload["models"]] == ["qwen38", "other"]
    assert payload["models"][0] == {
        "model": "qwen38", "state": "ready", "proxy": "http://127.0.0.1:5800", "cmd": "ninfer-serve",
    }


def test_running_json_is_the_default_format(monkeypatch, capsys):
    client = FakeClient("http://127.0.0.1:8081", running=[_model()])
    _patch_target(monkeypatch, client)

    assert cli.main(["swapper", "running", "--url", "http://127.0.0.1:8081"]) == cli.EXIT_OK
    json.loads(capsys.readouterr().out)  # does not raise


def test_running_text_format(monkeypatch, capsys):
    client = FakeClient("http://127.0.0.1:8081", running=[_model()])
    _patch_target(monkeypatch, client)

    assert cli.main(["swapper", "running", "--url", "http://127.0.0.1:8081", "--format", "text"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "qwen38" in out and "ready" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_running_empty_list_is_still_exit_ok(monkeypatch, capsys):
    client = FakeClient("http://127.0.0.1:8081", running=[])
    _patch_target(monkeypatch, client)

    assert cli.main(["swapper", "running", "--url", "http://127.0.0.1:8081"]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["models"] == []


# ---------------------------------------------------------------------------
# terminate
# ---------------------------------------------------------------------------


class _TerminateResult:
    def __init__(self, pid, swapper_pid, signal, escalated):
        self.process = type("P", (), {"pid": pid, "swapper_pid": swapper_pid})()
        self.signal = signal
        self.escalated = escalated


def test_terminate_passes_grace_seconds_and_emits_json(monkeypatch, capsys):
    client = FakeClient("http://127.0.0.1:8081")
    _patch_target(monkeypatch, client)
    calls = []

    def fake_terminate(passed_client, model, *, grace_s):
        calls.append((passed_client, model, grace_s))
        return _TerminateResult(pid=18596, swapper_pid=279, signal="SIGTERM", escalated=False)

    monkeypatch.setattr(cli, "terminate_model", fake_terminate)

    assert cli.main(
        ["swapper", "terminate", "qwen38", "--url", "http://127.0.0.1:8081", "--grace-seconds", "5"]
    ) == cli.EXIT_OK
    assert calls == [(client, "qwen38", 5.0)]
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "protocol": cli.SWAPPER_PROTOCOL_VERSION,
        "endpoint": "http://127.0.0.1:8081",
        "model": "qwen38",
        "pid": 18596,
        "swapper_pid": 279,
        "signal": "SIGTERM",
        "escalated": False,
    }


def test_terminate_default_grace_seconds_is_ten(monkeypatch, capsys):
    client = FakeClient("http://127.0.0.1:8081")
    _patch_target(monkeypatch, client)
    calls = []
    monkeypatch.setattr(
        cli, "terminate_model",
        lambda c, m, *, grace_s: (calls.append(grace_s), _TerminateResult(1, 2, "SIGTERM", False))[1],
    )

    assert cli.main(["swapper", "terminate", "qwen38", "--url", "http://127.0.0.1:8081"]) == cli.EXIT_OK
    assert calls == [10.0]


def test_terminate_text_notes_escalation(monkeypatch, capsys):
    client = FakeClient("http://127.0.0.1:8081")
    _patch_target(monkeypatch, client)
    monkeypatch.setattr(
        cli, "terminate_model",
        lambda c, m, *, grace_s: _TerminateResult(1, 2, "SIGKILL", True),
    )

    assert cli.main(
        ["swapper", "terminate", "qwen38", "--url", "http://127.0.0.1:8081", "--format", "text"]
    ) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "SIGKILL" in out and "escalated" in out


def test_terminate_missing_model_maps_exit_failure(monkeypatch, capsys):
    """ModelNotRunningError.exit_code == 1: the CLI maps it with no logic of its own."""
    client = FakeClient("http://127.0.0.1:8081")
    _patch_target(monkeypatch, client)
    monkeypatch.setattr(
        cli, "terminate_model",
        lambda c, m, *, grace_s: (_ for _ in ()).throw(ModelNotRunningError(f"model {m!r} is not running")),
    )

    assert cli.main(["swapper", "terminate", "ghost", "--url", "http://127.0.0.1:8081"]) == cli.EXIT_FAILURE
    envelope = json.loads(capsys.readouterr().err)["error"]
    assert envelope["kind"] == "model-not-running"
    assert "ghost" in envelope["message"]


def test_terminate_safety_refusal_is_exit_failure(monkeypatch, capsys):
    client = FakeClient("http://127.0.0.1:8081")
    _patch_target(monkeypatch, client)
    monkeypatch.setattr(
        cli, "terminate_model",
        lambda c, m, *, grace_s: (_ for _ in ()).throw(SafetyRefusal("no direct child listens")),
    )

    assert cli.main(["swapper", "terminate", "qwen38", "--url", "http://127.0.0.1:8081"]) == cli.EXIT_FAILURE
    assert json.loads(capsys.readouterr().err)["error"]["kind"] == "safety-refusal"


def test_terminate_non_local_target_is_exit_usage(monkeypatch, capsys):
    client = FakeClient("http://127.0.0.1:8081")
    _patch_target(monkeypatch, client)
    monkeypatch.setattr(
        cli, "terminate_model",
        lambda c, m, *, grace_s: (_ for _ in ()).throw(NonLocalTargetError("refusing non-loopback swapper")),
    )

    assert cli.main(["swapper", "terminate", "qwen38", "--url", "http://192.168.1.20:8081"]) == cli.EXIT_USAGE
    assert json.loads(capsys.readouterr().err)["error"]["kind"] == "non-local-target"


def test_terminate_inspection_indeterminate_is_exit_five(monkeypatch, capsys):
    client = FakeClient("http://127.0.0.1:8081")
    _patch_target(monkeypatch, client)
    monkeypatch.setattr(
        cli, "terminate_model",
        lambda c, m, *, grace_s: (_ for _ in ()).throw(InspectionIndeterminate("access denied")),
    )

    assert cli.main(["swapper", "terminate", "qwen38", "--url", "http://127.0.0.1:8081"]) == cli.EXIT_INDETERMINATE
    assert json.loads(capsys.readouterr().err)["error"]["kind"] == "inspection-indeterminate"


def test_terminate_resolve_failure_is_exit_usage(monkeypatch, capsys):
    """A bad --endpoint name never even builds a client."""
    monkeypatch.setattr(
        cli, "resolve_swapper_url",
        lambda **_kw: (_ for _ in ()).throw(SwapperUsageError("unknown model-endpoint entry 'nope'")),
    )
    called = []
    monkeypatch.setattr(cli, "SwapperClient", lambda url: called.append(url) or pytest.fail("no client"))

    assert cli.main(["swapper", "terminate", "qwen38", "--endpoint", "nope"]) == cli.EXIT_USAGE
    assert called == []
    assert "nope" in json.loads(capsys.readouterr().err)["error"]["message"]


# ---------------------------------------------------------------------------
# unload -- refuses without both --all and --accept-no-drain
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra_args",
    [
        [],
        ["--all"],
        ["--accept-no-drain"],
    ],
)
def test_unload_refuses_without_both_flags(capsys, extra_args):
    with pytest.raises(SystemExit) as exc:
        cli.main(["swapper", "unload", "--url", "http://127.0.0.1:8081", *extra_args])
    assert exc.value.code == 2


def test_unload_with_both_flags_calls_the_library(monkeypatch, capsys):
    client = FakeClient("http://127.0.0.1:8081", running=[_model()])
    _patch_target(monkeypatch, client)

    assert cli.main(
        ["swapper", "unload", "--url", "http://127.0.0.1:8081", "--all", "--accept-no-drain"]
    ) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["protocol"] == cli.SWAPPER_PROTOCOL_VERSION
    assert [m["model"] for m in payload["unloaded"]] == ["qwen38"]


def test_unload_text_format_reports_each_model(monkeypatch, capsys):
    client = FakeClient("http://127.0.0.1:8081", running=[_model(), _model(model="other")])
    _patch_target(monkeypatch, client)

    assert cli.main(
        ["swapper", "unload", "--url", "http://127.0.0.1:8081", "--all", "--accept-no-drain", "--format", "text"]
    ) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "qwen38" in out and "other" in out


def test_unload_incomplete_maps_exit_failure(monkeypatch, capsys):
    from llm_scripting_kit.swapper import UnloadIncomplete

    client = FakeClient("http://127.0.0.1:8081", unload_error=UnloadIncomplete("/running still lists qwen38"))
    _patch_target(monkeypatch, client)

    assert cli.main(
        ["swapper", "unload", "--url", "http://127.0.0.1:8081", "--all", "--accept-no-drain"]
    ) == cli.EXIT_FAILURE
    assert json.loads(capsys.readouterr().err)["error"]["kind"] == "unload-incomplete"


# ---------------------------------------------------------------------------
# strays
# ---------------------------------------------------------------------------


def _stray_rec(pid, name, owner):
    return ProcessRecord(pid=pid, ppid=1, name=name, owner=owner, create_time=100.0)


def test_strays_none_found_is_exit_ok(monkeypatch, capsys):
    monkeypatch.setattr(cli, "find_strays", lambda: ())

    assert cli.main(["swapper", "strays"]) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"protocol": cli.SWAPPER_PROTOCOL_VERSION, "strays": []}


def test_strays_found_is_exit_failure(monkeypatch, capsys):
    monkeypatch.setattr(cli, "find_strays", lambda: (_stray_rec(701, "llama-server", "1000"),))

    assert cli.main(["swapper", "strays"]) == cli.EXIT_FAILURE
    payload = json.loads(capsys.readouterr().out)
    assert [s["pid"] for s in payload["strays"]] == [701]


def test_strays_text_format(monkeypatch, capsys):
    monkeypatch.setattr(cli, "find_strays", lambda: (_stray_rec(701, "llama-server", "1000"),))

    assert cli.main(["swapper", "strays", "--format", "text"]) == cli.EXIT_FAILURE
    out = capsys.readouterr().out
    assert "701" in out and "llama-server" in out


def test_strays_text_format_none_found(monkeypatch, capsys):
    monkeypatch.setattr(cli, "find_strays", lambda: ())

    assert cli.main(["swapper", "strays", "--format", "text"]) == cli.EXIT_OK
    assert "no strays" in capsys.readouterr().out


def test_strays_indeterminate_maps_exit_five(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "find_strays",
        lambda: (_ for _ in ()).throw(InspectionIndeterminate("cannot classify PID 800")),
    )

    assert cli.main(["swapper", "strays"]) == cli.EXIT_INDETERMINATE
    assert json.loads(capsys.readouterr().err)["error"]["kind"] == "inspection-indeterminate"


# ---------------------------------------------------------------------------
# guard-launch: the facade a launcher (e.g. model-server.sh) delegates to.
# Quiet on success; exit 3 on refusal -- a code distinct from EXIT_FAILURE
# (1) on purpose, because an uncaught Python exception also exits 1, and the
# launcher must fail OPEN on anything but an unambiguous refusal (see
# LaunchRefused's docstring in swapper.py; orchestrator decision,
# swapper-cli-u3). This is the plan's launcher-delegation test -- it proves
# the CLI seam a shell launcher calls behaves per contract, independent of
# model-server.sh itself (owned by a different unit).
# ---------------------------------------------------------------------------


def test_guard_launch_is_quiet_on_success(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(cli, "check_launch_allowed", lambda *, caller_pid: seen.setdefault("pid", caller_pid))

    assert cli.main(["swapper", "guard-launch", "--caller-pid", "4242"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert seen == {"pid": 4242}


def test_guard_launch_refused_is_exit_three_with_diagnosis(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "check_launch_allowed",
        lambda *, caller_pid: (_ for _ in ()).throw(
            LaunchRefused("llama-swap is running for this user (PID 279); launch through it")
        ),
    )

    assert cli.main(["swapper", "guard-launch", "--caller-pid", "700"]) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    envelope = json.loads(captured.err)["error"]
    assert envelope["kind"] == "launch-refused"
    assert "279" in envelope["message"]


def test_guard_launch_unknown_caller_pid_is_exit_usage(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "check_launch_allowed",
        lambda *, caller_pid: (_ for _ in ()).throw(SwapperUsageError(f"caller PID {caller_pid} does not exist")),
    )

    assert cli.main(["swapper", "guard-launch", "--caller-pid", "999999"]) == cli.EXIT_USAGE
    assert capsys.readouterr().out == ""


def test_guard_launch_indeterminate_is_exit_five(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "check_launch_allowed",
        lambda *, caller_pid: (_ for _ in ()).throw(InspectionIndeterminate("cannot read parent of PID 700")),
    )

    assert cli.main(["swapper", "guard-launch", "--caller-pid", "700"]) == cli.EXIT_INDETERMINATE


def test_guard_launch_requires_caller_pid(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["swapper", "guard-launch"])
    assert exc.value.code == 2


def test_guard_launch_takes_no_target(capsys):
    """guard-launch never resolves a swapper target -- it inspects the local
    process table only, so --endpoint/--url are not accepted here."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["swapper", "guard-launch", "--caller-pid", "1", "--url", "http://x"])
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# Error-kind kebab-casing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc, expected_kind",
    [
        (NonLocalTargetError("x"), "non-local-target"),
        (SafetyRefusal("x"), "safety-refusal"),
        (InspectionIndeterminate("x"), "inspection-indeterminate"),
        (ModelNotRunningError("x"), "model-not-running"),
        (LaunchRefused("x"), "launch-refused"),
        (SwapperUsageError("x"), "swapper-usage"),
    ],
)
def test_swapper_error_kind_is_kebab_case_of_the_class_name(exc, expected_kind):
    assert cli._swapper_error_kind(exc) == expected_kind


# ---------------------------------------------------------------------------
# terminate-listener
# ---------------------------------------------------------------------------


def _replacement(action="terminated", pid=900, signal="SIGTERM", escalated=False):
    from llm_scripting_kit.swapper import ListenerReplacement

    return ListenerReplacement(
        port=8080, action=action, pid=pid, create_time=12.5 if pid else None,
        signal=signal if pid else None, escalated=escalated,
    )


def test_terminate_listener_requires_accept_replace(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "terminate_listener",
        lambda *a, **k: pytest.fail("must not run without --accept-replace"),
    )
    with pytest.raises(SystemExit) as exc:
        cli.main(["swapper", "terminate-listener", "--port", "8080"])
    assert exc.value.code == 2


def test_terminate_listener_requires_port(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["swapper", "terminate-listener", "--accept-replace"])
    assert exc.value.code == 2


def test_terminate_listener_json_shape(monkeypatch, capsys):
    seen = {}

    def fake(port, **kw):
        seen.update(port=port, **kw)
        return _replacement()

    monkeypatch.setattr(cli, "terminate_listener", fake)
    rc = cli.main(["swapper", "terminate-listener", "--port", "8080", "--accept-replace"])
    assert rc == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "protocol": cli.SWAPPER_PROTOCOL_VERSION,
        "port": 8080,
        "action": "terminated",
        "pid": 900,
        "create_time": 12.5,
        "signal": "SIGTERM",
        "escalated": False,
    }
    assert seen["port"] == 8080 and seen["grace_s"] == 20.0


def test_terminate_listener_free_port_text(monkeypatch, capsys):
    monkeypatch.setattr(cli, "terminate_listener", lambda *a, **k: _replacement("none", None))
    rc = cli.main([
        "swapper", "terminate-listener", "--port", "8080", "--accept-replace",
        "--format", "text",
    ])
    assert rc == cli.EXIT_OK
    assert "free" in capsys.readouterr().out


@pytest.mark.parametrize(
    "exc,code,kind",
    [
        (SafetyRefusal("occupied"), cli.EXIT_FAILURE, "safety-refusal"),
        (InspectionIndeterminate("denied"), cli.EXIT_INDETERMINATE, "inspection-indeterminate"),
    ],
)
def test_terminate_listener_errors_map_to_exit_codes(monkeypatch, capsys, exc, code, kind):
    def boom(*a, **k):
        raise exc

    monkeypatch.setattr(cli, "terminate_listener", boom)
    rc = cli.main(["swapper", "terminate-listener", "--port", "8080", "--accept-replace"])
    assert rc == code
    assert json.loads(capsys.readouterr().err)["error"]["kind"] == kind
