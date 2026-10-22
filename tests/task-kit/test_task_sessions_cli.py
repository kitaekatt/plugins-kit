"""The working-session CLI: ``work`` registers and publishes, ``sessions`` lists / releases / publishes, and
``list`` / ``show`` / ``status`` display a task's registrations.

Every test runs the real ``task.py`` as a subprocess beneath a stand-in Claude process (``sessions_cli_support``),
against a real git repository with a bare remote. Each guard was shown to fail with its protection reverted; the
revert is named in the test's docstring.
"""

from __future__ import annotations

import json
import subprocess

import pytest
import yaml
from git_remote_support import git
from sessions_cli_support import HOST, TASK_CLI, Rig, tree_digest

from task_system import session_publish, sessions_cli
from task_system.sessions_config import SessionsConfigError, load_sessions_config

SID_A = "aaaaaaaa-0000-4000-8000-000000000001"
SID_B = "bbbbbbbb-0000-4000-8000-000000000002"
SID_C = "cccccccc-0000-4000-8000-000000000003"
FOREIGN_FILE = """\
_schema_version: 1
host: otherhost
machine: 0123456789abcdef
os_instance: linux
sessions:
- session_id: ffffffff-0000-4000-8000-00000000000f
  harness: claude-code
  task: alpha
  checkout: elsewhere
  registered_at: '{registered}'
  process:
    pid: 4242
    created: 1.5
    recorded_at: '{registered}'
"""


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig-empty"))


@pytest.fixture
def rig(tmp_path):
    built = Rig(tmp_path)
    yield built
    built.close()


@pytest.fixture
def alpha(rig):
    rig.add_task("alpha")
    return rig


def work(rig: Rig, harness, ref: str = "dev/tasks/alpha"):
    return rig.cli(harness, ["work", ref])


def session_lines(result) -> list[str]:
    return [line for line in result.stderr.splitlines() if line.startswith("session: ")]


def write_foreign(rig: Rig, hours_ago: int = 3) -> None:
    import datetime

    registered = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    path = rig.tasks / ".sessions" / "otherhost.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(FOREIGN_FILE.format(registered=registered), encoding="utf-8")


# -- work: register and publish ------------------------------------------------------------------------------


def test_work_registers_the_session_and_publishes_the_record(alpha):
    """Revert: drop the register_for_work call in _cmd_work -> no record, no commit, no push (red)."""
    rig = alpha
    harness = rig.harness(SID_A)
    rig.transcript(SID_A)

    result = work(rig, harness)

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("== task init -- invoke each of these now")
    assert session_lines(result) == [f"session: registered {HOST} on dev/tasks/alpha (session aaaaaaaa); published"]
    [record] = rig.records()
    assert record["session_id"] == SID_A and record["task"] == "alpha" and record["checkout"] == "project"
    assert record["transcript_path"] == f"~/.claude/projects/-rig-project/{SID_A}.jsonl"
    assert record["process"]["pid"] == harness.pid
    assert rig.remote_text(f".sessions/{HOST}.yaml") == rig.session_file().read_text(encoding="utf-8")
    assert git(rig.tasks, "status", "--porcelain").stdout == ""
    assert rig.local_files() == [], "the registration lives in git alone; the data directory holds locks only"


def test_work_stdout_and_exit_code_are_what_they_were_without_registration(alpha):
    """Revert: print the session lines to stdout instead of stderr -> the stdout comparison fails (red)."""
    rig = alpha
    plain = rig.plain(["work", "dev/tasks/alpha"])
    registered = work(rig, rig.harness(SID_A))
    refused = work(rig, rig.harness(SID_B, extra_env={"TASK_KIT_AGENT_DEPTH": "1"}))

    assert plain.returncode == registered.returncode == refused.returncode == 0
    assert plain.stdout == registered.stdout == refused.stdout
    assert plain.stderr == ""
    assert session_lines(registered) and session_lines(refused)


