"""Tests for session-bootstrap.sh's interpreter prelude.

The prelude appends ``BOOTSTRAP_PYTHON`` and ``BOOTSTRAP_PROJECT_PYTHON`` to
the session's ``$CLAUDE_ENV_FILE`` before either skip gate, so a
gate-skipped session still gets both names (contract: interface-v3 section
4; resolution order: bootstrap_lib/interpreter_env.py).

Harness. The hook is COPIED into a scaffolded plugin root under tmp_path and
run with:

- a temporary HOME holding an executable fake at each deterministic
  interpreter path. The fake also stands in for the engine: invoked with
  anything but ``-c`` it records its cwd and argv;
- ``CLAUDE_BOOTSTRAP_DATA_ROOT`` under tmp_path, so no real data dir is read
  or written;
- a fake ``uname`` that reports Linux, so a full pass never enters the
  Windows branch that writes the User PATH registry value. As a second
  barrier, PowerShell directories are dropped from PATH and SYSTEMROOT points
  at a missing directory.

Most tests seed the Layer-1 guard with the session id, so the hook exits
right after the prelude and never provisions. Only the tests that need a
full pass (N1, T12f, T12i, the engine-argument and console tests) run
``_provision``, and each waits for the fake engine's record before it
asserts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "plugins" / "bootstrap" / "hooks" / "sessionstart" / "session-bootstrap.sh"
RESOLVER = REPO_ROOT / "plugins" / "bootstrap" / "shell" / "project-python.sh"

MKT = "mkt"
SID = "sess-u9-prelude"
HOOK_JSON = json.dumps({"session_id": SID, "hook_event_name": "SessionStart", "source": "startup"})
PRELUDE_START = "# --- Interpreter names for this session"
PRELUDE_END = "# --- Per-session marker"

# Inherited names that would change what the prelude resolves.
SCRUB = (
    "VIRTUAL_ENV", "BOOTSTRAP_PYTHON", "BOOTSTRAP_PROJECT_PYTHON", "UV_PROJECT_ENVIRONMENT",
    "CLAUDE_ENV_FILE", "CLAUDE_PLUGIN_TEST", "OSTYPE", "BOOTSTRAP_PP_NO_REGISTER",
    "PROMPT_COMMAND", "CLAUDE_BOOTSTRAP_DATA_ROOT",
)


def _find_bash() -> str | None:
    """A POSIX bash; on Windows, Git Bash rather than the WSL launcher."""
    candidates = []
    if os.name == "nt":
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


def _bash_ostype() -> str:
    if BASH is None:
        return ""
    env = {k: v for k, v in os.environ.items() if k != "OSTYPE"}
    out = subprocess.run([BASH, "-c", 'printf %s "$OSTYPE"'], capture_output=True,
                         text=True, env=env, timeout=60)
    return out.stdout


OSTYPE = _bash_ostype()
WINDOWS_BASH = OSTYPE.startswith(("msys", "cygwin", "win32"))
STD_REL = (".local/share/python-standalone/python/python.exe" if WINDOWS_BASH
           else ".local/bin/python3")
VENV_REL = "Scripts/python.exe" if WINDOWS_BASH else "bin/python"


def _prelude_text() -> str:
    text = HOOK.read_text(encoding="utf-8")
    start = text.index(PRELUDE_START)
    return text[start:text.index(PRELUDE_END, start)]


def _write_exe(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8", newline="\n")
    path.chmod(0o755)
    return path


def _fake_python(path: Path, record: Path) -> Path:
    rec = record.as_posix()
    return _write_exe(path, (
        'case "${1:-}" in -c) exit 0 ;; esac\n'
        f'{{ printf "%s\\n" "$PWD"; printf "%s\\n" "$@"; }} > "{rec}.part.$$"\n'
        f'mv -f "{rec}.part.$$" "{rec}"\n'
    ))


@dataclass
class Scaffold:
    root: Path
    plugin_root: Path
    hook: Path
    resolver: Path
    home: Path
    fakebin: Path
    data_root: Path
    plugin_data: Path
    project: Path
    env_file: Path
    engine_record: Path

    @property
    def std(self) -> str:
        """The value the prelude must write for BOOTSTRAP_PYTHON."""
        return f"{self.home.as_posix()}/{STD_REL}"

    def seed_guard(self) -> None:
        self.plugin_data.mkdir(parents=True, exist_ok=True)
        (self.plugin_data / "last_session_id").write_text(SID, encoding="utf-8")

    def write_record(self, key: str, content: str) -> Path:
        path = self.plugin_data / "project_python" / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
        return path

    def exports(self, name: str) -> list[str]:
        if not self.env_file.exists():
            return []
        prefix = f"export {name}="
        return [line[len(prefix):] for line in self.env_file.read_text(encoding="utf-8").splitlines()
                if line.startswith(prefix)]

    def fake_exe(self, rel: str) -> Path:
        return _write_exe(self.root / rel, "exit 0\n")


def _scaffold(tmp_path: Path, *, resolver_text: str | None = None,
              home_name: str = "home") -> Scaffold:
    plugin_root = tmp_path / MKT / "bootstrap" / "0.0.0"
    hook = plugin_root / "hooks" / "sessionstart" / "session-bootstrap.sh"
    hook.parent.mkdir(parents=True)
    hook.write_text(HOOK.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    resolver = plugin_root / "shell" / "project-python.sh"
    resolver.parent.mkdir(parents=True)
    resolver.write_text(resolver_text if resolver_text is not None
                        else RESOLVER.read_text(encoding="utf-8"),
                        encoding="utf-8", newline="\n")
    home = tmp_path / home_name
    engine_record = tmp_path / "engine_args"
    for rel in (".local/share/python-standalone/python/python.exe", ".local/bin/python3"):
        _fake_python(home / rel, engine_record)
    fakebin = tmp_path / "fakebin"
    _write_exe(fakebin / "uname", 'case "${1:-}" in -m) echo x86_64 ;; *) echo Linux ;; esac\n')
    data_root = tmp_path / "data"
    project = tmp_path / "proj"
    project.mkdir()
    env_file = tmp_path / "session-env" / "sessionstart-hook-0.sh"
    env_file.parent.mkdir()
    return Scaffold(tmp_path, plugin_root, hook, resolver, home, fakebin, data_root,
                    data_root / MKT / "bootstrap", project, env_file, engine_record)


def _msys(path: Path) -> str:
    """HOME as Git Bash presents it (/c/Users/you); POSIX paths unchanged.

    Passing the /c/ form also keeps Git Bash from rewriting a HOME under
    %TEMP% (where pytest's tmp_path lives) to its /tmp mount.
    """
    p = path.as_posix()
    if WINDOWS_BASH and re.match(r"[A-Za-z]:/", p):
        return f"/{p[0].lower()}{p[2:]}"
    return p


def _env(s: Scaffold, *, env_file: bool = True, extra_bin: Path | None = None,
         **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in SCRUB}
    env["HOME"] = _msys(s.home)
    env["CLAUDE_BOOTSTRAP_DATA_ROOT"] = s.data_root.as_posix()
    if env_file:
        env["CLAUDE_ENV_FILE"] = str(s.env_file)
    rest = [p for p in env.get("PATH", "").split(os.pathsep)
            if p and "powershell" not in p.lower()]
    front = [str(s.fakebin)] + ([str(extra_bin)] if extra_bin else [])
    front.append(str(Path(BASH).parent))
    env["PATH"] = os.pathsep.join(front + rest)
    if os.name == "nt":
        env["SYSTEMROOT"] = str(s.root / "no-systemroot")
    env.update(extra)
    return env


def _run(s: Scaffold, *args: str, env: dict[str, str] | None = None,
         stdin: str = HOOK_JSON) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, str(s.hook), *args], input=stdin, capture_output=True, text=True,
        env=env if env is not None else _env(s), cwd=s.project, timeout=120,
    )


def _wait_for(path: Path, timeout: float = 90.0) -> bool:
    """Poll for a file that SHOULD appear (see test_sessionstart_rescue)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def _engine_args(s: Scaffold) -> list[str]:
    assert _wait_for(s.engine_record), "the pass never launched the (fake) engine"
    return s.engine_record.read_text(encoding="utf-8").splitlines()[1:]


def _arg(args: list[str], flag: str) -> str:
    assert flag in args, f"{flag} missing from engine argv {args}"
    return args[args.index(flag) + 1]


def _hook_key(s: Scaffold) -> str:
    """The hook's _PROJECT_KEY: sha1 of $PWD exactly as bash spells it."""
    out = subprocess.run([BASH, "-c", 'printf %s "$PWD"'], capture_output=True, text=True,
                         env=_env(s), cwd=s.project, timeout=60)
    return hashlib.sha1(out.stdout.encode("utf-8")).hexdigest()


def _sentinel_resolver(target: Path, seen: Path) -> str:
    """A resolver that records its inputs and prints ``target``."""
    return (
        "bootstrap_resolve_project_python() {\n"
        f'    printf "%s\\n%s\\n%s\\n" "$1" "${{BOOTSTRAP_PP_NO_REGISTER:-}}" '
        f'"${{BOOTSTRAP_PYTHON:-}}" > "{seen.as_posix()}"\n'
        f'    printf "%s\\n" "{target.as_posix()}"\n'
        "}\n"
    )


def _q(value: str) -> str:
    return f"'{value}'"


class TestPreludeStatic:
    """Text-level contract; cheap and platform-independent."""

    def test_block_sits_after_the_project_key_and_before_layer1(self):
        text = HOOK.read_text(encoding="utf-8")
        start = text.index(PRELUDE_START)
        assert text.index('_COOLDOWN_FILE="$_COOLDOWN_DIR/last_run_epoch.$_PROJECT_KEY"') < start
        assert start < text.index('cat "$_GUARD_FILE"'), "prelude must precede the Layer-1 gate"
        assert start < text.index('if [ -f "$_COOLDOWN_FILE" ]'), "prelude must precede Layer 2"

    def test_bash32_and_fork_discipline(self):
        # T12e. macOS /bin/bash is 3.2 and the rc templates are also read by
        # zsh; none of these constructs exist there or behave the same.
        block = _prelude_text()
        code = "\n".join(line for line in block.splitlines()
                         if not line.lstrip().startswith("#"))
        forbidden = {
            r"\$\{[A-Za-z_]+\^": "case modification (bash 4)",
            r"\$\{[A-Za-z_]+,": "case modification (bash 4)",
            r"\bdeclare\s+-[aAn]": "declare -A/-a/-n",
            r"\b(mapfile|readarray|coproc)\b": "bash 4 builtin",
            r"\bread\b[^\n]*\s-[pa]\b": "read -p/-a",
            r"\blocal\s+-n\b": "nameref",
            r"\|&|&>>|;;&|;&": "bash 4 operator",
            r"@[QEPAa]\}": "parameter transformation",
            r"\[\[\s+-v\b": "[[ -v ]]",
            r"\b[A-Za-z_]+=\(": "array assignment",
            r"\bcygpath\b": "cygpath fork",
            r"\$\{?OS\b(?!TYPE)": "$OS (only set inside _provision)",
            r"`": "backtick substitution",
            r"\bEPOCHREALTIME\b|\bprintf\s+-v\b": "bash 4+ feature",
        }
        for pattern, why in forbidden.items():
            assert not re.search(pattern, code), f"prelude uses {why}: {pattern}"
        # Exactly one fork: the resolver subshell, stderr-silenced inside it.
        subs = re.findall(r"\$\(", code)
        assert len(subs) == 1, f"prelude must fork once (the resolver), found {len(subs)}"
        line = next(l for l in code.splitlines() if "$(" in l)
        assert "exec 2>/dev/null" in line and "BOOTSTRAP_PP_NO_REGISTER=1" in line
        assert "while IFS= read -r" in code, "the env-file scan must be a read loop"

    def test_hook_parses(self):
        if BASH is None:
            pytest.skip("bash not available")
        out = subprocess.run([BASH, "-n", str(HOOK)], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr

    def test_engine_launches_carry_the_project_key(self):
        text = HOOK.read_text(encoding="utf-8")
        assert text.count('--project-key "$_PROJECT_KEY"') == 2, (
            "both the background launch and the console exec pass --project-key"
        )


@needs_bash
class TestNormalize:
    """_ie_norm on its own, both OS branches, on any host with bash."""

    CASES = {
        "msys": [("/c/Users/you", "C:/Users/you"), ("/C/x", "C:/x"), ("/d", "D:"),
                 ("C:\\Users\\you\\.venv", "C:/Users/you/.venv"), ("/usr/bin", "/usr/bin"),
                 ("/cd/x", "/cd/x"), ("D:/a", "D:/a")],
        "linux-gnu": [("/c/Users/you", "/c/Users/you"), ("/home/you", "/home/you")],
    }

    @pytest.mark.parametrize("ostype", sorted(CASES))
    def test_forms(self, ostype, tmp_path):
        text = HOOK.read_text(encoding="utf-8")
        start = text.index("_ie_norm() {")
        func = text[start:text.index("\n}\n", start) + 3]
        body = "set -u\n" + func + "".join(
            f"_ie_norm '{src}'; printf '%s\\n' \"$_ie_n\"\n" for src, _ in self.CASES[ostype])
        # A script FILE, not `bash -c`: the Windows command line strips the
        # backslashes this function exists to handle.
        script = tmp_path / "norm.sh"
        script.write_text(body, encoding="utf-8", newline="\n")
        env = dict(os.environ, OSTYPE=ostype)
        out = subprocess.run([BASH, str(script)], capture_output=True, text=True, env=env, timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.splitlines() == [want for _, want in self.CASES[ostype]]


@needs_bash
class TestPreludeBehavior:

    def test_t12a_written_when_layer1_guard_matches(self, tmp_path):
        s = _scaffold(tmp_path)
        s.seed_guard()
        result = _run(s)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "", "the Layer-1 skip emits nothing"
        assert not (s.plugin_data / "cooldowns" / f"last_run_epoch.{_hook_key(s)}").exists(), (
            "the gate skipped, so no pass may have been stamped"
        )
        assert s.exports("BOOTSTRAP_PYTHON") == [_q(s.std)]
        assert s.exports("BOOTSTRAP_PROJECT_PYTHON") == [_q(s.std)]
        assert not s.engine_record.exists()

    def test_t12b_virtual_env_beats_record_and_resolver(self, tmp_path):
        seen = tmp_path / "resolver_seen"
        s = _scaffold(tmp_path, resolver_text=_sentinel_resolver(tmp_path / "resolved" / "python", seen))
        s.fake_exe("resolved/python")
        venv_py = s.fake_exe(f"venv/{VENV_REL}")
        rec_py = s.fake_exe("recpy/python")
        s.write_record(_hook_key(s), rec_py.as_posix() + "\n")
        s.seed_guard()
        result = _run(s, env=_env(s, VIRTUAL_ENV=str(tmp_path / "venv")))
        assert result.returncode == 0, result.stderr
        assert s.exports("BOOTSTRAP_PROJECT_PYTHON") == [_q(venv_py.as_posix())]
        assert not seen.exists(), "the resolver must not run when VIRTUAL_ENV resolved"

    def test_t12b_virtual_env_without_interpreter_is_skipped(self, tmp_path):
        s = _scaffold(tmp_path)
        rec_py = s.fake_exe("recpy/python")
        s.write_record(_hook_key(s), rec_py.as_posix() + "\n")
        (tmp_path / "emptyvenv").mkdir()
        s.seed_guard()
        result = _run(s, env=_env(s, VIRTUAL_ENV=str(tmp_path / "emptyvenv")))
        assert result.returncode == 0, result.stderr
        assert s.exports("BOOTSTRAP_PROJECT_PYTHON") == [_q(rec_py.as_posix())]

    def test_t12b_record_beats_resolver(self, tmp_path):
        seen = tmp_path / "resolver_seen"
        s = _scaffold(tmp_path, resolver_text=_sentinel_resolver(tmp_path / "resolved" / "python", seen))
        s.fake_exe("resolved/python")
        rec_py = s.fake_exe("recpy/python")
        s.write_record(_hook_key(s), rec_py.as_posix() + "\n")
        s.seed_guard()
        result = _run(s)
        assert result.returncode == 0, result.stderr
        assert s.exports("BOOTSTRAP_PROJECT_PYTHON") == [_q(rec_py.as_posix())]
        assert not seen.exists(), "the resolver must not run when the record resolved"

    def test_t12b_resolver_last_with_its_inputs(self, tmp_path):
        seen = tmp_path / "resolver_seen"
        target = tmp_path / "resolved" / "python"
        s = _scaffold(tmp_path, resolver_text=_sentinel_resolver(target, seen))
        s.fake_exe("resolved/python")
        s.seed_guard()
        result = _run(s)
        assert result.returncode == 0, result.stderr
        assert s.exports("BOOTSTRAP_PROJECT_PYTHON") == [_q(target.as_posix())]
        start_dir, no_register, engine = seen.read_text(encoding="utf-8").splitlines()
        assert hashlib.sha1(start_dir.encode("utf-8")).hexdigest() == _hook_key(s), (
            "the resolver starts from the hook's $PWD"
        )
        assert no_register == "1"
        assert engine == s.std

    def test_resolver_is_sourced_in_a_subshell_with_stderr_discarded(self, tmp_path):
        # A resolver that exits at source time would end the hook itself if it
        # were sourced in the main shell.
        s = _scaffold(tmp_path, resolver_text="echo resolver-noise >&2\nexit 3\n")
        s.seed_guard()
        result = _run(s)
        assert result.returncode == 0, result.stderr
        assert "resolver-noise" not in result.stderr
        assert s.exports("BOOTSTRAP_PROJECT_PYTHON") == [_q(s.std)]

    def test_resolver_output_that_is_not_an_interpreter_falls_back(self, tmp_path):
        s = _scaffold(tmp_path, resolver_text=_sentinel_resolver(tmp_path / "missing" / "python",
                                                                 tmp_path / "seen"))
        s.seed_guard()
        result = _run(s)
        assert result.returncode == 0, result.stderr
        assert s.exports("BOOTSTRAP_PROJECT_PYTHON") == [_q(s.std)]

    @pytest.mark.parametrize("ending", ["\n", "", "\r\n"], ids=["lf", "no-newline", "crlf"])
    def test_t12h_record_honoured_under_hash_key(self, tmp_path, ending):
        s = _scaffold(tmp_path)
        rec_py = s.fake_exe("recpy/python")
        s.write_record(_hook_key(s), rec_py.as_posix() + ending)
        s.seed_guard()
        result = _run(s)
        assert result.returncode == 0, result.stderr
        assert s.exports("BOOTSTRAP_PROJECT_PYTHON") == [_q(rec_py.as_posix())]

    def test_t12h_record_naming_a_missing_file_is_ignored(self, tmp_path):
        s = _scaffold(tmp_path)
        s.write_record(_hook_key(s), (tmp_path / "gone" / "python").as_posix() + "\n")
        s.seed_guard()
        result = _run(s)
        assert result.returncode == 0, result.stderr
        assert s.exports("BOOTSTRAP_PROJECT_PYTHON") == [_q(s.std)]

    def test_t12i_record_ignored_under_global_key(self, tmp_path):
        s = _scaffold(tmp_path)
        nohash = tmp_path / "nohash"
        for tool in ("sha1sum", "shasum"):
            _write_exe(nohash / tool, "cat >/dev/null\n")
        rec_py = s.fake_exe("recpy/python")
        s.write_record("_global_", rec_py.as_posix() + "\n")
        result = _run(s, env=_env(s, extra_bin=nohash))
        assert result.returncode == 0, result.stderr
        assert _arg(_engine_args(s), "--project-key") == "_global_", (
            "the hash tools were shadowed, so the key must be _global_"
        )
        assert s.exports("BOOTSTRAP_PROJECT_PYTHON") == [_q(s.std)]

    def test_opt_out_record_suppresses_the_project_name(self, tmp_path):
        s = _scaffold(tmp_path)
        s.fake_exe(f"venv/{VENV_REL}")
        s.write_record(_hook_key(s), "opt_out\n")
        s.seed_guard()
        result = _run(s, env=_env(s, VIRTUAL_ENV=str(tmp_path / "venv")))
        assert result.returncode == 0, result.stderr
        assert s.exports("BOOTSTRAP_PYTHON") == [_q(s.std)]
        assert s.exports("BOOTSTRAP_PROJECT_PYTHON") == [], (
            "an opted-out project gets no project name, VIRTUAL_ENV included"
        )

    def test_t12c_appends_and_never_truncates(self, tmp_path):
        s = _scaffold(tmp_path)
        original = "export OTHER_TOOL='kept'\n# a comment another writer left\n"
        s.env_file.write_text(original, encoding="utf-8", newline="\n")
        s.seed_guard()
        result = _run(s)
        assert result.returncode == 0, result.stderr
        text = s.env_file.read_text(encoding="utf-8")
        assert text.startswith(original)
        assert text[len(original):] == (
            f"export BOOTSTRAP_PYTHON={_q(s.std)}\nexport BOOTSTRAP_PROJECT_PYTHON={_q(s.std)}\n"
        )

    def test_t12c_last_line_without_newline_stays_intact(self, tmp_path):
        s = _scaffold(tmp_path)
        s.env_file.write_text("export OTHER_TOOL='kept'", encoding="utf-8", newline="\n")
        s.seed_guard()
        result = _run(s)
        assert result.returncode == 0, result.stderr
        lines = s.env_file.read_text(encoding="utf-8").splitlines()
        assert lines == ["export OTHER_TOOL='kept'",
                         f"export BOOTSTRAP_PYTHON={_q(s.std)}",
                         f"export BOOTSTRAP_PROJECT_PYTHON={_q(s.std)}"]
        sourced = subprocess.run(
            [BASH, "-c", '. "$1" && printf "%s|%s" "$OTHER_TOOL" "$BOOTSTRAP_PYTHON"', "_",
             s.env_file.as_posix()], capture_output=True, text=True, timeout=60)
        assert sourced.stdout == f"kept|{s.std}", sourced.stderr

    def test_t12d_quote_in_home_writes_nothing(self, tmp_path):
        s = _scaffold(tmp_path, home_name="ho'me")
        s.seed_guard()
        result = _run(s)
        assert result.returncode == 0, result.stderr
        assert not s.env_file.exists() or s.env_file.read_text(encoding="utf-8") == ""

    def test_t12d_quote_in_virtual_env_writes_nothing(self, tmp_path):
        s = _scaffold(tmp_path)
        s.fake_exe(f"ve'nv/{VENV_REL}")
        s.seed_guard()
        result = _run(s, env=_env(s, VIRTUAL_ENV=str(tmp_path / "ve'nv")))
        assert result.returncode == 0, result.stderr
        assert not s.env_file.exists() or s.env_file.read_text(encoding="utf-8") == ""

    def test_t12g_engine_verified_project_value_is_kept(self, tmp_path):
        s = _scaffold(tmp_path)
        s.fake_exe(f"venv/{VENV_REL}")
        s.env_file.write_text("export BOOTSTRAP_PROJECT_PYTHON='/verified/python'\n",
                              encoding="utf-8", newline="\n")
        s.seed_guard()
        result = _run(s, env=_env(s, VIRTUAL_ENV=str(tmp_path / "venv")))
        assert result.returncode == 0, result.stderr
        assert s.exports("BOOTSTRAP_PROJECT_PYTHON") == ["'/verified/python'"]
        assert s.exports("BOOTSTRAP_PYTHON") == [_q(s.std)]

    def test_t12g_engine_verified_engine_value_is_kept(self, tmp_path):
        s = _scaffold(tmp_path)
        s.env_file.write_text("export BOOTSTRAP_PYTHON='/engine/python'\n",
                              encoding="utf-8", newline="\n")
        s.seed_guard()
        result = _run(s)
        assert result.returncode == 0, result.stderr
        assert s.exports("BOOTSTRAP_PYTHON") == ["'/engine/python'"]
        assert s.exports("BOOTSTRAP_PROJECT_PYTHON") == [_q(s.std)]

    def test_no_env_file_or_no_interpreter_writes_nothing(self, tmp_path):
        s = _scaffold(tmp_path)
        s.seed_guard()
        result = _run(s, env=_env(s, env_file=False))
        assert result.returncode == 0, result.stderr
        (s.home / STD_REL).unlink()
        result = _run(s)
        assert result.returncode == 0, result.stderr
        assert not s.env_file.exists(), "no env file named, then no interpreter: nothing to write"

    def test_t12f_three_runs_one_line_per_name(self, tmp_path):
        s = _scaffold(tmp_path)
        first = _run(s)
        assert first.returncode == 0, first.stderr
        _engine_args(s)  # the first run is a full pass; let it launch before rerunning
        for _ in range(2):
            again = _run(s)
            assert again.returncode == 0, again.stderr
            assert again.stdout == "", "same session id: the Layer-1 gate skips"
        assert s.exports("BOOTSTRAP_PYTHON") == [_q(s.std)]
        assert s.exports("BOOTSTRAP_PROJECT_PYTHON") == [_q(s.std)]

    def test_n1_project_var_preseeded_engine_var_absent(self, tmp_path):
        # N1: under `set -u`, every prelude variable must be initialised on
        # the path where the inherited environment carries only the project
        # name, no VIRTUAL_ENV, and no record.
        s = _scaffold(tmp_path)
        env = _env(s, BOOTSTRAP_PROJECT_PYTHON="/inherited/python")
        assert "BOOTSTRAP_PYTHON" not in env and "VIRTUAL_ENV" not in env
        result = _run(s, env=env)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == '{"continue": true, "suppressOutput": true}'
        assert "unbound variable" not in result.stderr
        _engine_args(s)
        pending = s.plugin_data / "bootstrap_display.pending"
        assert not pending.exists() or "shell error" not in pending.read_text(encoding="utf-8")
        assert s.exports("BOOTSTRAP_PYTHON") == [_q(s.std)]
        assert s.exports("BOOTSTRAP_PROJECT_PYTHON") == [_q(s.std)]

    def test_background_engine_gets_the_hooks_project_key(self, tmp_path):
        s = _scaffold(tmp_path)
        result = _run(s)
        assert result.returncode == 0, result.stderr
        args = _engine_args(s)
        assert "--background" in args
        project_dir = _arg(args, "--project-dir")
        assert _arg(args, "--project-key") == hashlib.sha1(project_dir.encode("utf-8")).hexdigest()

    def test_console_mode_skips_the_prelude(self, tmp_path):
        s = _scaffold(tmp_path)
        result = _run(s, "--console", stdin="")
        assert result.returncode == 0, result.stderr
        args = _engine_args(s)
        assert "--console" in args
        assert _arg(args, "--project-key") == hashlib.sha1(
            _arg(args, "--project-dir").encode("utf-8")).hexdigest()
        assert not s.env_file.exists(), "a console run has no session env file to write"

    @pytest.mark.skipif(not WINDOWS_BASH, reason="MSYS drive form exists only under Windows bash")
    def test_windows_values_use_drive_letter_form(self, tmp_path):
        s = _scaffold(tmp_path)
        venv_py = s.fake_exe(f"venv/{VENV_REL}")
        s.seed_guard()
        result = _run(s, env=_env(s, VIRTUAL_ENV=_msys(tmp_path / "venv")))
        assert result.returncode == 0, result.stderr
        # Git Bash hands the hook HOME as /c/...; both values must come out C:/...
        assert s.exports("BOOTSTRAP_PYTHON") == [_q(s.std)]
        assert re.match(r"'[A-Z]:/", s.exports("BOOTSTRAP_PYTHON")[0])
        assert s.exports("BOOTSTRAP_PROJECT_PYTHON") == [_q(venv_py.as_posix())]
