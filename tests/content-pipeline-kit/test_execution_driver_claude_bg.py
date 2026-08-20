"""Tests for content_pipeline.execution.drivers.claude_bg -- B1, steps 1-11.

No automated test in this module ever reaches a real subprocess for
``claude``: ``_no_real_claude_subprocess`` (autouse) replaces
``claude_bg._default_runner`` with a stub that fails the test outright if
anything ever calls it, and every test that needs a `claude` response
supplies its own scripted ``runner`` via :class:`FakeRunner`.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import os
import subprocess
import sys
import textwrap

import pytest

from content_pipeline.execution.adapter import RunAdapter, WorkerEnvironment
from content_pipeline.execution.drivers import claude_bg
from content_pipeline.execution.drivers.claude_bg import (
    ANSWER_FENCE_PREFIX,
    BILLING_DIVERTING_VARS,
    AgentsJsonParseError,
    AnswerFenceMismatchError,
    ClaudeCli,
    ClaudeExecutableNotFoundError,
    DispatchReport,
    LaunchMisconfigurationError,
    OpenDispatch,
    ParseResult,
    PreflightError,
    SessionRecord,
    WorkerCommand,
    WorkerEnvironmentBillingLeakError,
    answer_path_for,
    build_launch_prompt,
    classify_settled_failure,
    compose_worker_environment,
    dispatch_unit,
    dispatch_wave,
    envelope_path_for,
    enumerate_worker_invocations,
    format_fenced_answer,
    parse_agents_json,
    parse_fenced_answer,
    preflight,
    reclaim_attempt_count,
    reclaimable_units,
    supervise_tick,
    worker_envelopes_for,
)
from content_pipeline.execution.model import (
    AlreadyClaimedError,
    AttemptKind,
    NoOpenDispatchError,
    RunHaltedError,
    RunRecord,
    StaleDispatcherLeaseError,
    TerminalStateError,
    UnitState,
)
from content_pipeline.execution.store import ExecutionStore

LIB_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir,
                 "plugins", "content-pipeline-kit", "lib")
)


@pytest.fixture(autouse=True)
def _no_real_claude_subprocess(monkeypatch):
    """No test in this module may reach a real subprocess for `claude`."""

    def _raise(*args, **kwargs):
        raise AssertionError(
            "a test reached the REAL default claude runner; every test must "
            "supply its own scripted `runner=` instead"
        )

    monkeypatch.setattr(claude_bg, "_default_runner", _raise)


# ---------------------------------------------------------------------------
# FakeRunner -- a scriptable claude process, driven per-invocation
# ---------------------------------------------------------------------------


class FakeRunner:
    """Scripts responses by argv PREFIX match (the longest matching key
    wins) and logs every call for later assertions. Any unscripted argv
    raises loudly rather than silently returning something plausible."""

    def __init__(self, scripts=None, *, default=None):
        self.scripts = dict(scripts or {})
        self.default = default
        self.calls = []  # list of (argv, kwargs)

    def script(self, argv_prefix, response):
        self.scripts[tuple(argv_prefix)] = response

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append((argv, kwargs))
        best_match = None
        for prefix, response in self.scripts.items():
            if tuple(argv[: len(prefix)]) == prefix and (
                best_match is None or len(prefix) > len(best_match[0])
            ):
                best_match = (prefix, response)
        if best_match is not None:
            response = best_match[1]
            if isinstance(response, list):
                # A SEQUENCE of responses for this prefix: advance through it
                # on each matching call, staying on the last entry once
                # exhausted (mutates the same list object stored in
                # self.scripts, so state persists across calls).
                if len(response) > 1:
                    return response.pop(0)
                return response[0]
            return response
        if self.default is not None:
            return self.default
        raise AssertionError(f"FakeRunner: no script for argv {argv!r}")


def _cli(runner) -> ClaudeCli:
    return ClaudeCli(executable="claude", runner=runner)


# ===========================================================================
# Step 1 -- the claude process seam: command construction
# ===========================================================================


def test_resolve_executable_uses_configured_value_first():
    cli = ClaudeCli(executable=r"C:\tools\claude.exe", runner=FakeRunner())
    assert cli.resolve_executable() == r"C:\tools\claude.exe"


def test_resolve_executable_refuses_when_not_found(monkeypatch):
    monkeypatch.setattr(claude_bg.shutil, "which", lambda name: None)
    cli = ClaudeCli(executable=None, runner=FakeRunner())
    with pytest.raises(ClaudeExecutableNotFoundError):
        cli.resolve_executable()


def test_resolve_executable_falls_back_to_path(monkeypatch):
    monkeypatch.setattr(claude_bg.shutil, "which", lambda name: r"D:\bin\claude.exe")
    cli = ClaudeCli(executable=None, runner=FakeRunner())
    assert cli.resolve_executable() == r"D:\bin\claude.exe"


def test_launch_bg_argv_is_positional_prompt_never_dash_p():
    runner = FakeRunner()
    runner.script(("claude", "--bg"), ("backgrounded * abc123", "", 0))
    cli = _cli(runner)
    cli.launch_bg("do the thing")
    argv, kwargs = runner.calls[-1]
    assert argv == ["claude", "--bg", "do the thing"]


def test_launch_bg_extra_args_come_before_the_positional_prompt():
    runner = FakeRunner()
    runner.script(("claude", "--bg"), ("backgrounded * abc123", "", 0))
    cli = _cli(runner)
    cli.launch_bg("prompt text", extra_args=["--permission-mode", "manual"])
    argv, _ = runner.calls[-1]
    assert argv == ["claude", "--bg", "--permission-mode", "manual", "prompt text"]


def test_agents_json_default_requests_all_sessions():
    runner = FakeRunner()
    runner.script(("claude", "agents"), ("[]", "", 0))
    cli = _cli(runner)
    cli.agents_json()
    argv, _ = runner.calls[-1]
    assert argv == ["claude", "agents", "--json", "--all"]


def test_agents_json_all_sessions_false_omits_all_flag():
    runner = FakeRunner()
    runner.script(("claude", "agents"), ("[]", "", 0))
    cli = _cli(runner)
    cli.agents_json(all_sessions=False)
    argv, _ = runner.calls[-1]
    assert argv == ["claude", "agents", "--json"]


@pytest.mark.parametrize("verb,method", [("stop", "stop"), ("rm", "rm"), ("respawn", "respawn")])
def test_lifecycle_verbs_are_emitted_top_level_never_under_agents(verb, method):
    runner = FakeRunner()
    runner.script(("claude", verb), ("ok", "", 0))
    cli = _cli(runner)
    getattr(cli, method)("sess-123")
    argv, _ = runner.calls[-1]
    assert argv == ["claude", verb, "sess-123"]
    assert "agents" not in argv


def test_version_argv():
    runner = FakeRunner()
    runner.script(("claude", "--version"), ("2.1.233 (Claude Code)", "", 0))
    cli = _cli(runner)
    cli.version()
    argv, _ = runner.calls[-1]
    assert argv == ["claude", "--version"]


def test_claude_cli_has_no_logs_method():
    """Deliberate: `claude logs <id>` is live-daemon-only (P13); shipping a
    method for it would let a later halt-classification path for a SETTLED
    unit reach for the wrong channel."""
    assert not hasattr(ClaudeCli, "logs")


_AGENTS_PERMITTED_SHAPES = ({"claude", "agents", "--json"}, {"claude", "agents", "--json", "--all"})


def _assert_agents_token_invariant(calls):
    """The shared assertion: every logged argv carrying the "agents" token
    must be one of the two permitted shapes, and never adjacent to a
    lifecycle verb."""
    for argv, _kwargs in calls:
        if "agents" in argv:
            assert set(argv) in _AGENTS_PERMITTED_SHAPES, f"unexpected 'agents' argv shape: {argv!r}"
            idx = argv.index("agents")
            if idx + 1 < len(argv):
                assert argv[idx + 1] not in ("stop", "logs", "rm", "respawn"), argv


def _call_every_public_command_method(cli, *, dummy: str = "dummy-arg"):
    """Dynamically discover and invoke every public, non-private METHOD
    ``type(cli)`` defines (introspective -- no hand-maintained method list),
    supplying a dummy positional string for every required positional
    parameter and leaving every parameter with a default alone.

    Deliberately does not special-case any method by name: a method added
    to :class:`ClaudeCli` (or a subclass) LATER is discovered and called
    automatically, which is the whole point (see
    ``test_introspective_invariant_catches_a_method_the_enumerated_list_would_miss``).
    ``resolve_executable`` (no argv, not a command) is skipped because it
    takes no runner call at all -- calling it is harmless but adds nothing.
    Dataclass FIELDS (``executable``, ``runner``) are excluded -- ``runner``
    in particular is a plain function object at class scope (a dataclass
    default), which ``inspect.isfunction`` would otherwise mistake for a
    method and call unbound.
    """
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(type(cli))}
    for name, member in inspect.getmembers(type(cli), predicate=inspect.isfunction):
        if name.startswith("_") or name == "resolve_executable" or name in field_names:
            continue
        sig = inspect.signature(member)
        args = []
        params = list(sig.parameters.values())[1:]  # skip `self`
        for param in params:
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            if param.default is not inspect.Parameter.empty:
                continue
            args.append(dummy)
        getattr(cli, name)(*args)


def test_module_wide_agents_token_invariant():
    """P3: the token "agents" appears in exactly ONE argv shape this module
    builds for real dispatch -- [exe, "agents", "--json"] or
    [exe, "agents", "--json", "--all"] -- and is never adjacent to a
    lifecycle verb.

    Introspective (not a hand-enumerated call list): discovers and calls
    every public ``ClaudeCli`` command-building method via
    ``_call_every_public_command_method``, so a method added later is
    covered with no list to maintain.

    MUTATION CHECK (performed manually, see the task report): changing
    ClaudeCli._lifecycle's argv to `[exe, "agents", verb, session_id]`
    flips this test red, because that argv contains "agents" immediately
    followed by a lifecycle verb -- neither of the two permitted shapes.
    """
    runner = FakeRunner(default=("", "", 0))
    cli = _cli(runner)
    _call_every_public_command_method(cli)
    _assert_agents_token_invariant(runner.calls)


def test_introspective_invariant_catches_a_method_the_enumerated_list_would_miss():
    """Proof the introspective form catches something the hand-enumerated
    form (the module's original shape, which called exactly
    ``launch_bg``/``agents_json``/``stop``/``rm``/``respawn``/``version`` by
    name) would silently miss: a NEW method added to a ``ClaudeCli``
    subclass, never added to any hand-maintained call list, that violates
    P3's invariant by emitting the forbidden `agents <lifecycle-verb>`
    shape.

    The old, hand-enumerated form calls a fixed set of method NAMES -- it
    would never call ``evil_lifecycle`` at all, so it would report a clean
    pass no matter what that method does. The introspective form discovers
    it via ``inspect.getmembers`` and calls it automatically, so its
    violation is caught with no test-author action.
    """

    class ExtendedClaudeCli(ClaudeCli):
        def evil_lifecycle(self, session_id):
            exe = self.resolve_executable()
            return self._invoke([exe, "agents", "stop", session_id])

    runner = FakeRunner(default=("", "", 0))
    cli = ExtendedClaudeCli(executable="claude", runner=runner)
    _call_every_public_command_method(cli)

    with pytest.raises(AssertionError):
        _assert_agents_token_invariant(runner.calls)

    # And the old hand-enumerated shape (the six original method names)
    # genuinely would NOT have caught it -- it never calls evil_lifecycle.
    # Simulate the old form directly: only the six originally-named methods.
    runner2 = FakeRunner(default=("", "", 0))
    cli2 = ExtendedClaudeCli(executable="claude", runner=runner2)
    cli2.launch_bg("hello")
    cli2.agents_json(all_sessions=True)
    cli2.agents_json(all_sessions=False)
    cli2.stop("s1")
    cli2.rm("s2")
    cli2.respawn("s3")
    cli2.version()
    _assert_agents_token_invariant(runner2.calls)  # passes -- the old form never saw the violation


# ===========================================================================
# Step 2 -- preflight
# ===========================================================================


_AGENTS_HELP_TEXT = (
    "Usage: claude agents [options] [command]\n\nManage background sessions "
    "(subcommands: stop, logs, rm, respawn)"
)
_STOP_HELP_TEXT = "Usage: claude stop <id>\n\nStop a background session"
_LOGS_HELP_TEXT = "Usage: claude logs <id>\n\nShow logs for a background session"
_RM_HELP_TEXT = "Usage: claude rm <id>\n\nRemove a background session"
_RESPAWN_HELP_TEXT = "Usage: claude respawn <id>\n\nRespawn a background session"


def _healthy_runner(*, agents_json_body="[]", version=("2.1.233", "", 0)):
    """A FakeRunner scripted to pass every preflight check."""
    runner = FakeRunner()
    runner.script(("claude", "--version"), version)
    runner.script(("claude", "agents", "--json"), (agents_json_body, "", 0))
    runner.script(("claude", "agents", "--help"), (_AGENTS_HELP_TEXT, "", 0))
    runner.script(("claude", "agents", "stop", "--help"), (_AGENTS_HELP_TEXT, "", 0))
    runner.script(("claude", "stop", "--help"), (_STOP_HELP_TEXT, "", 0))
    runner.script(("claude", "logs", "--help"), (_LOGS_HELP_TEXT, "", 0))
    runner.script(("claude", "rm", "--help"), (_RM_HELP_TEXT, "", 0))
    runner.script(("claude", "respawn", "--help"), (_RESPAWN_HELP_TEXT, "", 0))
    runner.script(("claude", "--bg", "-p"), ("", "error: option '-p' cannot be used with '--bg'", 1))
    return runner


def test_preflight_happy_path_returns_report():
    cli = _cli(_healthy_runner())
    report = preflight(cli, env={})
    assert report.executable == "claude"
    assert report.agents_json_sample == []
    assert "2.1.233" in report.version_output


# -- check 1: executable resolvable -------------------------------------------


def test_preflight_refuses_when_executable_not_found(monkeypatch):
    monkeypatch.setattr(claude_bg.shutil, "which", lambda name: None)
    cli = ClaudeCli(executable=None, runner=_healthy_runner())
    with pytest.raises(PreflightError):
        preflight(cli, env={})


# -- check 2: version recorded, never a gate ----------------------------------


def test_preflight_accepts_unrecognized_version_string():
    """ACCEPT case: an unrecognized `--version` string must pass -- version
    is recorded, never a gate."""
    runner = _healthy_runner(version=("Claude Code vNext-experimental-????", "", 0))
    cli = _cli(runner)
    report = preflight(cli, env={})
    assert "vNext" in report.version_output


def test_preflight_version_nonzero_exit_only_warns():
    runner = _healthy_runner(version=("", "boom", 1))
    cli = _cli(runner)
    report = preflight(cli, env={})  # must not raise -- version is never a gate
    assert report.warnings  # a warning was recorded


# -- check 3: auth fails closed (exact-name membership) -----------------------


@pytest.mark.parametrize("var_name", BILLING_DIVERTING_VARS)
def test_preflight_refuses_when_any_billing_diverting_var_is_set(var_name):
    cli = _cli(_healthy_runner())
    with pytest.raises(PreflightError):
        preflight(cli, env={var_name: "some-value"})


def test_preflight_accepts_when_no_credential_variable_is_set():
    """ACCEPT case: no credential variable set at all."""
    cli = _cli(_healthy_runner())
    preflight(cli, env={})  # must not raise


def test_preflight_accepts_empty_string_api_key():
    """ACCEPT case: ANTHROPIC_API_KEY="" is unset, matching
    WorkerEnvironment.check's own forbidden-var truthiness."""
    cli = _cli(_healthy_runner())
    preflight(cli, env={"ANTHROPIC_API_KEY": ""})  # must not raise