def test_nothing_is_registered_or_printed_outside_a_claude_code_subprocess(alpha):
    rig = alpha

    result = rig.plain(["work", "dev/tasks/alpha"])

    assert result.returncode == 0 and result.stderr == ""
    assert not (rig.tasks / ".sessions").exists()
    assert not (rig.data_dir / "sessions").exists()


def test_a_blocked_work_registers_nothing(alpha):
    """Revert: register before state_ops.work (the gate) -> the blocked task is still registered (red)."""
    rig = alpha
    (rig.tasks / "alpha" / "task.yaml").write_text(
        (rig.tasks / "alpha" / "task.yaml").read_text(encoding="utf-8") + "\n", encoding="utf-8")
    (rig.tasks / "alpha" / "plan.md").write_text("changed but uncommitted\n", encoding="utf-8")  # validate warns
    harness = rig.harness(SID_A)

    result = work(rig, harness)

    assert result.returncode == 1 and "validate blocks work" in result.stderr
    assert result.stdout == "" and session_lines(result) == []
    assert not (rig.tasks / ".sessions").exists()


def test_a_missing_task_folder_registers_nothing(alpha):
    rig = alpha

    result = work(rig, rig.harness(SID_A), "dev/tasks/no-such-task")

    assert result.returncode == 1 and session_lines(result) == []
    assert not (rig.tasks / ".sessions").exists()


def test_a_task_under_tmp_registers_locally_and_never_touches_git(rig):
    """Revert: publish for a tmp root too -> the line names a repository, not 'local only' (red)."""
    rig.add_task("scratch", "tmp")
    harness = rig.harness(SID_A)

    result = work(rig, harness, "tmp/scratch")

    assert result.returncode == 0, result.stderr
    assert session_lines(result) == [
        f"session: registered {HOST} on tmp/scratch (session aaaaaaaa); local only: tmp is machine-local"]
    assert (rig.project / "tmp" / ".sessions" / f"{HOST}.yaml").is_file()
    assert not (rig.tasks / ".sessions").exists()
    assert git(rig.remote, "log", "--oneline", "-3").stdout.count("\n") == 1  # the seed commit only


def test_a_non_publishable_durable_root_reports_the_refusal_and_work_still_works(alpha):
    """Revert: remove the preflight call -> the record is written although git ignores it (red)."""
    rig = alpha
    (rig.tasks / ".git" / "info" / "exclude").write_text(".sessions/\n", encoding="utf-8")
    harness = rig.harness(SID_A)

    result = work(rig, harness)

    assert result.returncode == 0 and result.stdout.startswith("== task init")
    [line] = session_lines(result)
    assert line.startswith("session: not registered: git ignores .sessions/ in ")
    assert not (rig.tasks / ".sessions").exists()
    assert rig.local_files() == []


def test_a_durable_root_without_an_upstream_is_refused_the_same_way(alpha):
    rig = alpha
    git(rig.tasks, "branch", "--unset-upstream")

    result = work(rig, rig.harness(SID_A))

    assert result.returncode == 0
    [line] = session_lines(result)
    assert line.startswith("session: not registered: ") and "upstream" in line
    assert not (rig.tasks / ".sessions").exists()


def test_offline_the_record_is_committed_and_the_line_says_it_is_unpublished(alpha):
    """Revert: skip the publish call -> the record stays an uncommitted edit and git status is dirty (red)."""
    rig = alpha
    good_url = rig.go_offline()
    harness = rig.harness(SID_A)

    result = work(rig, harness)

    assert result.returncode == 0 and result.stdout.startswith("== task init")
    [line] = session_lines(result)
    assert line.startswith(f"session: registered {HOST} on dev/tasks/alpha (session aaaaaaaa); "
                           "committed; not published: fetch failed: ")
    assert session_publish.CANNOT_SEE in line
    rel = f".sessions/{HOST}.yaml"
    assert git(rig.tasks, "status", "--porcelain", "--", rel).stdout == ""
    assert git(rig.tasks, "log", "-1", "--format=%s", "--", rel).stdout.strip() == f"task-kit sessions: {HOST}"
    assert not rig.remote_has(rel)

    rig.go_online(good_url)
    drained = rig.cli(harness, ["sessions", "publish"])
    assert drained.returncode == 0, drained.stdout + drained.stderr
    assert "published" in drained.stdout
    assert rig.remote_has(rel)


