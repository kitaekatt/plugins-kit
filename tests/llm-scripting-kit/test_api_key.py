"""Tests for llm_scripting_kit.api_key precedence resolution."""

import pytest

from llm_scripting_kit import constants
from llm_scripting_kit.api_key import get_api_key
from llm_scripting_kit.env_file import write_env_file

CANONICAL_REL = (".local-data", "plugins-kit", "llm-scripting-kit", ".env")
LEGACY_REL = (".local-data", "llm-scripting-kit", ".env")


def _canonical(project_root):
    return project_root.joinpath(*CANONICAL_REL)


def _legacy(project_root):
    return project_root.joinpath(*LEGACY_REL)


@pytest.fixture
def isolated_paths(monkeypatch, tmp_path):
    """Redirect USER_ENV_FILE and project_env_file into tmp_path so tests
    never touch the developer's real credential file."""
    user_env = tmp_path / "user" / ".env"
    monkeypatch.setattr(constants, "USER_ENV_FILE", user_env)
    # api_key.py imported USER_ENV_FILE at module level; also patch there.
    monkeypatch.setattr("llm_scripting_kit.api_key.USER_ENV_FILE", user_env)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # The one-time legacy notice is process-global; clear it so each test sees it.
    monkeypatch.setattr("llm_scripting_kit.api_key._LEGACY_NOTICE_SEEN", set())
    return tmp_path


class TestProjectPathShape:
    def test_canonical_project_path_is_marketplace_namespaced(self, tmp_path):
        """The project key path must mirror the project config.yaml path.

        Both live under ``<project>/.local-data/<marketplace>/<plugin>/`` --
        that symmetry is the whole point of the canonical location.
        """
        assert constants.project_env_file(tmp_path) == _canonical(tmp_path)

    def test_legacy_project_path_omits_the_marketplace_segment(self, tmp_path):
        assert constants.legacy_project_env_file(tmp_path) == _legacy(tmp_path)

    def test_project_env_files_is_highest_precedence_first(self, tmp_path):
        assert constants.project_env_files(tmp_path) == [
            _canonical(tmp_path),
            _legacy(tmp_path),
        ]


class TestGetApiKey:
    def test_missing_returns_missing_source(self, isolated_paths):
        result = get_api_key(project_root=isolated_paths / "project")
        assert result.key is None
        assert result.source == "missing"
        assert result.source_path is None
        assert result.legacy_location is False

    def test_env_var_wins(self, isolated_paths, monkeypatch):
        # Populate every layer; env should still win.
        monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
        write_env_file(
            _canonical(isolated_paths / "project"),
            {"OPENROUTER_API_KEY": "from-project"},
        )
        write_env_file(
            _legacy(isolated_paths / "project"),
            {"OPENROUTER_API_KEY": "from-legacy"},
        )
        write_env_file(
            isolated_paths / "user" / ".env",
            {"OPENROUTER_API_KEY": "from-user"},
        )
        result = get_api_key(project_root=isolated_paths / "project")
        assert result.key == "from-env"
        assert result.source == "env"
        assert result.source_path is None

    def test_project_wins_over_user(self, isolated_paths):
        write_env_file(
            _canonical(isolated_paths / "project"),
            {"OPENROUTER_API_KEY": "from-project"},
        )
        write_env_file(
            isolated_paths / "user" / ".env",
            {"OPENROUTER_API_KEY": "from-user"},
        )
        result = get_api_key(project_root=isolated_paths / "project")
        assert result.key == "from-project"
        assert result.source == "project"
        assert result.source_path == _canonical(isolated_paths / "project")
        assert result.legacy_location is False

    def test_user_when_only_user_set(self, isolated_paths):
        write_env_file(
            isolated_paths / "user" / ".env",
            {"OPENROUTER_API_KEY": "from-user"},
        )
        result = get_api_key(project_root=isolated_paths / "project")
        assert result.key == "from-user"
        assert result.source == "user"
        assert result.source_path == isolated_paths / "user" / ".env"

    def test_empty_env_var_falls_through_to_files(self, isolated_paths, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "")
        write_env_file(
            isolated_paths / "user" / ".env",
            {"OPENROUTER_API_KEY": "from-user"},
        )
        result = get_api_key(project_root=isolated_paths / "project")
        assert result.key == "from-user"
        assert result.source == "user"


class TestLegacyProjectLocation:
    """The superseded project path stays readable; it is flagged, not ignored."""

    def test_legacy_location_still_resolves(self, isolated_paths):
        write_env_file(
            _legacy(isolated_paths / "project"),
            {"OPENROUTER_API_KEY": "from-legacy"},
        )
        result = get_api_key(project_root=isolated_paths / "project")
        assert result.key == "from-legacy"
        assert result.source == "project"
        assert result.source_path == _legacy(isolated_paths / "project")
        assert result.legacy_location is True

    def test_legacy_location_wins_over_user(self, isolated_paths):
        write_env_file(
            _legacy(isolated_paths / "project"),
            {"OPENROUTER_API_KEY": "from-legacy"},
        )
        write_env_file(
            isolated_paths / "user" / ".env",
            {"OPENROUTER_API_KEY": "from-user"},
        )
        result = get_api_key(project_root=isolated_paths / "project")
        assert result.key == "from-legacy"
        assert result.legacy_location is True

    def test_canonical_wins_over_legacy(self, isolated_paths):
        write_env_file(
            _canonical(isolated_paths / "project"),
            {"OPENROUTER_API_KEY": "from-canonical"},
        )
        write_env_file(
            _legacy(isolated_paths / "project"),
            {"OPENROUTER_API_KEY": "from-legacy"},
        )
        result = get_api_key(project_root=isolated_paths / "project")
        assert result.key == "from-canonical"
        assert result.source_path == _canonical(isolated_paths / "project")
        assert result.legacy_location is False

    def test_legacy_hit_emits_a_visible_notice(self, isolated_paths, capsys):
        """Silence is the defect being fixed -- the fallback must announce itself."""
        write_env_file(
            _legacy(isolated_paths / "project"),
            {"OPENROUTER_API_KEY": "from-legacy"},
        )
        get_api_key(project_root=isolated_paths / "project")
        err = capsys.readouterr().err
        assert str(_legacy(isolated_paths / "project")) in err
        assert str(_canonical(isolated_paths / "project")) in err

    def test_notice_is_emitted_once_per_process(self, isolated_paths, capsys):
        write_env_file(
            _legacy(isolated_paths / "project"),
            {"OPENROUTER_API_KEY": "from-legacy"},
        )
        get_api_key(project_root=isolated_paths / "project")
        capsys.readouterr()
        get_api_key(project_root=isolated_paths / "project")
        assert capsys.readouterr().err == ""

    def test_canonical_hit_emits_no_notice(self, isolated_paths, capsys):
        write_env_file(
            _canonical(isolated_paths / "project"),
            {"OPENROUTER_API_KEY": "from-canonical"},
        )
        get_api_key(project_root=isolated_paths / "project")
        assert capsys.readouterr().err == ""