def test_preflight_accepts_unrelated_prefix_sharing_variable():
    """ACCEPT case: a substring match would wrongly refuse these -- refusing
    a healthy environment is how an operator disables the check."""
    cli = _cli(_healthy_runner())
    preflight(
        cli,
        env={"ANTHROPIC_LOG": "debug", "CLAUDE_CODE_ENABLE_TELEMETRY": "1"},
    )  # must not raise


def test_preflight_auth_check_is_never_a_substring_match():
    """MUTATION CHECK target: if the auth check were rewritten as a
    substring scan (`any(name in key for key in env for name in
    BILLING_DIVERTING_VARS)` or similar), this accept case would flip to a
    refusal. Pinned directly as a regression rather than only asserted by
    manual mutation."""
    cli = _cli(_healthy_runner())
    preflight(cli, env={"ANTHROPIC_LOG_LEVEL": "debug"})  # shares the ANTHROPIC prefix; must pass


# -- check 4: agents --json runs, decodes, yields a list -----------------------


def test_preflight_accepts_empty_agents_json_list():
    """ACCEPT case: agents --json returning an empty list must pass."""
    cli = _cli(_healthy_runner(agents_json_body="[]"))
    report = preflight(cli, env={})
    assert report.agents_json_sample == []


def test_preflight_refuses_non_list_agents_json():
    runner = _healthy_runner(agents_json_body='{"not": "a list"}')
    cli = _cli(runner)
    with pytest.raises(PreflightError):
        preflight(cli, env={})


def test_preflight_refuses_malformed_agents_json():
    runner = _healthy_runner(agents_json_body="not json at all")
    cli = _cli(runner)
    with pytest.raises(PreflightError):
        preflight(cli, env={})


def test_preflight_refuses_nonzero_agents_json_exit():
    runner = _healthy_runner()
    runner.script(("claude", "agents", "--json"), ("", "boom", 1))
    cli = _cli(runner)
    with pytest.raises(PreflightError):
        preflight(cli, env={})


# -- check 5: lifecycle verbs behave (P3) --------------------------------------


def test_preflight_passes_when_verb_help_is_distinct_and_names_the_verb():
    cli = _cli(_healthy_runner())
    preflight(cli, env={})  # must not raise


def test_preflight_refuses_when_a_verb_help_is_byte_identical_to_agents_help():
    """MUTATION CHECK: delete this assertion and the fake whose `stop
    --help` echoes `agents --help` stops raising."""
    runner = _healthy_runner()
    runner.script(("claude", "stop", "--help"), (_AGENTS_HELP_TEXT, "", 0))
    cli = _cli(runner)
    with pytest.raises(PreflightError):
        preflight(cli, env={})


def test_preflight_refuses_when_verb_help_never_mentions_the_verb():
    runner = _healthy_runner()
    runner.script(("claude", "rm", "--help"), ("some unrelated generic help text", "", 0))
    cli = _cli(runner)
    with pytest.raises(PreflightError):
        preflight(cli, env={})


def test_preflight_passes_when_agents_stop_help_matches_plain_agents_help():
    """The documented P3 silent shape: `claude agents stop --help` must BE
    the plain `agents` help."""
    cli = _cli(_healthy_runner())
    preflight(cli, env={})  # must not raise (already scripted to match)


def test_preflight_refuses_loudly_if_agents_stop_help_diverges_from_agents_help():
    """If the platform changed and `agents stop --help` now differs from
    plain `agents --help`, the report says so loudly rather than silently
    trusting stale assumptions."""
    runner = _healthy_runner()
    runner.script(("claude", "agents", "stop", "--help"), ("a totally different help text", "", 0))
    cli = _cli(runner)
    with pytest.raises(PreflightError):
        preflight(cli, env={})


# -- check 6: `claude --bg -p x` is rejected -----------------------------------


def test_preflight_passes_when_bg_dash_p_is_rejected():
    cli = _cli(_healthy_runner())
    preflight(cli, env={})  # must not raise (already scripted to reject)


def test_preflight_refuses_when_bg_dash_p_exits_zero():
    runner = _healthy_runner()
    runner.script(("claude", "--bg", "-p"), ("backgrounded * xyz", "", 0))
    cli = _cli(runner)
    with pytest.raises(PreflightError):
        preflight(cli, env={})


def test_preflight_refuses_when_bg_dash_p_appears_to_have_spawned():
    runner = _healthy_runner()
    runner.script(("claude", "--bg", "-p"), ("backgrounded * xyz", "some conflict text", 1))
    cli = _cli(runner)
    with pytest.raises(PreflightError):
        preflight(cli, env={})


# ===========================================================================
# Step 3 -- store migration: dispatcher lease + dispatches table
# ===========================================================================


def _seeded_store(tmp_path, *, unit_ids=("u0", "u1")) -> ExecutionStore:
    store = ExecutionStore(tmp_path / "run.db")
    store.create_run("run-1", driver="claude_bg", backend="claude-bg", model="m", adapter_version="")
    store.register_units("run-1", list(unit_ids))
    return store


def test_migration_adds_dispatcher_lease_columns_and_dispatches_table(tmp_path):
    store = _seeded_store(tmp_path)
    run = store.get_run("run-1")
    assert run.dispatcher_id is None
    assert run.dispatcher_lease_expires_at is None
    assert run.dispatcher_fence == 0
    assert store.open_dispatches("run-1") == []


def test_acquire_dispatcher_lease_first_acquire_succeeds(tmp_path):
    store = _seeded_store(tmp_path)
    fence = store.acquire_dispatcher_lease("run-1", "dispatcher-a", lease_seconds=60, at=1000.0)
    assert fence == 1
    run = store.get_run("run-1")
    assert run.dispatcher_id == "dispatcher-a"
    assert run.dispatcher_fence == 1
    assert run.dispatcher_lease_expires_at == 1060.0


def test_acquire_dispatcher_lease_fails_for_a_second_live_dispatcher(tmp_path):
    store = _seeded_store(tmp_path)
    store.acquire_dispatcher_lease("run-1", "dispatcher-a", lease_seconds=60, at=1000.0)
    fence = store.acquire_dispatcher_lease("run-1", "dispatcher-b", lease_seconds=60, at=1010.0)
    assert fence is None
    run = store.get_run("run-1")
    assert run.dispatcher_id == "dispatcher-a"  # untouched


def test_acquire_dispatcher_lease_succeeds_for_a_second_dispatcher_after_expiry(tmp_path):
    store = _seeded_store(tmp_path)
    store.acquire_dispatcher_lease("run-1", "dispatcher-a", lease_seconds=60, at=1000.0)
    fence = store.acquire_dispatcher_lease("run-1", "dispatcher-b", lease_seconds=60, at=2000.0)
    assert fence == 2
    run = store.get_run("run-1")
    assert run.dispatcher_id == "dispatcher-b"


def test_same_dispatcher_may_reacquire_after_its_own_lease_expired(tmp_path):
    store = _seeded_store(tmp_path)
    store.acquire_dispatcher_lease("run-1", "dispatcher-a", lease_seconds=60, at=1000.0)
    fence = store.acquire_dispatcher_lease("run-1", "dispatcher-a", lease_seconds=60, at=2000.0)
    assert fence == 2


def test_renew_dispatcher_lease_extends_expiry(tmp_path):
    store = _seeded_store(tmp_path)
    fence = store.acquire_dispatcher_lease("run-1", "dispatcher-a", lease_seconds=60, at=1000.0)
    new_expiry = store.renew_dispatcher_lease(
        "run-1", "dispatcher-a", fence, lease_seconds=60, at=1050.0
    )
    assert new_expiry == 1110.0


def test_renew_dispatcher_lease_rejects_stale_fence(tmp_path):
    store = _seeded_store(tmp_path)
    store.acquire_dispatcher_lease("run-1", "dispatcher-a", lease_seconds=60, at=1000.0)
    with pytest.raises(StaleDispatcherLeaseError):
        store.renew_dispatcher_lease("run-1", "dispatcher-a", 999, lease_seconds=60, at=1010.0)


def test_renew_dispatcher_lease_rejects_wrong_dispatcher(tmp_path):
    store = _seeded_store(tmp_path)
    fence = store.acquire_dispatcher_lease("run-1", "dispatcher-a", lease_seconds=60, at=1000.0)
    with pytest.raises(StaleDispatcherLeaseError):
        store.renew_dispatcher_lease("run-1", "dispatcher-b", fence, lease_seconds=60, at=1010.0)


def test_release_dispatcher_lease_clears_holder_and_lets_another_acquire(tmp_path):
    store = _seeded_store(tmp_path)
    fence = store.acquire_dispatcher_lease("run-1", "dispatcher-a", lease_seconds=60, at=1000.0)
    store.release_dispatcher_lease("run-1", "dispatcher-a", fence, at=1005.0)
    run = store.get_run("run-1")
    assert run.dispatcher_id is None
    assert run.dispatcher_lease_expires_at is None
    assert run.dispatcher_fence == 1  # monotonic counter, never reset

    fence2 = store.acquire_dispatcher_lease("run-1", "dispatcher-b", lease_seconds=60, at=1006.0)
    assert fence2 == 2


def test_release_dispatcher_lease_rejects_stale_fence(tmp_path):
    store = _seeded_store(tmp_path)
    store.acquire_dispatcher_lease("run-1", "dispatcher-a", lease_seconds=60, at=1000.0)
    with pytest.raises(StaleDispatcherLeaseError):
        store.release_dispatcher_lease("run-1", "dispatcher-a", 999, at=1005.0)


def test_record_dispatch_and_open_dispatches(tmp_path):
    store = _seeded_store(tmp_path)
    dispatch_id = store.record_dispatch("run-1", "u0", "worker-1", at=1000.0)
    assert isinstance(dispatch_id, int)
    open_ = store.open_dispatches("run-1")
    assert len(open_) == 1
    assert open_[0].unit_id == "u0"
    assert open_[0].worker_id == "worker-1"
    assert open_[0].session_id is None
    assert open_[0].settled_at is None


def test_record_dispatch_second_open_dispatch_for_same_unit_is_rejected(tmp_path):
    """The guarded uniqueness index: at most one OPEN dispatch per unit."""
    store = _seeded_store(tmp_path)
    store.record_dispatch("run-1", "u0", "worker-1", at=1000.0)
    with pytest.raises(Exception):  # sqlite3.IntegrityError
        store.record_dispatch("run-1", "u0", "worker-2", at=1001.0)


def test_settle_dispatch_closes_it_and_frees_the_unit_for_another_dispatch(tmp_path):
    store = _seeded_store(tmp_path)
    store.record_dispatch("run-1", "u0", "worker-1", at=1000.0)
    store.settle_dispatch("run-1", "u0", outcome="accepted", at=1010.0)
    assert store.open_dispatches("run-1") == []

    # freed: a new dispatch for the same unit is now allowed
    store.record_dispatch("run-1", "u0", "worker-2", at=1020.0)
    open_ = store.open_dispatches("run-1")
    assert len(open_) == 1
    assert open_[0].worker_id == "worker-2"


def test_settle_dispatch_attaches_session_id_once_known(tmp_path):
    """Author ruling: worker_id is minted before launch; the session id is
    recorded ALONGSIDE it once known."""
    store = _seeded_store(tmp_path)
    store.record_dispatch("run-1", "u0", "worker-1", at=1000.0)
    store.settle_dispatch("run-1", "u0", outcome="accepted", session_id="sess-abc", at=1010.0)
    # settled, so not in open_dispatches -- reopen to check the row directly
    with store._connect() as conn:  # test-only introspection
        row = conn.execute(
            "SELECT session_id, outcome FROM dispatches WHERE run_id = ? AND unit_id = ?",
            ("run-1", "u0"),
        ).fetchone()
    assert row["session_id"] == "sess-abc"
    assert row["outcome"] == "accepted"


