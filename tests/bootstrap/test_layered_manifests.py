"""Tests for engine._load_layered_manifests parse-error surfacing."""

import json
from pathlib import Path

import pytest

from bootstrap_lib.engine import (
    _load_layered_manifests,
    _normalize_project_shared_lib_imports,
    _process_project_venv,
)


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point HOME at a tmp dir so user-level bootstrap.json isolation is clean."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


class TestLoadLayeredManifests:
    def test_no_files_returns_empty(self, isolated_home, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        merged, errors = _load_layered_manifests(str(project))
        assert merged == {}
        assert errors == []

    def test_valid_layers_merged(self, isolated_home, tmp_path):
        # User layer
        user_claude = isolated_home / ".claude"
        user_claude.mkdir()
        (user_claude / "bootstrap.json").write_text(
            json.dumps({"plugins": [{"ref": "a:b", "scope": "user"}]})
        )
        # Project layer
        project = tmp_path / "project"
        project_claude = project / ".claude"
        project_claude.mkdir(parents=True)
        (project_claude / "bootstrap.json").write_text(
            json.dumps({"plugins": [{"ref": "c:d", "scope": "user"}]})
        )

        merged, errors = _load_layered_manifests(str(project))

        assert errors == []
        refs = {p["ref"] for p in merged["plugins"]}
        assert refs == {"a:b", "c:d"}

    def test_malformed_project_layer_surfaces_error(self, isolated_home, tmp_path):
        project = tmp_path / "project"
        project_claude = project / ".claude"
        project_claude.mkdir(parents=True)
        bad = project_claude / "bootstrap.json"
        # Missing comma between objects (real-world failure mode)
        bad.write_text(
            '{"plugins": [\n'
            '  {"ref": "a:b", "scope": "user"}\n'
            '  {"ref": "c:d", "scope": "user"}\n'
            ']}'
        )

        merged, errors = _load_layered_manifests(str(project))

        assert merged == {}
        assert len(errors) == 1
        assert errors[0]["path"] == str(bad)
        assert "JSON parse error" in errors[0]["error"]

    def test_malformed_layer_does_not_block_other_layers(self, isolated_home, tmp_path):
        # Valid user layer
        user_claude = isolated_home / ".claude"
        user_claude.mkdir()
        (user_claude / "bootstrap.json").write_text(
            json.dumps({"plugins": [{"ref": "a:b", "scope": "user"}]})
        )
        # Malformed project layer
        project = tmp_path / "project"
        project_claude = project / ".claude"
        project_claude.mkdir(parents=True)
        (project_claude / "bootstrap.json").write_text("{not json")

        merged, errors = _load_layered_manifests(str(project))

        # User layer still applied
        assert "plugins" in merged
        assert merged["plugins"][0]["ref"] == "a:b"
        # Error still reported
        assert len(errors) == 1
        assert "bootstrap.json" in errors[0]["path"]

    def test_legacy_user_bootstrap_parse_error_reported(self, isolated_home, tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        legacy = data_dir / "user-bootstrap.json"
        legacy.write_text("{bad")

        merged, errors = _load_layered_manifests(None, str(data_dir))

        assert merged == {}
        assert len(errors) == 1
        assert errors[0]["path"] == str(legacy)


class TestProjectVenvSharedLibImports:
    """project_venv.shared_lib_imports: layered merge and validation."""

    def test_user_and_project_layers_both_contribute(self, isolated_home, tmp_path):
        user_claude = isolated_home / ".claude"
        user_claude.mkdir()
        (user_claude / "bootstrap.json").write_text(json.dumps(
            {"project_venv": {"shared_lib_imports": ["lib_user", "lib_both"]}}))
        project = tmp_path / "project"
        (project / ".claude").mkdir(parents=True)
        (project / ".claude" / "bootstrap.json").write_text(json.dumps(
            {"project_venv": {"shared_lib_imports": [
                {"name": "lib_both"}, {"name": "lib_proj", "marketplace": "mk"}]}}))

        merged, errors = _load_layered_manifests(str(project))

        assert errors == []
        assert merged["project_venv"]["shared_lib_imports"] == [
            "lib_user", "lib_both", {"name": "lib_proj", "marketplace": "mk"}]

    def test_valid_forms_normalize(self):
        entries, failures = _normalize_project_shared_lib_imports({
            "shared_lib_imports": ["a", {"name": "b", "marketplace": "mk"},
                                   {"name": "a"}]})
        assert failures == []
        assert entries == [{"name": "a", "marketplace": None},
                           {"name": "b", "marketplace": "mk"}]

    @pytest.mark.parametrize("item", [
        42,
        "",
        {"marketplace": "mk"},
        {"name": 7},
        {"name": "a", "marketplace": ""},
        {"name": "a", "marketplace": 3},
        {"name": "a", "version": "1"},
        ["a"],
    ])
    def test_malformed_entry_is_a_descriptive_failure(self, item):
        entries, failures = _normalize_project_shared_lib_imports(
            {"shared_lib_imports": ["good", item]})
        assert entries == [{"name": "good", "marketplace": None}]
        assert len(failures) == 1
        f = failures[0]
        assert f["type"] == "project_venv" and f["plugin"] == "config"
        assert f["message"].startswith("shared_lib_imports entry [1]")
        assert repr(item) in f["message"]

    def test_non_list_value_is_a_failure(self):
        entries, failures = _normalize_project_shared_lib_imports(
            {"shared_lib_imports": "content_pipeline"})
        assert entries == []
        assert len(failures) == 1
        assert "must be a list" in failures[0]["message"]

    def test_process_project_venv_reports_malformed_entry(self, tmp_path):
        """The failure reaches the Step 3d failure list with no pyproject.toml."""
        _action, _ok, failures = _process_project_venv(
            {"shared_lib_imports": [{"name": "a", "marketplace": 3}]}, str(tmp_path))
        assert len(failures) == 1
        assert "shared_lib_imports entry [0]" in failures[0]["message"]
