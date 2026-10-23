"""The ``llm-kit`` launchers (``bin/llm-kit``, ``bin/llm-kit.cmd``) as subprocess tests.

The real shims run, copied into a throwaway plugin tree, against fake interpreters
(stub scripts that record how they were invoked) and, for the integration cases,
against a real venv copy. The rules under test are the root CLAUDE.md "Scoped
exception: Python CLI launcher shims" for a plugin-venv shim:

* the plugin venv, from its version-independent data path, is taken first;
* then ``BOOTSTRAP_PYTHON``, only when it resolves inside the bootstrap standalone
  directory (a stranger's variable never substitutes for the venv the shim was
  written to find);
* then the PATH-name fallback;
* with ``CLAUDE_BOOTSTRAP_DATA_ROOT`` set, ONLY the redirected venv, and exit 2
  when it is missing;
* with ``CLAUDE_PLUGIN_TEST`` set, exit 2 when the shim runs from the plugin cache
  of the claudx marketplace.

The cached-copy and redirected-venv refusals, with a real venv, are also pinned for
all five shims together in ``tests/repo-scripts/test_launcher_shim_data_root_redirect.py``;
the cases here add the interpreter order, argument pass-through and the whole path
through the real entrypoint. The POSIX shim needs a bash (Git Bash on Windows); its
``.cmd`` twin needs Windows. Every test runs with HOME and USERPROFILE in a scratch
directory and never starts a real model process.

Each refusal and ordering test names the edit that turns it red.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "llm-kit"
LIB_SRC = REPO / "packages" / "katsoft-llm-kit" / "src"
IS_WINDOWS = os.name == "nt"
STANDALONE_REL = Path(".local") / "share" / "python-standalone"
_SCRUBBED = (
    "CLAUDE_PLUGIN_TEST", "CLAUDE_PLUGIN_TEST_MARKETPLACE", "CLAUDE_PLUGIN_TEST_DIRS",
    "CLAUDE_BOOTSTRAP_DATA_ROOT", "BOOTSTRAP_PYTHON", "BOOTSTRAP_PROJECT_PYTHON",
    "_BOOTSTRAP_GUARD_VENV_REEXEC", "MODEL_ENDPOINTS_REGISTRY", "OPENROUTER_API_KEY",
)


def _find_bash() -> str | None:
    """Git Bash on Windows (never the WSL launcher), else bash on PATH."""
    candidates = []
    if IS_WINDOWS:
        candidates += [
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\usr\bin\bash.exe",
        ]
    found = shutil.which("bash")
    if found:
        candidates.append(found)
    for candidate in candidates:
        if Path(candidate).exists() and "WindowsApps" not in candidate and "System32" not in candidate:
            return candidate
    return None


BASH = _find_bash()


def _bash_is_windows() -> bool:
    if BASH is None:
        return False
    out = subprocess.run([BASH, "-c", "uname -s"], capture_output=True, text=True, timeout=60).stdout
    return out.upper().startswith(("MINGW", "MSYS", "CYGWIN"))


BASH_WINDOWS = _bash_is_windows()
VENV_PYTHON_REL = Path("Scripts") / "python.exe" if BASH_WINDOWS else Path("bin") / "python"
STANDALONE_PYTHON_REL = Path("python") / "python.exe" if BASH_WINDOWS else Path("python") / "bin" / "python3"

needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available")
windows_only = pytest.mark.skipif(not IS_WINDOWS, reason=".cmd launchers run only on Windows")


def _stub(path: Path, label: str) -> Path:
    """A fake interpreter: prints ``label`` then each argument on its own line, exits 0.

    A shebang script, which Git Bash executes even when it is named python.exe.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '#!/bin/sh\nprintf "%s\\n" "' + label + '"\nfor a in "$@"; do printf "arg=%s\\n" "$a"; done\nexit 0\n',
        encoding="utf-8", newline="\n")
    path.chmod(0o755)
    return path