def test_sessions_publish_exits_nonzero_while_the_record_cannot_be_published(alpha):
    rig = alpha
    rig.go_offline()
    harness = rig.harness(SID_A)
    work(rig, harness)

    result = rig.cli(harness, ["sessions", "publish"])

    assert result.returncode == 1 and "not published: fetch failed" in result.stdout


def test_sessions_publish_with_nothing_pending_succeeds(alpha):
    rig = alpha
    harness = rig.harness(SID_A)
    work(rig, harness)

    result = rig.cli(harness, ["sessions", "publish"])

    assert result.returncode == 0 and "nothing to publish" in result.stdout


def test_a_dashboard_run_never_registers(alpha):
    """Revert: remove the in_dashboard_run check -> the run registers and commits (red)."""
    rig = alpha

    result = work(rig, rig.harness(SID_A, extra_env={"TASK_KIT_AGENT_DEPTH": "1"}))

    assert result.returncode == 0
    [line] = session_lines(result)
    assert line.startswith("session: not registered: this is a dashboard run")
    assert not (rig.tasks / ".sessions").exists()


def test_sessions_record_false_registers_nothing(alpha):
    """Revert: ignore the config -> the record is written (red)."""
    rig = alpha
    rig.set_config(f"sessions:\n  host: {HOST}\n  record: false\n")

    result = work(rig, rig.harness(SID_A))

    assert result.returncode == 0
    assert session_lines(result) == ["session: not registered: sessions.record is false"]
    assert not (rig.tasks / ".sessions").exists()


def test_an_unusable_sessions_config_is_reported_not_swallowed(alpha):
    rig = alpha
    rig.set_config("sessions:\n  recrod: false\n")

    result = work(rig, rig.harness(SID_A))

    assert result.returncode == 0
    [line] = session_lines(result)
    assert line.startswith("session: not registered: ") and "unknown sessions keys ['recrod']" in line


def test_an_unidentifiable_harness_process_is_reported(alpha):
    rig = alpha
    harness = rig.harness(SID_A)

    result = harness.run("env -u CLAUDE_PID " + rig.command(["work", "dev/tasks/alpha"]))

    assert result.returncode == 0
    [line] = session_lines(result)
    assert line.startswith("session: not registered: CLAUDE_PID is not set")
    assert not (rig.tasks / ".sessions").exists()


# -- work: several registrations -----------------------------------------------------------------------------


def test_a_running_session_on_the_same_task_is_reported_and_both_show(alpha):
    rig = alpha
    first = rig.harness(SID_A)
    work(rig, first)

    second = work(rig, rig.harness(SID_B))

    lines = session_lines(second)
    assert lines[0].startswith(f"session: registered {HOST} on dev/tasks/alpha (session bbbbbbbb)")
    assert lines[1] == f"session: also working this task: {HOST} running <1m (session aaaaaaaa)"
    assert {r["session_id"] for r in rig.records()} == {SID_A, SID_B}


def test_a_second_task_names_the_first_the_session_still_holds(alpha):
    rig = alpha
    rig.add_task("beta")
    harness = rig.harness(SID_A)
    work(rig, harness)

    result = work(rig, harness, "dev/tasks/beta")

    assert "session: this session also holds alpha" in session_lines(result)
    assert {r["task"] for r in rig.records()} == {"alpha", "beta"}


def test_a_not_running_registration_by_another_session_is_kept_and_the_release_command_is_named(alpha):
    """The takeover policy REFUSES (sessions.not_running_takeover_policy); work reports the kept record.

    Revert: drop the kept_not_running lines -> the release command is never named (red).
    """
    rig = alpha
    first = rig.harness(SID_A)
    work(rig, first)
    first.close()

    result = work(rig, rig.harness(SID_B))

    assert result.returncode == 0
    lines = session_lines(result)
    assert lines[0].startswith(f"session: registered {HOST} on dev/tasks/alpha (session bbbbbbbb)")
    kept = [line for line in lines if line.startswith("session: kept: ")]
    assert len(kept) == 1
    assert "its process is not running" in kept[0] and "task sessions release dev/tasks/alpha --all-local" in kept[0]
    assert {r["session_id"] for r in rig.records()} == {SID_A, SID_B}