def test_settle_dispatch_with_no_open_dispatch_raises(tmp_path):
    store = _seeded_store(tmp_path)
    with pytest.raises(NoOpenDispatchError):
        store.settle_dispatch("run-1", "u0", outcome="accepted")


def test_reopen_preserves_dispatcher_lease_and_dispatch_rows(tmp_path):
    db_path = tmp_path / "run.db"
    store = ExecutionStore(db_path)
    store.create_run("run-1", driver="claude_bg", backend="claude-bg", model="m", adapter_version="")
    store.register_units("run-1", ["u0"])
    store.acquire_dispatcher_lease("run-1", "dispatcher-a", lease_seconds=60, at=1000.0)
    store.record_dispatch("run-1", "u0", "worker-1", session_id="sess-1", at=1000.0)

    reopened = ExecutionStore(db_path)
    run = reopened.get_run("run-1")
    assert run.dispatcher_id == "dispatcher-a"
    open_ = reopened.open_dispatches("run-1")
    assert len(open_) == 1
    assert open_[0].session_id == "sess-1"


# ===========================================================================
# Step 4 -- worker environment composition
# ===========================================================================


def _run_with_environment(environment):
    return RunRecord(
        id="run-1",
        driver="claude_bg",
        backend="claude-bg",
        model="m",
        adapter_version="",
        created_at=0.0,
        environment=environment,
    )


def test_default_adapter_declares_nothing_child_env_unchanged_minus_billing(monkeypatch):
    """A-min contract: declaring nothing behaves exactly as before the
    feature existed -- unchanged child env minus only BILLING_DIVERTING_VARS."""
    base = {"PATH": "/usr/bin", "OTHER_VAR": "x", "ANTHROPIC_API_KEY": "secret"}
    run = _run_with_environment(None)
    adapter = RunAdapter()  # declares nothing
    child, cwd = compose_worker_environment(run, adapter, base=base)
    assert child == {"PATH": "/usr/bin", "OTHER_VAR": "x"}
    assert cwd is None


def test_required_vars_overlay_wins_over_dispatcher_base(monkeypatch):
    base = {"CONTENT_ROOT": "D:\\dispatcher\\wrong"}
    run = _run_with_environment({"CONTENT_ROOT": "D:\\correct\\root"})
    adapter = RunAdapter(environment=WorkerEnvironment(required_vars=("CONTENT_ROOT",)))
    child, cwd = compose_worker_environment(run, adapter, base=base)
    assert child["CONTENT_ROOT"] == "D:\\correct\\root"
    assert cwd is None


def test_forbidden_vars_are_subtracted_from_child(monkeypatch):
    base = {"SECRET_TOKEN": "s3cr3t", "OK_VAR": "1"}
    run = _run_with_environment({})
    adapter = RunAdapter(environment=WorkerEnvironment(forbidden_vars=("SECRET_TOKEN",)))
    child, _cwd = compose_worker_environment(run, adapter, base=base)
    assert "SECRET_TOKEN" not in child
    assert child["OK_VAR"] == "1"


@pytest.mark.parametrize("var_name", BILLING_DIVERTING_VARS)
def test_all_billing_diverting_vars_are_subtracted(var_name):
    base = {var_name: "x", "KEEP": "1"}
    run = _run_with_environment({})
    adapter = RunAdapter()
    child, _cwd = compose_worker_environment(run, adapter, base=base)
    assert var_name not in child
    assert child["KEEP"] == "1"


def test_cwd_completion_uses_recorded_cwd_when_require_cwd(tmp_path):
    base = {}
    run = _run_with_environment({"__cwd__": str(tmp_path)})
    adapter = RunAdapter(
        environment=WorkerEnvironment(cwd_vars=("PWD",), require_cwd=True)
    )
    child, cwd = compose_worker_environment(run, adapter, base=base)
    assert cwd == str(tmp_path)
    assert child["PWD"] == str(tmp_path)


