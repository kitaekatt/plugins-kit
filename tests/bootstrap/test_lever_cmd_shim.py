"""Tests for hooks/sessionstart/lever-cmd-shim.sh, the cmd.exe and PowerShell
twins of the ~/.local/bin levers on Windows.

On Windows the levers are extensionless bash scripts. cmd.exe never matches an
extensionless file, and Windows PowerShell 5.1 resolves `bootstrap` to it as an
Application that it does not run as bash: `bootstrap run` returned at once,
printed nothing, and did nothing. The SessionStart hook now writes a
`<lever>.cmd` beside each lever. Both shells prefer the .cmd when the two are in
one directory. The .cmd runs the lever under Git for Windows bash by absolute
path, never through a bare `bash` lookup that can reach WSL's
C:\\Windows\\System32\\bash.exe.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_DIR = REPO_ROOT / "plugins" / "bootstrap" / "hooks" / "sessionstart"
SESSION_BOOTSTRAP = HOOK_DIR / "session-bootstrap.sh"
SHIM_LIB = HOOK_DIR / "lever-cmd-shim.sh"

IS_WINDOWS = os.name == "nt"


def _find_bash() -> str | None:
    """A POSIX bash. On Windows prefer Git Bash; never WSL's System32 launcher."""
    candidates = []
    if IS_WINDOWS:
        candidates.extend([
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\usr\bin\bash.exe",
        ])
    found = shutil.which("bash")
    if found:
        candidates.append(found)
    for c in candidates:
        if c and Path(c).exists() and "WindowsApps" not in c and "System32" not in c:
            return c
    return None


BASH = _find_bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available on this platform")

_SYSROOT = os.environ.get("SystemRoot", r"C:\Windows")
CMD_EXE = Path(_SYSROOT) / "System32" / "cmd.exe"
POWERSHELL_EXE = Path(_SYSROOT) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
needs_windows_shells = pytest.mark.skipif(
    not (IS_WINDOWS and BASH and CMD_EXE.exists() and POWERSHELL_EXE.exists()),
    reason="needs Windows with Git Bash, cmd.exe and Windows PowerShell",
)


def _lib(script: str, *args: str, path_prefix: str = "/usr/bin") -> subprocess.CompletedProcess:
    """Source the shim library in bash and run `script` with positional args."""
    prelude = f'PATH="{path_prefix}:$PATH"\n. "{SHIM_LIB.as_posix()}"\n'
    return subprocess.run(
        [BASH, "-c", prelude + script, "_", *args], capture_output=True,
    )


def _render(lever: str, bash_win: str) -> bytes:
    result = _lib('bootstrap_lever_cmd_shim "$1" "$2"', lever, bash_win)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _install(bin_dir: Path, lever: str, bash_win: str) -> subprocess.CompletedProcess:
    return _lib('bootstrap_install_lever_cmd_shim "$1" "$2" "$3"',
                str(bin_dir), lever, bash_win)


def _resolved_git_bash() -> str:
    result = _lib("bootstrap_lever_bash_win")
    assert result.returncode == 0, result.stderr
    return result.stdout.decode().strip()


class TestHookWiring:
    """Static checks that the SessionStart hook uses the library."""

    def _lever_loop(self) -> str:
        text = SESSION_BOOTSTRAP.read_text()
        match = re.search(r"^for _lever in .+?; do$(.*?)^done$", text, re.MULTILINE | re.DOTALL)
        assert match, "the lever install loop must exist"
        return match.group(1)

    def test_hook_sources_the_library(self) -> None:
        assert SHIM_LIB.is_file()
        text = SESSION_BOOTSTRAP.read_text()
        assert '. "$SCRIPT_DIR/lever-cmd-shim.sh"' in text
        assert '_LEVER_BASH_WIN="$(bootstrap_lever_bash_win)"' in text

    def test_shim_is_installed_in_the_windows_branch_of_the_lever_loop(self) -> None:
        loop = self._lever_loop()
        windows, _, unix = loop.partition("\n    else\n")
        assert 'if [[ "$OS" == MINGW* ]] || [[ "$OS" == MSYS* ]]; then' in windows
        assert ('bootstrap_install_lever_cmd_shim "$LOCAL_BIN" "$_lever" "$_LEVER_BASH_WIN"'
                in windows), "each Windows lever copy must get its .cmd twin"
        assert "bootstrap_install_lever_cmd_shim" not in unix, (
            "Unix symlinks the levers; there is no cmd.exe to shim for"
        )

    def test_install_failure_is_logged(self) -> None:
        loop = self._lever_loop()
        assert 'log_entry "levers: FAILED - could not write' in loop
        assert 'log_entry "levers: wrote' in loop


