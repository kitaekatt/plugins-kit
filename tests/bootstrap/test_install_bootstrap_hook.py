"""`bootstrap install-hook` and the ensure-bootstrap hook it writes.

Two contracts:

1. The generator writes the hook script with the running bootstrap's version
   as the floor, keeps exactly one SessionStart entry across re-runs, leaves
   every other setting alone, and refuses a read-only target before writing.
2. The generated hook does nothing when the project opted out (no
   .claude/bootstrap.json) or bootstrap is already at the floor, and otherwise
   runs the marketplace + install/update sequence through the claude CLI.
"""

import importlib.util
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "bootstrap"


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = _load("install_bootstrap_hook")
cli = _load("bootstrap_cli")


def _find_bash():
    """Git Bash on Windows (WSL bash cannot see this tree), bash elsewhere."""
    candidates = []
    if os.name == "nt":
        candidates += [r"C:\Program Files\Git\usr\bin\bash.exe",
                       r"C:\Program Files\Git\bin\bash.exe"]
    found = shutil.which("bash")
    if found:
        candidates.append(found)
    for c in candidates:
        if c and Path(c).exists() and "WindowsApps" not in c and "System32" not in c:
            return c
    return None


BASH = _find_bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available")

PLUGIN_VERSION = json.loads(
    (PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]


def _project(tmp_path, settings=None, bootstrap_json=True):
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    if bootstrap_json:
        (project / ".claude" / "bootstrap.json").write_text("{}", encoding="utf-8")
    if settings is not None:
        (project / ".claude" / "settings.json").write_text(
            json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return project


def _ours(settings):
    return [h for g in settings["hooks"]["SessionStart"] for h in g["hooks"]
            if gen.HOOK_FILENAME in h["command"]]


# --------------------------------------------------------------------------
# Generator
# --------------------------------------------------------------------------

class TestGenerator:

    def test_writes_hook_with_running_version_and_one_settings_entry(self, tmp_path):
        project = _project(tmp_path)
        result = gen.install(str(project), str(PLUGIN_ROOT))

        assert result["min_version"] == PLUGIN_VERSION
        body = (project / ".claude" / "hooks" / gen.HOOK_FILENAME).read_text(encoding="utf-8")
        assert 'MIN_VERSION="%s"' % PLUGIN_VERSION in body
        assert gen.VERSION_PLACEHOLDER not in body
        settings = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert len(_ours(settings)) == 1
        # The session never waits on the hook; exit 2 wakes Claude with its report.
        assert _ours(settings)[0]["async"] is True
        assert _ours(settings)[0]["asyncRewake"] is True

    def test_rerun_replaces_entry_and_keeps_other_hooks_and_keys(self, tmp_path):
        other = {"type": "command", "command": "bash other.sh", "timeout": 30}
        stale = {"type": "command", "command": "bash old/" + gen.HOOK_FILENAME}
        project = _project(tmp_path, settings={
            "permissions": {"allow": ["Read"]},
            "enabledPlugins": {"bootstrap@plugins-kit": True},
            "hooks": {"SessionStart": [{"hooks": [other]}, {"hooks": [stale]}]},
        })
        gen.install(str(project), str(PLUGIN_ROOT))
        second = gen.install(str(project), str(PLUGIN_ROOT))

        settings = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert settings["permissions"] == {"allow": ["Read"]}
        assert settings["enabledPlugins"] == {"bootstrap@plugins-kit": True}
        assert settings["hooks"]["SessionStart"][0] == {"hooks": [other]}
        assert len(_ours(settings)) == 1
        assert _ours(settings)[0]["command"] == gen.HOOK_COMMAND
        assert not second["hook_changed"] and not second["settings_changed"]

    def test_non_ascii_settings_content_is_preserved(self, tmp_path):
        project = _project(tmp_path, settings={"note": "caf\u00e9"})
        gen.install(str(project), str(PLUGIN_ROOT))
        text = (project / ".claude" / "settings.json").read_text(encoding="utf-8")
        assert "caf\u00e9" in text

    def test_crlf_files_keep_crlf_and_rerun_is_a_no_op(self, tmp_path):
        project = _project(tmp_path)
        settings_path = project / ".claude" / "settings.json"
        settings_path.write_bytes(b'{\r\n  "hooks": {}\r\n}\r\n')
        gen.install(str(project), str(PLUGIN_ROOT))

        hook_path = project / ".claude" / "hooks" / gen.HOOK_FILENAME
        settings_bytes = settings_path.read_bytes()
        assert b"\r\n" in settings_bytes
        assert b"\n" not in settings_bytes.replace(b"\r\n", b"")
        # A fresh hook file is LF; simulate a Perforce CRLF checkout of it.
        assert b"\r\n" not in hook_path.read_bytes()
        hook_path.write_bytes(hook_path.read_bytes().replace(b"\n", b"\r\n"))

        second = gen.install(str(project), str(PLUGIN_ROOT))
        assert not second["hook_changed"] and not second["settings_changed"]
        assert settings_path.read_bytes() == settings_bytes

    def test_lf_settings_stay_lf(self, tmp_path):
        project = _project(tmp_path)
        settings_path = project / ".claude" / "settings.json"
        settings_path.write_bytes(b'{\n  "hooks": {}\n}\n')
        gen.install(str(project), str(PLUGIN_ROOT))
        assert b"\r\n" not in settings_path.read_bytes()

    def test_refuses_project_without_bootstrap_json(self, tmp_path):
        project = _project(tmp_path, bootstrap_json=False)
        with pytest.raises(gen.InstallError, match="bootstrap.json"):
            gen.install(str(project), str(PLUGIN_ROOT))
        assert not (project / ".claude" / "hooks").exists()

    def test_read_only_settings_is_refused_before_anything_is_written(self, tmp_path):
        project = _project(tmp_path, settings={"hooks": {}})
        settings_path = project / ".claude" / "settings.json"
        before = settings_path.read_text(encoding="utf-8")
        os.chmod(settings_path, stat.S_IREAD)
        try:
            with pytest.raises(gen.InstallError, match="read-only"):
                gen.install(str(project), str(PLUGIN_ROOT))
        finally:
            os.chmod(settings_path, stat.S_IREAD | stat.S_IWRITE)
        assert settings_path.read_text(encoding="utf-8") == before
        assert not (project / ".claude" / "hooks" / gen.HOOK_FILENAME).exists()

    def test_cli_verb_uses_the_working_directory(self, tmp_path, monkeypatch, capsys):
        project = _project(tmp_path)
        monkeypatch.chdir(project)
        assert cli.main(["--plugin-root", str(PLUGIN_ROOT), "install-hook"]) == 0
        assert (project / ".claude" / "hooks" / gen.HOOK_FILENAME).exists()
        assert PLUGIN_VERSION in capsys.readouterr().out

    def test_cli_verb_reports_refusal_with_exit_2(self, tmp_path, monkeypatch):
        project = _project(tmp_path, bootstrap_json=False)
        monkeypatch.chdir(project)
        assert cli.main(["--plugin-root", str(PLUGIN_ROOT), "install-hook"]) == 2


# --------------------------------------------------------------------------
# Generated hook, driven against a fake claude CLI
# --------------------------------------------------------------------------

# @ROLE@ is "path" for the copy first on PATH (the one a machine starts with)
# and "fixed" for the copy the fake installer writes. Only the "path" copy
# honours $FAKE_DIR/crash_marketplace_update, like an outdated CLI that panics.
FAKE_CLAUDE = r'''#!/usr/bin/env bash
# Fake claude CLI: logs each call, serves listings from files in $FAKE_DIR.
printf '%s\n' "$*" >> "$FAKE_DIR/calls.log"
if [ -n "${CLAUDECODE:-}" ]; then echo "CLAUDECODE leaked" >> "$FAKE_DIR/calls.log"; fi
case "$*" in
  "--version")
    if [ "@ROLE@" = fixed ]; then echo "2.1.300 (Claude Code)"; else cat "$FAKE_DIR/version"; fi ;;
  "plugin list --json") cat "$FAKE_DIR/plugins.json" ;;
  "plugin marketplace list --json") cat "$FAKE_DIR/marketplaces.json" ;;
  "plugin marketplace update "*)
    if [ "@ROLE@" = path ] && [ -f "$FAKE_DIR/crash_marketplace_update" ]; then
      echo "panic: index out of bounds: index 0, len 0"; exit 3
    fi ;;
  "plugin install "*|"plugin update "*)
    [ -f "$FAKE_DIR/fail_plugin_step" ] && { echo "boom"; exit 1; }
    cp "$FAKE_DIR/plugins_after.json" "$FAKE_DIR/plugins.json" ;;
esac
exit 0
'''

# The native installer, reached as `powershell ... install.ps1` under Git Bash
# and as `curl ... install.sh | bash` elsewhere. Both run INSTALL_SCRIPT.
INSTALL_SCRIPT = r'''printf 'installer\n' >> "$FAKE_DIR/calls.log"
[ -f "$FAKE_DIR/fail_installer" ] && { echo "download failed"; exit 1; }
mkdir -p "$HOME/.local/bin"
cp "$FAKE_DIR/fixed_claude" "$HOME/.local/bin/claude"
chmod +x "$HOME/.local/bin/claude"
'''

FAKE_POWERSHELL = '#!/usr/bin/env bash\nexec bash "$FAKE_DIR/install.sh"\n'
FAKE_CURL = '#!/usr/bin/env bash\ncat "$FAKE_DIR/install.sh"\n'


def _path_without_claude():
    """The test's PATH minus every directory holding a real claude CLI."""
    names = ("claude", "claude.exe", "claude.cmd")
    return os.pathsep.join(
        d for d in os.environ.get("PATH", "").split(os.pathsep)
        if d and not any(Path(d, n).exists() for n in names))


def _record(version, scope, project_path=None):
    rec = {"id": "bootstrap@plugins-kit", "version": version, "scope": scope,
           "enabled": True, "installPath": "C:\\x\\bootstrap\\" + version}
    if project_path is not None:
        rec["projectPath"] = project_path
    return rec


def _other_plugin():
    # A nested object exercises the parser's depth tracking.
    return {"id": "other@plugins-kit", "version": "9.9.9", "scope": "user",
            "errorDetails": [{"id": "bootstrap@plugins-kit", "version": "0.0.1"}]}


class HookHarness:

    def __init__(self, tmp_path, min_version="1.2.0", bootstrap_json=True, cli="native"):
        """``cli`` is where the machine's claude lives: "native" (~/.local/bin,
        where the native installer puts it), "other" (another tool's install
        directory on PATH), or None (not installed)."""
        self.project = _project(tmp_path, bootstrap_json=bootstrap_json)
        self.fake = tmp_path / "fake"
        self.fake.mkdir()
        self.home = tmp_path / "home"
        self.home.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        cli_dirs = {"native": self.home / ".local" / "bin", "other": bin_dir}
        if cli is not None:
            cli_dirs[cli].mkdir(parents=True, exist_ok=True)
            self._script(cli_dirs[cli] / "claude", FAKE_CLAUDE.replace("@ROLE@", "path"))
        self._script(bin_dir / "powershell", FAKE_POWERSHELL)
        self._script(bin_dir / "curl", FAKE_CURL)
        self._script(self.fake / "fixed_claude", FAKE_CLAUDE.replace("@ROLE@", "fixed"))
        (self.fake / "install.sh").write_bytes(INSTALL_SCRIPT.encode("ascii"))
        (self.fake / "version").write_text("2.1.56 (Claude Code)\n", encoding="ascii")
        self.bin_dir = bin_dir
        template = (PLUGIN_ROOT / "templates" / gen.HOOK_FILENAME).read_bytes()
        self.hook = tmp_path / gen.HOOK_FILENAME
        self.hook.write_bytes(template.replace(b"@MIN_VERSION@", min_version.encode()))
        self.set_marketplaces(present=True)

    @staticmethod
    def _script(path, body):
        path.write_bytes(body.encode("ascii"))
        path.chmod(0o755)

    def flag(self, name):
        (self.fake / name).write_text("", encoding="ascii")

    @property
    def stamp(self):
        return (self.home / ".claude" / "plugins" / "data" / "plugins-kit" / "bootstrap"
                / "ensure-bootstrap-cli-repair")

    def set_plugins(self, records, after=None):
        (self.fake / "plugins.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
        (self.fake / "plugins_after.json").write_text(
            json.dumps(after if after is not None else records, indent=2), encoding="utf-8")

    def set_marketplaces(self, present):
        items = [{"name": "plugins-kit", "source": "git"}] if present else []
        (self.fake / "marketplaces.json").write_text(json.dumps(items, indent=2), encoding="utf-8")

    def run(self):
        env = dict(os.environ)
        env["PATH"] = str(self.bin_dir) + os.pathsep + _path_without_claude()
        env["HOME"] = str(self.home)
        env["FAKE_DIR"] = str(self.fake)
        env["CLAUDE_PROJECT_DIR"] = str(self.project)
        env["CLAUDECODE"] = "1"
        proc = subprocess.run([BASH, str(self.hook)], env=env, capture_output=True,
                              text=True, timeout=60)
        log = self.fake / "calls.log"
        calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return proc, calls


@needs_bash
class TestGeneratedHook:

    def test_opt_out_without_bootstrap_json_makes_no_calls(self, tmp_path):
        h = HookHarness(tmp_path, bootstrap_json=False)
        h.set_plugins([])
        proc, calls = h.run()
        assert proc.returncode == 0
        assert calls == []
        assert proc.stdout == ""

    def test_healthy_install_only_lists(self, tmp_path):
        h = HookHarness(tmp_path)
        h.set_plugins([_other_plugin(), _record("1.10.0", "user")])
        proc, calls = h.run()
        assert proc.returncode == 0
        assert calls == ["plugin list --json"]
        assert proc.stdout == ""

    def test_missing_plugin_and_marketplace_adds_updates_installs(self, tmp_path):
        h = HookHarness(tmp_path)
        h.set_marketplaces(present=False)
        h.set_plugins([_other_plugin()], after=[_record("1.2.0", "user")])
        proc, calls = h.run()
        # Exit 2 with the message on stderr is what wakes Claude (asyncRewake).
        assert proc.returncode == 2
        assert proc.stdout == ""
        assert calls == [
            "plugin list --json",
            "plugin marketplace list --json",
            "plugin marketplace add https://github.com/kitaekatt/plugins-kit.git",
            "plugin marketplace update plugins-kit",
            "plugin install bootstrap@plugins-kit --scope user",
            "plugin list --json",
        ]
        assert "installed bootstrap@plugins-kit 1.2.0" in proc.stderr
        assert "restart Claude Code" in proc.stderr

    def test_old_project_record_is_updated_at_its_scope(self, tmp_path):
        h = HookHarness(tmp_path)
        # Separator style and drive-letter case do not matter; see the next test.
        raw = str(h.project).replace("/", "\\")
        project_path = raw[0].swapcase() + raw[1:]
        h.set_plugins(
            [_record("1.5.0", "user"), _record("1.1.9", "project", project_path),
             _record("0.1.0", "project", "C:\\elsewhere")],
            after=[_record("1.5.0", "user"), _record("1.2.1", "project", project_path)])
        proc, calls = h.run()
        assert calls == [
            "plugin list --json",
            "plugin marketplace list --json",
            "plugin marketplace update plugins-kit",
            "plugin update bootstrap@plugins-kit --scope project",
            "plugin list --json",
        ]
        assert proc.returncode == 2
        assert "updated bootstrap@plugins-kit 1.2.1" in proc.stderr

    def test_record_for_a_differently_cased_path_does_not_count(self, tmp_path):
        # Claude Code treats D:\Dev\x and D:\dev\x as different projects, so a
        # record under the other spelling is not an install for this session.
        h = HookHarness(tmp_path)
        raw = str(h.project)
        other_case = raw[:-4] + raw[-4:].swapcase()
        assert other_case != raw
        h.set_plugins([_record("0.1.0", "local", other_case)],
                      after=[_record("1.2.0", "user")])
        proc, calls = h.run()
        assert "plugin install bootstrap@plugins-kit --scope user" in calls
        assert not any(c.startswith("plugin update") for c in calls)

    def test_other_projects_records_do_not_count(self, tmp_path):
        h = HookHarness(tmp_path)
        h.set_plugins([_record("9.0.0", "project", "C:\\elsewhere")],
                      after=[_record("1.2.0", "user")])
        proc, calls = h.run()
        assert "plugin install bootstrap@plugins-kit --scope user" in calls

    def test_crashing_cli_is_reinstalled_and_the_install_retried(self, tmp_path):
        # The observed wedge: an outdated CLI on PATH panics on marketplace
        # update, so bootstrap can never be installed through it.
        h = HookHarness(tmp_path)
        h.flag("crash_marketplace_update")
        h.set_plugins([_record("0.10.11", "project", str(h.project))],
                      after=[_record("1.2.0", "project", str(h.project))])
        proc, calls = h.run()
        assert proc.returncode == 2
        assert calls.count("installer") == 1
        update = "plugin update bootstrap@plugins-kit --scope project"
        assert calls.index("installer") < calls.index(update)
        assert "updated bootstrap@plugins-kit 1.2.0" in proc.stderr
        assert "2.1.56 -> 2.1.300" in proc.stderr
        assert h.stamp.read_text(encoding="ascii").strip() == "2.1.300"

    def test_missing_cli_is_installed_then_bootstrap_installed(self, tmp_path):
        h = HookHarness(tmp_path, cli=None)
        h.set_plugins([], after=[_record("1.2.0", "user")])
        proc, calls = h.run()
        assert proc.returncode == 2
        assert calls[0] == "installer"
        assert "plugin install bootstrap@plugins-kit --scope user" in calls
        assert "installed bootstrap@plugins-kit 1.2.0" in proc.stderr
        assert "missing -> 2.1.300" in proc.stderr

    def test_healthy_path_never_touches_the_cli_install(self, tmp_path):
        h = HookHarness(tmp_path)
        h.flag("crash_marketplace_update")
        h.set_plugins([_record("1.10.0", "user")])
        proc, calls = h.run()
        assert proc.returncode == 0
        assert "installer" not in calls
        assert not h.stamp.exists()

    def test_failure_with_another_cause_reports_once_reinstalled(self, tmp_path):
        h = HookHarness(tmp_path)
        h.set_plugins([])
        h.flag("fail_plugin_step")
        proc, calls = h.run()
        assert proc.returncode == 2
        assert calls.count("installer") == 1
        assert "could not bring bootstrap@plugins-kit to 1.2.0" in proc.stderr
        assert "It still failed afterwards" in proc.stderr
        assert "boom" in proc.stderr
        assert "CLAUDECODE leaked" not in calls

        # The next session finds the CLI it already reinstalled and does not
        # download it again.
        (h.fake / "calls.log").unlink()
        proc, calls = h.run()
        assert proc.returncode == 2
        assert "installer" not in calls
        assert "already reinstalled by this hook" in proc.stderr

    def test_cli_installed_by_another_tool_is_not_replaced(self, tmp_path):
        h = HookHarness(tmp_path, cli="other")
        h.flag("crash_marketplace_update")
        h.set_plugins([])
        proc, calls = h.run()
        assert proc.returncode == 2
        assert "installer" not in calls
        assert "panic: index out of bounds" in proc.stderr
        assert "update it with the tool that installed it" in proc.stderr
        assert not (h.home / ".local").exists()

    def test_failed_reinstall_is_reported_with_both_errors(self, tmp_path):
        h = HookHarness(tmp_path)
        h.flag("crash_marketplace_update")
        h.flag("fail_installer")
        h.set_plugins([])
        proc, calls = h.run()
        assert proc.returncode == 2
        assert "panic: index out of bounds" in proc.stderr
        assert "Reinstalling the claude CLI also failed" in proc.stderr
        assert "download failed" in proc.stderr
        assert not h.stamp.exists()

    def test_version_compare_is_numeric(self, tmp_path):
        h = HookHarness(tmp_path, min_version="0.99.0")
        h.set_plugins([_record("0.116.0", "user")])
        _, calls = h.run()
        assert calls == ["plugin list --json"]
