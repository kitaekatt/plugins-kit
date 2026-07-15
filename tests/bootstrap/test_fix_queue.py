"""Unit tests for bootstrap_lib/fix_queue.py.

Covers the engine half of the interactive-remediation system: descriptor
harvesting into typed FixTasks, queue/shim write-regenerate-clear (cleanup when
nothing is deferred), the aggregated fix-all item, the interactive launch, and
the privilege dispatcher.

Replaces test_elevation.py, which tested a shell-script GENERATOR that no longer
exists. Its golden-content tests (the `"<bash>" -c "<cmd>"` line shape, the
CRLF-not-CRCRLF assertion, the `~`/$HOME pre-expansion matrix) are deliberately
NOT ported: they pinned the two hacks the queue removes -- command strings are
now data, so there is no splice to escape and no sudo-HOME to compensate for.
The behaviors that survive are asserted here instead, at the level that still
means something (a command reaches bash verbatim; a task is elevated or not).
"""

import json
import os

import pytest

import bootstrap_lib.fix_queue as fq
from bootstrap_lib.fix_queue import FixTask


def desc(method="command", os_="ubuntu", **kw):
    d = {"method": method, "os": os_}
    d.update(kw)
    return {"elevation": d}


# --------------------------------------------------------------------------- #
# FixTask.to_json
# --------------------------------------------------------------------------- #

class TestFixTaskToJson:
    def test_zero_timeout_is_preserved(self):
        """A meaningful `timeout: 0` must survive serialization. The old
        `v not in (None, [], False)` filter used `==`, so 0 matched False and
        was dropped -- this guards the identity-check replacement."""
        out = FixTask(id="t", kind="command", label="L", timeout=0).to_json()
        assert out["timeout"] == 0

    def test_unset_optionals_are_dropped(self):
        """Required fields always serialize; unset optionals stay out so the
        queue file stays readable."""
        out = FixTask(id="t", kind="command", label="L").to_json()
        assert out == {"id": "t", "kind": "command", "label": "L"}


# --------------------------------------------------------------------------- #
# Default timeout mirror
# --------------------------------------------------------------------------- #

class TestDefaultTaskTimeoutMirror:
    def test_mirrors_env_check_default(self):
        """DEFAULT_TASK_TIMEOUT is a hand-copy of env_features'
        ENV_CHECK_DEFAULT_TIMEOUT, not an import (a top-level import would drag
        heavy modules into fix_queue's near-stdlib-only import graph). The copy
        can silently skew, so pin the two constants equal here."""
        import bootstrap_lib.env_features as env_features
        assert fq.DEFAULT_TASK_TIMEOUT == env_features.ENV_CHECK_DEFAULT_TIMEOUT


# --------------------------------------------------------------------------- #
# Privilege dispatcher
# --------------------------------------------------------------------------- #

class TestPrivilegesAvailable:
    def test_windows_uses_admin_token(self, monkeypatch):
        monkeypatch.setattr(fq, "windows_admin_available", lambda: True)
        monkeypatch.setattr(fq, "sudo_noninteractive_available",
                            lambda: pytest.fail("must not probe sudo on windows"))
        assert fq.privileges_available("windows") is True

    def test_unix_uses_sudo_probe(self, monkeypatch):
        monkeypatch.setattr(fq, "sudo_noninteractive_available", lambda: True)
        monkeypatch.setattr(fq, "windows_admin_available",
                            lambda: pytest.fail("must not probe admin token on unix"))
        assert fq.privileges_available("ubuntu") is True
        assert fq.privileges_available("macos") is True

    def test_unix_sudo_missing_is_false(self, monkeypatch):
        monkeypatch.setattr(fq, "sudo_noninteractive_available", lambda: False)
        assert fq.privileges_available("ubuntu") is False


# --------------------------------------------------------------------------- #
# queue_from_failures
# --------------------------------------------------------------------------- #

