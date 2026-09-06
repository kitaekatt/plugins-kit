"""A Homebrew cask whose install invokes sudo routes to the elevation queue.

The defect this pins: bootstrap recognised the TTY requirement for the Homebrew
INSTALLER (queued it, asked the user to run bootstrap-fix.sh) but not for a CASK
whose payload is a signed .pkg. That install ran inline, Homebrew called
`sudo /usr/sbin/installer`, sudo died for want of a terminal, and 25+ lines of
raw brew output (caveats, licence notice, download progress) landed in the
failure message -- with an EMPTY elevate queue, so bootstrap-fix.sh was a no-op.

Four layers, in the order the work flows:
  * brew.cask_root_requirement -- ahead-of-the-attempt detection, fail-open;
  * brew.is_sudo_tty_failure   -- the after-the-fact backstop signature;
  * engine._strategy_brew      -- both routes produce a brew_cask descriptor
                                  and a short, dump-free message;
  * fix_queue / fix_runner     -- the descriptor becomes an UNELEVATED task
                                  carrying a briefing the runner prints, and
                                  holds on, BEFORE anything executes.
"""

import json
from unittest.mock import patch

import bootstrap_lib.brew as brew
import bootstrap_lib.engine as engine
import bootstrap_lib.fix_queue as fq
import bootstrap_lib.fix_runner as fr
import bootstrap_lib.path_check as path_check
import bootstrap_lib.path_repair as path_repair
import bootstrap_lib.tool_paths as tool_paths


def _info_json(artifacts, caveats=None):
    return json.dumps({"formulae": [], "casks": [
        {"token": "x", "artifacts": artifacts, "caveats": caveats}]})


def _brew_output(text, ok=True):
    """Patch brew's binary lookup + subprocess wrapper to return `text`."""
    return (patch.object(brew, "_brew_bin", lambda: "/opt/homebrew/bin/brew"),
            patch.object(brew, "_run_brew", lambda b, a, timeout=600: (ok, text)))


# --------------------------------------------------------------------------- #
# 1. Ahead-of-the-attempt detection
# --------------------------------------------------------------------------- #

class TestCaskRootRequirement:
    def test_json_stdout_is_not_contaminated_by_stderr_warning(self, monkeypatch):
        payload = _info_json([{"app": ["Thing.app"]}])
        monkeypatch.setattr(brew.sys, "platform", "darwin")
        monkeypatch.setattr(brew, "_brew_bin", lambda: "/opt/homebrew/bin/brew")
        monkeypatch.setattr(
            brew, "run_captured", lambda *args, **kwargs: (0, payload, "warning"),
            raising=False,
        )

        info = brew.cask_root_requirement("x")

        assert info.known is True
        assert info.needs_root is False

    def _call(self, payload, ok=True, cask="x"):
        p1, p2 = _brew_output(payload, ok=ok)
        with patch.object(brew.sys, "platform", "darwin"), p1, p2:
            return brew.cask_root_requirement(cask)

    def test_pkg_artifact_needs_root(self):
        info = self._call(_info_json([{"pkg": ["Thing.pkg"]}]))
        assert info.needs_root is True
        assert info.known is True
        assert "sudo /usr/sbin/installer" in info.reason

    def test_plain_app_or_binary_cask_does_not_need_root(self):
        info = self._call(_info_json([{"app": ["Thing.app"]},
                                      {"binary": ["thing"]}]))
        assert info.needs_root is False
        assert info.known is True
        assert info.reason == ""

    def test_sudo_installer_script_needs_root(self):
        info = self._call(_info_json(
            [{"installer": [{"script": {"executable": "setup", "sudo": True}}]}]))
        assert info.needs_root is True
        assert "sudo: true" in info.reason

    def test_manual_installer_does_not_need_root(self):
        info = self._call(_info_json([{"installer": [{"manual": "Thing.app"}]}]))
        assert info.needs_root is False

    def test_caveats_are_carried_through(self):
        info = self._call(_info_json([{"pkg": ["T.pkg"]}],
                                     caveats="Enable it in System Settings.\n"))
        assert info.caveats == "Enable it in System Settings."

    def test_unparseable_json_fails_open_as_unknown(self):
        info = self._call("not json at all")
        assert info.needs_root is False
        assert info.known is False

    def test_failed_query_fails_open_as_unknown(self):
        info = self._call("", ok=False)
        assert (info.needs_root, info.known) == (False, False)

    def test_non_macos_is_a_no_op(self):
        with patch.object(brew.sys, "platform", "win32"):
            assert brew.cask_root_requirement("x").known is False