def test_work_again_in_the_same_session_refreshes_instead_of_duplicating(alpha):
    rig = alpha
    harness = rig.harness(SID_A)
    work(rig, harness)

    result = work(rig, harness)

    assert session_lines(result)[0].startswith(f"session: refreshed {HOST} on dev/tasks/alpha (session aaaaaaaa)")
    assert len(rig.records()) == 1


# -- sessions release ----------------------------------------------------------------------------------------


def test_release_at_hand_off_removes_the_registration_and_publishes(alpha):
    """Revert: make release_command skip sessions.release -> the record stays (red)."""
    rig = alpha
    harness = rig.harness(SID_A)
    work(rig, harness)
    assert len(rig.records()) == 1

    result = rig.cli(harness, ["sessions", "release", "dev/tasks/alpha"])

    assert result.returncode == 0, result.stderr
    assert result.stdout == (f"session: released {HOST} from dev/tasks/alpha "
                             "(1 registration: session aaaaaaaa); published\n")
    assert rig.records() == []
    assert yaml.safe_load(rig.remote_text(f".sessions/{HOST}.yaml"))["sessions"] == []
    assert rig.local_files() == []


def test_release_removes_only_the_callers_registration(alpha):
    rig = alpha
    first, second = rig.harness(SID_A), rig.harness(SID_B)
    work(rig, first)
    work(rig, second)

    rig.cli(first, ["sessions", "release", "dev/tasks/alpha"])

    assert [r["session_id"] for r in rig.records()] == [SID_B]


def test_release_with_nothing_registered_says_so_and_succeeds(alpha):
    rig = alpha
    harness = rig.harness(SID_A)

    result = rig.cli(harness, ["sessions", "release", "dev/tasks/alpha"])

    assert result.returncode == 0
    assert result.stdout.startswith(f"session: nothing to release for dev/tasks/alpha on {HOST}")


def test_release_all_local_removes_every_local_registration_from_a_plain_shell(alpha):
    """Revert: pass all_local=False in release_command -> the plain shell has no identity and exits 1 (red)."""
    rig = alpha
    first, second = rig.harness(SID_A), rig.harness(SID_B)
    work(rig, first)
    work(rig, second)
    first.close()
    second.close()

    result = rig.plain(["sessions", "release", "dev/tasks/alpha", "--all-local"])

    assert result.returncode == 0, result.stderr
    assert "released" in result.stdout and "(2 registrations: " in result.stdout and "published" in result.stdout
    assert rig.records() == []
    assert yaml.safe_load(rig.remote_text(f".sessions/{HOST}.yaml"))["sessions"] == []


def test_release_without_all_local_outside_a_session_names_the_flag(alpha):
    rig = alpha
    harness = rig.harness(SID_A)
    work(rig, harness)

    result = rig.plain(["sessions", "release", "dev/tasks/alpha"])

    assert result.returncode == 1 and "--all-local" in result.stderr and result.stdout == ""
    assert len(rig.records()) == 1


def test_release_on_a_closed_task_still_removes_the_registration(alpha):
    rig = alpha
    harness = rig.harness(SID_A)
    work(rig, harness)
    assert rig.cli(harness, ["close", "dev/tasks/alpha"]).returncode == 0

    result = rig.cli(harness, ["sessions", "release", "dev/tasks/alpha"])

    assert result.returncode == 0 and rig.records() == []


def test_release_offline_still_releases_locally_and_says_what_is_pending(alpha):
    rig = alpha
    harness = rig.harness(SID_A)
    work(rig, harness)
    rig.go_offline()

    result = rig.cli(harness, ["sessions", "release", "dev/tasks/alpha"])

    assert result.returncode == 0 and "committed; not published: fetch failed" in result.stdout
    assert rig.records() == []