def test_cwd_completion_uses_dispatchers_own_cwd_when_require_cwd_is_false(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    base = {}
    run = _run_with_environment({})
    adapter = RunAdapter(environment=WorkerEnvironment(cwd_vars=("PWD",)))
    child, cwd = compose_worker_environment(run, adapter, base=base)
    assert cwd == str(tmp_path)
    assert child["PWD"] == str(tmp_path)


def test_cwd_vars_empty_leaves_cwd_none(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    base = {}
    run = _run_with_environment({})
    adapter = RunAdapter()
    child, cwd = compose_worker_environment(run, adapter, base=base)
    assert cwd is None
    assert "PWD" not in child


def test_cwd_vars_reintroducing_a_billing_name_is_refused(tmp_path):
    """The purpose of re-running check 3 after step 4: a declared cwd_var
    whose NAME collides with a billing-diverting name reintroduces it, and
    that must be caught rather than silently shipped to the child."""
    base = {}
    run = _run_with_environment({"__cwd__": str(tmp_path)})
    adapter = RunAdapter(
        environment=WorkerEnvironment(cwd_vars=("ANTHROPIC_BASE_URL",), require_cwd=True)
    )
    with pytest.raises(WorkerEnvironmentBillingLeakError):
        compose_worker_environment(run, adapter, base=base)


def test_recheck_after_cwd_completion_is_a_check_not_a_second_subtraction(tmp_path):
    """MUTATION CHECK companion: proves step 5 actually runs (not merely
    step 3's subtraction re-run with nothing left to catch) -- the leak
    is only visible AFTER step 4 adds it."""
    # base has no billing vars, so step 3 subtracts nothing -- step 4's
    # cwd_vars completion is what introduces the colliding name.
    base = {}
    run = _run_with_environment({"__cwd__": str(tmp_path)})
    adapter = RunAdapter(
        environment=WorkerEnvironment(cwd_vars=("CLAUDE_CODE_USE_BEDROCK",), require_cwd=True)
    )
    with pytest.raises(WorkerEnvironmentBillingLeakError) as excinfo:
        compose_worker_environment(run, adapter, base=base)
    assert "CLAUDE_CODE_USE_BEDROCK" in excinfo.value.names


# -- the real subprocess protocol-mount verification (spec's sharpest test) --
#
# Invariant 8 coverage, RETARGETED. This used to drive the `claim` verb,
# because `claim` was the worker's first invocation and therefore the first
# thing `_require_compatible_run` refused a mismatched worker on. The
# DISPATCHER claims now (see `dispatch_unit`), so `claim` is no longer a
# worker verb at all and the environment refusal has to be demonstrated on
# `read` -- the worker's actual first invocation -- with `submit` alongside
# it, since the load-bearing property is that a mismatched worker can never
# get OUTPUT ACCEPTED.
#
# What genuinely narrowed, and is asserted rather than assumed: a mismatched
# worker now CONSUMES A SESSION and holds the dispatcher's lease until its
# `read` is refused, where previously its `claim` was refused and the unit
# stayed pending. No wrong-root output can be accepted either way.

_SUBPROCESS_VERB_SCRIPT = r"""
import json
import sys

sys.path.insert(0, sys.argv[1])

from content_pipeline.execution.adapter import RunAdapter, WorkerEnvironment
from content_pipeline.execution.protocol import PROTOCOL_VERSION, build_handlers, dispatch
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.pipeline.workunit import FlatChunkStrategy

db_path, run_id, unit_id, worker_id, verb = sys.argv[2:7]
extra = json.loads(sys.argv[7]) if len(sys.argv) > 7 else {}

store = ExecutionStore(db_path)
adapter = RunAdapter(
    user_for=lambda u: "user:" + u.id,
    parse_fn=lambda t: t,
    apply=lambda uid, payload: None,
    environment=WorkerEnvironment(required_vars=("CONTENT_ROOT",), require_cwd=True),
)
handlers = build_handlers(store, adapter, strategy=FlatChunkStrategy(select=lambda s: []))
payload = {"run_id": run_id, "unit_id": unit_id, "worker_id": worker_id}
payload.update(extra)
envelope = {"protocol_version": PROTOCOL_VERSION, "verb": verb, "payload": payload}
result = dispatch(envelope, handlers)
print(json.dumps(result))
"""


def _run_verb_subprocess(env, cwd, db_path, run_id, unit_id, worker_id, verb, **extra):
    argv = [
        sys.executable, "-c", _SUBPROCESS_VERB_SCRIPT, LIB_ROOT, str(db_path),
        run_id, unit_id, worker_id, verb,
    ]
    if extra:
        argv.append(json.dumps(extra))
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _seed_environment_run(tmp_path):
    """A run whose store-recorded snapshot carries CONTENT_ROOT and a cwd,
    for the two real-subprocess tests below. Returns
    ``(store, db_path, worker_cwd, recorded_content_root)``."""
    db_path = tmp_path / "run.db"
    worker_cwd = tmp_path / "workdir"
    worker_cwd.mkdir()
    recorded_content_root = str(tmp_path / "content_root")

    store = ExecutionStore(db_path)
    store.create_run(
        "run-1",
        driver="claude_bg",
        backend="claude-bg",
        model="m",
        adapter_version="",
        environment={"CONTENT_ROOT": recorded_content_root, "__cwd__": str(worker_cwd)},
    )
    store.register_units("run-1", ["u0"])
    return store, db_path, worker_cwd, recorded_content_root


def test_composed_environment_makes_a_real_worker_read_succeed(tmp_path):
    """The environment verification that matters: dispatch a run whose
    WorkerEnvironment declares required_vars=("CONTENT_ROOT",),
    require_cwd=True from a process whose CONTENT_ROOT differs from the
    run's snapshot, and observe -- through a REAL protocol.build_handlers
    mount in a subprocess with the COMPOSED environment -- that the worker's
    FIRST verb, `read`, SUCCEEDS and returns real prepared content."""
    store, db_path, worker_cwd, recorded_content_root = _seed_environment_run(tmp_path)
    run = store.get_run("run-1")
    adapter = RunAdapter(environment=WorkerEnvironment(required_vars=("CONTENT_ROOT",), require_cwd=True))

    # This DISPATCHING process's own CONTENT_ROOT differs from the run's snapshot.
    dispatcher_base = dict(os.environ)
    dispatcher_base["CONTENT_ROOT"] = str(tmp_path / "wrong_root")

    child_env, cwd = compose_worker_environment(run, adapter, base=dispatcher_base)
    assert cwd == str(worker_cwd)
    assert child_env["CONTENT_ROOT"] == recorded_content_root

    result = _run_verb_subprocess(child_env, cwd, db_path, "run-1", "u0", "worker-1", "read")
    assert result["ok"] is True, result
    assert result["result"]["user"] == "user:u0"

    # ACCEPT DIRECTION, the whole point: a matching worker CAN get output
    # accepted. The dispatcher claims first, as it does in production.
    claim = store.claim_unit("run-1", "u0", "worker-1")
    result = _run_verb_subprocess(
        child_env, cwd, db_path, "run-1", "u0", "worker-1", "submit",
        fencing_token=claim.fencing_token, text="the answer",
    )
    assert result["ok"] is True, result
    assert result["result"]["accepted"] is True
    assert store.get_unit("run-1", "u0").accepted_text == "the answer"


def test_without_the_overlay_a_real_worker_is_refused_and_can_never_be_accepted(tmp_path):
    """The mirror, and the property that actually matters after the
    dispatcher took over claiming: without the overlay (the raw dispatcher
    environment, mismatched CONTENT_ROOT and cwd), `read` is refused with
    WorkerEnvironmentMismatchError -- and so is `submit`, even holding a
    perfectly VALID fencing token. A mismatched worker can consume a session
    now (it is launched against an already-claimed unit), but it can never
    get output ACCEPTED, which is what invariant 8 is for."""
    store, db_path, _worker_cwd, _recorded_content_root = _seed_environment_run(tmp_path)

    mismatched_env = dict(os.environ)
    mismatched_env["CONTENT_ROOT"] = str(tmp_path / "wrong_root")
    mismatched_cwd = str(tmp_path)  # not worker_cwd

    result = _run_verb_subprocess(
        mismatched_env, mismatched_cwd, db_path, "run-1", "u0", "worker-1", "read"
    )
    assert result["ok"] is False
    assert result["error"]["type"] == "WorkerEnvironmentMismatchError"

    # The dispatcher's claim is real and its token is current -- the ONLY
    # thing wrong is the worker's environment.
    claim = store.claim_unit("run-1", "u0", "worker-1")
    result = _run_verb_subprocess(
        mismatched_env, mismatched_cwd, db_path, "run-1", "u0", "worker-1", "submit",
        fencing_token=claim.fencing_token, text="wrong-root output",
    )
    assert result["ok"] is False
    assert result["error"]["type"] == "WorkerEnvironmentMismatchError"

    unit = store.get_unit("run-1", "u0")
    assert unit.state is not UnitState.ACCEPTED
    assert unit.accepted_text is None


# ===========================================================================
# Shared fixtures for steps 5-11
# ===========================================================================


def _seeded_dispatch_store(tmp_path, *, unit_ids=("u0",)) -> ExecutionStore:
    store = ExecutionStore(tmp_path / "run.db")
    store.create_run("run-1", driver="claude_bg", backend="claude-bg", model="m", adapter_version="")
    store.register_units("run-1", list(unit_ids))
    return store


def _worker_command(tmp_path) -> WorkerCommand:
    answer_dir = tmp_path / "answers"
    answer_dir.mkdir(exist_ok=True)
    envelope_dir = tmp_path / "envelopes"
    envelope_dir.mkdir(exist_ok=True)
    return WorkerCommand(
        argv=("python", "mytool.py", "run"),
        answer_dir=str(answer_dir),
        envelope_dir=str(envelope_dir),
    )


def _bg_record(*, id="a1b2c3d4", session_id="sess-1", state="working", **extra):
    rec = {"kind": "background", "id": id, "sessionId": session_id, "state": state}
    rec.update(extra)
    return rec


def _pending_unit(store, run_id, unit_id):
    return next(u for u in store.list_units(run_id) if u.unit_id == unit_id)


def _claim_and_open(store, run_id, unit_id, worker_id, session_id, short_id, *, at=1000.0, lease_seconds=100.0):
    claim = store.claim_unit(run_id, unit_id, worker_id, lease_seconds=lease_seconds, at=at)
    store.record_dispatch(run_id, unit_id, worker_id, session_id=session_id, at=at)
    return OpenDispatch(
        unit_id=unit_id,
        worker_id=worker_id,
        session_id=session_id,
        id=short_id,
        fencing_token=claim.fencing_token,
        claimed_by=worker_id,
    )


# ===========================================================================
# Step 5 -- launch prompt and the enumerated invocation set (P5)
# ===========================================================================


def test_enumerate_worker_invocations_are_exact_and_deterministic(tmp_path):
    wc = _worker_command(tmp_path)
    first = enumerate_worker_invocations(wc, "run-1", "u0", "worker-a")
    second = enumerate_worker_invocations(wc, "run-1", "u0", "worker-a")
    assert first == second

    assert len(first) == 6
    (
        read_cmd,
        submit_cmd,
        fail_cmd,
        write_answer_cmd,
        write_submit_cmd,
        write_fail_cmd,
    ) = first
    for cmd in (read_cmd, submit_cmd, fail_cmd):
        assert "protocol" in cmd and "@" in cmd
    answer_path = answer_path_for(wc, "run-1", "u0")
    assert "submit" not in read_cmd  # no verb leaks across a different verb's path
    assert "--text-file=" in submit_cmd
    assert answer_path in submit_cmd
    assert answer_path in write_answer_cmd
    # The dispatcher claims (see dispatch_unit), so no worker invocation is
    # a claim and no claim envelope is ever named.
    for cmd in first:
        assert "claim" not in cmd.lower()
    assert envelope_path_for(wc, "run-1", "u0", "read") in read_cmd
    assert envelope_path_for(wc, "run-1", "u0", "submit") in submit_cmd
    assert envelope_path_for(wc, "run-1", "u0", "submit") in write_submit_cmd
    assert envelope_path_for(wc, "run-1", "u0", "fail") in fail_cmd
    assert envelope_path_for(wc, "run-1", "u0", "fail") in write_fail_cmd
    # No invocation ever carries a fencing token -- it is not known until
    # AFTER `claim` runs (P5's determinism constraint).
    for cmd in first:
        assert "fencing" not in cmd.lower()


def test_enumerate_worker_invocations_differ_per_unit(tmp_path):
    wc = _worker_command(tmp_path)
    a = enumerate_worker_invocations(wc, "run-1", "u0", "worker-a")
    b = enumerate_worker_invocations(wc, "run-1", "u1", "worker-a")
    assert a != b


def test_answer_path_for_is_deterministic_and_unit_specific(tmp_path):
    wc = _worker_command(tmp_path)
    p1 = answer_path_for(wc, "run-1", "u0")
    p2 = answer_path_for(wc, "run-1", "u0")
    p3 = answer_path_for(wc, "run-1", "u1")
    assert p1 == p2
    assert p1 != p3
    assert p1.startswith(wc.answer_dir)


def test_build_launch_prompt_names_ids_and_carries_invocations_verbatim(tmp_path):
    wc = _worker_command(tmp_path)
    prompt = build_launch_prompt(wc, "run-1", "u0", "worker-a", 42)
    invocations = enumerate_worker_invocations(wc, "run-1", "u0", "worker-a")

    assert "run-1" in prompt
    assert "u0" in prompt
    assert "worker-a" in prompt
    for inv in invocations:
        assert inv in prompt, f"invocation not carried verbatim: {inv!r}"
    assert answer_path_for(wc, "run-1", "u0") in prompt


def test_launch_prompt_carries_the_real_fencing_token_and_no_claim_step(tmp_path):
    """The token the DISPATCHER's claim returned reaches the worker here, in
    the prompt -- and nowhere else. The prompt names it literally, offers no
    claim step to obtain one, and still hands the worker the submit/fail
    TEMPLATES with ``<FENCING_TOKEN>`` unsubstituted (the worker substitutes;
    only the SOURCE of the value changed).

    MUTATION: interpolate the token into the templates directly (in
    ``_envelope_payload_text``) -- the template assertions here go red, and
    so does ``test_no_enumerated_invocation_carries_a_fencing_token``, the
    P5 anchor, since the templates feed the enumerated Write-tool targets'
    own envelopes."""
    wc = _worker_command(tmp_path)
    token = 987654
    prompt = build_launch_prompt(wc, "run-1", "u0", "worker-a", token)

    assert str(token) in prompt
    # No claim invocation, no claim envelope, and no instruction to claim.
    assert "claim" not in prompt.lower()
    assert not os.path.exists(envelope_path_for(wc, "run-1", "u0", "claim"))
    # The read envelope IS pre-written; submit/fail are the worker's job.
    assert os.path.exists(envelope_path_for(wc, "run-1", "u0", "read"))
    assert not os.path.exists(envelope_path_for(wc, "run-1", "u0", "submit"))
    assert not os.path.exists(envelope_path_for(wc, "run-1", "u0", "fail"))

    # The templates still carry the placeholder verbatim.
    envelopes = worker_envelopes_for(wc, "run-1", "u0", "worker-a")
    for verb in ("submit", "fail"):
        template = envelopes[verb][1]
        assert "<FENCING_TOKEN>" in template
        assert str(token) not in template
        assert template in prompt or textwrap.indent(template, "     ") in prompt

    # The fence line the worker must put on its answer artifact.
    assert f"{ANSWER_FENCE_PREFIX} {token}" in prompt


# ===========================================================================
# Step 7 -- the reconciler (tested ahead of step 6, which consumes it)
# ===========================================================================


def test_parse_agents_json_accepts_exactly_four_required_fields():
    """ACCEPT case."""
    body = json.dumps([_bg_record(id="i1", session_id="s1", state="working")])
    result = parse_agents_json(body)
    assert len(result.sessions) == 1
    session = result.sessions[0]
    assert session.id == "i1"
    assert session.session_id == "s1"
    assert session.state == "working"
    assert result.ignored == 0


def test_parse_agents_json_accepts_verbatim_2026_08_17_record():
    """ACCEPT case: the verbatim P4 record carrying pid/status/waitingFor
    alongside id/state."""
    record = _bg_record(
        id="p1", session_id="s1", state="blocked",
        pid=12345, status="waiting", waitingFor="permission prompt",
    )
    result = parse_agents_json(json.dumps([record]))
    session = result.sessions[0]
    assert session.pid == 12345
    assert session.status == "waiting"
    assert session.waiting_for == "permission prompt"


def test_parse_agents_json_accepts_unknown_future_fields():
    """ACCEPT case: three unknown future fields."""
    record = _bg_record(id="i1", session_id="s1", state="working")
    record["totallyNewField"] = "x"
    record["anotherOne"] = 42
    record["thirdOne"] = None
    result = parse_agents_json(json.dumps([record]))
    assert len(result.sessions) == 1


def test_parse_agents_json_epoch_ms_started_at_is_a_distinct_seconds_attribute():
    """ACCEPT case: startedAt is epoch milliseconds."""
    record = _bg_record(id="i1", session_id="s1", state="working", startedAt=1734000000000)
    result = parse_agents_json(json.dumps([record]))
    session = result.sessions[0]
    assert session.started_at_ms == 1734000000000
    assert session.started_at_seconds == 1734000000.0


def test_parse_agents_json_accepts_settled_only_listing():
    """ACCEPT case: a listing whose only background record is settled
    (visible only under --all)."""
    record = _bg_record(id="i1", session_id="s1", state="stopped")
    result = parse_agents_json(json.dumps([record]))
    assert result.sessions[0].state == "stopped"


def test_parse_agents_json_accepts_empty_list():
    """ACCEPT case: an idle machine (zero background records) is normal,
    not an error."""
    result = parse_agents_json("[]")
    assert result.sessions == ()
    assert result.ignored == 0


def test_parse_agents_json_filters_interactive_record_with_no_id_first():
    """MUTATION CHECK: swapping the filter/validate order would raise on
    this fixture (the orchestrator's own interactive record, no id) instead
    of silently ignoring it."""
    interactive = {
        "kind": "interactive", "pid": 111, "cwd": "/x", "startedAt": 1,
        "sessionId": "sess-orch", "name": "n", "status": "running",
    }
    result = parse_agents_json(json.dumps([interactive]))
    assert result.sessions == ()
    assert result.ignored == 1


def test_parse_agents_json_filters_record_with_no_kind_at_all():
    no_kind = {"id": "x", "sessionId": "s", "state": "working"}
    result = parse_agents_json(json.dumps([no_kind]))
    assert result.sessions == ()
    assert result.ignored == 1


def test_parse_agents_json_raises_on_missing_required_field_for_background_record():
    record = {"kind": "background", "id": "i1", "state": "working"}  # missing sessionId
    with pytest.raises(AgentsJsonParseError):
        parse_agents_json(json.dumps([record]))


def test_parse_agents_json_raises_on_non_list():
    with pytest.raises(AgentsJsonParseError):
        parse_agents_json(json.dumps({"not": "a list"}))


def test_parse_agents_json_raises_on_non_object_element():
    with pytest.raises(AgentsJsonParseError):
        parse_agents_json(json.dumps(["not-an-object"]))


def test_parse_agents_json_raises_on_malformed_json():
    with pytest.raises(AgentsJsonParseError):
        parse_agents_json("not json at all")


# ===========================================================================
# Step 6 -- dispatch one unit, confirmed by an observed transition (P11)
# ===========================================================================


def test_dispatch_unit_confirms_via_observed_transition_not_via_banner(tmp_path):
    store = _seeded_dispatch_store(tmp_path)
    wc = _worker_command(tmp_path)
    runner = FakeRunner()
    runner.script(("claude", "--bg"), ("backgrounded * a1b2c3d4", "", 0))
    runner.script(
        ("claude", "agents", "--json"),
        (json.dumps([_bg_record(id="a1b2c3d4", session_id="sess-xyz", state="working")]), "", 0),
    )
    cli = _cli(runner)

    unit = _pending_unit(store, "run-1", "u0")

    opened = dispatch_unit(
        store, "run-1", unit, cli, wc,
        worker_id="worker-fixed", sleep_fn=lambda s: None, clock_fn=lambda: 1000.0,
    )
    assert opened.session_id == "sess-xyz"
    assert opened.id == "a1b2c3d4"
    assert opened.worker_id == "worker-fixed"
    assert opened.claimed_by == "worker-fixed"


def test_dispatch_unit_holds_the_claim_before_the_launch(tmp_path):
    """Part D: the DISPATCHER claims, before ``cli.launch_bg`` -- observed
    from INSIDE the fake launcher, which is the only place that ordering is
    visible. The returned OpenDispatch carries that claim's OWN values, not
    a post-confirmation read of the store.

    MUTATION A: move ``store.claim_unit`` after ``cli.launch_bg`` -- the
    in-launcher assertions go red (the unit is still PENDING at launch, and
    the prompt has no real token to name).
    MUTATION B: revert to capturing the fence from a post-confirm
    ``store.get_unit`` -- ``test_..._capture_is_exact_when_a_worker_claims_
    after_confirmation`` below goes red."""
    store = _seeded_dispatch_store(tmp_path)
    wc = _worker_command(tmp_path)
    runner = FakeRunner()
    runner.script(("claude", "--bg"), ("backgrounded * a1b2c3d4", "", 0))
    runner.script(
        ("claude", "agents", "--json"),
        (json.dumps([_bg_record(id="a1b2c3d4", session_id="sess-xyz", state="working")]), "", 0),
    )
    cli = _cli(runner)
    unit = _pending_unit(store, "run-1", "u0")

    observed = {}
    original_launch = cli.launch_bg

    def fake_launch(prompt, **kwargs):
        row = store.get_unit("run-1", "u0")
        observed["state"] = row.state
        observed["claimed_by"] = row.claimed_by
        observed["fencing_token"] = row.fencing_token
        observed["prompt"] = prompt
        return original_launch(prompt, **kwargs)

    cli.launch_bg = fake_launch  # type: ignore[assignment]

    opened = dispatch_unit(
        store, "run-1", unit, cli, wc,
        worker_id="worker-fixed", sleep_fn=lambda s: None, clock_fn=lambda: 1000.0, at=1000.0,
    )

    # Already CLAIMED, by the minted worker_id, at the moment of launch.
    assert observed["state"] is UnitState.CLAIMED
    assert observed["claimed_by"] == "worker-fixed"
    # ... and the launch prompt names that claim's own token.
    assert f"Fencing token: {observed['fencing_token']}" in observed["prompt"]

    # The OpenDispatch carries the claim's own values.
    assert opened.fencing_token == observed["fencing_token"]
    assert opened.claimed_by == "worker-fixed"


def test_dispatch_unit_captures_the_fence_by_construction_never_by_re_reading(tmp_path):
    """MUTATION B's target, stated as the property rather than the race.

    The capture used to be a ``store.get_unit`` read taken AFTER the
    launch-confirmation poll, which raced the worker's own claim: a worker
    that claimed after confirmation left the dispatcher holding a PRE-claim
    fence, ``supervise_tick``'s drift guard dropped the slot, ``dropped``
    does no store write, so the dispatch row was never settled and the unit
    became permanently unreclaimable.

    That race is closed by construction: the dispatcher holds the claim, so
    the fence is the value it was handed, and nothing after the launch may
    re-derive it. Asserted directly -- zero ``get_unit`` reads once the
    launch has begun.

    MUTATION B: reinstate the post-confirm ``unit_row = store.get_unit(...)``
    capture -- the read count below goes to 1 -> red."""
    store = _seeded_dispatch_store(tmp_path)
    wc = _worker_command(tmp_path)
    runner = FakeRunner()
    runner.script(("claude", "--bg"), ("backgrounded * a1b2c3d4", "", 0))
    runner.script(
        ("claude", "agents", "--json"),
        (json.dumps([_bg_record(id="a1b2c3d4", session_id="sess-xyz", state="working")]), "", 0),
    )
    cli = _cli(runner)
    unit = _pending_unit(store, "run-1", "u0")

    launched = {"yet": False}
    reads_after_launch = []
    original_get_unit = store.get_unit

    def _spy_get_unit(*args, **kwargs):
        if launched["yet"]:
            reads_after_launch.append(args)
        return original_get_unit(*args, **kwargs)

    store.get_unit = _spy_get_unit  # type: ignore[assignment]

    original_launch = cli.launch_bg

    def fake_launch(prompt, **kwargs):
        launched["yet"] = True
        return original_launch(prompt, **kwargs)

    cli.launch_bg = fake_launch  # type: ignore[assignment]

    opened = dispatch_unit(
        store, "run-1", unit, cli, wc,
        worker_id="worker-fixed", sleep_fn=lambda s: None, clock_fn=lambda: 1000.0, at=1000.0,
    )

    assert reads_after_launch == [], (
        "dispatch_unit re-read the unit after launching; the fence/claimant "
        "capture must be the dispatcher's own claim values, not a read that "
        "races the worker"
    )
    row = original_get_unit("run-1", "u0")
    assert opened.fencing_token == row.fencing_token
    assert opened.claimed_by == row.claimed_by == "worker-fixed"


def test_dispatch_unit_duplicate_suppression_bites_before_any_launch_spend(tmp_path):
    store = _seeded_dispatch_store(tmp_path)
    wc = _worker_command(tmp_path)
    runner = FakeRunner(default=("backgrounded * aaaaaaaa", "", 0))
    cli = _cli(runner)
    unit = _pending_unit(store, "run-1", "u0")

    store.record_dispatch("run-1", "u0", "worker-existing", at=1000.0)  # already an OPEN dispatch

    with pytest.raises(Exception):  # sqlite3.IntegrityError, surfaced by record_dispatch
        dispatch_unit(store, "run-1", unit, cli, wc, worker_id="worker-new", clock_fn=lambda: 1000.0)

    launch_calls = [argv for argv, _kwargs in runner.calls if len(argv) >= 2 and argv[1] == "--bg" and argv[2:3] != ["-p"]]
    assert launch_calls == [], "launch_bg must never be reached once record_dispatch raises"


def test_dispatch_unit_state_failed_within_window_is_launch_misconfiguration(tmp_path):
    """MUTATION CHECK anchor: if dispatch_unit returned on the launch
    banner/exit-code instead of the observed agents_json transition, this
    exit-0-then-failed fake would be treated as a confirmed dispatch instead
    of raising."""
    store = _seeded_dispatch_store(tmp_path)
    wc = _worker_command(tmp_path)
    runner = FakeRunner()
    runner.script(("claude", "--bg"), ("backgrounded * badbad01", "", 0))
    runner.script(
        ("claude", "agents", "--json"),
        (json.dumps([_bg_record(id="badbad01", session_id="sess-bad", state="failed")]), "", 0),
    )
    runner.script(("claude", "rm"), ("removed", "", 0))
    cli = _cli(runner)
    unit = _pending_unit(store, "run-1", "u0")

    with pytest.raises(LaunchMisconfigurationError):
        dispatch_unit(store, "run-1", unit, cli, wc, worker_id="worker-a", clock_fn=lambda: 1000.0)

    assert store.open_dispatches("run-1") == []  # settled, not left open
    rm_calls = [argv for argv, _kwargs in runner.calls if len(argv) >= 2 and argv[1] == "rm"]
    assert rm_calls and rm_calls[0][2] == "badbad01"


def test_launch_misconfiguration_releases_the_dispatcher_held_claim(tmp_path):
    """The dispatcher claims before launching, so a launch that never
    reaches a confirmed state must give the claim back: no worker ever
    started, and a unit left CLAIMED with a live lease on behalf of a
    session that does not exist is unclaimable for the whole lease.

    MUTATION: delete the ``store.fail_unit`` release in ``dispatch_unit``'s
    misconfiguration branch -- the unit stays CLAIMED here -> red."""
    store = _seeded_dispatch_store(tmp_path)
    wc = _worker_command(tmp_path)
    runner = FakeRunner()
    runner.script(("claude", "--bg"), ("backgrounded * badbad01", "", 0))
    runner.script(
        ("claude", "agents", "--json"),
        (json.dumps([_bg_record(id="badbad01", session_id="sess-bad", state="failed")]), "", 0),
    )
    runner.script(("claude", "rm"), ("removed", "", 0))
    cli = _cli(runner)
    unit = _pending_unit(store, "run-1", "u0")

    with pytest.raises(LaunchMisconfigurationError):
        dispatch_unit(
            store, "run-1", unit, cli, wc, worker_id="worker-a",
            clock_fn=lambda: 1000.0, at=1000.0,
        )

    row = store.get_unit("run-1", "u0")
    assert row.state is UnitState.PENDING, (
        f"unit left {row.state!r} after a launch that never started a worker"
    )
    assert row.claimed_by is None
    assert row.lease_expires_at is None
    # ACCEPT DIRECTION: the release is a RETRY, never a terminal failure --
    # the unit must be immediately dispatchable again.
    assert store.open_dispatches("run-1") == []
    assert [u.unit_id for u in store.list_units("run-1") if u.state is UnitState.PENDING] == ["u0"]


def test_dispatch_unit_never_appearing_is_launch_misconfiguration(tmp_path):
    store = _seeded_dispatch_store(tmp_path)
    wc = _worker_command(tmp_path)
    runner = FakeRunner()
    runner.script(("claude", "--bg"), ("backgrounded * ffffffff", "", 0))
    runner.script(("claude", "agents", "--json"), ("[]", "", 0))  # never shows up
    cli = _cli(runner)
    unit = _pending_unit(store, "run-1", "u0")

    clock = {"t": 1000.0}
    with pytest.raises(LaunchMisconfigurationError):
        dispatch_unit(
            store, "run-1", unit, cli, wc, worker_id="worker-a",
            launch_confirm_seconds=5.0, poll_interval_s=1.0,
            clock_fn=lambda: clock["t"],
            sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s),
        )
    assert store.open_dispatches("run-1") == []


# ===========================================================================
# Step 8 -- status classification, renewal, stall detection (D5, P12, P13)
# ===========================================================================


def test_supervise_tick_renews_working_with_lease_for_formula(tmp_path):
    store = _seeded_dispatch_store(tmp_path)
    od = _claim_and_open(store, "run-1", "u0", "worker-a", "sess-1", "short1", at=1000.0, lease_seconds=50.0)
    runner = FakeRunner()
    runner.script(
        ("claude", "agents", "--json"),
        (json.dumps([_bg_record(id="short1", session_id="sess-1", state="working")]), "", 0),
    )
    cli = _cli(runner)
    adapter = RunAdapter(expected_unit_seconds=100.0)

    result = supervise_tick(store, "run-1", cli, adapter, {"u0": od}, at=1010.0)
    assert result.renewed == ("u0",)
    unit = store.get_unit("run-1", "u0")
    expected = 1010.0 + claude_bg.lease_for(100.0)
    assert unit.lease_expires_at == expected


def test_supervise_tick_renews_running_too_when_still_claimed(tmp_path):
    """ACCEPT-DIRECTION check for the not-CLAIMED guard: the guard must not
    refuse a genuinely working unit under EITHER live session state."""
    store = _seeded_dispatch_store(tmp_path)
    od = _claim_and_open(store, "run-1", "u0", "worker-a", "sess-1", "short1", at=1000.0, lease_seconds=50.0)
    runner = FakeRunner()
    runner.script(
        ("claude", "agents", "--json"),
        (json.dumps([_bg_record(id="short1", session_id="sess-1", state="running")]), "", 0),
    )
    cli = _cli(runner)

    result = supervise_tick(store, "run-1", cli, RunAdapter(expected_unit_seconds=100.0), {"u0": od}, at=1010.0)
    assert result.renewed == ("u0",)
    assert store.get_unit("run-1", "u0").lease_expires_at == 1010.0 + claude_bg.lease_for(100.0)


def test_supervise_tick_working_but_already_accepted_skips_renewal_only(tmp_path):
    """The submit-then-exit window: the worker submitted through the protocol
    and its session has not exited yet, so ``agents --json`` still reports
    ``working`` while the unit is ACCEPTED.

    MUTATION: drop the ``current_unit.state is not UnitState.CLAIMED`` guard
    -- ``store.renew_lease`` raises ``NotClaimedError`` straight out of
    ``supervise_tick`` -> red."""
    store = _seeded_dispatch_store(tmp_path)
    od = _claim_and_open(store, "run-1", "u0", "worker-a", "sess-1", "short1", at=1000.0, lease_seconds=50.0)
    original_expiry = store.get_unit("run-1", "u0").lease_expires_at
    store.accept_unit("run-1", "u0", od.fencing_token, text="answer", at=1001.0)

    runner = FakeRunner()
    runner.script(
        ("claude", "agents", "--json"),
        (json.dumps([_bg_record(id="short1", session_id="sess-1", state="working")]), "", 0),
    )
    cli = _cli(runner)

    result = supervise_tick(store, "run-1", cli, RunAdapter(), {"u0": od}, at=1010.0)

    # Renewal skipped -- and NOTHING else WITHIN THE GRACE: the session is
    # alive, so it keeps its slot, and the dispatch stays open for the `done`
    # branch to settle. The grace is what bounds that wait; see
    # test_supervise_tick_ends_a_terminal_unit_whose_session_overstays_the_grace.
    assert result.renewed == ()
    assert result.settled == {}
    assert result.dropped == ()
    assert result.halted is None
    assert store.get_unit("run-1", "u0").lease_expires_at == original_expiry
    assert [d.unit_id for d in store.open_dispatches("run-1")] == ["u0"]

    # Nothing is stranded by leaving it open: ACCEPTED is terminal, so the
    # unit is never a reclaim candidate, at any time.
    assert reclaimable_units(store, "run-1", at=original_expiry + 10_000) == []

def test_supervise_tick_ends_a_terminal_unit_whose_session_overstays_the_grace(tmp_path):
    """The bound on the branch above. Nothing else in the system can close
    this dispatch -- the unit is terminal so it is never a candidate again,
    and ``accept_unit`` leaves ``claimed_by``/the fence intact so the drift
    guard never drops it -- so past ``terminal_exit_grace_seconds`` the tick
    stops/rms the session itself and settles.

    MUTATION: make the grace check an unconditional ``continue`` -- the
    dispatch is never settled, the session is never stopped, and
    ``dispatch_wave`` polls forever -> red."""
    store = _seeded_dispatch_store(tmp_path)
    od = _claim_and_open(store, "run-1", "u0", "worker-a", "sess-1", "short1", at=1000.0, lease_seconds=50.0)
    store.accept_unit("run-1", "u0", od.fencing_token, text="answer", at=1001.0)

    runner = FakeRunner()
    runner.script(
        ("claude", "agents", "--json"),
        (json.dumps([_bg_record(id="short1", session_id="sess-1", state="working")]), "", 0),
    )
    runner.script(("claude", "stop"), ("", "", 0))
    runner.script(("claude", "rm"), ("", "", 0))
    cli = _cli(runner)

    # First observation starts the clock; still inside the grace.
    first = supervise_tick(
        store, "run-1", cli, RunAdapter(), {"u0": od},
        terminal_exit_grace_seconds=100.0, at=1010.0,
    )
    assert first.settled == {}
    assert od.terminal_since == 1010.0

    # One tick still inside it -- the grace runs from the FIRST observation,
    # not from each one.
    inside = supervise_tick(
        store, "run-1", cli, RunAdapter(), {"u0": od},
        terminal_exit_grace_seconds=100.0, at=1109.0,
    )
    assert inside.settled == {}
    assert [d.unit_id for d in store.open_dispatches("run-1")] == ["u0"]

    # Past it: the session is ended and the dispatch closed.
    after = supervise_tick(
        store, "run-1", cli, RunAdapter(), {"u0": od},
        terminal_exit_grace_seconds=100.0, at=1110.0,
    )
    assert after.settled == {"u0": "session_lingering"}
    assert after.renewed == ()
    assert after.halted is None
    assert store.open_dispatches("run-1") == []
    argvs = [argv for argv, _kw in runner.calls]
    assert ["claude", "stop", "short1"] in argvs
    assert ["claude", "rm", "short1"] in argvs
    # The unit itself is untouched: its accepted answer stands.
    unit = store.get_unit("run-1", "u0")
    assert unit.state is UnitState.ACCEPTED
    assert unit.accepted_text == "answer"


def test_supervise_tick_settles_a_lingering_session_even_when_stop_fails(tmp_path):
    """``stop``/``rm`` are best-effort; an unreachable daemon must not keep
    the dispatch open (that is the liveness defect all over again).

    MUTATION: drop the try/except around the lifecycle calls -- the raised
    error escapes the tick -> red."""
    store = _seeded_dispatch_store(tmp_path)
    od = _claim_and_open(store, "run-1", "u0", "worker-a", "sess-1", "short1", at=1000.0, lease_seconds=50.0)
    store.accept_unit("run-1", "u0", od.fencing_token, text="answer", at=1001.0)

    def _runner(argv, **kwargs):
        argv = list(argv)
        if argv[1:3] == ["agents", "--json"]:
            return (
                json.dumps([_bg_record(id="short1", session_id="sess-1", state="working")]),
                "",
                0,
            )
        raise OSError("the daemon is gone")

    cli = ClaudeCli(executable="claude", runner=_runner)
    od.terminal_since = 1000.0
    result = supervise_tick(
        store, "run-1", cli, RunAdapter(), {"u0": od},
        terminal_exit_grace_seconds=100.0, at=1200.0,
    )
    assert result.settled == {"u0": "session_lingering"}
    assert store.open_dispatches("run-1") == []

def test_supervise_tick_settles_the_accepted_unit_once_its_session_exits(tmp_path):
    """The other half: the slot IS released -- on the first tick after the
    session exits, by the ``done`` branch, exactly as for a worker that
    submits and exits between two ticks."""
    store = _seeded_dispatch_store(tmp_path)
    od = _claim_and_open(store, "run-1", "u0", "worker-a", "sess-1", "short1", at=1000.0, lease_seconds=50.0)
    store.accept_unit("run-1", "u0", od.fencing_token, text="answer", at=1001.0)

    runner = FakeRunner()
    runner.script(
        ("claude", "agents", "--json"),
        (json.dumps([_bg_record(id="short1", session_id="sess-1", state="working")]), "", 0),
    )
    cli = _cli(runner)
    assert supervise_tick(store, "run-1", cli, RunAdapter(), {"u0": od}, at=1010.0).settled == {}

    runner2 = FakeRunner()
    runner2.script(
        ("claude", "agents", "--json"),
        (json.dumps([_bg_record(id="short1", session_id="sess-1", state="done")]), "", 0),
    )
    result = supervise_tick(store, "run-1", _cli(runner2), RunAdapter(), {"u0": od}, at=1020.0)
    assert result.settled == {"u0": "accepted"}
    assert store.open_dispatches("run-1") == []


def test_supervise_tick_blocked_stops_renewing_with_no_grace(tmp_path):
    """MUTATION CHECK anchor: renewing unconditionally on `blocked` must be
    observed (the lease is still live past its original expiry) -> red."""
    store = _seeded_dispatch_store(tmp_path)
    od = _claim_and_open(store, "run-1", "u0", "worker-a", "sess-1", "short1", at=1000.0, lease_seconds=50.0)
    original_expiry = store.get_unit("run-1", "u0").lease_expires_at
    runner = FakeRunner()
    runner.script(
        ("claude", "agents", "--json"),
        (json.dumps([_bg_record(id="short1", session_id="sess-1", state="blocked")]), "", 0),
    )
    cli = _cli(runner)
    adapter = RunAdapter()

    result = supervise_tick(store, "run-1", cli, adapter, {"u0": od}, at=1010.0)
    assert result.renewed == ()
    assert result.settled == {"u0": "blocked"}
    unit = store.get_unit("run-1", "u0")
    assert unit.lease_expires_at == original_expiry  # untouched -- never renewed

    # Observed still expired past its original expiry -- and now reclaimable,
    # because the dispatch was settled rather than left open forever.
    assert original_expiry <= 1051.0
    reclaimable = reclaimable_units(store, "run-1", at=1051.0)
    assert [u.unit_id for u in reclaimable] == ["u0"]


def test_supervise_tick_blocked_stops_and_rms_the_session(tmp_path):
    """Part C, hygiene: the ``blocked`` branch ends the session before
    settling, mirroring the ``session_lingering`` branch -- settling removes
    the dispatch from ``open_dispatches``, so the wave's exit cleanup will
    no longer stop/rm it and a live session would be leaked.

    This is NOT the fix for the claim collision, and must not be read as
    one: ``stop``/``rm`` return ``(stdout, stderr, rc)`` and this loop
    ignores a nonzero rc as well as an exception, so a session that refuses
    to die is still left running.

    MUTATION 1: remove the two calls -> the argv assertions go red.
    MUTATION 2: move them AFTER an unguarded ``settle_dispatch`` -> the
    ordering assertion goes red."""
    store = _seeded_dispatch_store(tmp_path)
    od = _claim_and_open(store, "run-1", "u0", "worker-a", "sess-1", "short1", at=1000.0, lease_seconds=50.0)

    order = []

    def _runner(argv, **kwargs):
        argv = list(argv)
        if argv[1:3] == ["agents", "--json"]:
            return (
                json.dumps([_bg_record(id="short1", session_id="sess-1", state="blocked")]),
                "",
                0,
            )
        order.append(argv)
        return ("", "", 0)

    cli = ClaudeCli(executable="claude", runner=_runner)

    original_settle = store.settle_dispatch

    def _settle(*args, **kwargs):
        order.append(["settle_dispatch"])
        return original_settle(*args, **kwargs)

    store.settle_dispatch = _settle  # type: ignore[assignment]

    result = supervise_tick(store, "run-1", cli, RunAdapter(), {"u0": od}, at=1010.0)

    assert result.settled == {"u0": "blocked"}
    assert ["claude", "stop", "short1"] in order
    assert ["claude", "rm", "short1"] in order
    # ... both BEFORE the settle, which is what stops the wave's exit
    # cleanup from being the only thing that could have ended the session.
    assert order.index(["claude", "stop", "short1"]) < order.index(["settle_dispatch"])
    assert order.index(["claude", "rm", "short1"]) < order.index(["settle_dispatch"])


def test_supervise_tick_blocked_settles_even_when_stop_raises(tmp_path):
    """The ACCEPT direction of the same change: ``stop``/``rm`` are
    best-effort, so an unreachable daemon must not keep the dispatch open --
    that is the liveness defect all over again. Mirrors
    ``test_supervise_tick_settles_a_lingering_session_even_when_stop_fails``.

    MUTATION: drop the try/except around the two lifecycle calls -- the
    raised error escapes the tick -> red."""
    store = _seeded_dispatch_store(tmp_path)
    od = _claim_and_open(store, "run-1", "u0", "worker-a", "sess-1", "short1", at=1000.0, lease_seconds=50.0)

    def _runner(argv, **kwargs):
        argv = list(argv)
        if argv[1:3] == ["agents", "--json"]:
            return (
                json.dumps([_bg_record(id="short1", session_id="sess-1", state="blocked")]),
                "",
                0,
            )
        raise OSError("the daemon is gone")

    cli = ClaudeCli(executable="claude", runner=_runner)

    result = supervise_tick(store, "run-1", cli, RunAdapter(), {"u0": od}, at=1010.0)
    assert result.settled == {"u0": "blocked"}
    assert store.open_dispatches("run-1") == []
    # And the unit is still reclaimable once its lease expires -- the settle
    # happened despite the failing lifecycle calls.
    assert [u.unit_id for u in reclaimable_units(store, "run-1", at=1051.0)] == ["u0"]


def test_supervise_tick_failed_classifies_rate_limit_and_halts(tmp_path, monkeypatch):
    store = _seeded_dispatch_store(tmp_path)
    od = _claim_and_open(store, "run-1", "u0", "worker-a", "sess-1", "short1", at=1000.0)
    runner = FakeRunner()
    runner.script(
        ("claude", "agents", "--json"),
        (json.dumps([_bg_record(id="short1", session_id="sess-1", state="failed")]), "", 0),
    )
    cli = _cli(runner)
    adapter = RunAdapter()
    monkeypatch.setattr(claude_bg, "classify_settled_failure", lambda *a, **k: "rate_limit")

    result = supervise_tick(store, "run-1", cli, adapter, {"u0": od}, at=1010.0)
    assert result.settled == {"u0": "failed"}
    assert result.halted == "rate_limit"
    run = store.get_run("run-1")
    assert run.halted_kind == "rate_limit"
    unit = store.get_unit("run-1", "u0")
    assert unit.state is UnitState.PENDING  # D4: returned to PENDING, not terminally failed


def test_supervise_tick_failed_ordinary_does_not_halt(tmp_path, monkeypatch):
    store = _seeded_dispatch_store(tmp_path)
    od = _claim_and_open(store, "run-1", "u0", "worker-a", "sess-1", "short1", at=1000.0)
    runner = FakeRunner()
    runner.script(
        ("claude", "agents", "--json"),
        (json.dumps([_bg_record(id="short1", session_id="sess-1", state="failed")]), "", 0),
    )
    cli = _cli(runner)
    adapter = RunAdapter()
    monkeypatch.setattr(claude_bg, "classify_settled_failure", lambda *a, **k: None)

    result = supervise_tick(store, "run-1", cli, adapter, {"u0": od}, at=1010.0)
    assert result.halted is None
    run = store.get_run("run-1")
    assert run.halted_kind is None


def test_supervise_tick_done_accepted_settles_success_without_classifying(tmp_path, monkeypatch):
    store = _seeded_dispatch_store(tmp_path)
    od = _claim_and_open(store, "run-1", "u0", "worker-a", "sess-1", "short1", at=1000.0)
    store.accept_unit("run-1", "u0", od.fencing_token, text="ok", at=1005.0)
    runner = FakeRunner()
    runner.script(
        ("claude", "agents", "--json"),
        (json.dumps([_bg_record(id="short1", session_id="sess-1", state="done")]), "", 0),
    )
    cli = _cli(runner)
    adapter = RunAdapter()
    calls = {"n": 0}
    monkeypatch.setattr(
        claude_bg, "classify_settled_failure",
        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1),
    )

    result = supervise_tick(store, "run-1", cli, adapter, {"u0": od}, at=1010.0)
    assert result.settled == {"u0": "accepted"}
    assert calls["n"] == 0  # never classified -- success, not a failure


