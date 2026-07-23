"""Tests for openrouter_kit.api_key precedence resolution."""

import pytest

from openrouter_kit import constants
from openrouter_kit.api_key import get_api_key
from openrouter_kit.env_file import write_env_file


@pytest.fixture
def isolated_paths(monkeypatch, tmp_path):
    """Redirect USER_ENV_FILE and project_env_file into tmp_path so tests
    never touch the developer's real credential file."""
    user_env = tmp_path / "user" / ".env"
    monkeypatch.setattr(constants, "USER_ENV_FILE", user_env)
    # api_key.py imported USER_ENV_FILE at module level; also patch there.
    monkeypatch.setattr("openrouter_kit.api_key.USER_ENV_FILE", user_env)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    return tmp_path


class TestGetApiKey:
    def test_missing_returns_missing_source(self, isolated_paths):
        result = get_api_key(project_root=isolated_paths / "project")
        assert result.key is None
        assert result.source == "missing"
        assert result.source_path is None

    def test_env_var_wins(self, isolated_paths, monkeypatch):
        # Populate every layer; env should still win.
        monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
        write_env_file(
            isolated_paths / "project" / ".local-data" / "openrouter-kit" / ".env",
            {"OPENROUTER_API_KEY": "from-project"},
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
            isolated_paths / "project" / ".local-data" / "openrouter-kit" / ".env",
            {"OPENROUTER_API_KEY": "from-project"},
        )
        write_env_file(
            isolated_paths / "user" / ".env",
            {"OPENROUTER_API_KEY": "from-user"},
        )
        result = get_api_key(project_root=isolated_paths / "project")
        assert result.key == "from-project"
        assert result.source == "project"
        assert result.source_path == isolated_paths / "project" / ".local-data" / "openrouter-kit" / ".env"

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
