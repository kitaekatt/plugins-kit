"""Tests for llm_scripting_kit.swapper: HTTP schema, locality, and the
process-safety rules, driven through a fake inspector so no real process is
ever inspected or signalled."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from llm_scripting_kit import swapper as sw
from llm_scripting_kit._swapper_process import ProcessRecord
from llm_scripting_kit.model_endpoints import EndpointEntry, EndpointRegistry, HARNESS_KIND

ME = "1000"
OTHER = "1001"
SWAP_URL = "http://127.0.0.1:8081"
PROXY_PORT = 5800
MODEL = "qwen38"

SWAPPER_PID = 279
CHILD_PID = 18596


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def running_body(*entries):
    return json.dumps({"running": list(entries)}).encode()


def entry(model=MODEL, proxy=f"http://127.0.0.1:{PROXY_PORT}", state="ready", cmd="ninfer-serve"):
    return {"model": model, "state": state, "proxy": proxy, "cmd": cmd}


class FakeTransport:
    def __init__(self, responses):
        # url -> list of bodies (popped in order; last one repeats) or an exception
        self.responses = {k: list(v) if isinstance(v, list) else [v] for k, v in responses.items()}
        self.calls = []

    def __call__(self, method, url, timeout_s):
        self.calls.append((method, url))
        queue = self.responses.get(url)
        if queue is None:
            raise AssertionError(f"unexpected request {method} {url}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item


def client_for(body=None, url=SWAP_URL, **extra):
    body = body if body is not None else running_body(entry())
    responses = {f"{url}/running": body}
    responses.update(extra)
    transport = FakeTransport(responses)
    return sw.SwapperClient(url, transport=transport), transport


def rec(pid, ppid, name, owner=ME, create_time=None, cmdline=None):
    return ProcessRecord(
        pid=pid,
        ppid=ppid,
        name=name,
        owner=owner,
        create_time=float(pid) if create_time is None else create_time,
        cmdline=cmdline,
    )


class FakeInspector:
    """In-memory process table. Records every signal; sends none."""

    def __init__(self, procs, listens=None, owner=ME):
        self.procs = {p.pid: p for p in procs}
        self.listens = listens or {}
        self.owner = owner
        self.denied = set()  # PIDs whose listen_addrs raise indeterminate
        self.signals = []
        self.exits_on = {"SIGTERM"}  # signals after which wait_gone reports gone
        self.process_calls = 0
        self.listen_calls = []
        self.on_process_call = {}  # call number -> callable(self)
        self.on_signal = {}  # signal -> callable(self), run before recording

    def current_owner(self):
        return self.owner

    def process(self, pid):
        self.process_calls += 1
        hook = self.on_process_call.get(self.process_calls)
        if hook:
            hook(self)
        if pid not in self.procs:
            raise sw.ProcessNotFound(f"no {pid}")
        return self.procs[pid]

    def processes(self):
        return tuple(self.procs.values())

    def listen_addrs(self, pid):
        self.listen_calls.append(pid)
        if pid in self.denied:
            raise sw.InspectionIndeterminate(f"denied {pid}")
        if pid not in self.procs:
            raise sw.ProcessNotFound(f"no {pid}")
        return frozenset(self.listens.get(pid, ()))

    def signal(self, pid, create_time, sig):
        hook = self.on_signal.get(sig)
        if hook:
            hook(self)
        current = self.procs.get(pid)
        if current is None or current.create_time != create_time:
            raise sw.ProcessNotFound(f"{pid} replaced")
        self.signals.append((pid, sig))

    def wait_gone(self, pid, create_time, timeout_s):
        gone = bool(self.signals) and self.signals[-1][1] in self.exits_on
        if gone:  # an exited process leaves the table with its sockets
            self.procs.pop(pid, None)
            self.listens.pop(pid, None)
        return gone


def standard_table(**overrides):
    procs = [
        rec(1, 0, "init", owner="0"),
        rec(SWAPPER_PID, 1, "llama-swap"),
        rec(CHILD_PID, SWAPPER_PID, "ninfer-serve"),
    ]
    listens = {
        SWAPPER_PID: {("127.0.0.1", 8081)},
        CHILD_PID: {("127.0.0.1", PROXY_PORT)},
    }
    insp = FakeInspector(procs, listens)
    for k, v in overrides.items():
        setattr(insp, k, v)
    return insp


# ---------------------------------------------------------------------------
# URL resolution and locality
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://127.0.0.1:8081/v1", "http://127.0.0.1:8081"),
        ("http://127.0.0.1:8081/v1/", "http://127.0.0.1:8081"),
        ("http://localhost:8081/", "http://localhost:8081"),
        ("https://box.example:9000", "https://box.example:9000"),
    ],
)
def test_resolve_url_strips_v1(raw, expected):
    assert sw.resolve_swapper_url(endpoint=None, url=raw) == expected


@pytest.mark.parametrize("endpoint,url", [(None, None), ("a", "http://127.0.0.1:1")])
def test_resolve_requires_exactly_one_target(endpoint, url):
    with pytest.raises(sw.SwapperUsageError):
        sw.resolve_swapper_url(endpoint=endpoint, url=url)


def test_resolve_rejects_non_http_url():
    with pytest.raises(sw.SwapperUsageError):
        sw.resolve_swapper_url(endpoint=None, url="ftp://127.0.0.1:8081")


def _registry():
    return EndpointRegistry(
        default_id="local",
        entries={
            "local": EndpointEntry(id="local", base_url="http://127.0.0.1:8081/v1", model="m"),
            "cli": EndpointEntry(
                id="cli", base_url=None, model="m", kind=HARNESS_KIND, harness="codex-cli"
            ),
        },
    )


def test_resolve_from_registry_entry():
    assert sw.resolve_swapper_url(endpoint="local", url=None, registry=_registry()) == SWAP_URL


def test_resolve_registry_harness_entry_is_usage_error():
    with pytest.raises(sw.SwapperUsageError):
        sw.resolve_swapper_url(endpoint="cli", url=None, registry=_registry())


def test_resolve_unknown_registry_entry_is_usage_error():
    with pytest.raises(sw.SwapperUsageError, match="unknown"):
        sw.resolve_swapper_url(endpoint="nope", url=None, registry=_registry())


@pytest.mark.parametrize(
    "url,local",
    [
        ("http://127.0.0.1:8081", True),
        ("http://127.3.4.5:8081", True),
        ("http://localhost:8081", True),
        ("http://[::1]:8081", True),
        ("http://192.168.1.20:8081", False),
        ("http://0.0.0.0:8081", False),
        ("http://box.example:8081", False),
    ],
)
def test_is_loopback_url(url, local):
    assert sw.is_loopback_url(url) is local


# ---------------------------------------------------------------------------
# HTTP schema
# ---------------------------------------------------------------------------


def test_running_parses_entries():
    client, _ = client_for(running_body(entry(), entry(model="other", cmd="x")))
    models = client.running()
    assert models[0] == sw.RunningModel(
        model=MODEL, state="ready", proxy=f"http://127.0.0.1:{PROXY_PORT}", cmd="ninfer-serve"
    )
    assert [m.model for m in models] == [MODEL, "other"]


def test_running_allows_remote_swapper():
    url = "http://192.168.1.20:8081"
    client, _ = client_for(running_body(entry()), url=url)
    assert len(client.running()) == 1


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        json.dumps({"running": {}}).encode(),
        json.dumps({"running": ["x"]}).encode(),
        json.dumps({"running": [{"model": "m", "state": "ready"}]}).encode(),
        json.dumps({"running": [{"model": "", "state": "ready", "proxy": "p"}]}).encode(),
        json.dumps({"running": [{"model": "m", "state": "r", "proxy": "p", "cmd": 3}]}).encode(),
    ],
)
def test_running_rejects_malformed_payload(body):
    client, _ = client_for(body)
    with pytest.raises(sw.SwapperProtocolError):
        client.running()


def test_running_http_failure_propagates():
    client, _ = client_for(sw.SwapperHTTPError("down"))
    with pytest.raises(sw.SwapperHTTPError):
        client.running()


# ---------------------------------------------------------------------------
# Unload
# ---------------------------------------------------------------------------


def test_unload_refuses_non_loopback_before_any_request():
    url = "http://192.168.1.20:8081"
    client, transport = client_for(url=url)
    with pytest.raises(sw.NonLocalTargetError):
        client.unload_all()
    assert transport.calls == []


def test_unload_returns_prior_set_and_verifies_empty():
    client, transport = client_for(
        [running_body(entry()), running_body()],
        **{f"{SWAP_URL}/api/models/unload": b"OK"},
    )
    before = client.unload_all(settle_s=0)
    assert [m.model for m in before] == [MODEL]
    assert transport.calls == [
        ("GET", f"{SWAP_URL}/running"),
        ("POST", f"{SWAP_URL}/api/models/unload"),
        ("GET", f"{SWAP_URL}/running"),
    ]


def test_unload_incomplete_when_running_stays_non_empty():
    client, _ = client_for(running_body(entry()), **{f"{SWAP_URL}/api/models/unload": b"OK"})
    with pytest.raises(sw.UnloadIncomplete, match=MODEL):
        client.unload_all(settle_s=0)


# ---------------------------------------------------------------------------
# Terminate: the safety invariants
# ---------------------------------------------------------------------------


def test_terminate_sends_sigterm_to_verified_child_only():
    client, _ = client_for()
    insp = standard_table()
    result = sw.terminate_model(client, MODEL, inspector=insp)
    assert insp.signals == [(CHILD_PID, "SIGTERM")]
    assert result.signal == "SIGTERM" and result.escalated is False
    assert result.process.pid == CHILD_PID
    assert result.process.swapper_pid == SWAPPER_PID
    assert result.process.model.model == MODEL


def test_terminate_escalates_to_sigkill_only_after_timeout():
    client, _ = client_for()
    insp = standard_table()
    insp.exits_on = {"SIGKILL"}
    result = sw.terminate_model(client, MODEL, grace_s=0.5, inspector=insp)
    assert insp.signals == [(CHILD_PID, "SIGTERM"), (CHILD_PID, "SIGKILL")]
    assert result.signal == "SIGKILL" and result.escalated is True


def test_terminate_fails_when_process_survives_sigkill():
    client, _ = client_for()
    insp = standard_table()
    insp.exits_on = set()
    with pytest.raises(sw.TerminationFailed):
        sw.terminate_model(client, MODEL, grace_s=0, inspector=insp)
    assert [s for _, s in insp.signals] == ["SIGTERM", "SIGKILL"]


def test_terminate_refuses_non_child_listener():
    """A same-user server on the proxy port whose parent is NOT the swapper."""
    client, _ = client_for()
    insp = FakeInspector(
        [
            rec(1, 0, "init", owner="0"),
            rec(SWAPPER_PID, 1, "llama-swap"),
            rec(700, 1, "bash"),
            rec(CHILD_PID, 700, "ninfer-serve"),  # manual launch from a shell
        ],
        {SWAPPER_PID: {("127.0.0.1", 8081)}, CHILD_PID: {("127.0.0.1", PROXY_PORT)}},
    )
    with pytest.raises(sw.SafetyRefusal, match="no direct child"):
        sw.terminate_model(client, MODEL, inspector=insp)
    assert insp.signals == []


def test_terminate_refuses_grandchild_listener():
    client, _ = client_for()
    insp = FakeInspector(
        [
            rec(SWAPPER_PID, 1, "llama-swap"),
            rec(500, SWAPPER_PID, "sh"),
            rec(CHILD_PID, 500, "ninfer-serve"),
        ],
        {SWAPPER_PID: {("127.0.0.1", 8081)}, CHILD_PID: {("127.0.0.1", PROXY_PORT)}},
    )
    with pytest.raises(sw.SafetyRefusal):
        sw.terminate_model(client, MODEL, inspector=insp)
    assert insp.signals == []


def test_terminate_refuses_child_not_listening_on_proxy_port():
    client, _ = client_for()
    insp = standard_table()
    insp.listens[CHILD_PID] = {("127.0.0.1", PROXY_PORT + 1)}
    with pytest.raises(sw.SafetyRefusal):
        sw.terminate_model(client, MODEL, inspector=insp)
    assert insp.signals == []


def test_terminate_never_signals_lookalikes():
    """Same-name servers elsewhere are never matched by name."""
    client, _ = client_for()
    insp = standard_table()
    insp.procs[900] = rec(900, 1, "ninfer-serve")
    insp.procs[901] = rec(901, 1, "llama-server")
    insp.listens[900] = {("127.0.0.1", 9999)}
    sw.terminate_model(client, MODEL, inspector=insp)
    assert {pid for pid, _ in insp.signals} == {CHILD_PID}


@pytest.mark.parametrize("name", ["llama-swap-old", "llama-swapper", "llama", "Llama-Swap"])
def test_terminate_requires_exact_swapper_name(name):
    client, _ = client_for()
    insp = standard_table()
    insp.procs[SWAPPER_PID] = rec(SWAPPER_PID, 1, name)
    with pytest.raises(sw.SafetyRefusal, match="named exactly"):
        sw.terminate_model(client, MODEL, inspector=insp)
    assert insp.signals == []


def test_terminate_accepts_windows_exe_suffix():
    client, _ = client_for()
    insp = standard_table()
    insp.procs[SWAPPER_PID] = rec(SWAPPER_PID, 1, "llama-swap.exe")
    sw.terminate_model(client, MODEL, inspector=insp)
    assert insp.signals == [(CHILD_PID, "SIGTERM")]


def test_terminate_refuses_other_users_swapper():
    client, _ = client_for()
    insp = standard_table()
    insp.procs[SWAPPER_PID] = rec(SWAPPER_PID, 1, "llama-swap", owner=OTHER)
    insp.procs[CHILD_PID] = rec(CHILD_PID, SWAPPER_PID, "ninfer-serve", owner=OTHER)
    with pytest.raises(sw.SafetyRefusal):
        sw.terminate_model(client, MODEL, inspector=insp)
    assert insp.signals == []


def test_terminate_refuses_child_owned_by_other_user():
    client, _ = client_for()
    insp = standard_table()
    insp.procs[CHILD_PID] = rec(CHILD_PID, SWAPPER_PID, "ninfer-serve", owner=OTHER)
    with pytest.raises(sw.SafetyRefusal):
        sw.terminate_model(client, MODEL, inspector=insp)
    assert insp.signals == []


def test_terminate_requires_swapper_listening_on_target_port():
    client, _ = client_for()
    insp = standard_table()
    insp.listens[SWAPPER_PID] = {("127.0.0.1", 9090)}
    with pytest.raises(sw.SafetyRefusal, match="port 8081"):
        sw.terminate_model(client, MODEL, inspector=insp)
    assert insp.signals == []


def test_terminate_refuses_two_swappers_on_port():
    client, _ = client_for()
    insp = standard_table()
    insp.procs[280] = rec(280, 1, "llama-swap")
    insp.listens[280] = {("0.0.0.0", 8081)}
    with pytest.raises(sw.SafetyRefusal, match="several"):
        sw.terminate_model(client, MODEL, inspector=insp)


def test_terminate_refuses_non_loopback_target_before_any_request():
    client, transport = client_for(url="http://192.168.1.20:8081")
    insp = standard_table()
    with pytest.raises(sw.NonLocalTargetError) as info:
        sw.terminate_model(client, MODEL, inspector=insp)
    assert info.value.exit_code == sw.EXIT_USAGE
    assert transport.calls == [] and insp.signals == []


def test_terminate_refuses_non_loopback_proxy():
    client, _ = client_for(running_body(entry(proxy=f"http://10.0.0.5:{PROXY_PORT}")))
    insp = standard_table()
    with pytest.raises(sw.SafetyRefusal, match="non-loopback"):
        sw.terminate_model(client, MODEL, inspector=insp)
    assert insp.signals == []


def test_terminate_missing_model():
    client, _ = client_for(running_body(entry(model="other")))
    insp = standard_table()
    with pytest.raises(sw.ModelNotRunningError):
        sw.terminate_model(client, MODEL, inspector=insp)
    assert insp.signals == []


def test_terminate_requires_exact_model_match():
    client, _ = client_for(running_body(entry(model=MODEL + "-big")))
    with pytest.raises(sw.ModelNotRunningError):
        sw.terminate_model(client, MODEL, inspector=standard_table())


def test_terminate_rechecks_identity_before_sigterm():
    """PID reused between verification and the signal: nothing is sent."""
    client, _ = client_for()
    insp = standard_table()

    def reuse(self):
        self.procs[CHILD_PID] = rec(CHILD_PID, SWAPPER_PID, "ninfer-serve", create_time=9e9)

    # call 1 is locate's own identity check; call 2 is the pre-SIGTERM check.
    insp.on_process_call[2] = reuse
    with pytest.raises(sw.ProcessChangedError):
        sw.terminate_model(client, MODEL, inspector=insp)
    assert insp.signals == []


def test_signal_layer_also_refuses_reused_pid():
    """Even if the PID is swapped after the re-read, the signal itself refuses."""
    client, _ = client_for()
    insp = standard_table()

    def reuse(self):
        self.procs[CHILD_PID] = rec(CHILD_PID, SWAPPER_PID, "ninfer-serve", create_time=9e9)

    insp.on_signal["SIGTERM"] = reuse
    with pytest.raises(sw.ProcessChangedError):
        sw.terminate_model(client, MODEL, inspector=insp)
    assert insp.signals == []


def test_terminate_rechecks_identity_before_sigkill():
    """Reparented after SIGTERM (swapper died): SIGKILL is not sent."""
    client, _ = client_for()
    insp = standard_table()
    insp.exits_on = set()

    def reparent(self):
        self.procs[CHILD_PID] = rec(CHILD_PID, 1, "ninfer-serve")

    insp.on_process_call[3] = reparent
    with pytest.raises(sw.ProcessChangedError):
        sw.terminate_model(client, MODEL, grace_s=0, inspector=insp)
    assert insp.signals == [(CHILD_PID, "SIGTERM")]


def test_exit_between_timeout_and_sigkill_counts_as_sigterm():
    client, _ = client_for()
    insp = standard_table()
    insp.exits_on = set()

    def vanish(self):
        del self.procs[CHILD_PID]

    insp.on_process_call[3] = vanish
    result = sw.terminate_model(client, MODEL, grace_s=0, inspector=insp)
    assert result.signal == "SIGTERM" and result.escalated is False
    assert insp.signals == [(CHILD_PID, "SIGTERM")]


def test_negative_grace_is_usage_error():
    client, _ = client_for()
    with pytest.raises(sw.SwapperUsageError):
        sw.terminate_model(client, MODEL, grace_s=-1, inspector=standard_table())


# ---------------------------------------------------------------------------
# Permission denied is indeterminate, never "not found"
# ---------------------------------------------------------------------------


def test_denied_swapper_listeners_is_indeterminate():
    client, _ = client_for()
    insp = standard_table()
    insp.denied.add(SWAPPER_PID)
    with pytest.raises(sw.InspectionIndeterminate) as info:
        sw.terminate_model(client, MODEL, inspector=insp)
    assert info.value.exit_code == sw.EXIT_INDETERMINATE
    assert insp.signals == []


def test_denied_child_listeners_is_indeterminate():
    client, _ = client_for()
    insp = standard_table()
    insp.denied.add(CHILD_PID)
    with pytest.raises(sw.InspectionIndeterminate):
        sw.terminate_model(client, MODEL, inspector=insp)
    assert insp.signals == []


def test_unreadable_parent_without_listener_is_indeterminate():
    client, _ = client_for()
    insp = standard_table()
    insp.procs[CHILD_PID] = ProcessRecord(
        pid=CHILD_PID, ppid=None, name=None, owner=None, create_time=0.0
    )
    with pytest.raises(sw.InspectionIndeterminate):
        sw.terminate_model(client, MODEL, inspector=insp)
    assert insp.signals == []


def test_unreadable_swapper_owner_is_indeterminate():
    client, _ = client_for()
    insp = standard_table()
    insp.procs[SWAPPER_PID] = rec(SWAPPER_PID, 1, "llama-swap", owner=None)
    with pytest.raises(sw.InspectionIndeterminate):
        sw.terminate_model(client, MODEL, inspector=insp)


# ---------------------------------------------------------------------------
# Strays
# ---------------------------------------------------------------------------


def test_find_strays_reports_unmanaged_servers_only():
    insp = standard_table()
    insp.procs[700] = rec(700, 1, "bash")
    insp.procs[701] = rec(701, 700, "llama-server")  # manual launch
    insp.procs[702] = rec(702, 1, "llama-server", owner=OTHER)  # another user
    insp.procs[703] = rec(703, 700, "vim")
    strays = sw.find_strays(inspector=insp)
    assert [s.pid for s in strays] == [701]


def test_find_strays_managed_through_launcher_shell_is_not_stray():
    insp = standard_table()
    insp.procs[500] = rec(500, SWAPPER_PID, "bash")
    insp.procs[501] = rec(501, 500, "ninfer-serve")
    assert sw.find_strays(inspector=insp) == ()


def test_find_strays_classifies_mlx_by_argv():
    insp = standard_table()
    insp.procs[800] = rec(
        800, 1, "Python", cmdline=("Python", "-m", "mlx_lm.server", "--port", "8082")
    )
    insp.procs[801] = rec(801, 1, "python3.12", cmdline=("/venv/bin/mlx_lm.server",))
    insp.procs[802] = rec(802, 1, "python3", cmdline=("python3", "app.py"))
    assert [s.pid for s in sw.find_strays(inspector=insp)] == [800, 801]


def test_find_strays_uid_override():
    insp = standard_table()
    insp.procs[702] = rec(702, 1, "llama-server", owner=OTHER)
    assert [s.pid for s in sw.find_strays(inspector=insp, uid=OTHER)] == [702]


def test_find_strays_unreadable_argv_is_indeterminate():
    insp = standard_table()
    insp.procs[800] = rec(800, 1, "python3", cmdline=None)
    with pytest.raises(sw.InspectionIndeterminate):
        sw.find_strays(inspector=insp)


def test_find_strays_unreadable_name_of_own_process_is_indeterminate():
    insp = standard_table()
    insp.procs[800] = rec(800, 1, None)
    with pytest.raises(sw.InspectionIndeterminate):
        sw.find_strays(inspector=insp)


# ---------------------------------------------------------------------------
# Launch guard
# ---------------------------------------------------------------------------


def test_launch_allowed_when_caller_descends_from_swapper():
    insp = standard_table()
    insp.procs[500] = rec(500, SWAPPER_PID, "bash")
    assert sw.check_launch_allowed(caller_pid=500, inspector=insp) is None


def test_launch_refused_beside_active_same_user_swapper():
    insp = standard_table()
    insp.procs[700] = rec(700, 1, "bash")
    with pytest.raises(sw.LaunchRefused, match=str(SWAPPER_PID)):
        sw.check_launch_allowed(caller_pid=700, inspector=insp)


def test_launch_allowed_without_any_swapper():
    insp = FakeInspector([rec(1, 0, "init", owner="0"), rec(700, 1, "bash")])
    assert sw.check_launch_allowed(caller_pid=700, inspector=insp) is None


def test_launch_ignores_other_users_swapper():
    insp = FakeInspector(
        [rec(1, 0, "init", owner="0"), rec(700, 1, "bash"), rec(279, 1, "llama-swap", owner=OTHER)]
    )
    assert sw.check_launch_allowed(caller_pid=700, inspector=insp) is None


def test_launch_unknown_caller_is_usage_error():
    with pytest.raises(sw.SwapperUsageError):
        sw.check_launch_allowed(caller_pid=4242, inspector=standard_table())


def test_launch_unreadable_ancestry_is_indeterminate():
    insp = standard_table()
    insp.procs[700] = rec(700, None, "bash")
    with pytest.raises(sw.InspectionIndeterminate):
        sw.check_launch_allowed(caller_pid=700, inspector=insp)


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------


def test_exit_codes_follow_cli_contract():
    assert sw.SwapperError.exit_code == 1
    assert sw.SafetyRefusal.exit_code == 1
    assert sw.ModelNotRunningError.exit_code == 1
    assert sw.SwapperUsageError.exit_code == 2
    assert sw.NonLocalTargetError.exit_code == 2
    # LaunchRefused is 3, not the SafetyRefusal/SwapperError default of 1:
    # an uncaught Python exception also exits 1, so model-server.sh's
    # fail-open guard needs a code that means ONLY "explicitly refused"
    # (orchestrator decision, swapper-cli-u3).
    assert sw.LaunchRefused.exit_code == 3
    assert sw.InspectionIndeterminate.exit_code == 5


def test_http_only_import_loads_neither_psutil_nor_process_layer(plugin_root):
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "import llm_scripting_kit.swapper as s;"
        "s.SwapperClient('http://127.0.0.1:1');"
        "print('psutil' in sys.modules, 'llm_scripting_kit._swapper_process' in sys.modules)"
    )
    import os

    out = subprocess.run(
        [sys.executable, "-c", code, os.path.join(plugin_root, "lib")],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "False False"


def test_urllib_transport_posts_unload_with_empty_body(monkeypatch):
    seen = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"OK"

    def fake_urlopen(req, timeout):
        seen.append((req.get_method(), req.full_url, req.data))
        return Resp()

    monkeypatch.setattr(sw.urllib.request, "urlopen", fake_urlopen)
    sw._urllib_transport("POST", f"{SWAP_URL}{sw.UNLOAD_PATH}", 1.0)
    assert seen == [("POST", f"{SWAP_URL}/api/models/unload", b"")]


# ---------------------------------------------------------------------------
# terminate_listener: explicit replacement of the server on one port
# ---------------------------------------------------------------------------

REPL_PORT = 8080
STRAY_PID = 900


def _flags(*values):
    """A port_free stub returning ``values`` in order, then the last forever."""
    seq = list(values)
    calls = []

    def port_free(port):
        calls.append(port)
        return seq.pop(0) if len(seq) > 1 else seq[0]

    port_free.calls = calls
    return port_free


def repl_table(name="ninfer-serve", cmdline=None):
    procs = [
        rec(1, 0, "init", owner="0"),
        rec(STRAY_PID, 1, name, cmdline=cmdline),
    ]
    return FakeInspector(procs, {STRAY_PID: {("0.0.0.0", REPL_PORT)}})


def test_terminate_listener_free_port_is_a_noop():
    insp = repl_table()
    insp.listens = {}
    res = sw.terminate_listener(REPL_PORT, inspector=insp, port_free=_flags(True))
    assert res.action == "none" and res.pid is None
    assert insp.signals == []


def test_terminate_listener_terms_the_one_recognized_server():
    insp = repl_table()
    res = sw.terminate_listener(
        REPL_PORT, inspector=insp, port_free=_flags(False, True), settle_s=0
    )
    assert insp.signals == [(STRAY_PID, "SIGTERM")]
    assert res.action == "terminated" and res.pid == STRAY_PID
    assert res.signal == "SIGTERM" and res.escalated is False


@pytest.mark.parametrize(
    "name,cmdline",
    [
        ("llama-server", None),
        ("python3", ("python3", "-m", "mlx_lm.server", "--port", "8080")),
    ],
)
def test_terminate_listener_recognizes_every_server_kind(name, cmdline):
    insp = repl_table(name=name, cmdline=cmdline)
    res = sw.terminate_listener(
        REPL_PORT, inspector=insp, port_free=_flags(False, True), settle_s=0
    )
    assert res.pid == STRAY_PID and insp.signals == [(STRAY_PID, "SIGTERM")]


def test_terminate_listener_skips_other_owner_before_listener_inspection():
    """Another owner's process is discarded on its KNOWN owner alone, so an
    unreadable listener table there (root, another user) cannot block."""
    insp = repl_table()
    insp.procs[50] = rec(50, 1, "sshd", owner="0")
    insp.denied.add(50)
    res = sw.terminate_listener(
        REPL_PORT, inspector=insp, port_free=_flags(False, True), settle_s=0
    )
    assert 50 not in insp.listen_calls
    assert res.pid == STRAY_PID


def test_terminate_listener_unknown_owner_candidate_that_is_unreadable_refuses():
    insp = repl_table()
    insp.procs[60] = ProcessRecord(
        pid=60, ppid=None, name=None, owner=None, create_time=0.0, cmdline=None
    )
    insp.denied.add(60)
    with pytest.raises(sw.InspectionIndeterminate):
        sw.terminate_listener(REPL_PORT, inspector=insp, port_free=_flags(False))
    assert insp.signals == []


def test_terminate_listener_refuses_unrecognized_same_owner_listener():
    insp = repl_table(name="nginx")
    with pytest.raises(sw.SafetyRefusal):
        sw.terminate_listener(REPL_PORT, inspector=insp, port_free=_flags(False))
    assert insp.signals == []


def test_terminate_listener_refuses_when_occupied_but_no_same_owner_listener():
    insp = repl_table()
    insp.listens = {}
    with pytest.raises(sw.SafetyRefusal):
        sw.terminate_listener(REPL_PORT, inspector=insp, port_free=_flags(False))
    assert insp.signals == []


def test_terminate_listener_refuses_unreadable_same_owner_process_before_signalling():
    insp = repl_table()
    insp.procs[70] = rec(70, 1, "helper")
    insp.denied.add(70)
    with pytest.raises(sw.InspectionIndeterminate):
        sw.terminate_listener(REPL_PORT, inspector=insp, port_free=_flags(False))
    assert insp.signals == []


def test_terminate_listener_refuses_two_recognized_listeners():
    insp = repl_table()
    insp.procs[901] = rec(901, 1, "llama-server")
    insp.listens[901] = {("127.0.0.1", REPL_PORT)}
    with pytest.raises(sw.SafetyRefusal):
        sw.terminate_listener(REPL_PORT, inspector=insp, port_free=_flags(False))
    assert insp.signals == []


def test_terminate_listener_refuses_a_swapper_child():
    insp = repl_table()
    insp.procs[STRAY_PID] = rec(STRAY_PID, 279, "ninfer-serve")
    insp.procs[279] = rec(279, 1, "llama-swap")
    with pytest.raises(sw.SafetyRefusal):
        sw.terminate_listener(REPL_PORT, inspector=insp, port_free=_flags(False))
    assert insp.signals == []


def test_terminate_listener_refuses_occupied_port_after_term_without_recognized_listener():
    """TERM succeeds, but an unrecognized process now holds the port: refuse,
    and never signal the replacement."""
    insp = repl_table()

    def wait_gone(pid, ct, timeout_s):
        insp.procs.pop(STRAY_PID)
        insp.procs[950] = rec(950, 1, "nginx")
        insp.listens = {950: {("0.0.0.0", REPL_PORT)}}
        return True

    insp.wait_gone = wait_gone
    with pytest.raises(sw.SafetyRefusal):
        sw.terminate_listener(
            REPL_PORT, inspector=insp, port_free=_flags(False), settle_s=0
        )
    assert insp.signals == [(STRAY_PID, "SIGTERM")]


def test_terminate_listener_escalates_to_kill_only_for_same_pid_and_create_time():
    insp = repl_table()
    insp.exits_on = {"SIGKILL"}
    res = sw.terminate_listener(
        REPL_PORT, inspector=insp, port_free=_flags(False, True), grace_s=0, settle_s=0
    )
    assert insp.signals == [(STRAY_PID, "SIGTERM"), (STRAY_PID, "SIGKILL")]
    assert res.signal == "SIGKILL" and res.escalated is True


def test_terminate_listener_does_not_kill_a_reused_pid():
    insp = repl_table()

    def wait_gone(pid, ct, timeout_s):
        # The verified server exited and the PID was reused by a stranger.
        insp.procs[pid] = rec(pid, 1, "nginx", create_time=ct + 500.0)
        insp.listens.pop(pid, None)
        return False

    insp.wait_gone = wait_gone
    res = sw.terminate_listener(
        REPL_PORT, inspector=insp, port_free=_flags(False, True), grace_s=0, settle_s=0
    )
    assert insp.signals == [(STRAY_PID, "SIGTERM")]
    assert res.escalated is False


def test_terminate_listener_fails_when_server_survives_kill():
    insp = repl_table()
    insp.exits_on = set()
    with pytest.raises(sw.TerminationFailed):
        sw.terminate_listener(
            REPL_PORT, inspector=insp, port_free=_flags(False), grace_s=0
        )
    assert [s for _, s in insp.signals] == ["SIGTERM", "SIGKILL"]


def test_terminate_listener_refuses_when_identity_changes_before_signal():
    insp = repl_table()
    insp.on_process_call[1] = lambda i: i.procs.__setitem__(
        STRAY_PID, rec(STRAY_PID, 1, "ninfer-serve", create_time=9999.0)
    )
    with pytest.raises(sw.ProcessChangedError):
        sw.terminate_listener(REPL_PORT, inspector=insp, port_free=_flags(False))
    assert insp.signals == []


@pytest.mark.parametrize("port", [0, -1, 70000])
def test_terminate_listener_rejects_invalid_port(port):
    with pytest.raises(sw.SwapperUsageError):
        sw.terminate_listener(port, inspector=repl_table(), port_free=_flags(True))


# -- "free" needs the bind check AND an empty listener scan ------------------


def _holds_after_exit(insp):
    """wait_gone reports the server gone but its listener stays in the table."""
    insp.wait_gone = lambda pid, ct, timeout_s: True


@pytest.mark.parametrize("ip", ["192.0.2.10", "::1", "::"])
def test_terminate_listener_sees_listener_on_non_loopback_address(ip):
    """A bind check that reads free must not hide a recognized server that is
    LISTENing on one specific (LAN or IPv6) address."""
    insp = repl_table()
    insp.listens = {STRAY_PID: {(ip, REPL_PORT)}}
    res = sw.terminate_listener(
        REPL_PORT, inspector=insp, port_free=_flags(True), settle_s=0
    )
    assert insp.signals == [(STRAY_PID, "SIGTERM")]
    assert res.action == "terminated" and res.pid == STRAY_PID


def test_terminate_listener_settle_scan_overrides_a_free_bind_check():
    """After TERM the bind check says free, but the scan still finds the
    listener: success must not be reported."""
    insp = repl_table()
    insp.listens = {STRAY_PID: {("192.0.2.10", REPL_PORT)}}
    _holds_after_exit(insp)
    with pytest.raises(sw.SafetyRefusal):
        sw.terminate_listener(
            REPL_PORT, inspector=insp, port_free=_flags(True), settle_s=0
        )
    assert insp.signals == [(STRAY_PID, "SIGTERM")]


def test_terminate_listener_free_bind_with_unreadable_scan_is_indeterminate():
    insp = repl_table()
    insp.procs[70] = rec(70, 1, "helper")
    insp.denied.add(70)
    with pytest.raises(sw.InspectionIndeterminate):
        sw.terminate_listener(REPL_PORT, inspector=insp, port_free=_flags(True))
    assert insp.signals == []