# -- list / show / status display ----------------------------------------------------------------------------


def test_list_show_and_status_display_running_not_running_and_unverified(alpha):
    """Revert (each): drop working_suffix in _format_list_line / the working: lines in show / the sessions: block
    in status / the _attach_sessions call in collect_listing -> the matching assertion fails (red)."""
    rig = alpha
    write_foreign(rig, hours_ago=3)
    live = rig.harness(SID_A)
    work(rig, live)
    gone = rig.harness(SID_B)
    work(rig, gone)
    gone.close()

    listed = rig.plain(["list"])
    shown = rig.plain(["show", "dev/tasks/alpha"])
    status = rig.plain(["status", "dev/tasks/alpha"])

    assert listed.returncode == shown.returncode == status.returncode == 0
    task_line = next(line for line in listed.stdout.splitlines() if line.startswith("dev/tasks/alpha  active"))
    assert task_line.endswith(f"[working: {HOST} running <1m; {HOST} not running <1m; otherhost unverified 3h]")
    fields = task_line.split("  ")
    assert fields[:3] == ["dev/tasks/alpha", "active", "-"]  # the first fields are unchanged
    working = [line for line in shown.stdout.splitlines() if line.startswith("working: ")]
    assert len(working) == 3
    assert working[0].startswith(f"working: {HOST} running (project, session aaaaaaaa, since ")
    assert working[1].startswith(f"working: {HOST} not running (project, session bbbbbbbb, since ")
    assert working[2].startswith("working: otherhost unverified (elsewhere, session ffffffff, since ")
    assert "sessions:\n" in status.stdout
    block = status.stdout.split("sessions:\n", 1)[1].splitlines()[:3]
    assert [line.strip().split(" ")[:2] for line in block] == [[HOST, "running"], [HOST, "not"], ["otherhost", "unverified"]]


def test_show_and_status_without_registrations(alpha):
    rig = alpha

    shown = rig.plain(["show", "dev/tasks/alpha"])
    status = rig.plain(["status", "dev/tasks/alpha"])
    listed = rig.plain(["list"])

    assert shown.stdout.splitlines()[-1] == "working: -"
    assert "sessions: none" in status.stdout.splitlines()
    assert "[working" not in listed.stdout
    assert listed.stderr == ""


def test_list_json_and_yaml_carry_sessions_per_task(alpha):
    """Revert: drop the sessions key in _view_dict -> KeyError (red)."""
    rig = alpha
    write_foreign(rig)
    harness = rig.harness(SID_A)
    work(rig, harness)

    as_json = json.loads(rig.plain(["list", "--format", "json"]).stdout)
    as_yaml = yaml.safe_load(rig.plain(["list", "--format", "yaml"]).stdout)

    [task] = as_json["tasks"]
    states = {(s["host"], s["state"]) for s in task["sessions"]}
    assert states == {(HOST, "running"), ("otherhost", "unverified")}
    mine = next(s for s in task["sessions"] if s["host"] == HOST)
    assert mine["session_id"] == SID_A and mine["checkout"] == "project" and mine["this_machine"] is True
    assert as_yaml["tasks"][0]["sessions"] == task["sessions"]


def test_a_task_without_a_sessions_directory_costs_no_machine_lookup(alpha, monkeypatch):
    rig = alpha

    def fail(*args, **kwargs):
        raise AssertionError("the machine must not be identified when no .sessions directory exists")

    monkeypatch.setattr(sessions_cli.machine_module, "current", fail)
    reader = sessions_cli.SessionReader()

    assert reader.records_for(rig.project, "dev/tasks", "alpha") == ()
    assert reader.notes == []