class TestQueueFromFailures:
    def test_empty_failures_is_empty_queue(self):
        assert fq.queue_from_failures([], "ubuntu") == []

    def test_failures_without_descriptors_ignored(self):
        assert fq.queue_from_failures([{"type": "tool"}, {"type": "venv"}], "ubuntu") == []

    def test_apt_packages_collapse_into_one_task(self):
        """One apt task, not one per package: co-dependent packages fail when
        installed one at a time."""
        tasks = fq.queue_from_failures(
            [desc("apt", package="net-tools"), desc("apt", package="tmux")], "ubuntu")
        assert len(tasks) == 1
        assert tasks[0].kind == "apt"
        assert tasks[0].packages == ["net-tools", "tmux"]
        assert tasks[0].elevated is True

    def test_commands_become_one_task_each_in_pass_order(self):
        tasks = fq.queue_from_failures(
            [desc(command="first", label="First"), desc(command="second", label="Second")],
            "ubuntu")
        assert [t.command for t in tasks] == ["first", "second"]
        assert [t.label for t in tasks] == ["First", "Second"]

    def test_brew_installer_leads_the_slow_group_and_is_not_elevated(self):
        """The Homebrew installer refuses to run as root and elevates itself
        where it needs to, so wrapping it in sudo would break it.

        Both it and a `brew install` command are COST_SLOW, so front-loading
        cannot separate them: the installer must still come first, or the
        install it enables has no brew to run under.
        """
        tasks = fq.queue_from_failures(
            [desc(command="brew install jq", os_="macos", label="Install jq",
                  cost="slow"),
             desc("brew_installer", os_="macos")], "macos")
        assert tasks[0].kind == "brew_installer"
        assert tasks[0].elevated is False
        assert tasks[1].kind == "command"

    def test_apt_precedes_slow_commands(self):
        """Ordering inside a cost class is preserved, so apt's batched install
        still lands before the slow commands that may want those packages."""
        tasks = fq.queue_from_failures(
            [desc(command="c", label="C", cost="slow"),
             desc("apt", package="net-tools")], "ubuntu")
        assert [t.kind for t in tasks] == ["apt", "command"]

    def test_quick_commands_are_front_loaded_ahead_of_apt(self):
        """The point of the reorder: the user watching the window sees the cheap
        fixes land and finish instead of staring at a package download first."""
        tasks = fq.queue_from_failures(
            [desc("apt", package="net-tools"),
             desc(command="ln -s a b", label="Link")], "ubuntu")
        assert [t.kind for t in tasks] == ["command", "apt"]

    def test_only_current_os_descriptors_collected(self):
        tasks = fq.queue_from_failures(
            [desc(command="linux", os_="ubuntu", label="L"),
             desc(command="win", os_="windows", label="W")], "windows")
        assert [t.command for t in tasks] == ["win"]

    def test_label_falls_back_to_the_command(self):
        """A producer that forgets a label must still yield something a human
        can read, not a KeyError."""
        tasks = fq.queue_from_failures([desc(command="bash x.sh fix")], "ubuntu")
        assert tasks[0].label == "bash x.sh fix"

    def test_task_labels_are_the_message_item_list(self):
        tasks = fq.queue_from_failures(
            [desc(command="a", label="Link starship-config"),
             desc(command="b", label="ssh-server-windows")], "ubuntu")
        assert fq.task_labels(tasks) == ["Link starship-config", "ssh-server-windows"]


# --------------------------------------------------------------------------- #
# Cost classification + ordering
# --------------------------------------------------------------------------- #

class TestCostOf:
    def test_explicit_cost_wins_over_the_timeout_heuristic(self):
        """The declared field is the manifest's statement of intent; the timeout
        rule is only an inference for entries that declared nothing."""
        assert fq.cost_of({"cost": "quick", "timeout": 3600}) == fq.COST_QUICK
        assert fq.cost_of({"cost": "slow", "timeout": 5}) == fq.COST_SLOW

    def test_a_timeout_above_the_default_reads_as_slow(self):
        """An author raising the timeout is telling us this one is different --
        in practice, that it downloads (winget install Nvidia.CUDA: 3600)."""
        assert fq.cost_of({"timeout": fq.DEFAULT_TASK_TIMEOUT + 1}) == fq.COST_SLOW

    def test_the_default_timeout_reads_as_quick(self):
        """Taking the default says nothing, so it must not imply 'slow' -- that
        would flag every ordinary env_check fix as a download."""
        assert fq.cost_of({"timeout": fq.DEFAULT_TASK_TIMEOUT}) == fq.COST_QUICK
        assert fq.cost_of({}) == fq.COST_QUICK

    def test_garbage_cost_falls_back_rather_than_raising(self):
        """A descriptor is data from a manifest; a typo must not crash the pass
        that is trying to tell the user what is broken."""
        assert fq.cost_of({"cost": "medium"}) == fq.COST_QUICK
        assert fq.cost_of({"cost": "medium", "timeout": 3600}) == fq.COST_SLOW

    def test_bool_timeout_is_not_read_as_an_int(self):
        """bool is an int subclass; True > 600 is False, but the guard keeps the
        intent explicit rather than accidental."""
        assert fq.cost_of({"timeout": True}) == fq.COST_QUICK


