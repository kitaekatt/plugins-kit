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

FAKE_CLAUDE = r'''#!/usr/bin/env bash
# Fake claude CLI: logs each call, serves listings from files in $FAKE_DIR.
printf '%s\n' "$*" >> "$FAKE_DIR/calls.log"
if [ -n "${CLAUDECODE:-}" ]; then echo "CLAUDECODE leaked" >> "$FAKE_DIR/calls.log"; fi
case "$*" in
  "plugin list --json") cat "$FAKE_DIR/plugins.json" ;;
  "plugin marketplace list --json") cat "$FAKE_DIR/marketplaces.json" ;;
  "plugin install "*|"plugin update "*)
    [ -f "$FAKE_DIR/fail_plugin_step" ] && { echo "boom"; exit 1; }
    cp "$FAKE_DIR/plugins_after.json" "$FAKE_DIR/plugins.json" ;;
esac
exit 0
'''


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

    def __init__(self, tmp_path, min_version="1.2.0", bootstrap_json=True):
        self.project = _project(tmp_path, bootstrap_json=bootstrap_json)
        self.fake = tmp_path / "fake"
        self.fake.mkdir()
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        claude = bin_dir / "claude"
        claude.write_bytes(FAKE_CLAUDE.encode("ascii"))
        claude.chmod(0o755)
        self.bin_dir = bin_dir
        template = (PLUGIN_ROOT / "templates" / gen.HOOK_FILENAME).read_bytes()
        self.hook = tmp_path / gen.HOOK_FILENAME
        self.hook.write_bytes(template.replace(b"@MIN_VERSION@", min_version.encode()))
        self.set_marketplaces(present=True)

    def set_plugins(self, records, after=None):
        (self.fake / "plugins.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
        (self.fake / "plugins_after.json").write_text(
            json.dumps(after if after is not None else records, indent=2), encoding="utf-8")

    def set_marketplaces(self, present):
        items = [{"name": "plugins-kit", "source": "git"}] if present else []
        (self.fake / "marketplaces.json").write_text(json.dumps(items, indent=2), encoding="utf-8")

    def run(self):
        env = dict(os.environ)
        env["PATH"] = str(self.bin_dir) + os.pathsep + env.get("PATH", "")
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
        assert proc.returncode == 0
        assert calls == [
            "plugin list --json",
            "plugin marketplace list --json",
            "plugin marketplace add https://github.com/kitaekatt/plugins-kit.git",
            "plugin marketplace update plugins-kit",
            "plugin install bootstrap@plugins-kit --scope user",
            "plugin list --json",
        ]
        message = json.loads(proc.stdout)["systemMessage"]
        assert "installed bootstrap@plugins-kit 1.2.0" in message

    def test_old_project_record_is_updated_at_its_scope(self, tmp_path):
        h = HookHarness(tmp_path)
        project_path = str(h.project).replace("/", "\\").upper()
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
        assert "updated bootstrap@plugins-kit 1.2.1" in json.loads(proc.stdout)["systemMessage"]

    def test_other_projects_records_do_not_count(self, tmp_path):
        h = HookHarness(tmp_path)
        h.set_plugins([_record("9.0.0", "project", "C:\\elsewhere")],
                      after=[_record("1.2.0", "user")])
        proc, calls = h.run()
        assert "plugin install bootstrap@plugins-kit --scope user" in calls

    def test_failed_step_reports_and_exits_zero(self, tmp_path):
        h = HookHarness(tmp_path)
        h.set_plugins([])
        (h.fake / "fail_plugin_step").write_text("", encoding="utf-8")
        proc, calls = h.run()
        assert proc.returncode == 0
        message = json.loads(proc.stdout)["systemMessage"]
        assert "could not bring bootstrap@plugins-kit to 1.2.0" in message
        assert "boom" in message
        assert "CLAUDECODE leaked" not in calls

    def test_version_compare_is_numeric(self, tmp_path):
        h = HookHarness(tmp_path, min_version="0.99.0")
        h.set_plugins([_record("0.116.0", "user")])
        _, calls = h.run()
        assert calls == ["plugin list --json"]