@needs_bash
class TestRender:
    BASH_WIN = r"C:\Program Files (x86)\Git\usr\bin\bash.exe"

    def test_crlf_and_ascii(self) -> None:
        body = _render("bootstrap", self.BASH_WIN)
        body.decode("ascii")
        assert body.endswith(b"\r\n")
        assert body.count(b"\n") == body.count(b"\r\n"), "every line must end CRLF"

    def test_runs_the_sibling_lever_under_the_embedded_bash(self) -> None:
        lines = _render("bootstrap", self.BASH_WIN).decode().split("\r\n")
        assert f'set "_BOOTSTRAP_BASH={self.BASH_WIN}"' in lines
        assert r'set "PATH=C:\Program Files (x86)\Git\usr\bin;%PATH%"' in lines, (
            "bash's own directory must lead PATH, as fix_runner._child_env does"
        )
        assert 'set "_BOOTSTRAP_LEVER=%~dp0bootstrap"' in lines
        run = lines.index('"%_BOOTSTRAP_BASH%" "%_BOOTSTRAP_LEVER:\\=/%" %*')
        assert lines[run + 1] == "exit /b %ERRORLEVEL%", "bash's exit code must be returned"

    def test_never_runs_a_bare_bash(self) -> None:
        lines = _render("bootstrap", self.BASH_WIN).decode().split("\r\n")
        # Lines that can execute something: not comments, not messages.
        code = [ln for ln in lines if ln
                and not re.match(r"(rem |>&2 echo )", ln, re.IGNORECASE)]
        bare = [ln for ln in code if re.search(r'(^|[\s"&|(])bash(\.exe)?(\s|$)', ln, re.IGNORECASE)]
        assert not bare, f"a bare bash lookup can resolve WSL bash: {bare}"

    def test_percent_in_the_bash_path_is_escaped(self) -> None:
        body = _render("bootstrap", r"C:\Tools 100%\Git\usr\bin\bash.exe").decode()
        assert r'set "_BOOTSTRAP_BASH=C:\Tools 100%%\Git\usr\bin\bash.exe"' in body
        assert r'set "PATH=C:\Tools 100%%\Git\usr\bin;%PATH%"' in body


@needs_bash
class TestInstall:
    BASH_A = r"C:\Program Files\Git\usr\bin\bash.exe"
    BASH_B = r"D:\PortableGit\usr\bin\bash.exe"

    def test_writes_once_then_leaves_a_current_file_alone(self, tmp_path: Path) -> None:
        first = _install(tmp_path, "bootstrap", self.BASH_A)
        assert first.returncode == 0, first.stderr
        assert first.stdout.decode().strip() == "wrote"
        dst = tmp_path / "bootstrap.cmd"
        assert dst.read_bytes() == _render("bootstrap", self.BASH_A)
        before = dst.stat().st_mtime_ns

        second = _install(tmp_path, "bootstrap", self.BASH_A)
        assert second.returncode == 0, second.stderr
        assert second.stdout == b"", "a current shim must not be rewritten"
        assert dst.stat().st_mtime_ns == before
        assert sorted(p.name for p in tmp_path.iterdir()) == ["bootstrap.cmd"], (
            "no temp file left behind, and no .ps1 twin (Restricted policy "
            "would refuse it instead of falling back to the .cmd)"
        )

    def test_replaces_a_stale_shim(self, tmp_path: Path) -> None:
        dst = tmp_path / "bootstrap.cmd"
        dst.write_bytes(b"@echo off\r\nrem shim from an older bootstrap\r\n")
        result = _install(tmp_path, "bootstrap", self.BASH_A)
        assert result.stdout.decode().strip() == "wrote"
        assert dst.read_bytes() == _render("bootstrap", self.BASH_A)

    def test_rewrites_when_the_bash_path_changes(self, tmp_path: Path) -> None:
        _install(tmp_path, "bootstrap", self.BASH_A)
        result = _install(tmp_path, "bootstrap", self.BASH_B)
        assert result.stdout.decode().strip() == "wrote"
        assert self.BASH_B.encode() in (tmp_path / "bootstrap.cmd").read_bytes()

    def test_unwritable_target_fails(self, tmp_path: Path) -> None:
        result = _install(tmp_path / "missing", "bootstrap", self.BASH_A)
        assert result.returncode != 0
        assert result.stdout == b""