# --------------------------------------------------------------------------- #
# 2. The after-the-fact signature
# --------------------------------------------------------------------------- #

class TestSudoTtyFailure:
    def test_matches_the_observed_sudo_message(self):
        assert brew.is_sudo_tty_failure(
            "==> Installing Cask thing\nsudo: a terminal is required to read "
            "the password; either use the -S option\nError: Failure while "
            "executing")

    def test_matches_no_tty_present(self):
        assert brew.is_sudo_tty_failure("sudo: no tty present and no askpass "
                                        "program specified")

    def test_ordinary_failure_does_not_match(self):
        assert not brew.is_sudo_tty_failure(
            "Error: Cask 'nope' is unavailable: No Cask with this name exists.")

    def test_empty_output_does_not_match(self):
        assert not brew.is_sudo_tty_failure("")


# --------------------------------------------------------------------------- #
# 3. Engine routing
# --------------------------------------------------------------------------- #

def _stub(monkeypatch):
    monkeypatch.setattr(path_check, "add_path_to_shell_config", lambda d: (True, "stub"))
    monkeypatch.setattr(tool_paths, "record", lambda *a, **k: None)
    monkeypatch.setattr(path_repair, "repair_path", lambda: None)
    monkeypatch.setattr(brew, "ensure_brew",
                        lambda: brew.BrewResult(True, "/opt/homebrew/bin/brew", "ok"))
    monkeypatch.setenv("PATH", "/nonexistent-for-tests")


def _entry(name="thing", cask="thing"):
    return {"name": name, "install": {"macos": {"brew": {"cask": cask}}}}


def _process(entry, action_entries):
    return engine._process_tool_entry(
        entry, "macos", "/data", "", action_entries, [], [], plugin_name="p")


# The exact brew transcript shape the observed defect dumped into the message.
_RAW_BREW_DUMP = "\n".join([
    "==> Downloading https://example.invalid/thing.zip",
    "######################################################### 100.0%",
    "==> Installing Cask thing",
    "==> Running installer for thing with sudo; the password prompt is below",
    "sudo: a terminal is required to read the password; either use the -S option",
    "Error: Failure while executing; `/usr/bin/sudo -E -- /usr/sbin/installer` exited with 1.",
] + ["==> Caveats line %d" % i for i in range(20)])


class TestEngineRoutesPkgCaskToElevation:
    def test_pkg_cask_is_queued_without_being_attempted(self, monkeypatch):
        _stub(monkeypatch)
        monkeypatch.setattr(brew, "cask_root_requirement",
                            lambda cask, timeout=120: brew.CaskInfo(
                                True, "the cask ships a signed .pkg, which Homebrew "
                                "installs with `sudo /usr/sbin/installer`",
                                "Enable the daemon in System Settings.", True))
        monkeypatch.setattr(brew, "brew_install",
                            lambda **k: (_ for _ in ()).throw(
                                AssertionError("must not attempt a root cask inline")))

        action_entries = []
        failure = _process(_entry(), action_entries)

        assert failure["install_state"] == "brew_failed"
        assert failure["elevation"]["method"] == "brew_cask"
        assert failure["elevation"]["cask"] == "thing"
        assert failure["elevation"]["caveats"] == "Enable the daemon in System Settings."
        # The message a user reads is one line, not a brew transcript.
        assert failure["message"] == "`thing` needs your password to install"
        assert len(failure["message"].splitlines()) == 1
        assert all("Downloading" not in e for e in action_entries)

    def test_plain_cask_still_installs_inline(self, monkeypatch):
        _stub(monkeypatch)
        monkeypatch.setattr(brew, "cask_root_requirement",
                            lambda cask, timeout=120: brew.CaskInfo(False, "", "", True))
        calls = []

        def fake_install(formula=None, cask=None, tap=None, timeout=600):
            calls.append(cask)
            return brew.BrewResult(True, None, f"installed {cask} via brew")

        monkeypatch.setattr(brew, "brew_install", fake_install)
        tools_installed = []
        failure = engine._process_tool_entry(
            _entry(), "macos", "/data", "", [], [], tools_installed, plugin_name="p")

        assert failure is None
        assert calls == ["thing"]
        assert fq.queue_from_failures([], "macos") == []

    def test_unknown_cask_is_attempted_inline_fail_open(self, monkeypatch):
        _stub(monkeypatch)
        monkeypatch.setattr(brew, "cask_root_requirement",
                            lambda cask, timeout=120: brew.CaskInfo(False, "", "", False))
        calls = []
        monkeypatch.setattr(brew, "brew_install",
                            lambda formula=None, cask=None, tap=None, timeout=600:
                            (calls.append(cask),
                             brew.BrewResult(True, None, "installed"))[1])
        assert engine._process_tool_entry(
            _entry(), "macos", "/data", "", [], [], [], plugin_name="p") is None
        assert calls == ["thing"]