class TestOrderTasks:
    def _t(self, name, cost):
        return FixTask(id=name, kind="command", label=name, command="x", cost=cost)

    def test_quick_precede_slow(self):
        out = fq.order_tasks([self._t("s", fq.COST_SLOW), self._t("q", fq.COST_QUICK)])
        assert [t.label for t in out] == ["q", "s"]

    def test_order_within_a_class_is_stable(self):
        """A sort would be free to permute equals; the incoming order carries
        real constraints (brew before brew-install), so it must survive."""
        out = fq.order_tasks([
            self._t("s1", fq.COST_SLOW), self._t("q1", fq.COST_QUICK),
            self._t("s2", fq.COST_SLOW), self._t("q2", fq.COST_QUICK)])
        assert [t.label for t in out] == ["q1", "q2", "s1", "s2"]

    def test_no_tasks_are_dropped(self):
        tasks = [self._t("a", fq.COST_QUICK), self._t("b", fq.COST_SLOW)]
        assert len(fq.order_tasks(tasks)) == 2


class TestCostSerialization:
    def test_slow_is_emitted(self):
        t = FixTask(id="i", kind="command", label="L", command="x", cost=fq.COST_SLOW)
        assert t.to_json()["cost"] == "slow"

    def test_quick_is_omitted_as_the_readers_default(self):
        """queue.json is a disclosure surface a human may open; a `cost: quick`
        on every line is noise."""
        t = FixTask(id="i", kind="command", label="L", command="x")
        assert "cost" not in t.to_json()

    def test_the_runner_accepts_what_the_queue_emits(self):
        """Cross-module contract: fix_runner.validate rejects an unknown cost,
        so the writer's vocabulary must be the reader's."""
        import bootstrap_lib.fix_runner as fr
        t = FixTask(id="i", kind="command", label="L", command="x", cost=fq.COST_SLOW)
        assert fr.validate({"version": fq.QUEUE_VERSION, "tasks": [t.to_json()]}) == []


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #

class TestRenderQueue:
    def test_quotes_in_a_command_survive_verbatim(self, monkeypatch):
        """The renderer this replaced REJECTED any command containing a double
        quote (it had no escaping rule for `"<bash>" -c "<cmd>"`). As data,
        quotes are unremarkable -- this is the regression guard for that."""
        monkeypatch.setattr(fq, "resolve_bash", lambda: "/usr/bin/bash")
        cmd = 'sh -c "echo hello"'
        out = json.loads(fq.render_queue(
            [FixTask(id="t", kind="command", label="L", elevated=True, command=cmd)],
            "ubuntu"))
        assert out["tasks"][0]["command"] == cmd

    def test_tilde_is_not_pre_expanded(self, monkeypatch):
        """The command stays verbatim data -- the old renderer rewrote ~/$HOME
        into it at render time.

        That rewrite existed for a real reason (sudo's env_reset points HOME at
        /root), which this queue does NOT escape: command tasks are elevated, so
        they are sudo'd. The correction moved to the ENVIRONMENT -- the runner
        passes `env HOME=<the user's home>` inside the sudo (see
        test_fix_runner.py::test_elevated_task_keeps_the_invoking_users_home) --
        which is what lets the command text stay untouched here.
        """
        monkeypatch.setattr(fq, "resolve_bash", lambda: "/usr/bin/bash")
        cmd = "bash ~/.claude/scripts/env/sudoers.sh fix"
        out = json.loads(fq.render_queue(
            [FixTask(id="t", kind="command", label="L", elevated=True, command=cmd)],
            "ubuntu"))
        assert out["tasks"][0]["command"] == cmd
        assert "~" in out["tasks"][0]["command"]

    def test_bash_is_baked_in_at_write_time(self, monkeypatch):
        """An elevated console's PATH may lack Git's bin dir, so the absolute
        bash is resolved in the session that still has a working PATH."""
        monkeypatch.setattr(fq, "resolve_bash", lambda: r"C:\Program Files\Git\usr\bin\bash.exe")
        out = json.loads(fq.render_queue(
            [FixTask(id="t", kind="command", label="L", command="x")], "windows"))
        assert out["bash"] == r"C:\Program Files\Git\usr\bin\bash.exe"

    def test_unresolvable_bash_raises_when_commands_queued(self, monkeypatch):
        monkeypatch.setattr(fq, "resolve_bash", lambda: None)
        with pytest.raises(RuntimeError, match="bash was not found"):
            fq.render_queue([FixTask(id="t", kind="command", label="L", command="x")],
                            "windows")

    def test_version_is_stamped(self, monkeypatch):
        monkeypatch.setattr(fq, "resolve_bash", lambda: "/usr/bin/bash")
        out = json.loads(fq.render_queue([FixTask(id="t", kind="apt", label="L",
                                                  packages=["x"])], "ubuntu"))
        assert out["version"] == fq.QUEUE_VERSION
        assert out["os"] == "ubuntu"


