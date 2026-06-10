"""Tests for bootstrap_lib/result.py — the shared check-result type (B12)."""

import pytest

from bootstrap_lib.result import Result


class TestResult:
    def test_core_fields(self):
        r = Result(passed=True, subject="git", message="found at /usr/bin/git")
        assert r.passed is True
        assert r.subject == "git"
        assert r.message == "found at /usr/bin/git"
        assert r.remediation_cmd is None
        assert r.extras == {}

    def test_extras_readable_as_attributes(self):
        r = Result(passed=True, subject="git", message="ok",
                   extras={"path": "/usr/bin/git", "on_path": True})
        assert r.path == "/usr/bin/git"
        assert r.on_path is True
        assert r.extras["path"] == "/usr/bin/git"

    def test_missing_extra_raises_attribute_error(self):
        r = Result(passed=False, subject="x", message="m", extras={"path": None})
        with pytest.raises(AttributeError) as exc:
            _ = r.nonexistent_field
        # The error names the available extras to aid debugging.
        assert "path" in str(exc.value)

    def test_frozen(self):
        r = Result(passed=True, subject="x", message="m")
        with pytest.raises(Exception):
            r.passed = False

    def test_check_modules_return_shared_type(self):
        """The previously-triplicated CheckResult shapes all return Result now."""
        from bootstrap_lib.tool_check import check_tool
        from bootstrap_lib.path_check import check_path_entry
        from bootstrap_lib.python_stub_check import check_python_stub
        from bootstrap_lib.venv_check import check_venv
        from bootstrap_lib.git_dep_check import check_git_dep

        assert isinstance(check_tool("nonexistent_xyz_tool"), Result)
        assert isinstance(check_path_entry("/nonexistent/dir"), Result)
        assert isinstance(check_python_stub("~/nonexistent", ["WindowsApps"]), Result)
        assert isinstance(check_venv("/nonexistent", "/nonexistent", []), Result)
        assert isinstance(
            check_git_dep("/nonexistent", "https://example.com/r.git", "main"), Result
        )