def test_supervise_tick_done_unaccepted_settles_and_classifies(tmp_path, monkeypatch):
    store = _seeded_dispatch_store(tmp_path)
    od = _claim_and_open(store, "run-1", "u0", "worker-a", "sess-1", "short1", at=1000.0)
    runner = FakeRunner()
    runner.script(
        ("claude", "agents", "--json"),
        (json.dumps([_bg_record(id="short1", session_id="sess-1", state="done")]), "", 0),
    )
    cli = _cli(runner)
    adapter = RunAdapter()
    calls = {"n": 0}
    monkeypatch.setattr(
        claude_bg, "classify_settled_failure",
        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1),
    )

    result = supervise_tick(store, "run-1", cli, adapter, {"u0": od}, at=1010.0)
    assert result.settled == {"u0": "done_unaccepted"}
    assert calls["n"] == 1


def test_supervise_tick_missing_from_all_settles_and_classifies(tmp_path, monkeypatch):
    store = _seeded_dispatch_store(tmp_path)
    od = _claim_and_open(store, "run-1", "u0", "worker-a", "sess-1", "short1", at=1000.0)
    runner = FakeRunner()
    runner.script(("claude", "agents", "--json"), ("[]", "", 0))
    cli = _cli(runner)
    adapter = RunAdapter()
    monkeypatch.setattr(claude_bg, "classify_settled_failure", lambda *a, **k: None)

    result = supervise_tick(store, "run-1", cli, adapter, {"u0": od}, at=1010.0)
    assert result.settled == {"u0": "missing"}