class TestWriteOrClearQueue:
    @pytest.fixture(autouse=True)
    def _bash(self, monkeypatch):
        monkeypatch.setattr(fq, "resolve_bash", lambda: "/usr/bin/bash")

    def test_writes_queue_and_shim(self, tmp_path):
        path = fq.write_or_clear_queue(
            [FixTask(id="t", kind="command", label="L", elevated=True, command="x")],
            str(tmp_path), "ubuntu")
        assert path == fq.queue_path(str(tmp_path))
        assert os.path.exists(path)
        assert os.path.exists(fq.shim_path(str(tmp_path), "ubuntu"))

    def test_shim_basename_is_per_os(self):
        assert fq.shim_basename("windows") == "bootstrap-fix.bat"
        assert fq.shim_basename("ubuntu") == "bootstrap-fix.sh"

    def test_unix_shim_warns_against_running_it_under_sudo(self, tmp_path):
        """The shim must run as the user; sudo-ing the whole thing is what
        creates root-owned files in the user's home."""
        fq.write_or_clear_queue(
            [FixTask(id="t", kind="command", label="L", elevated=True, command="x")],
            str(tmp_path), "ubuntu")
        body = open(fq.shim_path(str(tmp_path), "ubuntu")).read()
        assert "Do not run this whole script under sudo" in body

    def test_windows_shim_is_crlf_not_crcrlf(self, tmp_path):
        """.bat needs CRLF; the body is authored with \\r\\n so the writer must
        not translate again."""
        fq.write_or_clear_queue(
            [FixTask(id="t", kind="command", label="L", elevated=True, command="x")],
            str(tmp_path), "windows")
        raw = open(fq.shim_path(str(tmp_path), "windows"), "rb").read()
        assert b"\r\n" in raw
        assert b"\r\r\n" not in raw

    def test_empty_queue_removes_stale_queue_and_shim(self, tmp_path):
        """This is what makes the fix-all offer disappear once the ops succeed."""
        fq.write_or_clear_queue(
            [FixTask(id="t", kind="command", label="L", command="x")],
            str(tmp_path), "ubuntu")
        assert fq.write_or_clear_queue([], str(tmp_path), "ubuntu") is None
        assert not os.path.exists(fq.queue_path(str(tmp_path)))
        assert not os.path.exists(fq.shim_path(str(tmp_path), "ubuntu"))

    def test_empty_queue_on_fresh_dir_is_a_noop(self, tmp_path):
        assert fq.write_or_clear_queue([], str(tmp_path), "ubuntu") is None

    def test_regenerates_from_current_queue(self, tmp_path):
        fq.write_or_clear_queue(
            [FixTask(id="a", kind="command", label="Old", command="old")],
            str(tmp_path), "ubuntu")
        fq.write_or_clear_queue(
            [FixTask(id="b", kind="command", label="New", command="new")],
            str(tmp_path), "ubuntu")
        body = json.load(open(fq.queue_path(str(tmp_path))))
        assert [t["label"] for t in body["tasks"]] == ["New"]

    def test_runner_path_resolves_next_to_the_module(self):
        """Not ${CLAUDE_PLUGIN_ROOT}: on a version update that still points at
        the OLD cache dir."""
        assert os.path.basename(fq.runner_path()) == "fix_runner.py"
        assert os.path.exists(fq.runner_path())


# --------------------------------------------------------------------------- #
# The aggregated item
# --------------------------------------------------------------------------- #