def test_sessions_lists_states_and_pending_files(alpha):
    rig = alpha
    write_foreign(rig)
    live = rig.harness(SID_A)
    work(rig, live)
    rig.go_offline()
    work(rig, rig.harness(SID_B))

    text = rig.cli(live, ["sessions"])
    as_json = json.loads(rig.cli(live, ["sessions", "list", "--format", "json"]).stdout)

    assert text.returncode == 0
    assert text.stdout.splitlines()[0] == f"Working sessions (this machine: {HOST}):"
    assert any(f"dev/tasks/alpha  {HOST} running (project, " in line for line in text.stdout.splitlines())
    assert any("otherhost unverified (registered 3h ago)" in line for line in text.stdout.splitlines())
    assert "Pending publication" in text.stdout and f".sessions/{HOST}.yaml" in text.stdout
    assert {(s["task"], s["host"], s["state"]) for s in as_json["sessions"]} == {
        ("dev/tasks/alpha", HOST, "running"), ("dev/tasks/alpha", "otherhost", "unverified")}
    assert as_json["pending"][0]["files"] == [f".sessions/{HOST}.yaml"]


def test_sessions_root_flag_works_before_and_after_the_subcommand(alpha, tmp_path):
    """Revert: drop default=argparse.SUPPRESS from `sessions list --root` -> the parent's --root is overwritten by
    the subparser default and the listing reads the current directory instead (red)."""
    rig = alpha
    harness = rig.harness(SID_A)
    work(rig, harness)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(rig.python), str(TASK_CLI), *args], capture_output=True, text=True,
                              cwd=elsewhere, env=rig.env())

    before = run("sessions", "--root", str(rig.project), "list")
    after = run("sessions", "list", "--root", str(rig.project))
    bare = run("sessions", "--root", str(rig.project))

    for done in (before, after, bare):
        assert done.returncode == 0, done.stderr
        assert "dev/tasks/alpha" in done.stdout and "running" in done.stdout


# -- read verbs never write ----------------------------------------------------------------------------------


def test_the_read_verbs_leave_every_byte_alone(alpha):
    """Revert: have list_command call session_publish.publish -> the repository lock file appears (red)."""
    rig = alpha
    write_foreign(rig)
    live, gone = rig.harness(SID_A), rig.harness(SID_B)
    work(rig, live)
    work(rig, gone)
    gone.close()
    before = tree_digest(rig.project, rig.data_root, rig.remote)

    for args in (["list"], ["list", "--format", "json"], ["show", "dev/tasks/alpha"], ["status", "dev/tasks/alpha"],
                 ["sessions"], ["sessions", "list", "--format", "json"]):
        done = rig.cli(live, args)
        assert done.returncode == 0, (args, done.stderr)

    assert tree_digest(rig.project, rig.data_root, rig.remote) == before


# -- units ---------------------------------------------------------------------------------------------------


def test_the_sessions_config_defaults_and_refusals(tmp_path):
    path = tmp_path / "task.local.yaml"
    assert load_sessions_config(path).record is True and load_sessions_config(path).host is None
    path.write_text("project_directories: ['/x']\ndashboard: {}\n", encoding="utf-8")
    assert load_sessions_config(path).record is True
    path.write_text("sessions:\n  record: false\n  host: Box-1\n", encoding="utf-8")
    config = load_sessions_config(path)
    assert config.record is False and config.host == "Box-1"
    for bad in ("sessions: [1]\n", "sessions:\n  record: 'no'\n", "sessions:\n  host: 3\n", "sessions:\n  speed: 1\n"):
        path.write_text(bad, encoding="utf-8")
        with pytest.raises(SessionsConfigError):
            load_sessions_config(path)


def test_age_formatting():
    import datetime

    now = datetime.datetime(2026, 10, 21, 12, 0, 0, tzinfo=datetime.timezone.utc)
    assert sessions_cli.format_age("2026-10-21T11:59:30Z", now) == "<1m"
    assert sessions_cli.format_age("2026-10-21T11:55:00Z", now) == "5m"
    assert sessions_cli.format_age("2026-10-21T09:00:00Z", now) == "3h"
    assert sessions_cli.format_age("2026-10-19T12:00:00Z", now) == "2d"
    assert sessions_cli.format_age("garbage", now) == "?"