class Plugin:
    """The real launchers copied into a throwaway plugin tree, with its own HOME."""

    def __init__(self, tmp_path: Path, *, cache_marketplace: str | None = None) -> None:
        self.tmp = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir()
        if cache_marketplace is None:
            self.root = tmp_path / "plugin"
        else:
            self.root = self.home / ".claude" / "plugins" / "cache" / cache_marketplace / "llm-kit" / "1.0.0"
        (self.root / "bin").mkdir(parents=True)
        for name in ("llm-kit", "llm-kit.cmd", "llm_kit_entrypoint.py", "bootstrap_guard.py"):
            shutil.copy2(PLUGIN / "bin" / name, self.root / "bin" / name)
        self.redirect_root = tmp_path / "redirected-data"

    @property
    def entrypoint(self) -> Path:
        return self.root / "bin" / "llm_kit_entrypoint.py"

    def venv_dir(self, redirected: bool = False) -> Path:
        base = self.redirect_root if redirected else self.home / ".claude" / "plugins" / "data"
        return base / "plugins-kit" / "llm-kit" / ".venv"

    def venv_stub(self, label: str, redirected: bool = False) -> Path:
        return _stub(self.venv_dir(redirected) / VENV_PYTHON_REL, label)

    def standalone_stub(self, label: str, name: str = "alt/python3") -> Path:
        return _stub(self.home / STANDALONE_REL / name, label)

    def env(self, **extra: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in _SCRUBBED}
        env["HOME"] = str(self.home)
        env["USERPROFILE"] = str(self.home)
        env.update(extra)
        return env

    def posix(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        assert BASH is not None
        return subprocess.run([BASH, str(self.root / "bin" / "llm-kit"), *args], env=self.env(**env),
                              capture_output=True, text=True, timeout=120)


def _arg_lines(result: subprocess.CompletedProcess) -> list[str]:
    return [line[4:] for line in result.stdout.splitlines() if line.startswith("arg=")]


def _label(result: subprocess.CompletedProcess) -> str:
    return result.stdout.splitlines()[0] if result.stdout else ""


@pytest.fixture(scope="module")
def venv_template(tmp_path_factory) -> Path:
    """One real venv, copied per test (a venv is found by its pyvenv.cfg, not its path)."""
    root = tmp_path_factory.mktemp("llm-kit-venv-template") / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root)], check=True,
                   capture_output=True, timeout=120)
    return root