@pytest.mark.skipif(not (IS_WINDOWS and BASH), reason="needs Git Bash on Windows (cygpath)")
class TestBashLookup:
    def test_resolves_git_for_windows_bash(self) -> None:
        resolved = _resolved_git_bash()
        assert re.match(r"^[A-Za-z]:\\", resolved), resolved
        assert resolved.lower().endswith("\\bash.exe"), resolved
        assert "\\system32\\" not in resolved.lower()

    def test_refuses_a_system32_bash_first_on_path(self, tmp_path: Path) -> None:
        fake = tmp_path / "Windows" / "System32"
        fake.mkdir(parents=True)
        (fake / "bash").write_text("#!/bin/sh\nexit 0\n")
        (fake / "bash").chmod(0o755)
        fake_posix = subprocess.run(
            [BASH, "-c", 'PATH="/usr/bin:$PATH"; cygpath -u "$1"', "_", str(fake)],
            capture_output=True, text=True,
        ).stdout.strip()
        result = _lib("bootstrap_lever_bash_win", path_prefix=f"{fake_posix}:/usr/bin")
        assert result.returncode != 0
        assert result.stdout == b"", "a WSL-style System32 bash must never be embedded"


@needs_windows_shells
class TestEndToEnd:
    """Run a real shim from cmd.exe and Windows PowerShell with a plain Windows
    PATH (no Git usr/bin), the environment a PowerShell user has."""

    PROBE = (
        "#!/usr/bin/env bash\n"
        'echo "uname=$(uname -s)"\n'
        'echo "sed=$(command -v sed)"\n'
        'for a in "$@"; do echo "[$a]"; done\n'
        "exit 5\n"
    )

    def _bin(self, tmp_path: Path, bash_win: str) -> Path:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "probe").write_bytes(self.PROBE.encode())
        result = _install(bin_dir, "probe", bash_win)
        assert result.returncode == 0, result.stderr
        return bin_dir

    def _env(self, bin_dir: Path) -> dict:
        env = dict(os.environ)
        for name in ("MSYSTEM", "HOME"):
            env.pop(name, None)
        env["PATH"] = os.pathsep.join([
            str(bin_dir), str(Path(_SYSROOT) / "System32"), _SYSROOT,
        ])
        return env

    def _assert_probe_ran(self, result: subprocess.CompletedProcess) -> None:
        out = result.stdout
        assert "[a]" in out and "[b c]" in out, f"arguments not forwarded:\n{out}\n{result.stderr}"
        assert re.search(r"^uname=(MSYS|MINGW)", out, re.MULTILINE), out
        assert "sed=/usr/bin/sed" in out, f"Git usr/bin must lead PATH:\n{out}"
        assert result.returncode == 5, f"exit code not forwarded: {result.returncode}"

    def test_cmd_runs_the_lever_and_forwards_args_and_exit_code(self, tmp_path: Path) -> None:
        bin_dir = self._bin(tmp_path, _resolved_git_bash())
        # A command-line STRING: an argv list would be quoted with the C
        # runtime's \" rules, which cmd.exe does not parse.
        result = subprocess.run(
            f'"{CMD_EXE}" /d /c probe a "b c"',
            capture_output=True, text=True, env=self._env(bin_dir), timeout=60,
        )
        self._assert_probe_ran(result)

    def test_powershell_prefers_the_cmd_and_runs_it(self, tmp_path: Path) -> None:
        bin_dir = self._bin(tmp_path, _resolved_git_bash())
        result = subprocess.run(
            [str(POWERSHELL_EXE), "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Restricted", "-Command",
             "Write-Output ('resolved=' + (Get-Command probe).Source); "
             "probe a 'b c'; exit $LASTEXITCODE"],
            capture_output=True, text=True, env=self._env(bin_dir), timeout=120,
        )
        assert re.search(r"^resolved=.*\\probe\.cmd$", result.stdout, re.MULTILINE), result.stdout
        self._assert_probe_ran(result)

    def test_missing_bash_is_reported_not_silent(self, tmp_path: Path) -> None:
        bin_dir = self._bin(tmp_path, str(tmp_path / "no-git" / "bash.exe"))
        result = subprocess.run(
            [str(CMD_EXE), "/d", "/c", "probe"],
            capture_output=True, text=True, env=self._env(bin_dir), timeout=60,
        )
        assert result.returncode == 127
        assert "Git for Windows bash not found" in result.stderr
