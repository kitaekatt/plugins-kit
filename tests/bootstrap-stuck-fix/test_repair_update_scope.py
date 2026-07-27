"""Tests for the bootstrap-stuck-fix update-scope repair.

Same emphasis as the registry repair: this runs unattended on every session
start of every known user, so the properties that matter most are the ones
asserting it REFUSES to act -- on a healthy machine, on an ambiguous registry,
and on anything it does not recognize. A remediation that fires when it should
not is worse than the wedge it repairs.
"""

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "plugins" / "bootstrap-stuck-fix" / "scripts" / "repair_update_scope.py"
)


def _load_module():
    """Load by file path -- per the pythonpath note in pyproject.toml, per-plugin
    script dirs share one flat namespace and must not go on sys.path."""
    spec = importlib.util.spec_from_file_location("bsf_repair_update_scope", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_repair = _load_module()
plan_update = _repair.plan_update
latest_version = _repair.latest_version
run_update = _repair.run_update

REF = "bootstrap@plugins-kit"


def reg(*records, ref=REF):
    return {"plugins": {ref: list(records)}}


def mkt(version="0.60.0", name="bootstrap"):
    return {"plugins": [{"name": name, "version": version}]}


def project_record(version="0.57.0", path=r"C:\dev\proj"):
    return {"scope": "project", "projectPath": path, "version": version}


class TestPlanUpdate:
    def test_project_scope_record_behind_marketplace_is_updated_at_project(self):
        """The wedge itself: installed at project, manifest wants user, and the
        CLI refuses the user-scope request. Update where it actually lives."""
        scope, installed, newest = plan_update(reg(project_record()), mkt())
        assert (scope, installed, newest) == ("project", "0.57.0", "0.60.0")

    def test_user_scope_record_behind_marketplace_is_updated_at_user(self):
        scope, _, _ = plan_update(
            reg({"scope": "user", "version": "0.57.0"}), mkt()
        )
        assert scope == "user"

    def test_current_version_is_a_no_op(self):
        assert plan_update(reg(project_record("0.60.0")), mkt("0.60.0")) == (
            None, None, None,
        )

    def test_ahead_of_marketplace_is_a_no_op(self):
        """Never downgrade -- the version check is directional."""
        assert plan_update(reg(project_record("0.61.0")), mkt("0.60.0")) == (
            None, None, None,
        )

    def test_multiple_records_is_a_no_op(self):
        """That is the duplicate-record defect; acting on an ambiguous registry
        could deregister bootstrap entirely."""
        assert plan_update(
            reg(project_record(), {"scope": "user", "version": "0.57.0"}), mkt()
        ) == (None, None, None)

    def test_missing_ref_is_a_no_op(self):
        assert plan_update({"plugins": {}}, mkt()) == (None, None, None)

    def test_unreadable_registry_is_a_no_op(self):
        assert plan_update(None, mkt()) == (None, None, None)

    def test_unreadable_marketplace_is_a_no_op(self):
        assert plan_update(reg(project_record()), None) == (None, None, None)

    def test_record_without_version_is_a_no_op(self):
        assert plan_update(reg({"scope": "project"}), mkt()) == (None, None, None)

    def test_record_without_scope_is_a_no_op(self):
        assert plan_update(reg({"version": "0.57.0"}), mkt()) == (None, None, None)

    def test_bare_top_level_registry_shape_is_understood(self):
        assert plan_update({REF: [project_record()]}, mkt())[0] == "project"

    def test_version_comparison_is_numeric_not_lexical(self):
        """String compare would call 0.9.0 newer than 0.60.0."""
        scope, _, _ = plan_update(reg(project_record("0.9.0")), mkt("0.60.0"))
        assert scope == "project"


class TestLatestVersion:
    def test_finds_bootstrap_among_many(self):
        manifest = {"plugins": [
            {"name": "git-kit", "version": "9.9.9"},
            {"name": "bootstrap", "version": "0.60.0"},
        ]}
        assert latest_version(manifest) == "0.60.0"

    def test_absent_plugin_is_none(self):
        assert latest_version({"plugins": [{"name": "git-kit", "version": "1.0"}]}) is None

    def test_unrecognized_shape_is_none(self):
        assert latest_version({"plugins": "nope"}) is None
        assert latest_version(None) is None


class TestRunUpdate:
    def test_passes_the_recorded_scope_to_the_cli(self, monkeypatch):
        seen = {}

        class Proc:
            returncode = 0
            stdout = stderr = ""

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            return Proc()

        monkeypatch.setattr(_repair.subprocess, "run", fake_run)

        ok, _ = run_update("project", claude="claude")

        assert ok
        assert seen["cmd"] == [
            "claude", "plugin", "update", REF, "--scope", "project",
        ]

    def test_missing_cli_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setattr(_repair.shutil, "which", lambda name: None)
        ok, detail = run_update("project")
        assert not ok and "not found" in detail

    def test_subprocess_failure_is_reported_not_raised(self, monkeypatch):
        def boom(cmd, **kwargs):
            raise OSError("spawn failed")

        monkeypatch.setattr(_repair.subprocess, "run", boom)
        ok, detail = run_update("project", claude="claude")
        assert not ok and "spawn failed" in detail

    def test_nonzero_exit_surfaces_the_last_message_line(self, monkeypatch):
        class Proc:
            returncode = 1
            stdout = ""
            stderr = "noise\nPlugin is not installed at scope user"

        monkeypatch.setattr(_repair.subprocess, "run", lambda cmd, **kw: Proc())
        ok, detail = run_update("user", claude="claude")
        assert not ok and detail == "Plugin is not installed at scope user"


class TestMain:
    def test_healthy_machine_spawns_no_subprocess(self, tmp_path, monkeypatch):
        """The common path must stay cheap: two local reads, no CLI."""
        registry = tmp_path / "installed_plugins.json"
        registry.write_text(json.dumps(reg(project_record("0.60.0"))), encoding="utf-8")
        manifest = tmp_path / "marketplace.json"
        manifest.write_text(json.dumps(mkt("0.60.0")), encoding="utf-8")

        def explode(*a, **k):
            raise AssertionError("must not spawn a subprocess when up to date")

        monkeypatch.setattr(_repair.subprocess, "run", explode)

        assert _repair.main([
            "--registry", str(registry), "--marketplace", str(manifest),
        ]) == 0

    def test_dry_run_reports_without_updating(self, tmp_path, monkeypatch, capsys):
        registry = tmp_path / "installed_plugins.json"
        registry.write_text(json.dumps(reg(project_record())), encoding="utf-8")
        manifest = tmp_path / "marketplace.json"
        manifest.write_text(json.dumps(mkt()), encoding="utf-8")

        def explode(*a, **k):
            raise AssertionError("--dry-run must not run the update")

        monkeypatch.setattr(_repair.subprocess, "run", explode)

        assert _repair.main([
            "--registry", str(registry), "--marketplace", str(manifest),
            "--dry-run",
        ]) == 0
        out = capsys.readouterr().out
        assert "0.57.0 -> 0.60.0" in out and "project" in out

    def test_missing_files_exit_zero_silently(self, tmp_path):
        assert _repair.main([
            "--registry", str(tmp_path / "nope.json"),
            "--marketplace", str(tmp_path / "also-nope.json"),
        ]) == 0

    def test_failed_update_is_visible_and_still_exits_zero(
        self, tmp_path, monkeypatch, capsys
    ):
        """A silent persistent failure is how the machine stayed wedged
        unnoticed; it must exit 0 but say something."""
        registry = tmp_path / "installed_plugins.json"
        registry.write_text(json.dumps(reg(project_record())), encoding="utf-8")
        manifest = tmp_path / "marketplace.json"
        manifest.write_text(json.dumps(mkt()), encoding="utf-8")
        monkeypatch.setattr(_repair, "run_update", lambda scope: (False, "nope"))
        monkeypatch.setattr(_repair, "marker_path", lambda home=None: str(tmp_path / "m.json"))

        assert _repair.main([
            "--registry", str(registry), "--marketplace", str(manifest),
        ]) == 0
        assert "could not update" in capsys.readouterr().out