@needs_bash
class TestPosixLauncher:
    def test_the_plugin_venv_is_taken_first_and_receives_the_entrypoint_and_every_argument(self, tmp_path):
        """Edit that turns it red: point DATA_DIR at any other directory, or drop "$@" from the exec."""
        plugin = Plugin(tmp_path)
        plugin.venv_stub("VENV")
        result = plugin.posix("models", "endpoints", "--project-root", "a dir with spaces", "")
        assert result.returncode == 0, result.stderr
        assert _label(result) == "VENV"
        entry, *rest = _arg_lines(result)
        assert Path(entry).name == "llm_kit_entrypoint.py" and Path(entry).parent.name == "bin"
        assert rest == ["models", "endpoints", "--project-root", "a dir with spaces", ""]

    def test_a_valid_bootstrap_python_does_not_outrank_the_venv(self, tmp_path):
        """Edit that turns it red: test BOOTSTRAP_PYTHON before the venv in the interpreter chain."""
        plugin = Plugin(tmp_path)
        plugin.venv_stub("VENV")
        variable = plugin.standalone_stub("VARIABLE")
        result = plugin.posix("x", BOOTSTRAP_PYTHON=str(variable))
        assert _label(result) == "VENV"

    def test_bootstrap_python_is_used_only_inside_the_standalone_directory(self, tmp_path):
        """Edit that turns it red: drop the realpath-inside-standalone check around the variable."""
        plugin = Plugin(tmp_path)
        inside = plugin.standalone_stub("INSIDE")
        outside = _stub(plugin.home / "elsewhere" / "python3", "OUTSIDE")
        assert _label(plugin.posix("x", BOOTSTRAP_PYTHON=str(inside))) == "INSIDE"
        refused = plugin.posix("x", BOOTSTRAP_PYTHON=str(outside))
        assert "OUTSIDE" not in refused.stdout

    def test_the_path_fallback_is_last(self, tmp_path):
        plugin = Plugin(tmp_path)
        _stub(tmp_path / "pathbin" / "python3", "PATH-PYTHON3")
        path = os.pathsep.join([str(tmp_path / "pathbin"), os.environ.get("PATH", "")])
        result = plugin.posix("x", PATH=path)
        assert _label(result) == "PATH-PYTHON3" and _arg_lines(result)[-1] == "x"

    def test_no_interpreter_anywhere_exits_one(self, tmp_path):
        """No venv, no variable, and a PATH with nothing that is a Python."""
        plugin = Plugin(tmp_path)
        bare = tmp_path / "bare"
        bare.mkdir()
        tools = {name: shutil.which(name) for name in ("uname", "dirname", "tr", "printf", "cd", "pwd")}
        for name, found in tools.items():
            if found and name not in ("cd", "pwd", "printf"):
                # copy, not copy2: copy2 also replays st_flags, and macOS system tools carry
                # SF_RESTRICTED (/usr/bin/uname), which an unprivileged chflags on the copy refuses.
                # The stripped PATH needs only the executable bit, which copy preserves.
                shutil.copy(found, bare / Path(found).name)
        result = plugin.posix("x", PATH=str(bare))
        if result.returncode == 127:  # the stripped PATH lost a tool this host's bash needs
            pytest.skip("cannot build a Python-free PATH for this bash")
        assert result.returncode == 1 and "no Python interpreter found" in result.stderr

    def test_a_redirected_data_root_uses_only_the_redirected_venv(self, tmp_path):
        """Edit that turns it red: leave VENV_PY on the production path when the root is set."""
        plugin = Plugin(tmp_path)
        plugin.venv_stub("PRODUCTION")
        plugin.venv_stub("REDIRECTED", redirected=True)
        assert _label(plugin.posix("x", CLAUDE_BOOTSTRAP_DATA_ROOT=str(plugin.redirect_root))) == "REDIRECTED"
        assert _label(plugin.posix("x")) == "PRODUCTION"

    def test_a_missing_redirected_venv_exits_two_instead_of_using_production(self, tmp_path):
        """Edit that turns it red: restore the 'use the redirected venv only when it exists' fallthrough."""
        plugin = Plugin(tmp_path)
        plugin.venv_stub("PRODUCTION")
        plugin.redirect_root.mkdir()
        result = plugin.posix("x", CLAUDE_BOOTSTRAP_DATA_ROOT=str(plugin.redirect_root))
        assert result.returncode == 2 and result.stdout == ""
        assert "CLAUDE_BOOTSTRAP_DATA_ROOT" in result.stderr
        assert "refusing to fall back to the production venv" in result.stderr
        assert "llm-kit: " in result.stderr

    @pytest.mark.parametrize("marketplace", ["plugins-kit", "Plugins-Kit", None], ids=["owned", "other-case", "unset"])
    def test_the_cached_copy_is_refused_under_claudx(self, tmp_path, marketplace):
        """Edit that turns it red: delete the CLAUDE_PLUGIN_TEST block (the shim then runs)."""
        plugin = Plugin(tmp_path, cache_marketplace="plugins-kit")
        plugin.venv_stub("VENV")
        extra = {"CLAUDE_PLUGIN_TEST": "1"}
        if marketplace is not None:
            extra["CLAUDE_PLUGIN_TEST_MARKETPLACE"] = marketplace
        result = plugin.posix("x", **extra)
        assert result.returncode == 2 and result.stdout == ""
        assert "refusing to run the cached copy" in result.stderr and "CLAUDE_PLUGIN_TEST" in result.stderr

    def test_the_cache_is_kept_outside_claudx_and_for_another_marketplace(self, tmp_path):
        """Edit that turns it red: refuse every plugins/cache path regardless of the marketplace."""
        plugin = Plugin(tmp_path, cache_marketplace="plugins-kit")
        plugin.venv_stub("VENV")
        assert _label(plugin.posix("x")) == "VENV"
        assert _label(plugin.posix("x", CLAUDE_PLUGIN_TEST="1", CLAUDE_PLUGIN_TEST_MARKETPLACE="elsewhere")) == "VENV"

    def test_a_working_copy_runs_under_claudx_on_the_redirected_venv(self, tmp_path):
        plugin = Plugin(tmp_path)
        plugin.venv_stub("PRODUCTION")
        plugin.venv_stub("REDIRECTED", redirected=True)
        result = plugin.posix("x", CLAUDE_PLUGIN_TEST="1", CLAUDE_PLUGIN_TEST_MARKETPLACE="plugins-kit",
                              CLAUDE_BOOTSTRAP_DATA_ROOT=str(plugin.redirect_root))
        assert _label(result) == "REDIRECTED"


