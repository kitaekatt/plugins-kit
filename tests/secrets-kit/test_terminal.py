"""Spawning a terminal window for the passphrase verbs.

The security property under test is indirect but load-bearing: the passphrase
must reach `age` through a tty and never through this process. So what these
assert is that the launcher HANDS OFF -- it composes a command for another
window and returns -- rather than ever reading a passphrase itself.
"""

import subprocess
import sys

import pytest

from secrets_kit import SecretsError
from secrets_kit import terminal as term


def test_no_terminal_available_names_the_manual_command(monkeypatch):
    """A machine with no emulator must still tell the user what to type.

    Failing silently here would strand them: the verb is not impossible on
    such a machine, it just cannot be automated.
    """
    monkeypatch.setattr(term, "_current_platform", lambda: "linux")
    monkeypatch.setattr(term, "_LINUX_TERMINALS", ())

    with pytest.raises(SecretsError) as excinfo:
        term.launch(["/opt/sk/secrets-kit", "unlock"])

    rendered = str(excinfo.value)
    assert "/opt/sk/secrets-kit" in rendered
    assert "unlock" in rendered


def test_launch_failure_is_reported_not_swallowed(monkeypatch):
    """An OSError from the spawn must surface as an actionable failure.

    A launcher that reports success on a window that never opened is worse
    than one that fails: the user waits for a prompt that will never come.
    """
    monkeypatch.setattr(term, "_current_platform", lambda: "darwin")

    def boom(*a, **kw):
        raise OSError("no osascript")

    monkeypatch.setattr(term.subprocess, "Popen", boom)

    with pytest.raises(SecretsError) as excinfo:
        term.launch(["secrets-kit", "init"])
    assert "could not open a terminal" in str(excinfo.value)


def test_posix_command_holds_the_window_open():
    """The window must outlive the verb, or its output dies with it."""
    rendered = term._hold_open_posix(["secrets-kit", "unlock"])
    assert rendered.startswith("secrets-kit unlock")
    assert "read -r _" in rendered


def test_macos_launch_reports_an_osascript_failure(monkeypatch):
    """Regression: the launcher fired osascript and returned "a new Terminal
    window" unconditionally. Only a spawn OSError was caught, so an osascript
    that started and then FAILED -- Automation permission denied, an
    AppleScript error from the nested escaping -- still reported success, and
    the user went looking for a window that never opened."""
    monkeypatch.setattr(term, "_current_platform", lambda: "darwin")

    def failing(*a, **k):
        return subprocess.CompletedProcess(
            a[0] if a else [], 1, stdout="", stderr="execution error: Not authorized (-1743)")

    monkeypatch.setattr(term.subprocess, "run", failing)

    with pytest.raises(SecretsError) as excinfo:
        term.launch(["secrets-kit", "unlock"])
    assert "could not open a Terminal window" in str(excinfo.value)
    assert "-1743" in str(excinfo.value)


def test_macos_launch_returns_success_when_osascript_succeeds(monkeypatch):
    monkeypatch.setattr(term, "_current_platform", lambda: "darwin")
    monkeypatch.setattr(
        term.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0] if a else [], 0, stdout="tab 1 of window id 1", stderr=""),
    )
    assert term.launch(["secrets-kit", "unlock"]) == "a new Terminal window"


def test_macos_launch_reports_an_osascript_timeout(monkeypatch):
    monkeypatch.setattr(term, "_current_platform", lambda: "darwin")

    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd="osascript", timeout=term._LAUNCH_TIMEOUT)

    monkeypatch.setattr(term.subprocess, "run", hang)
    with pytest.raises(SecretsError) as excinfo:
        term.launch(["secrets-kit", "unlock"])
    assert "timed out" in str(excinfo.value)


def test_posix_hold_open_avoids_the_bash_only_read_dash_p():
    """Regression: `read -p` is a bash extension. zsh reads it as "from a
    coprocess" and fails with `read: -p: no coprocess`, so the window fell
    back to a prompt instead of waiting. zsh is the macOS login-shell default
    and Terminal's `do script` runs the login shell, so this broke the
    hold-open on every stock Mac."""
    rendered = term._hold_open_posix(["secrets-kit", "unlock"])
    assert "read -r -p" not in rendered
    assert "Press Enter to close this window" in rendered


def test_posix_command_quotes_paths_with_spaces():
    """An unquoted path with a space silently becomes two arguments."""
    rendered = term._hold_open_posix(["/Application Support/sk", "unlock"])
    assert "'/Application Support/sk'" in rendered


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launcher")
def test_windows_script_survives_quotes_and_spaces(monkeypatch, tmp_path):
    """Naive quoting truncates on an embedded quote; list2cmdline does not.

    Regression: the first cut wrapped each argument in literal double quotes,
    which produced a script that ran nothing at all for such an argument.
    """
    captured = {}

    def fake_popen(args, **kw):
        captured["args"] = args
        return None

    monkeypatch.setattr(term.subprocess, "Popen", fake_popen)

    argv = [r"C:\Program Files\py\python.exe", "-c", 'print("hi")']
    term._launch_windows(argv, "secrets-kit")

    script = captured["args"][-1]
    body = open(script, encoding="utf-8").read()
    assert subprocess.list2cmdline(argv) in body
    assert "pause" in body


def _load_cli():
    """Import the CLI by path -- it lives in scripts/, not on the package."""
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[2]
        / "plugins"
        / "secrets-kit"
        / "scripts"
        / "secrets_kit_cli.py"
    )
    spec = importlib.util.spec_from_file_location("secrets_kit_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("verb", ["unlock", "init", "rotate-identity"])
def test_new_terminal_hands_off_before_touching_anything(verb, monkeypatch, capsys):
    """The flag must short-circuit ahead of config, clone and guard work.

    If the handoff happened later, a machine with no secrets.json could not
    even be unlocked -- the verb would fail on configuration before reaching
    the window that was supposed to fix it.
    """
    cli = _load_cli()
    calls = []
    monkeypatch.setattr(
        cli, "relaunch_self", lambda v, extra=None: calls.append((v, extra)) or "a window"
    )

    def explode(*a, **kw):
        raise AssertionError("handoff must precede all real work")

    monkeypatch.setattr(cli, "_require_config", explode)

    assert cli.main([verb, "--new-terminal"]) == 0
    assert calls == [(verb, [])]
    assert "window" in capsys.readouterr().out


def test_without_the_flag_the_verb_runs_inline(monkeypatch):
    """The flag is opt-in: a human at a terminal still gets the prompt here."""
    cli = _load_cli()
    monkeypatch.setattr(
        cli, "relaunch_self", lambda *a, **kw: pytest.fail("must not spawn a window")
    )
    monkeypatch.setattr(cli, "_require_config", lambda: (_ for _ in ()).throw(
        SecretsError("reached the real body")
    ))
    assert cli.main(["unlock"]) == 1


def test_relaunch_self_targets_the_real_cli():
    """The rebuilt command must point at a CLI that exists.

    relaunch_self derives the path rather than taking it, so a package move
    would otherwise fail only at the moment a user is waiting on a prompt.
    """
    from pathlib import Path

    cli = Path(term.__file__).resolve().parents[1] / ".." / "scripts" / "secrets_kit_cli.py"
    assert cli.resolve().is_file()