class TestEngineBackstopsTheSudoSignature:
    def test_sudo_tty_failure_is_requeued_and_the_dump_suppressed(self, monkeypatch):
        _stub(monkeypatch)
        # Detection said "no root needed" (fail-open); the attempt proves otherwise.
        monkeypatch.setattr(brew, "cask_root_requirement",
                            lambda cask, timeout=120: brew.CaskInfo(False, "", "", False))
        monkeypatch.setattr(brew, "brew_install",
                            lambda formula=None, cask=None, tap=None, timeout=600:
                            brew.BrewResult(False, None, _RAW_BREW_DUMP))

        action_entries = []
        failure = _process(_entry(), action_entries)

        assert failure["elevation"]["method"] == "brew_cask"
        assert failure["message"] == "`thing` needs your password to install"
        for noise in ("Downloading", "100.0%", "Caveats line", "/usr/bin/sudo"):
            assert noise not in failure["message"]
            assert all(noise not in e for e in action_entries)

    def test_ordinary_brew_failure_keeps_its_message_and_is_not_queued(self, monkeypatch):
        _stub(monkeypatch)
        monkeypatch.setattr(brew, "cask_root_requirement",
                            lambda cask, timeout=120: brew.CaskInfo(False, "", "", True))
        monkeypatch.setattr(brew, "brew_install",
                            lambda formula=None, cask=None, tap=None, timeout=600:
                            brew.BrewResult(False, None, "Error: No Cask with this name exists."))
        failure = _process(_entry(), [])
        assert "elevation" not in failure
        assert "No Cask with this name exists" in failure["message"]


# --------------------------------------------------------------------------- #
# 4. Queue harvest + runner briefing
# --------------------------------------------------------------------------- #

def _cask_failure(caveats="", reason="the cask ships a signed .pkg, which "
                                     "Homebrew installs with `sudo /usr/sbin/installer`"):
    return {"elevation": {"method": "brew_cask", "os": "macos", "cask": "thing",
                          "reason": reason, "caveats": caveats}}


class TestQueueHarvest:
    def test_descriptor_becomes_an_unelevated_slow_command_task(self):
        tasks = fq.queue_from_failures([_cask_failure()], "macos")
        assert len(tasks) == 1
        task = tasks[0]
        assert task.kind == "command"
        assert task.command == "brew install --cask thing"
        # brew refuses to run as root; it elevates the one step that needs it.
        assert task.elevated is False
        assert task.cost == fq.COST_SLOW
        assert "thing" in task.label
        # Non-opportunistic, so the queue actually surfaces to the user.
        assert fq.has_actionable(tasks)

    def test_brew_installer_still_leads_the_cask(self):
        failures = [_cask_failure(),
                    {"elevation": {"method": "brew_installer", "os": "macos"}}]
        kinds = [t.kind for t in fq.queue_from_failures(failures, "macos")]
        assert kinds == ["brew_installer", "command"]

    def test_briefing_names_command_mechanism_and_why_bootstrap_could_not(self):
        task = fq.queue_from_failures([_cask_failure()], "macos")[0]
        text = "\n".join(task.explain)
        assert "brew install --cask thing" in text            # the exact command
        assert "administrator password" in text               # what needs root
        assert "sudo /usr/sbin/installer" in text             # by what mechanism
        assert "no terminal" in text and "hook" in text       # why bootstrap could not

    def test_caveats_are_surfaced_in_the_briefing(self):
        task = fq.queue_from_failures(
            [_cask_failure(caveats="Approve the daemon in System Settings > Privacy.")],
            "macos")[0]
        assert any("System Settings > Privacy" in line for line in task.explain)

    def test_explain_survives_serialization(self):
        tasks = fq.queue_from_failures([_cask_failure()], "macos")
        queue = json.loads(fq.render_queue(tasks, "macos"))
        assert queue["tasks"][0]["explain"]
        # And the runner accepts the queue it was handed.
        assert fr.validate(queue) == []

    def test_a_task_without_a_briefing_emits_no_explain_key(self):
        assert "explain" not in FixTaskNoBrief().to_json()


