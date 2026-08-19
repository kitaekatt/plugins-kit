"""Tests for content_pipeline.execution.drivers.claude_bg -- B1 foundation
(steps 1-4 only: the claude process seam, preflight, the store's dispatcher-
lease/dispatch-tracking migration, and worker-environment composition).

No automated test in this module ever reaches a real subprocess for
``claude``: ``_no_real_claude_subprocess`` (autouse) replaces
``claude_bg._default_runner`` with a stub that fails the test outright if
anything ever calls it, and every test that needs a `claude` response
supplies its own scripted ``runner`` via :class:`FakeRunner`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from content_pipeline.execution.adapter import RunAdapter, WorkerEnvironment
from content_pipeline.execution.drivers import claude_bg
from content_pipeline.execution.drivers.claude_bg import (
    BILLING_DIVERTING_VARS,
    ClaudeCli,
    ClaudeExecutableNotFoundError,
    PreflightError,
    WorkerEnvironmentBillingLeakError,
    compose_worker_environment,
    preflight,
)
from content_pipeline.execution.model import NoOpenDispatchError, RunRecord, StaleDispatcherLeaseError
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
            return best_match[1]
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


def test_module_wide_agents_token_invariant():
    """P3: the token "agents" appears in exactly ONE argv shape this module
    builds for real dispatch -- [exe, "agents", "--json"] or
    [exe, "agents", "--json", "--all"] -- and is never adjacent to a
    lifecycle verb. Exercises every ClaudeCli command-building method
    through one fake runner and scans every logged argv.

    MUTATION CHECK (performed manually, see the task report): changing
    ClaudeCli._lifecycle's argv to `[exe, "agents", verb, session_id]`
    flips this test red, because that argv contains "agents" immediately
    followed by a lifecycle verb -- neither of the two permitted shapes.
    """
    runner = FakeRunner(default=("", "", 0))
    cli = _cli(runner)
    cli.launch_bg("hello")
    cli.agents_json(all_sessions=True)
    cli.agents_json(all_sessions=False)
    cli.stop("s1")
    cli.rm("s2")
    cli.respawn("s3")
    cli.version()

    permitted = ({"claude", "agents", "--json"}, {"claude", "agents", "--json", "--all"})
    for argv, _kwargs in runner.calls:
        if "agents" in argv:
            assert set(argv) in permitted, f"unexpected 'agents' argv shape: {argv!r}"
            # and it must never be adjacent to a lifecycle verb
            idx = argv.index("agents")
            if idx + 1 < len(argv):
                assert argv[idx + 1] not in ("stop", "logs", "rm", "respawn"), argv


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

_SUBPROCESS_CLAIM_SCRIPT = r"""
import json
import sys

sys.path.insert(0, sys.argv[1])

from content_pipeline.execution.adapter import RunAdapter, WorkerEnvironment
from content_pipeline.execution.protocol import PROTOCOL_VERSION, build_handlers, dispatch
from content_pipeline.execution.store import ExecutionStore
from content_pipeline.pipeline.workunit import FlatChunkStrategy

db_path, run_id, unit_id, worker_id = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]

store = ExecutionStore(db_path)
adapter = RunAdapter(
    user_for=lambda u: "user:" + u.id,
    parse_fn=lambda t: t,
    apply=lambda uid, payload: None,
    environment=WorkerEnvironment(required_vars=("CONTENT_ROOT",), require_cwd=True),
)
handlers = build_handlers(store, adapter, strategy=FlatChunkStrategy(select=lambda s: []))
envelope = {
    "protocol_version": PROTOCOL_VERSION,
    "verb": "claim",
    "payload": {"run_id": run_id, "unit_id": unit_id, "worker_id": worker_id},
}
result = dispatch(envelope, handlers)
print(json.dumps(result))
"""


def _run_claim_subprocess(env, cwd, db_path, run_id, unit_id, worker_id):
    proc = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_CLAIM_SCRIPT, LIB_ROOT, str(db_path), run_id, unit_id, worker_id],
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


def test_composed_environment_makes_a_real_worker_claim_succeed(tmp_path):
    """The environment verification that matters: dispatch a run whose
    WorkerEnvironment declares required_vars=("CONTENT_ROOT",),
    require_cwd=True from a process whose CONTENT_ROOT differs from the
    run's snapshot, and observe -- through a REAL protocol.build_handlers
    mount in a subprocess with the COMPOSED environment -- that its claim
    verb SUCCEEDS."""
    store, db_path, worker_cwd, recorded_content_root = _seed_environment_run(tmp_path)
    run = store.get_run("run-1")
    adapter = RunAdapter(environment=WorkerEnvironment(required_vars=("CONTENT_ROOT",), require_cwd=True))

    # This DISPATCHING process's own CONTENT_ROOT differs from the run's snapshot.
    dispatcher_base = dict(os.environ)
    dispatcher_base["CONTENT_ROOT"] = str(tmp_path / "wrong_root")

    child_env, cwd = compose_worker_environment(run, adapter, base=dispatcher_base)
    assert cwd == str(worker_cwd)
    assert child_env["CONTENT_ROOT"] == recorded_content_root

    result = _run_claim_subprocess(child_env, cwd, db_path, "run-1", "u0", "worker-1")
    assert result["ok"] is True, result


def test_without_the_overlay_a_real_worker_claim_is_refused(tmp_path):
    """The mirror: without the overlay (the raw dispatcher environment,
    mismatched CONTENT_ROOT and cwd), claim returns
    WorkerEnvironmentMismatchError. This is what proves the accept-case test
    above tests anything at all."""
    _store, db_path, _worker_cwd, _recorded_content_root = _seed_environment_run(tmp_path)

    mismatched_env = dict(os.environ)
    mismatched_env["CONTENT_ROOT"] = str(tmp_path / "wrong_root")
    mismatched_cwd = str(tmp_path)  # not worker_cwd

    result = _run_claim_subprocess(mismatched_env, mismatched_cwd, db_path, "run-1", "u0", "worker-1")
    assert result["ok"] is False
    assert result["error"]["type"] == "WorkerEnvironmentMismatchError"