def test_supervise_tick_drops_slot_on_fence_and_claimant_drift(tmp_path):
    """MUTATION CHECK anchor: dropping the claimed_by/fence guard would let
    this tick RENEW a lease belonging to a NEW claimant -- observable as the
    original dispatcher's tick call succeeding and touching worker-b's
    lease."""
    store = _seeded_dispatch_store(tmp_path)
    od = _claim_and_open(store, "run-1", "u0", "worker-a", "sess-1", "short1", at=1000.0, lease_seconds=5.0)
    # Someone else reclaimed the unit (fence bumped, new claimant).
    store.fail_unit("run-1", "u0", od.fencing_token, terminal=False, at=1005.0)
    store.claim_unit("run-1", "u0", "worker-b", lease_seconds=50.0, at=1006.0)
    other_lease_expiry = store.get_unit("run-1", "u0").lease_expires_at

    runner = FakeRunner()
    runner.script(
        ("claude", "agents", "--json"),
        (json.dumps([_bg_record(id="short1", session_id="sess-1", state="working")]), "", 0),
    )
    cli = _cli(runner)
    adapter = RunAdapter()

    result = supervise_tick(store, "run-1", cli, adapter, {"u0": od}, at=1010.0)
    assert result.renewed == ()
    assert result.dropped == ("u0",)
    unit = store.get_unit("run-1", "u0")
    assert unit.claimed_by == "worker-b"
    assert unit.lease_expires_at == other_lease_expiry  # untouched by our (stale) dispatch


# ===========================================================================
# Step 9 -- reclaim selection and bounded reclaims
# ===========================================================================


def test_reclaimable_units_returns_expired_claimed_with_no_open_dispatch(tmp_path):
    store = _seeded_dispatch_store(tmp_path, unit_ids=("u0", "u1"))
    store.claim_unit("run-1", "u0", "worker-a", lease_seconds=10.0, at=1000.0)
    store.claim_unit("run-1", "u1", "worker-b", lease_seconds=10.0, at=1000.0)
    result = reclaimable_units(store, "run-1", at=1020.0)
    assert {u.unit_id for u in result} == {"u0", "u1"}


def test_reclaimable_units_excludes_units_with_an_open_dispatch(tmp_path):
    store = _seeded_dispatch_store(tmp_path, unit_ids=("u0", "u1"))
    store.claim_unit("run-1", "u0", "worker-a", lease_seconds=10.0, at=1000.0)
    store.claim_unit("run-1", "u1", "worker-b", lease_seconds=10.0, at=1000.0)
    store.record_dispatch("run-1", "u0", "worker-a", at=1000.0)
    result = reclaimable_units(store, "run-1", at=1020.0)
    assert {u.unit_id for u in result} == {"u1"}


def test_reclaimable_units_excludes_units_not_yet_expired(tmp_path):
    store = _seeded_dispatch_store(tmp_path)
    store.claim_unit("run-1", "u0", "worker-a", lease_seconds=100.0, at=1000.0)
    assert reclaimable_units(store, "run-1", at=1020.0) == []