def FixTaskNoBrief():
    return fq.FixTask(id="x", kind="command", label="Something", command="true")


class TestRunnerBriefing:
    def _queue(self):
        tasks = fq.queue_from_failures([_cask_failure()], "macos")
        return {"version": fr.QUEUE_VERSION, "os": "macos",
                "tasks": [t.to_json() for t in tasks]}

    def test_briefing_is_printed_and_ends_with_the_abort_offer(self, capsys, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt="": prompt)
        assert fr.print_briefings(self._queue()) is True
        out = capsys.readouterr().out
        assert "brew install --cask thing" in out
        assert "Press ENTER to continue, or Ctrl-C to abort. " \
               "Nothing has run yet, and aborting changes nothing." in fr.ABORT_PROMPT

    def test_ctrl_c_aborts_before_anything_runs(self, capsys, monkeypatch):
        def boom(prompt=""):
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", boom)
        assert fr.print_briefings(self._queue()) is False
        assert "Aborted. Nothing was run." in capsys.readouterr().out

    def test_no_console_declines_rather_than_prompting_for_a_password(self, monkeypatch):
        def eof(prompt=""):
            raise EOFError

        monkeypatch.setattr("builtins.input", eof)
        assert fr.print_briefings(self._queue()) is False

    def test_a_queue_with_no_briefings_never_holds(self, monkeypatch):
        monkeypatch.setattr("builtins.input",
                            lambda prompt="": (_ for _ in ()).throw(
                                AssertionError("must not hold without a briefing")))
        queue = {"version": fr.QUEUE_VERSION, "os": "macos",
                 "tasks": [FixTaskNoBrief().to_json()]}
        assert fr.print_briefings(queue) is True


class TestDuplicateCaskDeclarations:
    """A cask declared by more than one plugin yields ONE queue task.

    p4-kit and unreal-kit both declare the `p4` cask deliberately -- each
    genuinely needs the client and neither should force the other's install --
    and plugin manifests are processed per-plugin, so an absent cask reaches
    queue_from_failures once per declaring plugin.
    """

    def _failure(self, plugin, cask="p4"):
        return {
            "type": "tool", "name": "p4", "plugin": plugin,
            "message": f"`{cask}` needs your password to install",
            "install_state": "brew_failed", "install_cmd": None,
            "elevation": {"method": "brew_cask", "os": "macos",
                          "cask": cask, "reason": "ships a signed .pkg",
                          "caveats": ""},
        }

    def test_same_cask_from_two_plugins_queues_one_task(self):
        tasks = fq.queue_from_failures(
            [self._failure("p4-kit"), self._failure("unreal-kit")], "macos")
        cask_tasks = [t for t in tasks if t.id == "brew_cask:p4"]
        assert len(cask_tasks) == 1
        assert cask_tasks[0].command == "brew install --cask p4"

    def test_the_single_task_is_briefed_once(self):
        tasks = fq.queue_from_failures(
            [self._failure("p4-kit"), self._failure("unreal-kit")], "macos")
        briefed = [t for t in tasks if t.explain]
        assert len(briefed) == 1

    def test_distinct_casks_still_queue_separately(self):
        tasks = fq.queue_from_failures(
            [self._failure("p4-kit", "p4"),
             self._failure("other-kit", "some-other-cask")], "macos")
        ids = {t.id for t in tasks}
        assert "brew_cask:p4" in ids
        assert "brew_cask:some-other-cask" in ids