class TestFixQueueFailure:
    def test_windows_offers_fix_all_and_sets_the_uac_expectation(self):
        tasks = [FixTask(id="a", kind="command", label="Link starship-config"),
                 FixTask(id="b", kind="command", label="ssh-server-windows")]
        item = fq.fix_queue_failure(tasks, "windows", "C:/data")
        assert "Link starship-config, ssh-server-windows" in item["user_msg"]
        assert "fix-all" in item["user_msg"]
        assert "admin prompt" in item["user_msg"]
        assert item["labels"] == ["Link starship-config", "ssh-server-windows"]

    def test_windows_tells_claude_to_ask_rather_than_mention(self):
        """A 'type fix-all' line at session start scrolls past unread, so the
        offer only landed if the user happened to notice it. Claude is told to
        put the decision in front of them with AskUserQuestion."""
        item = fq.fix_queue_failure(
            [FixTask(id="a", kind="command", label="CUDA Toolkit")],
            "windows", "C:/data")
        agent = item["agent_msg"]
        assert "AskUserQuestion" in agent
        assert '"Do nothing"' in agent and '"Fix-all"' in agent
        # Do-nothing leads, so a reflexive Enter never triggers a UAC prompt.
        assert agent.index('"Do nothing"') < agent.index('"Fix-all"')
        assert "Do NOT run the queued commands yourself" in agent

    def test_the_question_repeats_what_the_user_was_told(self):
        """The question text and the user-facing intro are one string, so a
        reworded message can never leave the user answering a prompt that says
        something different from what they just read."""
        tasks = [FixTask(id="a", kind="command", label="CUDA Toolkit"),
                 FixTask(id="b", kind="command", label="ssh-server-windows")]
        item = fq.fix_queue_failure(tasks, "windows", "C:/data")
        intro = ("Bootstrap found issues that need admin access: "
                 "CUDA Toolkit, ssh-server-windows.")
        assert item["user_msg"].startswith(intro)
        assert f'"{intro} Fix them now?"' in item["agent_msg"]

    def test_no_ask_prompt_after_a_failed_launch(self):
        """The launch already happened and did not complete. Asking again would
        loop the UAC prompt -- the shim is the honest fallback."""
        item = fq.fix_queue_failure(
            [FixTask(id="a", kind="command", label="x")], "windows", "C:/data",
            launch_detail="declined")
        assert "AskUserQuestion" not in item["agent_msg"]

    def test_unix_offers_the_shim_not_fix_all(self):
        """Unix fix-all runs in a TTY-less subprocess, so offering fix-all there
        would promise a prompt that cannot be answered."""
        item = fq.fix_queue_failure(
            [FixTask(id="a", kind="command", label="sudoers")], "ubuntu", "/data")
        assert "bootstrap-fix.sh" in item["user_msg"]
        assert "Type 'fix-all'" not in item["user_msg"]

    def test_no_type_fixed_ritual(self):
        """The env gate re-runs every session until clean, so confirming is
        redundant ceremony."""
        item = fq.fix_queue_failure(
            [FixTask(id="a", kind="command", label="x")], "windows", "C:/data")
        assert "'fixed'" not in item["user_msg"]
        assert "'fixed'" not in item["agent_msg"]

    def test_agent_is_told_not_to_run_the_commands_itself(self):
        item = fq.fix_queue_failure(
            [FixTask(id="a", kind="command", label="x")], "windows", "C:/data")
        assert "Do NOT run" in item["agent_msg"]

    def test_launch_detail_prefixes_both_messages(self):
        item = fq.fix_queue_failure(
            [FixTask(id="a", kind="command", label="x")], "windows", "C:/data",
            launch_detail="The operation was canceled by the user")
        assert item["user_msg"].startswith("fix-all launched the fix runner but it did not complete")
        assert item["agent_msg"].startswith("fix-all launched the fix runner but it did not complete")

    def test_item_persists_across_sessions(self):
        item = fq.fix_queue_failure(
            [FixTask(id="a", kind="command", label="x")], "windows", "C:/data")
        assert item["persist_across_sessions"] is True
        assert item["type"] == "elevation_script"


# --------------------------------------------------------------------------- #
# Interactive launch
# --------------------------------------------------------------------------- #