def test_reclaim_attempt_count_counts_expire_attempts(tmp_path):
    store = _seeded_dispatch_store(tmp_path)
    store.claim_unit("run-1", "u0", "worker-a", lease_seconds=5.0, at=1000.0)
    store.claim_unit("run-1", "u0", "worker-b", lease_seconds=5.0, at=1010.0)  # 1st EXPIRE
    store.claim_unit("run-1", "u0", "worker-c", lease_seconds=5.0, at=1020.0)  # 2nd EXPIRE
    assert reclaim_attempt_count(store, "run-1", "u0") == 2


# ===========================================================================
# Step 10 -- halt classification for a settled unit (never `claude logs`)
# ===========================================================================


def test_classify_settled_failure_reads_transcript_tail_and_classifies(tmp_path):
    projects_root = tmp_path / "projects"
    project_dir = projects_root / "myproject"
    project_dir.mkdir(parents=True)
    transcript = project_dir / "sess-xyz.jsonl"
    transcript.write_text(
        '{"type": "assistant", "text": "irrelevant line"}\n'
        '{"type": "error", "text": "hit your limit, try again later"}\n',
        encoding="utf-8",
    )
    kind = classify_settled_failure("sess-xyz", projects_root=projects_root)
    assert kind == claude_bg.HALT_RATE_LIMIT


def test_classify_settled_failure_reads_job_state_text_fields_only(tmp_path):
    """P13: `state.json`'s own `state` field is never read for status; only
    detail/needs/output.result -- and this must still classify from those
    even when `state` itself says something unrelated (a disagreeing
    channel)."""
    jobs_root = tmp_path / "jobs"
    job_dir = jobs_root / "short1"
    job_dir.mkdir(parents=True)
    (job_dir / "state.json").write_text(
        json.dumps({"state": "working", "detail": "authentication_error occurred"}),
        encoding="utf-8",
    )
    kind = classify_settled_failure(
        "sess-none", job_id="short1", jobs_root=jobs_root, projects_root=tmp_path / "no-such-projects"
    )
    assert kind == claude_bg.HALT_AUTH


def test_classify_settled_failure_returns_none_for_ordinary_text(tmp_path):
    projects_root = tmp_path / "projects"
    project_dir = projects_root / "p"
    project_dir.mkdir(parents=True)
    (project_dir / "sess-xyz.jsonl").write_text(
        '{"type": "assistant", "text": "all good"}\n', encoding="utf-8"
    )
    kind = classify_settled_failure("sess-xyz", projects_root=projects_root)
    assert kind is None


def test_classify_settled_failure_never_raises_when_nothing_exists(tmp_path):
    kind = classify_settled_failure(
        "sess-nowhere", job_id="none",
        projects_root=tmp_path / "nope", jobs_root=tmp_path / "also-nope",
    )
    assert kind is None


# ===========================================================================
# Step 11 -- the loop
# ===========================================================================


def test_dispatch_wave_second_dispatcher_exits_without_launching(tmp_path):
    """MUTATION CHECK anchor: skipping the dispatcher-lease acquire would let
    two concurrent calls both launch."""
    store = _seeded_dispatch_store(tmp_path)
    wc = _worker_command(tmp_path)
    runner = _healthy_runner()
    cli = _cli(runner)
    wave = store.list_units("run-1")

    store.acquire_dispatcher_lease("run-1", "someone-else", lease_seconds=120.0, at=1000.0)

    report = dispatch_wave(
        store, "run-1", wave, RunAdapter(), cli=cli, worker_command=wc,
        at=1010.0, sleep_fn=lambda s: None, clock_fn=lambda: 1010.0,
    )
    assert report.dispatcher_acquired is False
    assert report.dispatched == ()
    assert report.aborted_reason == "dispatcher_lease_held_by_another_dispatcher"
    launch_calls = [argv for argv, _kwargs in runner.calls if len(argv) >= 2 and argv[1] == "--bg" and argv[2:3] != ["-p"]]
    assert launch_calls == []


def test_dispatch_wave_dispatcher_can_reacquire_its_own_expired_lease(tmp_path, monkeypatch):
    """ACCEPT case: a dispatcher must be able to re-acquire its OWN expired
    lease."""
    store = _seeded_dispatch_store(tmp_path)
    wc = _worker_command(tmp_path)
    monkeypatch.setattr(claude_bg, "_mint_worker_id", lambda: "fixed-dispatcher")
    store.acquire_dispatcher_lease("run-1", "fixed-dispatcher", lease_seconds=10.0, at=1000.0)

    runner = _healthy_runner()
    cli = _cli(runner)
    report = dispatch_wave(
        store, "run-1", [], RunAdapter(), cli=cli, worker_command=wc,
        at=1020.0, sleep_fn=lambda s: None, clock_fn=lambda: 1020.0,
    )
    assert report.dispatcher_acquired is True
    run = store.get_run("run-1")
    assert run.dispatcher_id is None  # released cleanly on exit


def test_dispatch_wave_bounds_reclaims_and_terminally_fails_third_attempt(tmp_path):
    """MUTATION CHECK anchor: removing the reclaim bound would let a third
    dispatch be observed instead of a terminal failure."""
    store = _seeded_dispatch_store(tmp_path)
    wc = _worker_command(tmp_path)
    store.claim_unit("run-1", "u0", "worker-1", lease_seconds=1.0, at=1000.0)
    store.claim_unit("run-1", "u0", "worker-2", lease_seconds=1.0, at=1002.0)  # 1st EXPIRE
    store.claim_unit("run-1", "u0", "worker-3", lease_seconds=1.0, at=1004.0)  # 2nd EXPIRE

    runner = _healthy_runner()
    cli = _cli(runner)
    report = dispatch_wave(
        store, "run-1", [], RunAdapter(), cli=cli, worker_command=wc,
        max_reclaims_per_unit=2, at=1010.0, sleep_fn=lambda s: None, clock_fn=lambda: 1010.0,
    )
    assert "u0" in report.failed_exhausted
    assert "u0" not in report.dispatched
    unit = store.get_unit("run-1", "u0")
    assert unit.state is UnitState.FAILED
    launch_calls = [argv for argv, _kwargs in runner.calls if len(argv) >= 2 and argv[1] == "--bg" and argv[2:3] != ["-p"]]
    assert launch_calls == []  # no third dispatch attempted


def test_dispatch_wave_launch_misconfiguration_aborts_whole_loop(tmp_path):
    store = _seeded_dispatch_store(tmp_path, unit_ids=("u0", "u1"))
    wc = _worker_command(tmp_path)
    runner = _healthy_runner()
    runner.script(("claude", "--bg"), ("backgrounded * baaaaaad", "", 0))
    runner.script(
        ("claude", "agents", "--json"),
        (json.dumps([_bg_record(id="baaaaaad", session_id="sess-bad", state="failed")]), "", 0),
    )
    runner.script(("claude", "rm"), ("removed", "", 0))
    cli = _cli(runner)
    wave = store.list_units("run-1")

    report = dispatch_wave(
        store, "run-1", wave, RunAdapter(), cli=cli, worker_command=wc,
        max_agents=2, at=1000.0, sleep_fn=lambda s: None, clock_fn=lambda: 1000.0,
    )
    assert report.aborted_reason == "launch_misconfiguration"
    assert report.dispatched == ()
    launch_calls = [argv for argv, _kwargs in runner.calls if len(argv) >= 2 and argv[1] == "--bg" and argv[2:3] != ["-p"]]
    assert len(launch_calls) == 1  # the second candidate was never attempted


def test_dispatch_wave_report_and_argv_never_carry_unit_content(tmp_path, monkeypatch):
    """Invariant 6 as applied to the dispatcher's own surfaces: no UNIT
    CONTENT in any argv or in the report. Deliberately not "nothing runtime"
    -- the launch prompt names the fencing token the dispatcher's own claim
    returned, so the launch argv legitimately carries a runtime value. What
    it must never carry is the unit's system/user text, which is exactly
    what SECRET stands in for here."""
    store = _seeded_dispatch_store(tmp_path)
    wc = _worker_command(tmp_path)
    SECRET = "TOP-SECRET-UNIT-PAYLOAD-XYZ"
    adapter = RunAdapter(user_for=lambda u: SECRET, system_for=lambda u: "sys " + SECRET)
    monkeypatch.setattr(claude_bg, "classify_settled_failure", lambda *a, **k: None)

    runner = _healthy_runner()
    runner.script(("claude", "--bg"), ("backgrounded * c0ffee01", "", 0))
    runner.script(
        ("claude", "agents", "--json"),
        [
            ("[]", "", 0),
            (json.dumps([_bg_record(id="c0ffee01", session_id="sess-1", state="working")]), "", 0),
            (json.dumps([_bg_record(id="c0ffee01", session_id="sess-1", state="done")]), "", 0),
        ],
    )
    cli = _cli(runner)
    wave = store.list_units("run-1")

    # The DISPATCHER claims before the launch now, so the fake worker only
    # has to do nothing at all for the tick to settle cleanly
    # (done_unaccepted) rather than drop the slot on drift.
    report = dispatch_wave(
        store, "run-1", wave, adapter, cli=cli, worker_command=wc, max_agents=1,
        at=1000.0, sleep_fn=lambda s: None, clock_fn=lambda: 1000.0,
    )
    for argv, _kwargs in runner.calls:
        assert SECRET not in " ".join(argv)
    serialized = json.dumps(dataclasses.asdict(report), default=str)
    assert SECRET not in serialized


def test_dispatch_wave_status_digest_equals_compute_status_and_carries_no_content(tmp_path):
    from content_pipeline.execution.status import compute_status as real_compute_status

    store = _seeded_dispatch_store(tmp_path, unit_ids=("u0",))
    wc = _worker_command(tmp_path)
    runner = _healthy_runner()
    cli = _cli(runner)

    report = dispatch_wave(
        store, "run-1", [], RunAdapter(), cli=cli, worker_command=wc,
        at=1000.0, sleep_fn=lambda s: None, clock_fn=lambda: 1000.0,
    )
    assert len(report.status_digests) >= 1
    expected = real_compute_status(store, "run-1", now=1000.0).to_dict()
    digest = report.status_digests[-1]
    assert digest["run_id"] == expected["run_id"]
    assert digest["total_units"] == expected["total_units"]
    assert digest["counts_by_state"] == expected["counts_by_state"]
    for forbidden in ("prompt", "payload", "text", "output"):
        assert forbidden not in digest


def test_dispatch_wave_happy_path_dispatches_and_observes_acceptance(tmp_path):
    store = _seeded_dispatch_store(tmp_path, unit_ids=("u0",))
    wc = _worker_command(tmp_path)

    runner = _healthy_runner()
    runner.script(("claude", "--bg"), ("backgrounded * abc12345", "", 0))
    runner.script(
        ("claude", "agents", "--json"),
        [
            ("[]", "", 0),
            (json.dumps([_bg_record(id="abc12345", session_id="sess-1", state="working")]), "", 0),
            (json.dumps([_bg_record(id="abc12345", session_id="sess-1", state="done")]), "", 0),
        ],
    )
    cli = _cli(runner)
    wave = store.list_units("run-1")

    original_launch = cli.launch_bg

    def fake_launch(prompt, **kwargs):
        """The fake WORKER: it does not claim (the dispatcher already did),
        it reads its fencing token out of the launch prompt -- exactly the
        channel a real worker gets it on -- and submits under it."""
        result = original_launch(prompt, **kwargs)
        import re as _re
        token = int(_re.search(r"Fencing token: (\d+)", prompt).group(1))
        store.accept_unit("run-1", "u0", token, text="answer", at=1001.0)
        return result

    cli.launch_bg = fake_launch  # type: ignore[assignment]

    report = dispatch_wave(
        store, "run-1", wave, RunAdapter(), cli=cli, worker_command=wc,
        max_agents=1, at=1000.0, sleep_fn=lambda s: None, clock_fn=lambda: 1000.0,
    )
    assert "u0" in report.dispatched
    assert "u0" in report.accepted
    unit = store.get_unit("run-1", "u0")
    assert unit.state is UnitState.ACCEPTED


# ---------------------------------------------------------------------------
# The launch-args seam: how a consumer selects a worker agent
# ---------------------------------------------------------------------------


def _dispatch_one_and_capture_launch_argv(tmp_path, **wave_kwargs):
    """Run one happy-path dispatch and return the single `--bg` launch argv."""
    store = _seeded_dispatch_store(tmp_path, unit_ids=("u0",))
    wc = _worker_command(tmp_path)

    runner = _healthy_runner()
    runner.script(("claude", "--bg"), ("backgrounded * abc12345", "", 0))
    runner.script(
        ("claude", "agents", "--json"),
        [
            ("[]", "", 0),
            (json.dumps([_bg_record(id="abc12345", session_id="sess-1", state="working")]), "", 0),
            (json.dumps([_bg_record(id="abc12345", session_id="sess-1", state="done")]), "", 0),
        ],
    )
    cli = _cli(runner)
    wave = store.list_units("run-1")

    original_launch = cli.launch_bg

    def fake_launch(prompt, **kwargs):
        result = original_launch(prompt, **kwargs)
        import re as _re

        token = int(_re.search(r"Fencing token: (\d+)", prompt).group(1))
        store.accept_unit("run-1", "u0", token, text="answer", at=1001.0)
        return result

    cli.launch_bg = fake_launch  # type: ignore[assignment]

    dispatch_wave(
        store, "run-1", wave, RunAdapter(), cli=cli, worker_command=wc,
        max_agents=1, at=1000.0, sleep_fn=lambda s: None, clock_fn=lambda: 1000.0,
        **wave_kwargs,
    )
    launch_calls = [
        argv for argv, _kwargs in runner.calls
        if len(argv) >= 2 and argv[1] == "--bg" and argv[2:3] != ["-p"]
    ]
    assert len(launch_calls) == 1, launch_calls
    return launch_calls[0]