def _real_plugin_tree(tmp_path: Path, template: Path) -> Plugin:
    """The shim in the plugin tree's shape, plus a stray lib/ copy of the library beside bin/ (the
    real plugin has none), and a real venv under the scratch HOME."""
    plugin = Plugin(tmp_path)
    shutil.copytree(LIB_SRC, plugin.root / "lib", ignore=shutil.ignore_patterns("__pycache__"))
    venv = plugin.venv_dir()
    venv.parent.mkdir(parents=True)
    shutil.copytree(template, venv, symlinks=True)
    return plugin


@needs_bash
class TestPosixLauncherThroughTheRealEntrypoint:
    def _env(self, plugin: Plugin) -> dict[str, str]:
        # The venv is stdlib-only. bootstrap_lib comes from the repo, as the shared-lib link does in
        # production, and llm_kit from the repo's package source, standing in for the installed katsoft-llm-kit
        # package the plugin venv carries: the entrypoint adds no path of its own.
        return {"PYTHONPATH": os.pathsep.join([str(REPO / "plugins" / "bootstrap"), str(LIB_SRC)])}

    def test_a_cli_call_runs_end_to_end_and_the_exit_code_propagates(self, tmp_path, venv_template):
        plugin = _real_plugin_tree(tmp_path, venv_template)
        ok = plugin.posix("models", "endpoints", **self._env(plugin))
        assert ok.returncode == 0, ok.stderr
        assert "openrouter" in json.loads(ok.stdout)["endpoints"]
        hint = plugin.posix("set-key", **self._env(plugin))
        assert hint.returncode == 2 and "'set-key' is now 'llm-kit account set-key'" in hint.stderr

    def test_the_code_that_runs_is_the_installed_library_never_a_copy_beside_the_shim(self, tmp_path, venv_template):
        """The entrypoint puts no lib/ on sys.path: a copy beside it is ignored, the importable one runs."""
        plugin = _real_plugin_tree(tmp_path, venv_template)
        marker = plugin.root / "lib" / "llm_kit" / "cli" / "__init__.py"
        text = marker.read_text(encoding="utf-8")
        marker.write_text(text.replace('EXIT_USAGE = 2', 'EXIT_USAGE = 2\nBESIDE_THE_SHIM = True', 1),
                          encoding="utf-8", newline="\n")
        probe = plugin.root / "bin" / "probe.py"
        probe.write_text(
            "import runpy, sys\nsys.argv = [sys.argv[0]]\n"
            "ns = runpy.run_path(sys.argv[0].replace('probe.py', 'llm_kit_entrypoint.py'), run_name='probe')\n"
            "import llm_kit.cli as cli\nprint(getattr(cli, 'BESIDE_THE_SHIM', False), cli.__file__)\n",
            encoding="utf-8")
        venv_python = plugin.venv_dir() / VENV_PYTHON_REL
        done = subprocess.run([str(venv_python), str(probe)], env=plugin.env(**self._env(plugin)),
                              capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stderr
        assert done.stdout.startswith("False ")
        ran = Path(done.stdout.split(" ", 1)[1].strip()).resolve()
        assert ran.is_relative_to(LIB_SRC.resolve())
        assert not ran.is_relative_to(plugin.root.resolve())


@windows_only
class TestCmdLauncher:
    """The ``.cmd`` twin on Windows. Fake interpreters here are ``.cmd`` files; a venv one is a real venv."""

    def _shim(self, plugin: Plugin) -> list[str]:
        return ["cmd.exe", "/d", "/c", str(plugin.root / "bin" / "llm-kit.cmd")]

    def _run(self, plugin: Plugin, *args: str, **env: str) -> subprocess.CompletedProcess:
        full = plugin.env(**env)
        return subprocess.run([*self._shim(plugin), *args], env=full, capture_output=True, text=True, timeout=120)

    def test_the_venv_is_first_and_arguments_pass_through(self, tmp_path, venv_template):
        plugin = Plugin(tmp_path)
        venv = plugin.venv_dir()
        venv.parent.mkdir(parents=True)
        shutil.copytree(venv_template, venv, symlinks=True)
        plugin.entrypoint.write_text("import sys\nprint(sys.prefix)\nprint('|'.join(sys.argv[1:]))\n", encoding="utf-8")
        result = self._run(plugin, "models", "endpoints")
        assert result.returncode == 0, result.stderr
        lines = result.stdout.splitlines()
        assert Path(lines[0]).resolve() == venv.resolve() and lines[1] == "models|endpoints"

    def test_a_missing_redirected_venv_exits_two(self, tmp_path):
        plugin = Plugin(tmp_path)
        plugin.redirect_root.mkdir()
        result = self._run(plugin, "x", CLAUDE_BOOTSTRAP_DATA_ROOT=str(plugin.redirect_root))
        assert result.returncode == 2 and "refusing to fall back to the production venv" in result.stderr

    def test_the_cached_copy_is_refused_under_claudx(self, tmp_path):
        plugin = Plugin(tmp_path, cache_marketplace="plugins-kit")
        result = self._run(plugin, "x", CLAUDE_PLUGIN_TEST="1", CLAUDE_PLUGIN_TEST_MARKETPLACE="plugins-kit")
        assert result.returncode == 2 and "refusing to run the cached copy" in result.stderr

    def test_with_no_interpreter_it_exits_one(self, tmp_path):
        plugin = Plugin(tmp_path)
        env = plugin.env(PATH=os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32"))
        result = subprocess.run([*self._shim(plugin), "x"], env=env, capture_output=True, text=True, timeout=120)
        assert result.returncode == 1 and "no Python interpreter found" in result.stderr


class TestShimShape:
    """Static facts about the shipped files that the behavior tests cannot see."""

    def test_the_shims_are_ascii_and_the_posix_one_is_executable_bash_3_safe(self):
        for name in ("llm-kit", "llm-kit.cmd", "llm_kit_entrypoint.py", "bootstrap_guard.py"):
            data = (PLUGIN / "bin" / name).read_bytes()
            data.decode("ascii")
            assert b"\r" not in data, name
        text = (PLUGIN / "bin" / "llm-kit").read_text(encoding="ascii")
        assert text.startswith("#!/usr/bin/env bash\n")
        # bash 3.2 / zsh: no associative arrays, no mapfile, no ${var,,}, no read -p, no [[ -v ]].
        for forbidden in ("declare -A", "mapfile", "readarray", ",,}", "^^}", "read -p", "[[ -v"):
            assert forbidden not in text, forbidden
        if not IS_WINDOWS:
            assert os.access(PLUGIN / "bin" / "llm-kit", os.X_OK)

    def test_the_launcher_targets_the_entrypoint_that_calls_the_console_main(self):
        entry = (PLUGIN / "bin" / "llm_kit_entrypoint.py").read_text(encoding="ascii")
        assert "from llm_kit.cli import main" in entry and "raise SystemExit(_run())" in entry
        assert "import sys" not in entry and ".insert(" not in entry and "Path(" not in entry
        # The console script is the katsoft-llm-kit distribution's; the plugin project installs nothing.
        assert "[project.scripts]" not in (PLUGIN / "pyproject.toml").read_text(encoding="ascii")
        assert 'llm-kit = "llm_kit.cli:main"' in (REPO / "packages" / "katsoft-llm-kit" / "pyproject.toml").read_text(encoding="ascii")
        for shim in ("llm-kit", "llm-kit.cmd"):
            assert "llm_kit_entrypoint.py" in (PLUGIN / "bin" / shim).read_text(encoding="ascii")