class TestLaunchFixRunner:
    def test_unix_never_launches(self, monkeypatch):
        """No TTY in the fix-all subprocess -- a sudo/secret prompt could not be
        answered, so the honest move is not to try."""
        monkeypatch.setattr(fq.subprocess, "run",
                            lambda *a, **k: pytest.fail("must not launch on unix"))
        assert fq.launch_fix_runner("/data/queue.json", "ubuntu") is None
        assert fq.launch_fix_runner("/data/queue.json", "macos") is None

    def test_windows_runs_as_elevated_and_waits(self, monkeypatch):
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            seen["timeout"] = kw.get("timeout")
            return type("P", (), {"returncode": 0, "stderr": ""})()

        monkeypatch.setattr(fq.subprocess, "run", fake_run)
        r = fq.launch_fix_runner("C:/data/queue.json", "windows")
        assert r.launched and r.succeeded
        ps_cmd = seen["argv"][-1]
        assert "-Verb RunAs" in ps_cmd
        assert "-Wait" in ps_cmd
        assert "--engine" in ps_cmd
        assert seen["timeout"] == fq.LAUNCH_TIMEOUT

    def test_uac_decline_surfaces_as_detail_not_success(self, monkeypatch):
        monkeypatch.setattr(fq.subprocess, "run", lambda *a, **k: type(
            "P", (), {"returncode": 1,
                      "stderr": "Start-Process : The operation was canceled by the user"})())
        r = fq.launch_fix_runner("C:/data/queue.json", "windows")
        assert r.launched and not r.succeeded
        assert "canceled by the user" in r.detail

    def test_task_failure_surfaces_exit_code(self, monkeypatch):
        monkeypatch.setattr(fq.subprocess, "run", lambda *a, **k: type(
            "P", (), {"returncode": 2, "stderr": ""})())
        r = fq.launch_fix_runner("C:/data/queue.json", "windows")
        assert not r.succeeded
        assert "exit code 2" in r.detail

    def test_walkaway_is_bounded(self, monkeypatch):
        import subprocess as sp

        def boom(*a, **k):
            raise sp.TimeoutExpired(cmd="powershell", timeout=600)

        monkeypatch.setattr(fq.subprocess, "run", boom)
        r = fq.launch_fix_runner("C:/data/queue.json", "windows")
        assert r.launched and not r.succeeded
        assert "timed out" in r.detail
        # The kill reaches only the waiter, not the elevated runner, so the
        # detail must NOT read as if the work stopped -- it points at the
        # re-check as the authority on what actually completed.
        assert "may still be running" in r.detail
        assert "re-check" in r.detail

    def test_launch_failure_reports_not_launched(self, monkeypatch):
        monkeypatch.setattr(fq.subprocess, "run",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
        r = fq.launch_fix_runner("C:/data/queue.json", "windows")
        assert not r.launched

    def test_apostrophe_in_path_is_escaped_for_powershell(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(fq.subprocess, "run", lambda argv, **k: (
            seen.update(cmd=argv[-1]),
            type("P", (), {"returncode": 0, "stderr": ""})())[1])
        fq.launch_fix_runner("C:/o'brien/queue.json", "windows")
        assert "o''brien" in seen["cmd"]

    def test_argumentlist_entries_are_double_quoted_for_the_child(self, monkeypatch):
        """REGRESSION GUARD. Start-Process joins -ArgumentList elements with
        SPACES and does not quote them for the child, so a space in the
        home-derived path (C:\\Users\\John Doe\\.claude\\plugins\\...) splits the
        elevated python's argv and it exits 2 without ever running the runner --
        which out here is indistinguishable from the runner's own exit code 2
        ("a task failed"), so it would be misreported rather than diagnosed.

        The PowerShell single-quote doubling above is a DIFFERENT layer and does
        not help: it protects the PowerShell parse, not the child's command line.
        """
        seen = {}
        monkeypatch.setattr(fq.subprocess, "run", lambda argv, **k: (
            seen.update(cmd=argv[-1]),
            type("P", (), {"returncode": 0, "stderr": ""})())[1])
        monkeypatch.setattr(fq, "runner_path",
                            lambda: r"C:\Users\John Doe\.claude\fix_runner.py")
        fq.launch_fix_runner(r"C:\Users\John Doe\data\queue.json", "windows")
        cmd = seen["cmd"]
        assert '\'"C:\\Users\\John Doe\\.claude\\fix_runner.py"\'' in cmd
        assert '\'"C:\\Users\\John Doe\\data\\queue.json"\'' in cmd
        # --engine has no space and needs no quoting; it must stay a bare literal.
        assert "'--engine'" in cmd
