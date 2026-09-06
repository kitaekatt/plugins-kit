"""Tests for plugins/bootstrap/lib/var_resolve.py."""

import os
from pathlib import Path

import pytest

from bootstrap_lib.var_resolve import resolve_vars, build_variables


class TestResolveVars:
    def test_simple_expansion(self):
        result = resolve_vars("${plugin_root}/stubs", {"plugin_root": "/opt/plugin"})
        assert result == "/opt/plugin/stubs"

    def test_multiple_vars(self):
        variables = {"plugin_root": "/opt/plugin", "data_dir": "/data"}
        result = resolve_vars("${plugin_root}/config in ${data_dir}", variables)
        assert result == "/opt/plugin/config in /data"

    def test_no_vars(self):
        result = resolve_vars("plain string", {"foo": "bar"})
        assert result == "plain string"

    def test_unresolved_returns_none(self):
        result = resolve_vars("${missing_var}/path", {"other": "val"})
        assert result is None

    def test_partial_unresolved_returns_none(self):
        result = resolve_vars("${known}/${unknown}", {"known": "ok"})
        assert result is None

    def test_empty_string(self):
        result = resolve_vars("", {"foo": "bar"})
        assert result == ""


class TestBuildVariables:
    def test_static_vars(self):
        variables = build_variables("/opt/plugin", "/data")
        assert variables["plugin_root"] == "/opt/plugin"
        assert variables["data_dir"] == "/data"

    def test_config_values_added(self):
        config = {"uproject": "/projects/MyGame/MyGame.uproject"}
        variables = build_variables("/opt/plugin", "/data", config)
        assert variables["uproject"] == "/projects/MyGame/MyGame.uproject"

    def test_dir_derived_from_file_path(self):
        config = {"uproject": "/projects/MyGame/MyGame.uproject"}
        variables = build_variables("/opt/plugin", "/data", config)
        # Path.parent uses OS-native separators, so compare with Path
        assert variables["uproject_dir"] == str(Path("/projects/MyGame"))

    def test_no_dir_for_simple_values(self):
        config = {"mode": "remote"}
        variables = build_variables("/opt/plugin", "/data", config)
        assert "mode_dir" not in variables

    def test_empty_config_values_skipped(self):
        config = {"uproject": "", "engine_dir": ""}
        variables = build_variables("/opt/plugin", "/data", config)
        assert "uproject" not in variables
        assert "engine_dir" not in variables

    def test_non_string_config_skipped(self):
        config = {"count": 42, "flag": True}
        variables = build_variables("/opt/plugin", "/data", config)
        assert "count" not in variables
        assert "flag" not in variables

    def test_none_config(self):
        variables = build_variables("/opt/plugin", "/data", None)
        assert len(variables) == 3  # plugin_root, data_dir, cwd

    def test_cwd_variable(self):
        variables = build_variables("/opt/plugin", "/data")
        assert "cwd" in variables
        assert variables["cwd"] == os.getcwd()

    def test_reserved_config_variables_cannot_override_static_values(self, tmp_path):
        variables = build_variables(
            "/real/plugin",
            "/real/data",
            {
                "cwd": "/spoofed/cwd",
                "plugin_root": "/spoofed/plugin",
                "data_dir": "/spoofed/data",
                "plugin_data_dir": "relative/data",
            },
        )

        assert variables["cwd"] == os.getcwd()
        assert variables["plugin_root"] == "/real/plugin"
        assert variables["data_dir"] == "/real/data"
        assert "plugin_data_dir" not in variables

    def test_plugin_data_dir_uses_project_and_namespace(self, tmp_path):
        variables = build_variables(
            "/opt/plugin",
            "/data/plugins-kit/demo-kit",
            project_root=str(tmp_path),
            marketplace="plugins-kit",
            plugin="demo-kit",
        )
        assert variables["plugin_data_dir"] == str(
            tmp_path / ".plugin-data" / "plugins-kit" / "demo-kit"
        )
        assert variables["cwd"] == str(tmp_path)

    def test_plugin_data_dir_honors_relative_override(self, tmp_path):
        variables = build_variables(
            "/opt/plugin",
            "/data/plugins-kit/demo-kit",
            {"plugin_data_dir": "Generated/PluginData"},
            project_root=str(tmp_path),
            marketplace="plugins-kit",
            plugin="demo-kit",
        )
        assert variables["plugin_data_dir"] == str(
            (tmp_path / "Generated" / "PluginData").resolve()
        )

    def test_plugin_data_dir_absent_without_project(self):
        variables = build_variables("/opt/plugin", "/data/plugins-kit/demo-kit")
        assert "plugin_data_dir" not in variables

    def test_bad_override_raises_rather_than_dropping_silently(self, tmp_path):
        """A malformed override must surface, not vanish.

        Swallowing it would leave the durable-path variable unexpanded in the
        manifest with nothing reported -- the silent-misconfiguration failure
        mode the durable project data pattern exists to prevent. The caller
        reports the ConfigError; it must not be absorbed here.
        """
        from bootstrap_lib.config_resolve import ConfigError

        with pytest.raises(ConfigError):
            build_variables(
                "/opt/plugin",
                "/data/plugins-kit/demo-kit",
                {"plugin_data_dir": str(tmp_path / "somewhere-absolute")},
                project_root=str(tmp_path),
                marketplace="plugins-kit",
                plugin="demo-kit",
            )