def test_dispatch_wave_forwards_extra_launch_args_to_the_launcher(tmp_path):
    """The seam: a consumer selects the shipped worker agent (or any other
    launch flag) by passing `extra_launch_args` to `dispatch_wave`, which
    reaches `ClaudeCli.launch_bg` unaltered and in order, ahead of the
    positional prompt."""
    argv = _dispatch_one_and_capture_launch_argv(
        tmp_path, extra_launch_args=("--agent", "pipeline-worker")
    )
    assert argv[:4] == ["claude", "--bg", "--agent", "pipeline-worker"]
    assert len(argv) == 5
    assert argv[4].startswith("Run id: run-1\n")


def test_dispatch_wave_default_launch_argv_is_byte_identical_without_the_seam(tmp_path):
    """REFUSAL DIRECTION: passing no seam argument must launch exactly what
    the driver launched before the seam existed -- [exe, "--bg", prompt] and
    nothing else. The driver must never select an agent on its own: whether
    `--agent` composes with `--bg` at all is not established."""
    argv = _dispatch_one_and_capture_launch_argv(tmp_path)
    assert argv[:2] == ["claude", "--bg"]
    assert len(argv) == 3
    assert argv[2].startswith("Run id: run-1\n")


# ---------------------------------------------------------------------------
# The post-claim window: nothing may strand a unit CLAIMED with an open
# dispatch row (that combination is unrecoverable -- `reclaimable_units`
# skips a unit with an open dispatch, and `dispatch_wave`'s exit cleanup
# only settles dispatches it is already tracking).
# ---------------------------------------------------------------------------


def _assert_unit_recovered(store, run_id="run-1", unit_id="u0"):
    """The post-cleanup state a stranded unit must be in: not CLAIMED, no
    lease, and NO open dispatch row -- i.e. a later wave can pick it up."""
    row = store.get_unit(run_id, unit_id)
    assert row.state is UnitState.PENDING, f"unit left {row.state!r} after a failed launch"
    assert row.claimed_by is None
    assert row.lease_expires_at is None
    assert store.open_dispatches(run_id) == [], "dispatch row left OPEN -- unit is unreclaimable"
    assert unit_id in [u.unit_id for u in store.list_units(run_id) if u.state is UnitState.PENDING]


def test_prompt_build_failure_after_the_claim_does_not_strand_the_unit(tmp_path, monkeypatch):
    """`build_launch_prompt` does real filesystem I/O (makedirs + a write of
    the read envelope) AFTER the dispatcher has claimed, so it can raise for
    reasons that have nothing to do with this unit.

    MUTATION: drop the `except BaseException:` release/settle guard in
    `dispatch_unit` -- the unit stays CLAIMED with an OPEN dispatch row and
    both assertions in `_assert_unit_recovered` go red."""
    store = _seeded_dispatch_store(tmp_path)
    wc = _worker_command(tmp_path)
    runner = _healthy_runner()
    cli = _cli(runner)
    unit = _pending_unit(store, "run-1", "u0")

    boom = OSError("no space left on device")

    def _explode(*args, **kwargs):
        raise boom

    monkeypatch.setattr(claude_bg, "build_launch_prompt", _explode)

    with pytest.raises(OSError) as excinfo:
        dispatch_unit(
            store, "run-1", unit, cli, wc, worker_id="worker-a",
            clock_fn=lambda: 1000.0, at=1000.0,
        )
    assert excinfo.value is boom, "cleanup replaced the original exception"
    _assert_unit_recovered(store)
    launch_calls = [argv for argv, _k in runner.calls if len(argv) >= 2 and argv[1] == "--bg"]
    assert launch_calls == []


def test_launch_bg_failure_after_the_claim_does_not_strand_the_unit(tmp_path):
    """`cli.launch_bg` resolves the executable (`ClaudeExecutableNotFoundError`)
    and spawns a process (`subprocess.TimeoutExpired`), both after the claim.

    MUTATION: drop the `except BaseException:` release/settle guard in
    `dispatch_unit` -- red on `_assert_unit_recovered`."""
    store = _seeded_dispatch_store(tmp_path)
    wc = _worker_command(tmp_path)
    cli = _cli(_healthy_runner())
    unit = _pending_unit(store, "run-1", "u0")

    boom = ClaudeExecutableNotFoundError("claude not on PATH")

    def _explode(prompt, **kwargs):
        raise boom

    cli.launch_bg = _explode  # type: ignore[assignment]

    with pytest.raises(ClaudeExecutableNotFoundError) as excinfo:
        dispatch_unit(
            store, "run-1", unit, cli, wc, worker_id="worker-a",
            clock_fn=lambda: 1000.0, at=1000.0,
        )
    assert excinfo.value is boom
    _assert_unit_recovered(store)


def test_post_claim_cleanup_failure_never_masks_the_original_exception(tmp_path, monkeypatch):
    """The cleanup runs while an exception is already in flight, so a store
    failure inside it must not replace the failure that caused it.

    MUTATION: make either half of `_release_claim_and_settle` unguarded --
    the RuntimeError below escapes instead of the original OSError -> red."""
    store = _seeded_dispatch_store(tmp_path)
    wc = _worker_command(tmp_path)
    cli = _cli(_healthy_runner())
    unit = _pending_unit(store, "run-1", "u0")

    boom = OSError("no space left on device")

    def _explode(*args, **kwargs):
        raise boom

    monkeypatch.setattr(claude_bg, "build_launch_prompt", _explode)

    def _cleanup_explodes(*args, **kwargs):
        raise RuntimeError("store is gone too")

    store.fail_unit = _cleanup_explodes  # type: ignore[assignment]
    store.settle_dispatch = _cleanup_explodes  # type: ignore[assignment]

    with pytest.raises(OSError) as excinfo:
        dispatch_unit(
            store, "run-1", unit, cli, wc, worker_id="worker-a",
            clock_fn=lambda: 1000.0, at=1000.0,
        )
    assert excinfo.value is boom


# ---------------------------------------------------------------------------
# Claim refusals reach the WAVE now that the dispatcher claims. The routine
# ones must not tear it down.
# ---------------------------------------------------------------------------


def _wave_store_with_refusing_claim(tmp_path, error, *, refuse_unit="u0", unit_ids=("u0", "u1")):
    """A store whose `claim_unit` refuses exactly `refuse_unit`."""
    store = _seeded_dispatch_store(tmp_path, unit_ids=unit_ids)
    original_claim = store.claim_unit

    def _claim(run_id, unit_id, worker_id, **kwargs):
        if unit_id == refuse_unit:
            raise error
        return original_claim(run_id, unit_id, worker_id, **kwargs)

    store.claim_unit = _claim  # type: ignore[assignment]
    return store


def _wave_with_one_healthy_launch(store, tmp_path, accept_unit_id, *, clock_fn=None, **kwargs):
    """Run `dispatch_wave` with a runner that confirms one launch and a fake
    worker that accepts `accept_unit_id` out of the launch prompt."""
    wc = _worker_command(tmp_path)
    runner = _healthy_runner()
    runner.script(("claude", "--bg"), ("backgrounded * abc12345", "", 0))
    runner.script(
        ("claude", "agents", "--json"),
        [
            ("[]", "", 0),
            (json.dumps([_bg_record(id="abc12345", session_id="sess-1", state="working")]), "", 0),
            (json.dumps([_bg_record(id="abc12345", session_id="sess-1", state="done")]), "", 0),
        ],
    )
    cli = _cli(runner)
    original_launch = cli.launch_bg

    def fake_launch(prompt, **launch_kwargs):
        result = original_launch(prompt, **launch_kwargs)
        if accept_unit_id is not None:
            import re as _re

            token = int(_re.search(r"Fencing token: (\d+)", prompt).group(1))
            store.accept_unit("run-1", accept_unit_id, token, text="answer", at=1001.0)
        return result

    cli.launch_bg = fake_launch  # type: ignore[assignment]

    report = dispatch_wave(
        store, "run-1", store.list_units("run-1"), RunAdapter(), cli=cli, worker_command=wc,
        max_agents=2, at=1000.0, sleep_fn=lambda s: None,
        clock_fn=clock_fn if clock_fn is not None else (lambda: 1000.0),
        **kwargs,
    )
    return report, runner


def test_dispatch_wave_terminal_state_claim_refusal_skips_the_unit_and_keeps_going(tmp_path):
    """The reachable routine race: a reclaim candidate's still-live prior
    worker settles it under a still-current token (neither `accept_unit` nor
    `fail_unit` checks lease expiry) between candidate selection and the
    dispatcher's claim, so `claim_unit` raises TerminalStateError. That is
    invariant 4's accepted duplicate spend, not an error.

    MUTATION: delete the `except (TerminalStateError, AlreadyClaimedError)`
    handler -- the error propagates out of `dispatch_wave`, no report is
    returned, and every other open dispatch is torn down -> red."""
    store = _wave_store_with_refusing_claim(
        tmp_path, TerminalStateError("'run-1'/'u0' is already accepted")
    )
    report, _runner = _wave_with_one_healthy_launch(store, tmp_path, "u1")

    assert isinstance(report, DispatchReport)
    assert report.dispatched == ("u1",)
    assert "u1" in report.accepted
    # VISIBLE, not silently dropped: `dispatch_unit` settled the refused
    # unit's dispatch row as `claim_failed` before re-raising.
    assert report.settled["u0"] == "claim_failed"
    assert report.aborted_reason is None
    assert store.open_dispatches("run-1") == []


def test_dispatch_wave_already_claimed_refusal_skips_the_unit_and_keeps_going(tmp_path):
    """MUTATION: delete the `except (TerminalStateError, AlreadyClaimedError)`
    handler -> red (the error escapes `dispatch_wave`)."""
    store = _wave_store_with_refusing_claim(
        tmp_path, AlreadyClaimedError("'run-1'/'u0' is claimed")
    )
    report, runner = _wave_with_one_healthy_launch(store, tmp_path, "u1")

    assert isinstance(report, DispatchReport)
    assert report.dispatched == ("u1",)
    assert report.settled["u0"] == "claim_failed"
    assert report.aborted_reason is None
    # The refused unit is not retried forever: one attempt, then the wave
    # stops selecting it (otherwise the loop spins until the stall bound).
    launch_calls = [
        argv for argv, _k in runner.calls
        if len(argv) >= 2 and argv[1] == "--bg" and argv[2:3] != ["-p"]
    ]
    assert len(launch_calls) == 1


def test_dispatch_wave_run_halted_claim_refusal_ends_the_wave_gracefully(tmp_path):
    """A halted RUN must end the wave the same way an OBSERVED halt does --
    the `halted` field and a readable report -- never as a tear-down.

    MUTATION: re-raise instead of setting `halted` -- no report is returned
    -> red. MUTATION 2: record it as `aborted_reason` instead of `halted` --
    the `halted` assertion goes red."""
    store = _wave_store_with_refusing_claim(
        tmp_path, RunHaltedError("run-1", "rate_limit"), refuse_unit="u0", unit_ids=("u0",)
    )
    report, runner = _wave_with_one_healthy_launch(store, tmp_path, None)

    assert isinstance(report, DispatchReport)
    assert report.halted == "rate_limit"
    assert report.dispatched == ()
    assert report.settled["u0"] == "claim_failed"
    launch_calls = [
        argv for argv, _k in runner.calls
        if len(argv) >= 2 and argv[1] == "--bg" and argv[2:3] != ["-p"]
    ]
    assert launch_calls == []
    assert store.open_dispatches("run-1") == []
    # Graceful, not torn down: the dispatcher lease was released cleanly.
    assert store.get_run("run-1").dispatcher_id is None


def test_dispatch_wave_still_propagates_an_unexpected_exception(tmp_path):
    """ACCEPT DIRECTION for the new handlers: they catch exactly
    TerminalStateError / AlreadyClaimedError / RunHaltedError. A genuine
    programming error must still surface.

    MUTATION: widen either handler to `except Exception` -- this ValueError
    is swallowed, no exception is raised -> red."""
    store = _wave_store_with_refusing_claim(tmp_path, ValueError("a real bug"), unit_ids=("u0",))
    with pytest.raises(ValueError):
        _wave_with_one_healthy_launch(store, tmp_path, None)
    # The `finally` still ran: the dispatcher lease is not left held.
    assert store.get_run("run-1").dispatcher_id is None


def test_dispatch_wave_does_not_retry_a_refused_claim_forever(tmp_path):
    """A refused unit stops being a candidate FOR THIS WAVE. Without that,
    an `AlreadyClaimedError` unit is re-selected every tick and re-attempted
    every tick; a refusal is not progress, so the wave spins until the stall
    bound (or forever, on a frozen clock).

    Run on an ADVANCING clock with a short stall bound so the mutation fails
    instead of hanging. MUTATION: drop the `claim_refused` filter on
    `candidates` -- the wave aborts with `wave_stalled` -> red."""
    store = _wave_store_with_refusing_claim(
        tmp_path, AlreadyClaimedError("'run-1'/'u0' is claimed")
    )
    ticks = {"t": 1000.0}

    def _advancing_clock():
        ticks["t"] += 1.0
        return ticks["t"]

    report, runner = _wave_with_one_healthy_launch(
        store, tmp_path, "u1", clock_fn=_advancing_clock, stall_timeout_seconds=10.0
    )
    assert report.aborted_reason is None, "the wave spun on a refused candidate"
    assert report.dispatched == ("u1",)
    launch_calls = [
        argv for argv, _k in runner.calls
        if len(argv) >= 2 and argv[1] == "--bg" and argv[2:3] != ["-p"]
    ]
    assert len(launch_calls) == 1
