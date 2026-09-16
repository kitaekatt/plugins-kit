"""Tests for bootstrap's terminal shell integration (interface-v3, section 5).

Three artifacts are pinned here:

* ``shell/project-python.sh`` -- the bash/zsh resolver and prompt hook, run
  under a real bash (Git Bash on Windows). zsh is not installed on the
  development host or on CI Ubuntu, so the zsh branch is checked STATICALLY
  only (``test_template_is_bash32_and_zsh_safe``); its zsh-only syntax is
  reached through ``eval`` so bash never parses it.
* ``shell/project-python.ps1`` -- the PowerShell twin, run when
  ``powershell``/``pwsh`` resolves.
* ``bootstrap_lib.shell_hook.ensure`` -- rc-file, PowerShell-profile, and
  template convergence, always with injected home/Documents paths.

Isolation: every subprocess runs with HOME, USERPROFILE, HOMEDRIVE/HOMEPATH,
APPDATA, LOCALAPPDATA, TEMP, and TMP pointed inside the test's tmp directory,
and with bytecode writes disabled; ``ensure`` only ever sees tmp paths. The
tests never source a template against the developer's real home.

Premise P25 (a forward-slash absolute interpreter path runs as a quoted
command under cmd.exe and PowerShell) is exercised by the two ``test_p25_*``
tests on Windows.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from bootstrap_lib import shell_hook
from bootstrap_lib.interpreter_env import ISOLATION_ENV

REPO_ROOT = Path(__file__).resolve().parents[2]
SHELL_DIR = REPO_ROOT / "plugins" / "bootstrap" / "shell"
SH_TEMPLATE = SHELL_DIR / "project-python.sh"
PS_TEMPLATE = SHELL_DIR / "project-python.ps1"
WINDOWS = os.name == "nt"
MKT = "test-mkt"

EXPECTED_RC_LINE = (
    '[ -f "$HOME/.claude/plugins/data/test-mkt/bootstrap/shell/project-python.sh" ]'
    ' && . "$HOME/.claude/plugins/data/test-mkt/bootstrap/shell/project-python.sh"'
    "  # Added by bootstrap (project-python)"
)
EXPECTED_PROFILE_LINE = (
    'if (Test-Path "$HOME\\.claude\\plugins\\data\\test-mkt\\bootstrap\\shell\\project-python.ps1")'
    ' { . "$HOME\\.claude\\plugins\\data\\test-mkt\\bootstrap\\shell\\project-python.ps1" }'
    "  # Added by bootstrap (project-python)"
)


# ---------------------------------------------------------------- helpers


def _find_bash() -> str | None:
    """Git Bash on Windows (never WSL's System32 bash); any bash elsewhere."""
    candidates = []
    if WINDOWS:
        candidates += [
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files\Git\bin\bash.exe",
        ]
    found = shutil.which("bash")
    if found:
        candidates.append(found)
    for cand in candidates:
        low = cand.lower()
        if os.path.isfile(cand) and "system32" not in low and "windowsapps" not in low:
            return cand
    return None


def _find_powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


BASH = _find_bash()
POWERSHELL = _find_powershell()
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available")
needs_powershell = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell not available")
windows_only = pytest.mark.skipif(not WINDOWS, reason="cmd.exe/PowerShell premise is Windows-only")

_SCRUB = (
    "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "BOOTSTRAP_PYTHON",
    "BOOTSTRAP_PROJECT_PYTHON", "_BOOTSTRAP_PP_LAST", "_BOOTSTRAP_PP_DIR",
    "BOOTSTRAP_PP_NO_REGISTER", "PROMPT_COMMAND", "BASH_ENV", "ENV",
    "CLAUDE_ENV_FILE", "HISTFILE", "PSModulePath",
)


def _fwd(path) -> str:
    """The path form the resolvers print: forward slashes, upper-case drive."""
    text = str(path)
    if WINDOWS:
        text = text.replace("\\", "/")
        if re.match(r"^[a-z]:", text):
            text = text[0].upper() + text[1:]
    return text


def _msys(path) -> str:
    """``C:\\x\\y`` -> ``/c/x/y`` (the Git Bash spelling of a native path)."""
    text = _fwd(path)
    return "/" + text[0].lower() + text[2:]


def _env(sandbox: Path, home, **extra) -> dict:
    """A subprocess environment confined to ``sandbox``.

    HOME and every Windows profile variable point inside the sandbox, so
    neither bash nor PowerShell can read or write the developer's profile.
    ``extra`` values of None remove the variable.
    """
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    # The known-folder API expands %USERPROFILE%\AppData\Local; when that
    # directory is missing it answers "" and PowerShell writes its module
    # cache RELATIVE to the cwd. Keep AppData inside the redirected profile.
    profile = str(sandbox / "profile")
    local = sandbox / "profile" / "AppData" / "Local"
    roaming = sandbox / "profile" / "AppData" / "Roaming"
    temp = sandbox / "Temp"
    for d in (local, roaming, temp):
        d.mkdir(parents=True, exist_ok=True)
    env.update({
        "HOME": str(home) if home is not None else "",
        "USERPROFILE": profile,
        "HOMEDRIVE": os.path.splitdrive(profile)[0] or "",
        "HOMEPATH": os.path.splitdrive(profile)[1],
        "APPDATA": str(roaming),
        "LOCALAPPDATA": str(local),
        "TEMP": str(temp),
        "TMP": str(temp),
        "HISTFILE": str(sandbox / "bash_history"),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    if home is None:
        env.pop("HOME")
    for key, value in extra.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = str(value)
    return env


def _run_bash(script: str, env: dict, *, cwd=None, interactive=False):
    """Run bash; the default cwd is $HOME (inside the sandbox), so a
    registering source walks only sandbox directories."""
    if cwd is None:
        home = env.get("HOME") or ""
        cwd = home if home and os.path.isdir(home) else env["TEMP"]
    args = [BASH, "--noprofile", "--norc"]
    if interactive:
        args.append("-i")
        run = subprocess.run(args, input=script, capture_output=True, text=True,
                             env=env, cwd=cwd, timeout=120)
    else:
        run = subprocess.run(args + ["-c", script], capture_output=True, text=True,
                             env=env, cwd=cwd, timeout=120)
    return run


def _values(stdout: str) -> dict:
    """``key=value`` lines -> dict (last one wins)."""
    out = {}
    for line in stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value
    return out


def _make_interp(venv_dir: Path) -> Path:
    """Create the platform interpreter file a venv carries."""
    if WINDOWS:
        exe = venv_dir / "Scripts" / "python.exe"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"MZ")
    else:
        exe = venv_dir / "bin" / "python"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
    return exe


def _make_venv(venv_dir: Path, *, cfg: bool = True) -> Path:
    venv_dir.mkdir(parents=True, exist_ok=True)
    if cfg:
        (venv_dir / "pyvenv.cfg").write_text("home = /nowhere\n")
    return _make_interp(venv_dir)


def _opt_out(project: Path, body: str = '{\n  "project_python": false\n}\n') -> None:
    (project / ".claude").mkdir(parents=True, exist_ok=True)
    (project / ".claude" / "bootstrap.json").write_text(body)


def _fake_engine_python(sandbox: Path) -> Path:
    engine = sandbox / "engine"
    engine.mkdir(parents=True, exist_ok=True)
    return _make_interp(engine)


@pytest.fixture
def sandbox(tmp_path):
    box = tmp_path / "sandbox"
    box.mkdir()
    return box


def _resolve(sandbox: Path, start, home, **extra) -> tuple[str, str]:
    """Run ``bootstrap_resolve_project_python`` under ``set -u``; (value, rc)."""
    env = _env(sandbox, home, PP_FILE=_fwd(SH_TEMPLATE), PP_START=start,
               BOOTSTRAP_PP_NO_REGISTER="1", **extra)
    run = _run_bash(
        'set -u; . "$PP_FILE"; bootstrap_resolve_project_python "$PP_START";'
        ' printf "rc=%s\\n" "$?"', env)
    assert run.returncode == 0, run.stderr
    lines = run.stdout.splitlines()
    assert len(lines) == 2, run.stdout
    return lines[0], lines[1]


# Counts the directories the walk visits by wrapping the opt-out probe, which
# the walk calls exactly once per directory.
_COUNT_VISITS = (
    'set -u; . "$PP_FILE"; _visits=0; '
    '_bpp_opted_out() { _visits=$((_visits + 1)); return 1; }; '
    'bootstrap_resolve_project_python "$PP_START" >/dev/null; printf "visits=%s\\n" "$_visits"'
)


def _visits(sandbox: Path, start: str, home) -> int:
    env = _env(sandbox, home, PP_FILE=_fwd(SH_TEMPLATE), PP_START=start,
               BOOTSTRAP_PP_NO_REGISTER="1")
    if home == "":
        env["HOME"] = ""
    run = _run_bash(_COUNT_VISITS, env)
    assert run.returncode == 0, run.stderr
    return int(_values(run.stdout)["visits"])


# ------------------------------------------------ shell_hook.ensure (T13a-c, k)


@pytest.fixture
def hook_env(tmp_path, monkeypatch):
    """Opt in to shell-hook writes, confined to tmp paths."""
    monkeypatch.delenv(ISOLATION_ENV, raising=False)
    monkeypatch.delenv(shell_hook.DATA_ROOT_ENV, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    docs = tmp_path / "docs"
    docs.mkdir()
    data_dir = tmp_path / "data" / MKT / "bootstrap"
    data_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return {"home": str(home), "docs": str(docs), "data_dir": str(data_dir)}


def test_rc_and_profile_line_text():
    """T13a (text): the lines name the CANONICAL data path and the marker."""
    assert shell_hook.rc_line(MKT) == EXPECTED_RC_LINE
    assert shell_hook.profile_line(MKT) == EXPECTED_PROFILE_LINE


@pytest.mark.parametrize("current_os,names", [
    ("ubuntu", [".bashrc"]),
    ("windows", [".bashrc"]),
    ("macos", [".bashrc", ".zshrc"]),
])
def test_rc_line_added_once_and_removed_on_disable(hook_env, current_os, names):
    """T13a: the rc line is added once, other lines survive, disable removes it."""
    home = Path(hook_env["home"])
    original = b"export FOO=1\n# keep me\n"
    for name in names:
        (home / name).write_bytes(original)
    for _ in range(2):
        actions, oks, failures = shell_hook.ensure(
            hook_env["data_dir"], current_os, enabled=True, home=str(home),
            documents=hook_env["docs"])
        assert failures == []
    for name in names:
        text = (home / name).read_text()
        assert text == original.decode() + EXPECTED_RC_LINE + "\n"
    assert not any("hook added" in a for a in actions)  # second run is steady

    actions, _oks, failures = shell_hook.ensure(
        hook_env["data_dir"], current_os, enabled=False, home=str(home),
        documents=hook_env["docs"])
    assert failures == []
    for name in names:
        assert (home / name).read_bytes() == original
    assert any("removed from" in a for a in actions)
    if current_os != "macos":
        assert not (home / ".zshrc").exists()


def test_rc_file_created_when_missing_and_stale_marked_line_replaced(hook_env):
    """T13a: a missing .bashrc is created; a marked line for another
    marketplace is replaced, not duplicated."""
    home = Path(hook_env["home"])
    shell_hook.ensure(hook_env["data_dir"], "ubuntu", enabled=True, home=str(home))
    assert (home / ".bashrc").read_text() == EXPECTED_RC_LINE + "\n"
    (home / ".bashrc").write_text(
        "a\n" + shell_hook.rc_line("old-mkt") + "\nb\n")
    shell_hook.ensure(hook_env["data_dir"], "ubuntu", enabled=True, home=str(home))
    assert (home / ".bashrc").read_text() == "a\nb\n" + EXPECTED_RC_LINE + "\n"


@pytest.mark.parametrize("var", [ISOLATION_ENV, shell_hook.DATA_ROOT_ENV])
def test_inert_under_isolation_and_data_root(hook_env, monkeypatch, var):
    """T13a: nothing is read or written when either signal is set."""
    monkeypatch.setenv(var, "1")
    home = Path(hook_env["home"])
    (home / "Documents" / "PowerShell").mkdir(parents=True)
    profile = home / "Documents" / "PowerShell" / "profile.ps1"
    profile.write_text("# existing\n")
    actions, oks, failures = shell_hook.ensure(
        hook_env["data_dir"], "windows", enabled=True, home=str(home))
    assert (actions, failures) == ([], [])
    assert any(var in o for o in oks)
    assert not (home / ".bashrc").exists()
    assert profile.read_text() == "# existing\n"
    assert not (Path(hook_env["data_dir"]) / "shell").exists()


def test_templates_copied_and_refreshed(hook_env):
    """T13b: both templates land in <data_dir>/shell and are rewritten on drift."""
    data_dir = Path(hook_env["data_dir"])
    actions, oks, failures = shell_hook.ensure(
        str(data_dir), "ubuntu", enabled=True, home=hook_env["home"])
    assert failures == []
    assert any("templates updated" in a for a in actions)
    for tpl in (SH_TEMPLATE, PS_TEMPLATE):
        assert (data_dir / "shell" / tpl.name).read_bytes() == tpl.read_bytes()

    (data_dir / "shell" / SH_TEMPLATE.name).write_bytes(b"stale\n")
    actions, oks, _ = shell_hook.ensure(
        str(data_dir), "ubuntu", enabled=True, home=hook_env["home"])
    assert any(SH_TEMPLATE.name in a for a in actions)
    assert (data_dir / "shell" / SH_TEMPLATE.name).read_bytes() == SH_TEMPLATE.read_bytes()

    actions, oks, _ = shell_hook.ensure(
        str(data_dir), "ubuntu", enabled=True, home=hook_env["home"])
    assert not any("templates" in a for a in actions)
    assert "shell: project-python templates ok" in oks


def test_profile_paths_and_documents_resolution(hook_env, monkeypatch):
    """T13c: profile paths come from the injected Documents; with none
    injected and the registry skipped, <home>/Documents is used."""
    docs = hook_env["docs"]
    assert shell_hook.profile_paths(docs) == [
        (os.path.join(docs, "WindowsPowerShell", "profile.ps1"), "WindowsPowerShell"),
        (os.path.join(docs, "PowerShell", "profile.ps1"), "PowerShell"),
    ]
    assert shell_hook.documents_dir(hook_env["home"], docs) == docs
    monkeypatch.setenv("BOOTSTRAP_SKIP_REGISTRY", "1")
    assert shell_hook.documents_dir(hook_env["home"]) == os.path.join(
        hook_env["home"], "Documents")


def _no_subprocess(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("shell_hook must not spawn a process")
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)


def test_profiles_existing_only_encoding_preserved(hook_env, monkeypatch):
    """T13c: a missing profile is never created; an existing UTF-16LE profile
    keeps its BOM, encoding and CRLF; disable restores the exact bytes. No
    process is spawned."""
    _no_subprocess(monkeypatch)
    monkeypatch.setattr(shell_hook, "_registry_policy", lambda edition: "RemoteSigned")
    docs = Path(hook_env["docs"])
    (docs / "PowerShell").mkdir()
    pwsh_profile = docs / "PowerShell" / "profile.ps1"
    original = b"\xff\xfe" + "Set-Alias ll Get-ChildItem\r\n".encode("utf-16-le")
    pwsh_profile.write_bytes(original)

    for _ in range(2):
        actions, oks, failures = shell_hook.ensure(
            hook_env["data_dir"], "windows", enabled=True,
            home=hook_env["home"], documents=str(docs))
        assert failures == []
    assert not (docs / "WindowsPowerShell" / "profile.ps1").exists()
    assert any("WindowsPowerShell/profile.ps1 absent - not created" in o for o in oks)
    data = pwsh_profile.read_bytes()
    assert data.startswith(b"\xff\xfe")
    assert data[2:].decode("utf-16-le") == (
        "Set-Alias ll Get-ChildItem\r\n" + EXPECTED_PROFILE_LINE + "\r\n")

    shell_hook.ensure(hook_env["data_dir"], "windows", enabled=False,
                      home=hook_env["home"], documents=str(docs))
    assert pwsh_profile.read_bytes() == original


def test_utf8_bom_and_plain_profiles(hook_env, monkeypatch):
    """T13c: UTF-8 with BOM keeps its BOM; a BOM-less LF profile stays LF."""
    monkeypatch.setattr(shell_hook, "_registry_policy", lambda edition: "RemoteSigned")
    docs = Path(hook_env["docs"])
    for sub in ("WindowsPowerShell", "PowerShell"):
        (docs / sub).mkdir()
    bom = docs / "WindowsPowerShell" / "profile.ps1"
    bom.write_bytes(b"\xef\xbb\xbf# bom\r\n")
    plain = docs / "PowerShell" / "profile.ps1"
    plain.write_bytes(b"# plain\n")
    shell_hook.ensure(hook_env["data_dir"], "windows", enabled=True,
                      home=hook_env["home"], documents=str(docs))
    assert bom.read_bytes() == (
        b"\xef\xbb\xbf# bom\r\n" + EXPECTED_PROFILE_LINE.encode() + b"\r\n")
    assert plain.read_bytes() == b"# plain\n" + EXPECTED_PROFILE_LINE.encode() + b"\n"


def test_signed_and_utf16be_profiles_skipped(hook_env, monkeypatch):
    """T13k: a signed profile and a UTF-16BE profile are left untouched."""
    monkeypatch.setattr(shell_hook, "_registry_policy", lambda edition: "RemoteSigned")
    docs = Path(hook_env["docs"])
    for sub in ("WindowsPowerShell", "PowerShell"):
        (docs / sub).mkdir()
    signed = docs / "WindowsPowerShell" / "profile.ps1"
    signed_bytes = (b"Write-Host hi\r\n# SIG # Begin signature block\r\n"
                    b"# MIIxyz\r\n# SIG # End signature block\r\n")
    signed.write_bytes(signed_bytes)
    be = docs / "PowerShell" / "profile.ps1"
    be_bytes = b"\xfe\xff" + "Write-Host hi\r\n".encode("utf-16-be")
    be.write_bytes(be_bytes)

    actions, oks, failures = shell_hook.ensure(
        hook_env["data_dir"], "windows", enabled=True,
        home=hook_env["home"], documents=str(docs))
    assert signed.read_bytes() == signed_bytes
    assert be.read_bytes() == be_bytes
    assert any("WindowsPowerShell/profile.ps1 is signed - skipped" in o for o in oks)
    assert any("PowerShell/profile.ps1 has an unrecognised encoding" in o for o in oks)
    assert failures == []


@pytest.mark.parametrize("policy,changed", [
    ("RemoteSigned", True), ("Unrestricted", True), ("Bypass", True),
    ("Restricted", False), ("AllSigned", False), ("Default", False),
])
def test_profile_requires_script_running_policy(hook_env, monkeypatch, policy, changed):
    """T13c: only a policy that runs local unsigned scripts gets the line."""
    monkeypatch.setattr(shell_hook, "_registry_policy", lambda edition: policy)
    docs = Path(hook_env["docs"])
    (docs / "WindowsPowerShell").mkdir()
    profile = docs / "WindowsPowerShell" / "profile.ps1"
    profile.write_bytes(b"# mine\r\n")
    _actions, oks, _failures = shell_hook.ensure(
        hook_env["data_dir"], "windows", enabled=True,
        home=hook_env["home"], documents=str(docs))
    assert (profile.read_bytes() != b"# mine\r\n") is changed
    if not changed:
        assert any(f"execution policy {policy}" in o for o in oks)


def test_effective_policy_sources(tmp_path, monkeypatch):
    """T13c: pwsh reads its CurrentUser config; defaults per edition; the
    registry (when it answers) wins."""
    monkeypatch.setenv("BOOTSTRAP_SKIP_REGISTRY", "1")
    docs = tmp_path / "docs"
    (docs / "PowerShell").mkdir(parents=True)
    assert shell_hook.effective_policy("PowerShell", str(docs)) == "RemoteSigned"
    (docs / "PowerShell" / "powershell.config.json").write_text(
        '{"Microsoft.PowerShell:ExecutionPolicy": "AllSigned"}')
    assert shell_hook.effective_policy("PowerShell", str(docs)) == "AllSigned"
    monkeypatch.setattr(shell_hook, "_registry_policy", lambda edition: "Bypass")
    assert shell_hook.effective_policy("PowerShell", str(docs)) == "Bypass"
    monkeypatch.setattr(shell_hook, "_registry_policy", lambda edition: None)
    monkeypatch.setattr(shell_hook, "_default_policy", lambda edition: "Restricted")
    assert shell_hook.effective_policy("WindowsPowerShell", str(docs)) == "Restricted"


# ------------------------------------------------------ template static checks


def test_templates_are_ascii_and_lf():
    for tpl in (SH_TEMPLATE, PS_TEMPLATE):
        data = tpl.read_bytes()
        data.decode("ascii")
        assert b"\r" not in data, tpl


@needs_bash
def test_template_bash_syntax():
    run = subprocess.run([BASH, "-n", str(SH_TEMPLATE)], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr


_BASH_ONLY = [
    (r"\$\{[A-Za-z_]+\^", "case modification (bash 4)"),
    (r"\$\{[A-Za-z_]+,", "case modification (bash 4)"),
    (r"\bread\s+-[a-z]*p", "read -p (bash-only)"),
    (r"\bdeclare\s+-[A-Za-z]*A", "associative array (bash 4)"),
    (r"\b(mapfile|readarray)\b", "mapfile (bash 4)"),
    (r"\blocal\s+-n\b", "nameref (bash 4.3)"),
    (r"[A-Za-z_]+=\(", "array assignment"),
    (r"<<<", "here-string"),
    (r"\$\{!", "indirect expansion (bash-only)"),
    (r"\bcygpath\b", "fork on the resolution path"),
    (r"\$\((?!\()", "command substitution (fork)"),
    (r"`", "command substitution (fork)"),
]


def test_template_is_bash32_and_zsh_safe():
    """Static zsh/bash-3.2 check (zsh is not installed here or on CI).

    Forbids bash-4 and bash-only constructs and forks; allows ``[[`` and
    ``shopt`` only inside the bash branch of ``_bpp_same``, and zsh-only
    ``${(L)...}`` only inside an eval string.
    """
    text = SH_TEMPLATE.read_text()
    code_lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    code = "\n".join(code_lines)
    for pattern, why in _BASH_ONLY:
        assert not re.search(pattern, code), why
    same = re.search(r"_bpp_same\(\) \{.*?\n\}", code, re.S).group(0)
    outside = code.replace(same, "")
    assert "[[" not in outside and "shopt" not in outside
    assert "eval '[ \"${(L)1}\" = \"${(L)2}\" ]'" in same
    assert "add-zsh-hook chpwd _bootstrap_project_python_update" in code
    assert "add-zsh-hook precmd _bootstrap_project_python_update" in code


# ----------------------------------------------------- bash resolver (T13d-f)


@needs_bash
def test_walk_up_finds_nearest_venv(sandbox):
    """T13d: the nearest venv with pyvenv.cfg wins; one without is skipped;
    a native backslash start path comes back in C:/ form."""
    home = sandbox / "home"
    _make_venv(home / ".venv")
    expected = _make_venv(home / "proj" / ".venv")
    _make_venv(home / "proj" / "a" / ".venv", cfg=False)
    start = home / "proj" / "a" / "b"
    start.mkdir(parents=True)
    value, rc = _resolve(sandbox, str(start), home)
    assert (value, rc) == (_fwd(expected), "rc=0")
    # HOME itself is checked before the walk stops.
    value, _ = _resolve(sandbox, str(home / "proj" / "a" / "b"), home / "proj" / "a")
    assert value == _fwd(_fake_or_standalone(home / "proj" / "a"))


def _fake_or_standalone(home: Path) -> str:
    if WINDOWS:
        return _fwd(home) + "/.local/share/python-standalone/python/python.exe"
    return str(home) + "/.local/bin/python3"


@needs_bash
def test_walk_up_stops_after_home(sandbox):
    """T13d: a venv ABOVE $HOME is never used; $HOME's own venv is."""
    top = sandbox / "top"
    _make_venv(top / ".venv")
    home = top / "home"
    start = home / "proj"
    start.mkdir(parents=True)
    engine = _fake_engine_python(sandbox)
    homes = [str(home), str(home) + os.sep]
    if WINDOWS:
        homes += [_msys(home), str(home).lower()]
    for spelled in homes:
        value, rc = _resolve(sandbox, str(start), spelled, BOOTSTRAP_PYTHON=engine)
        assert (value, rc) == (_fwd(engine), "rc=0"), spelled
    own = _make_venv(home / ".venv")
    value, _ = _resolve(sandbox, str(start), home, BOOTSTRAP_PYTHON=engine)
    assert value == _fwd(own)


@needs_bash
def test_walk_visits_stop_at_home_and_root(sandbox):
    """T13d: the walk visits exactly the directories from the start up to
    $HOME, or up to the root -- including a Windows drive path given with
    backslashes, a lower-case drive letter and an MSYS-spelled HOME."""
    if WINDOWS:
        assert _visits(sandbox, "Q:\\a\\b\\c", "q:\\A") == 3
        assert _visits(sandbox, "Q:\\a\\b\\c", "/q/a/") == 3
        assert _visits(sandbox, "q:/a/b", "") == 3  # Q:/a/b, Q:/a, Q:/
    else:
        assert _visits(sandbox, "/nonexistent-u10/a/b/c", "/nonexistent-u10/a/") == 3
        assert _visits(sandbox, "/nonexistent-u10/a/b", "") == 4  # ..., /


@needs_bash
@pytest.mark.skipif(not WINDOWS, reason="MSYS mount table is Windows-only")
def test_msys_mounted_cwd_prints_native_form(sandbox):
    """T13d: under Git Bash a Temp cwd reads /tmp/...; the resolver still
    prints the native C:/ spelling (mount table, no cygpath)."""
    home = sandbox / "home"
    proj = home / "proj"
    expected = _make_venv(proj / ".venv")
    # Git Bash mounts the user's temp directory at /tmp (a per-user mount
    # table, not this child's TEMP), and pytest's tmp tree normally lives there.
    env = _env(sandbox, home, PP_FILE=_fwd(SH_TEMPLATE), BOOTSTRAP_PP_NO_REGISTER="1")
    run = _run_bash(
        'set -u; . "$PP_FILE"; printf "pwd=%s\\n" "$PWD";'
        ' printf "value=%s\\n" "$(bootstrap_resolve_project_python)";'
        ' _bpp_norm /c/x/; printf "c=%s\\n" "$_BPP_R";'
        ' _bpp_norm "d:\\\\y"; printf "d=%s\\n" "$_BPP_R"; _bpp_norm /e; printf "e=%s\\n" "$_BPP_R"',
        env, cwd=str(proj))
    assert run.returncode == 0, run.stderr
    vals = _values(run.stdout)
    if not vals["pwd"].startswith("/tmp/"):
        pytest.skip(f"sandbox is not under the /tmp mount ({vals['pwd']})")
    assert vals["pwd"].endswith("/home/proj")
    assert vals["value"] == _fwd(expected)
    assert os.path.samefile(vals["value"], expected)
    assert (vals["c"], vals["d"], vals["e"]) == ("C:/x", "D:/y", "E:/")


@needs_bash
def test_virtual_env_and_uv_project_environment(sandbox):
    """T13d: VIRTUAL_ENV beats the walk; a relative UV_PROJECT_ENVIRONMENT
    renames the walked venv; an absolute one is tried first."""
    home = sandbox / "home"
    proj = home / "proj"
    _make_venv(proj / ".venv")
    active = _make_interp(sandbox / "active")
    value, _ = _resolve(sandbox, str(proj), home, VIRTUAL_ENV=sandbox / "active")
    assert value == _fwd(active)

    named = _make_venv(proj / "envs" / "p")
    value, _ = _resolve(sandbox, str(proj), home, UV_PROJECT_ENVIRONMENT="envs/p/")
    assert value == _fwd(named)

    elsewhere = _make_venv(sandbox / "abs-venv")
    value, _ = _resolve(sandbox, str(proj), home,
                        UV_PROJECT_ENVIRONMENT=sandbox / "abs-venv")
    assert value == _fwd(elsewhere)


@needs_bash
def test_fallback_to_bootstrap_python_then_standalone(sandbox):
    """T13f: no venv -> $BOOTSTRAP_PYTHON; unset -> the standalone path."""
    home = sandbox / "home"
    start = home / "proj"
    start.mkdir(parents=True)
    engine = _fake_engine_python(sandbox)
    value, rc = _resolve(sandbox, str(start), home, BOOTSTRAP_PYTHON=engine)
    assert (value, rc) == (_fwd(engine), "rc=0")
    value, rc = _resolve(sandbox, str(start), home)
    assert (value, rc) == (_fake_or_standalone(home), "rc=0")


# ---------------------------------------------------------- opt-out (bash)


@needs_bash
@pytest.mark.parametrize("body", [
    '{"project_python": false}',
    '{\n  "profiles": {},\n  "project_python"\t:\n    false\n}\n',
    '{"project_python":false,"tools":[]}',
])
def test_opt_out_stops_walk_up(sandbox, body):
    """Opt-out: the declaring directory stops the walk with no project value,
    even with a venv there, one above it, and an active VIRTUAL_ENV."""
    home = sandbox / "home"
    _make_venv(home / ".venv")
    proj = home / "proj"
    _make_venv(proj / ".venv")
    _opt_out(proj, body)
    start = proj / "src"
    start.mkdir()
    _make_interp(sandbox / "active")
    engine = _fake_engine_python(sandbox)
    for extra in ({}, {"VIRTUAL_ENV": sandbox / "active"}):
        value, rc = _resolve(sandbox, str(start), home, BOOTSTRAP_PYTHON=engine, **extra)
        assert (value, rc) == ("", "rc=1")


@needs_bash
@pytest.mark.parametrize("body", [
    '{"project_python": "false"}',
    '{"project_python": falsey}',
    '{"project_python_x": false}',
    '{"project_python": true}',
])
def test_opt_out_requires_literal_false(sandbox, body):
    home = sandbox / "home"
    proj = home / "proj"
    expected = _make_venv(proj / ".venv")
    _opt_out(proj, body)
    value, rc = _resolve(sandbox, str(proj), home)
    assert (value, rc) == (_fwd(expected), "rc=0")


@needs_bash
def test_venv_below_opt_out_is_nearest(sandbox):
    """A venv in a subdirectory is found before the walk reaches the opt-out."""
    home = sandbox / "home"
    proj = home / "proj"
    _opt_out(proj)
    inner = _make_venv(proj / "sub" / ".venv")
    value, rc = _resolve(sandbox, str(proj / "sub" / "deep"), home)
    assert (value, rc) == (_fwd(inner), "rc=0")


_HOOK_PRELUDE = 'set -u; . "$PP_FILE"; '
_SHOW = 'printf "%s=%s|%s\\n" "$1" "${BOOTSTRAP_PROJECT_PYTHON-unset}" "${_BOOTSTRAP_PP_LAST-unset}"'


def _hook_script(body: str) -> str:
    return (_HOOK_PRELUDE + "show() { " + _SHOW + "; }; " + body)


@needs_bash
def test_hook_unsets_only_its_own_value_in_opted_out_tree(sandbox):
    """Opt-out: entering an opted-out tree drops the hook's own value, never a
    user-set one; BOOTSTRAP_PYTHON is untouched."""
    home = sandbox / "home"
    live = home / "live"
    venv_py = _make_venv(live / ".venv")
    off = home / "off"
    _opt_out(off)
    engine = _fake_engine_python(sandbox)
    env = _env(sandbox, home, PP_FILE=_fwd(SH_TEMPLATE), BOOTSTRAP_PP_NO_REGISTER="1",
               LIVE=live, OFF=off, BOOTSTRAP_PYTHON=engine)
    run = _run_bash(_hook_script(
        'cd "$LIVE"; _bootstrap_project_python_update; show a; '
        'cd "$OFF"; _bootstrap_project_python_update; show b; '
        'printf "engine=%s\\n" "$BOOTSTRAP_PYTHON"; '
        'cd "$LIVE"; _bootstrap_project_python_update; show c; '
        'BOOTSTRAP_PROJECT_PYTHON=/user/choice; export BOOTSTRAP_PROJECT_PYTHON; '
        'cd "$OFF"; _bootstrap_project_python_update; show d'), env)
    assert run.returncode == 0, run.stderr
    vals = _values(run.stdout)
    assert vals["a"] == f"{_fwd(venv_py)}|{_fwd(venv_py)}"
    assert vals["b"] == "unset|"
    assert vals["engine"] == str(engine)
    assert vals["c"] == f"{_fwd(venv_py)}|{_fwd(venv_py)}"
    assert vals["d"] == f"/user/choice|{_fwd(venv_py)}"


# ------------------------------------------------------ bash hook (T13e, h-l)


@needs_bash
def test_update_respects_user_set_value(sandbox):
    """T13e: a value the user exported is never replaced; once unset, the
    hook takes over and follows the directory."""
    home = sandbox / "home"
    live = home / "live"
    venv_py = _make_venv(live / ".venv")
    other = home / "other"
    other.mkdir(parents=True)
    engine = _fake_engine_python(sandbox)
    env = _env(sandbox, home, PP_FILE=_fwd(SH_TEMPLATE), BOOTSTRAP_PP_NO_REGISTER="1",
               LIVE=live, OTHER=other, BOOTSTRAP_PYTHON=engine,
               BOOTSTRAP_PROJECT_PYTHON="/user/choice")
    run = _run_bash(_hook_script(
        'cd "$LIVE"; _bootstrap_project_python_update; show a; '
        'unset BOOTSTRAP_PROJECT_PYTHON; _bootstrap_project_python_update; show b; '
        'cd "$OTHER"; _bootstrap_project_python_update; show c'), env)
    assert run.returncode == 0, run.stderr
    vals = _values(run.stdout)
    assert vals["a"] == "/user/choice|unset"
    assert vals["b"] == f"{_fwd(venv_py)}|{_fwd(venv_py)}"
    assert vals["c"] == f"{_fwd(engine)}|{_fwd(engine)}"


@needs_bash
def test_bash_interactive_registration_newline_join(sandbox):
    """T13h: in an interactive bash the function joins PROMPT_COMMAND with a
    newline, a pre-existing `history -a;` still runs, and the prompt cycle
    updates the value after a cd."""
    home = sandbox / "home"
    live = home / "live"
    venv_py = _make_venv(live / ".venv")
    hist = sandbox / "hist"
    env = _env(sandbox, home, PP_FILE=_fwd(SH_TEMPLATE), LIVE=live)
    script = "\n".join([
        f"HISTFILE='{_fwd(hist)}'",
        "PROMPT_COMMAND='history -a;'",
        '. "$PP_FILE"',
        "nl=$'\\n'",
        'printf "pc=%s\\n" "${PROMPT_COMMAND//$nl/<NL>}"',
        'cd "$LIVE"',
        'printf "pp=%s\\n" "$BOOTSTRAP_PROJECT_PYTHON"',
        "echo marker-u10-history",
        "exit 0",
        "",
    ])
    run = _run_bash(script, env, interactive=True)
    assert run.returncode == 0, run.stderr
    assert "syntax error" not in run.stderr
    vals = _values(run.stdout)
    assert vals["pc"] == "history -a;<NL>_bootstrap_project_python_update"
    assert vals["pp"] == _fwd(venv_py)
    assert "marker-u10-history" in hist.read_text()


@needs_bash
def test_bash_registration_is_idempotent(sandbox):
    """T13i: sourcing twice registers the function once."""
    home = sandbox / "home"
    home.mkdir()
    env = _env(sandbox, home, PP_FILE=_fwd(SH_TEMPLATE))
    script = "\n".join([
        '. "$PP_FILE"', '. "$PP_FILE"',
        'n=0; rest="$PROMPT_COMMAND"',
        'while case "$rest" in *_bootstrap_project_python_update*) true;; *) false;; esac;'
        ' do n=$((n+1)); rest="${rest#*_bootstrap_project_python_update}"; done',
        'printf "count=%s\\n" "$n"', "exit 0", "",
    ])
    run = _run_bash(script, env, interactive=True)
    assert run.returncode == 0, run.stderr
    assert _values(run.stdout)["count"] == "1"


@needs_bash
def test_update_preserves_exit_status(sandbox):
    """T13j: $? survives a resolving call, a cached call, and a user-set skip."""
    home = sandbox / "home"
    home.mkdir()
    env = _env(sandbox, home, PP_FILE=_fwd(SH_TEMPLATE), BOOTSTRAP_PP_NO_REGISTER="1",
               BOOTSTRAP_PYTHON=_fake_engine_python(sandbox))
    run = _run_bash(_hook_script(
        'cd "$HOME"; (exit 3); _bootstrap_project_python_update; printf "a=%s\\n" "$?"; '
        '(exit 4); _bootstrap_project_python_update; printf "b=%s\\n" "$?"; '
        'BOOTSTRAP_PROJECT_PYTHON=/mine; (exit 5); _bootstrap_project_python_update;'
        ' printf "c=%s\\n" "$?"'), env)
    assert run.returncode == 0, run.stderr
    vals = _values(run.stdout)
    assert (vals["a"], vals["b"], vals["c"]) == ("3", "4", "5")


@needs_bash
def test_update_cache_and_refresh_keys(sandbox):
    """T13l: the result is cached on the key (a venv appearing in a PARENT
    is not seen until the key changes); an in-place venv, a VIRTUAL_ENV
    change, and a deleted interpreter each refresh it."""
    home = sandbox / "home"
    work = home / "parent" / "work"
    work.mkdir(parents=True)
    engine = _fake_engine_python(sandbox)
    active = _make_interp(sandbox / "active")
    parent_venv = home / "parent" / ".venv"
    work_venv = work / ".venv"
    env = _env(sandbox, home, PP_FILE=_fwd(SH_TEMPLATE), BOOTSTRAP_PP_NO_REGISTER="1",
               WORK=_fwd(work), BOOTSTRAP_PYTHON=engine, ACTIVE=_fwd(sandbox / "active"),
               PARENT_VENV=_fwd(parent_venv), WORK_VENV=_fwd(work_venv))
    rel = "Scripts/python.exe" if WINDOWS else "bin/python"
    mkvenv = (
        'mkvenv() { mkdir -p "$1/' + rel.rsplit("/", 1)[0] + '"; '
        + ("printf MZ > \"$1/" + rel + "\"; " if WINDOWS
           else "printf '#!/bin/sh\\n' > \"$1/" + rel + "\"; chmod +x \"$1/" + rel + "\"; ")
        + ': > "$1/pyvenv.cfg"; }; ')
    run = _run_bash(_hook_script(mkvenv +
        'cd "$WORK"; _bootstrap_project_python_update; show a; '
        'mkvenv "$PARENT_VENV"; _bootstrap_project_python_update; show b; '
        'mkvenv "$WORK_VENV"; _bootstrap_project_python_update; show c; '
        'VIRTUAL_ENV="$ACTIVE"; export VIRTUAL_ENV; _bootstrap_project_python_update; show d; '
        'unset VIRTUAL_ENV; _bootstrap_project_python_update; show e; '
        'case "$WORK_VENV" in */parent/work/.venv) rm -f "$WORK_VENV/' + rel + '" ;; esac; '
        '_bootstrap_project_python_update; show f'), env)
    assert run.returncode == 0, run.stderr
    vals = _values(run.stdout)
    work_py = _fwd(work_venv) + "/" + rel
    parent_py = _fwd(parent_venv) + "/" + rel
    assert vals["a"] == f"{_fwd(engine)}|{_fwd(engine)}"
    assert vals["b"] == f"{_fwd(engine)}|{_fwd(engine)}"  # cached
    assert vals["c"] == f"{work_py}|{work_py}"
    assert vals["d"] == f"{_fwd(active)}|{_fwd(active)}"
    assert vals["e"] == f"{work_py}|{work_py}"
    assert vals["f"] == f"{parent_py}|{parent_py}"


@needs_bash
def test_no_side_effects_when_not_registering(sandbox):
    """T13h/i: a non-interactive source, and an interactive one with
    BOOTSTRAP_PP_NO_REGISTER, define functions only."""
    home = sandbox / "home"
    home.mkdir()
    show = ('printf "state=[%s][%s][%s][%s]\\n" "${PROMPT_COMMAND-unset}"'
            ' "${BOOTSTRAP_PROJECT_PYTHON-unset}" "${_BOOTSTRAP_PP_DIR-unset}"'
            ' "${BOOTSTRAP_PYTHON-unset}"')
    env = _env(sandbox, home, PP_FILE=_fwd(SH_TEMPLATE))
    run = _run_bash('set -u; . "$PP_FILE"; ' + show, env)
    assert run.returncode == 0, run.stderr
    assert _values(run.stdout)["state"] == "[unset][unset][unset][unset]"

    env = _env(sandbox, home, PP_FILE=_fwd(SH_TEMPLATE), BOOTSTRAP_PP_NO_REGISTER="1")
    run = _run_bash('. "$PP_FILE"\n' + show + "\nexit 0\n", env, interactive=True)
    assert run.returncode == 0, run.stderr
    assert _values(run.stdout)["state"] == "[unset][unset][unset][unset]"


@needs_bash
def test_interactive_source_under_set_u(sandbox):
    """The registration path is `set -u` clean and exports BOOTSTRAP_PYTHON
    from the standalone path only when that file exists."""
    home = sandbox / "home"
    home.mkdir()
    env = _env(sandbox, home, PP_FILE=_fwd(SH_TEMPLATE))
    script = 'set -u\n. "$PP_FILE"\nprintf "bp=%s\\n" "${BOOTSTRAP_PYTHON-unset}"\nexit 0\n'
    run = _run_bash(script, env, interactive=True)
    assert run.returncode == 0, run.stderr
    assert "unbound" not in run.stderr
    assert _values(run.stdout)["bp"] == "unset"
    standalone = Path(_fake_or_standalone(home))
    standalone.parent.mkdir(parents=True)
    _write_interp_file(standalone)
    run = _run_bash(script, env, interactive=True)
    assert _values(run.stdout)["bp"] == _fwd(standalone)


def _write_interp_file(path: Path) -> None:
    if WINDOWS:
        path.write_bytes(b"MZ")
    else:
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)


# --------------------------------------------------------- PowerShell (T13g)


_PS_DRIVER = r"""
param([string]$Template, [string]$Root, [string]$Engine)
$ErrorActionPreference = 'Stop'
$env:BOOTSTRAP_PP_NO_REGISTER = '1'
. $Template
$home1 = Join-Path $Root 'home'
$env:HOME = $home1
"walk=" + (Resolve-BootstrapProjectPython (Join-Path $home1 'proj\a\b'))
$env:HOME = (Join-Path $Root 'top\home')
$env:BOOTSTRAP_PYTHON = $Engine
"above=" + (Resolve-BootstrapProjectPython (Join-Path $Root 'top\home\proj'))
$env:HOME = '/' + $Root.Substring(0, 1).ToLower() + ($Root.Substring(2) -replace '\\', '/') + '/top/home/'
"msyshome=" + (Resolve-BootstrapProjectPython (Join-Path $Root 'top\home\proj'))
$env:HOME = $home1
"optout=[" + (Resolve-BootstrapProjectPython (Join-Path $home1 'off\src')) + "]"
$env:VIRTUAL_ENV = (Join-Path $Root 'active')
"optoutve=[" + (Resolve-BootstrapProjectPython (Join-Path $home1 'off\src')) + "]"
"ve=" + (Resolve-BootstrapProjectPython (Join-Path $home1 'proj'))
Remove-Item Env:VIRTUAL_ENV
Remove-Item Env:BOOTSTRAP_PYTHON
$env:HOME = (Join-Path $Root 'top\home')
"standalone=" + (Resolve-BootstrapProjectPython (Join-Path $Root 'top\home\proj'))
$env:HOME = $home1
$env:BOOTSTRAP_PYTHON = $Engine
Remove-Item Env:BOOTSTRAP_PP_NO_REGISTER
Set-Location $home1
. $Template
Set-Location (Join-Path $home1 'proj')
$global:LASTEXITCODE = 7
$null = prompt
"pp1=" + $env:BOOTSTRAP_PROJECT_PYTHON + "|" + $env:_BOOTSTRAP_PP_LAST
"lec=" + $global:LASTEXITCODE
. $Template
"wrapped=" + ($global:BootstrapProjectPythonOriginalPrompt.ToString() -notmatch 'Update-BootstrapProjectPython')
Set-Location (Join-Path $home1 'off')
$null = prompt
"pp2=[" + $env:BOOTSTRAP_PROJECT_PYTHON + "][" + $env:_BOOTSTRAP_PP_LAST + "]"
$env:BOOTSTRAP_PROJECT_PYTHON = 'C:/user/choice'
Set-Location (Join-Path $home1 'proj')
$null = prompt
"pp3=" + $env:BOOTSTRAP_PROJECT_PYTHON
Set-Location (Join-Path $home1 'off\src')
$null = prompt
"pp4=" + $env:BOOTSTRAP_PROJECT_PYTHON
"engine=" + $env:BOOTSTRAP_PYTHON
"""


@needs_powershell
def test_powershell_resolver_and_prompt(sandbox):
    """T13g: the PowerShell twin walks up to the nearest venv, stops after
    HOME (native and MSYS spellings), honours the opt-out over VIRTUAL_ENV,
    falls back to BOOTSTRAP_PYTHON / the standalone path, and its prompt
    wrapper keeps $LASTEXITCODE, wraps once, and never removes a user value."""
    root = sandbox / "tree"
    home = root / "home"
    _make_venv(home / ".venv")
    proj_py = _make_venv(home / "proj" / ".venv")
    _make_venv(home / "proj" / "a" / ".venv", cfg=False)
    (home / "proj" / "a" / "b").mkdir(parents=True)
    _make_venv(home / "off" / ".venv")
    _opt_out(home / "off")
    (home / "off" / "src").mkdir()
    _make_venv(root / "top" / ".venv")
    (root / "top" / "home" / "proj").mkdir(parents=True)
    active_py = _make_interp(root / "active")
    engine = _fake_engine_python(sandbox)
    driver = sandbox / "driver.ps1"
    driver.write_text(_PS_DRIVER, encoding="ascii")
    env = _env(sandbox, home)
    args = [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive"]
    if WINDOWS:
        args += ["-ExecutionPolicy", "Bypass"]
    args += ["-File", str(driver), "-Template", str(PS_TEMPLATE),
             "-Root", str(root), "-Engine", _fwd(engine)]
    run = subprocess.run(args, capture_output=True, text=True, env=env,
                         cwd=str(sandbox), timeout=180)
    assert run.returncode == 0, run.stdout + run.stderr
    vals = _values(run.stdout)
    assert vals["walk"] == _fwd(proj_py)
    assert vals["above"] == _fwd(engine)
    assert vals["msyshome"] == _fwd(engine)
    assert vals["optout"] == "[]"
    assert vals["optoutve"] == "[]"
    assert vals["ve"] == _fwd(active_py)
    assert vals["standalone"] == _fake_or_standalone(root / "top" / "home")
    assert vals["pp1"] == f"{_fwd(proj_py)}|{_fwd(proj_py)}"
    assert vals["lec"] == "7"
    assert vals["wrapped"] == "True"
    assert vals["pp2"] == "[][]"
    assert vals["pp3"] == "C:/user/choice"
    assert vals["pp4"] == "C:/user/choice"
    assert vals["engine"] == _fwd(engine)


# ------------------------------------------------------------ P25 (T-cmd/T-ps)


def _interpreters() -> list[str]:
    """Real interpreters to run read-only: this venv's launcher and its base."""
    found = [sys.executable, getattr(sys, "_base_executable", sys.executable)]
    return sorted({_fwd(p) for p in found if p and os.path.isfile(p)})


@windows_only
def test_p25_forward_slash_interpreter_under_cmd(sandbox):
    """T-cmd: `"%BOOTSTRAP_PYTHON%" -c ...` runs a C:/-form interpreter, from a
    batch file and from a cmd.exe command line."""
    bat = sandbox / "p25.cmd"
    bat.write_bytes(b'@"%BOOTSTRAP_PYTHON%" -B -c "print(25001)"\r\n')
    for exe in _interpreters():
        assert "/" in exe and "\\" not in exe
        env = _env(sandbox, sandbox, BOOTSTRAP_PYTHON=exe)
        run = subprocess.run(["cmd.exe", "/d", "/c", str(bat)], capture_output=True,
                             text=True, env=env, cwd=str(sandbox), timeout=60)
        assert run.returncode == 0 and run.stdout.strip() == "25001", (exe, run)
        line = f'cmd.exe /d /s /c ""{exe}" -B -c "print(25002)""'
        run = subprocess.run(line, capture_output=True, text=True, env=env,
                             cwd=str(sandbox), timeout=60)
        assert run.returncode == 0 and run.stdout.strip() == "25002", (exe, run)


@windows_only
@needs_powershell
def test_p25_forward_slash_interpreter_under_powershell(sandbox):
    """T-ps: `& $env:BOOTSTRAP_PYTHON` and `& "C:/..."` run a C:/-form interpreter."""
    for exe in _interpreters():
        env = _env(sandbox, sandbox, BOOTSTRAP_PYTHON=exe)
        command = (f'& $env:BOOTSTRAP_PYTHON -B -c "print(25003)"; '
                   f'& "{exe}" -B -c "print(25004)"; exit $LASTEXITCODE')
        run = subprocess.run(
            [POWERSHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, env=env, cwd=str(sandbox), timeout=120)
        assert run.returncode == 0, (exe, run)
        assert run.stdout.split() == ["25003", "25004"], (exe, run)
