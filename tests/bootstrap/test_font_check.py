"""Tests for bootstrap_lib/font_check.py.

Detection is tested by pointing the scan dirs at a tmp_path. Installation is
tested end-to-end against a file:// font archive (no network), with the
per-user font dir redirected to tmp_path. Windows registration is a
best-effort side effect and is not asserted (it is a no-op off-Windows).
"""

import hashlib
import os
import pathlib
import sys
import zipfile

import pytest

from bootstrap_lib import font_check


def _sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_uri(path):
    return pathlib.Path(path).resolve().as_uri()


class TestFindInstalledFont:
    def test_finds_matching_font_by_glob(self, tmp_path, monkeypatch):
        (tmp_path / "JetBrainsMonoNerdFont-Regular.ttf").write_bytes(b"font")
        monkeypatch.setattr(font_check, "_scan_dirs", lambda: [str(tmp_path)])
        assert font_check.find_installed_font("*JetBrainsMono*NerdFont*") == \
            "JetBrainsMonoNerdFont-Regular.ttf"

    def test_match_is_case_insensitive(self, tmp_path, monkeypatch):
        (tmp_path / "JetBrainsMonoNerdFont-Bold.ttf").write_bytes(b"font")
        monkeypatch.setattr(font_check, "_scan_dirs", lambda: [str(tmp_path)])
        # Pattern in a different case than the filename still matches.
        assert font_check.find_installed_font("*jetbrainsmono*nerdfont*") is not None

    def test_returns_none_when_absent(self, tmp_path, monkeypatch):
        (tmp_path / "SomeOtherFont.ttf").write_bytes(b"font")
        monkeypatch.setattr(font_check, "_scan_dirs", lambda: [str(tmp_path)])
        assert font_check.find_installed_font("*JetBrainsMono*NerdFont*") is None

    def test_finds_font_nested_in_family_subdir(self, tmp_path, monkeypatch):
        nested = tmp_path / "jetbrains-mono"
        nested.mkdir()
        (nested / "JetBrainsMonoNerdFont-Italic.ttf").write_bytes(b"font")
        monkeypatch.setattr(font_check, "_scan_dirs", lambda: [str(tmp_path)])
        assert font_check.find_installed_font("*JetBrainsMono*NerdFont*") == \
            "JetBrainsMonoNerdFont-Italic.ttf"

    def test_missing_scan_dir_is_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            font_check, "_scan_dirs",
            lambda: [str(tmp_path / "does-not-exist"), str(tmp_path)],
        )
        (tmp_path / "Match-Regular.ttf").write_bytes(b"font")
        assert font_check.find_installed_font("*Match*") == "Match-Regular.ttf"


class TestCheckFont:
    def test_passed_when_present(self, tmp_path, monkeypatch):
        (tmp_path / "Match-Regular.ttf").write_bytes(b"font")
        monkeypatch.setattr(font_check, "_scan_dirs", lambda: [str(tmp_path)])
        res = font_check.check_font("*Match*")
        assert res.passed
        assert res.matched == "Match-Regular.ttf"

    def test_not_passed_when_absent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(font_check, "_scan_dirs", lambda: [str(tmp_path)])
        res = font_check.check_font("*Match*")
        assert not res.passed
        assert res.matched is None


class TestUserFontDir:
    def test_user_font_dir_is_per_user_location(self):
        d = font_check.user_font_dir()
        if sys.platform == "win32":
            assert "Fonts" in d and "Microsoft" in d
        elif sys.platform == "darwin":
            assert d.endswith("Library/Fonts")
        else:
            assert d.endswith(".local/share/fonts")


class TestInstallFont:
    def test_installs_faces_into_user_font_dir(self, tmp_path, monkeypatch):
        archive = tmp_path / "Fam.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("Fam-Regular.ttf", b"reg")
            z.writestr("Fam-Bold.ttf", b"bold")
            z.writestr("README.md", b"nope")
        sha = _sha256_of(archive)

        font_dir = tmp_path / "user-fonts"
        monkeypatch.setattr(font_check, "user_font_dir", lambda: str(font_dir))
        # Don't touch the real font cache / registry during the test.
        monkeypatch.setattr(font_check, "register_fonts", lambda paths: None)

        result = font_check.install_font(_file_uri(archive), sha)
        assert result.ok, result.message
        assert (font_dir / "Fam-Regular.ttf").is_file()
        assert (font_dir / "Fam-Bold.ttf").is_file()
        assert not (font_dir / "README.md").exists()
        assert len(result.files) == 2

    def test_hash_mismatch_reports_failure(self, tmp_path, monkeypatch):
        archive = tmp_path / "Fam.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("Fam-Regular.ttf", b"reg")
        font_dir = tmp_path / "user-fonts"
        monkeypatch.setattr(font_check, "user_font_dir", lambda: str(font_dir))
        monkeypatch.setattr(font_check, "register_fonts", lambda paths: None)
        result = font_check.install_font(_file_uri(archive), "0" * 64)
        assert not result.ok
        assert "sha256 mismatch" in result.message


class TestEngineFontsLoop:
    def test_successful_font_install_with_failed_recheck_is_failure(self, tmp_path, monkeypatch):
        from bootstrap_lib import engine as engine_mod
        from bootstrap_lib.font_check import FontInstallResult, FontCheckResult

        checks = iter([
            FontCheckResult(False, None, "missing"),
            FontCheckResult(False, None, "still missing"),
        ])
        monkeypatch.setattr("bootstrap_lib.font_check.check_font", lambda match: next(checks))
        monkeypatch.setattr(
            "bootstrap_lib.font_check.install_font",
            lambda *args, **kwargs: FontInstallResult(True, ["/tmp/Family.ttf"], "installed"),
        )
        action, ok = [], []
        failures = engine_mod._process_manifest(
            {"fonts": [{
                "name": "Family", "match": "*Family*",
                "download": {"url": "https://example.invalid/f.zip", "sha256": "abc"},
            }]},
            "linux", str(tmp_path / "data"), str(tmp_path / "root"),
            action, ok, plugin_name="t",
        )

        assert failures
        assert "re-check failed" in failures[0]["message"]
        assert not any("fonts installed" in item for item in action)

    def test_successful_url_download_with_failed_recheck_is_failure(self, tmp_path, monkeypatch):
        from bootstrap_lib import downloader, engine as engine_mod
        from bootstrap_lib.result import Result

        checks = iter([
            Result(False, "tool", "missing", extras={"install_cmd": None}),
            Result(False, "tool", "still missing", extras={"install_cmd": None}),
        ])
        monkeypatch.setattr(engine_mod, "_tool_check", lambda ctx: next(checks))
        monkeypatch.setattr(
            downloader, "download_and_install",
            lambda *args, **kwargs: downloader.DownloadResult(True, "/tmp/tool", "downloaded"),
        )
        action, ok, installed = [], [], []
        failure = engine_mod._process_tool_entry(
            {"name": "tool", "download": {
                "linux": {"url": "https://example.invalid/tool", "sha256": "abc"},
            }},
            "linux", str(tmp_path / "data"), "", action, ok, installed, plugin_name="t",
        )

        assert failure is not None
        assert "re-check failed" in failure["message"]
        assert installed == []
        assert not any("downloaded to" in item for item in action)

    def test_successful_ini_write_with_failed_recheck_is_failure(self, tmp_path, monkeypatch):
        from bootstrap_lib import engine as engine_mod
        from bootstrap_lib.ini_check import IniCheckResult

        ini_file = str(tmp_path / "settings.ini")
        checks = iter([
            IniCheckResult(False, ini_file, "[Section]", "key", "missing"),
            IniCheckResult(False, ini_file, "[Section]", "key", "still wrong"),
        ])
        monkeypatch.setattr("bootstrap_lib.ini_check.check_ini_setting", lambda *args: next(checks))
        monkeypatch.setattr("bootstrap_lib.ini_check.write_ini_setting", lambda *args: None)

        class Context:
            manifest = {"ini_settings": [{
                "file": ini_file, "section": "Section", "settings": {"key": "value"},
            }]}
            project_detected = True
            variables = {}
            action_entries = []
            ok_entries = []
            failures = []

            def ok(self, message):
                self.ok_entries.append(message)

            def action(self, message):
                self.action_entries.append(message)

            def fail(self, entry, **failure):
                self.action_entries.append(entry)
                self.failures.append(failure)

        ctx = Context()
        engine_mod._phase_ini_settings(ctx)

        assert ctx.failures
        assert "re-check failed" in ctx.failures[0]["message"]
        assert not any("set to" in item for item in ctx.action_entries)

    def test_malformed_entry_missing_name_is_skipped_not_fatal(self, tmp_path):
        # A font entry without "name" (e.g. a typo in a layered bootstrap.json)
        # must not abort the whole bootstrap run with a KeyError.
        from bootstrap_lib.engine import _process_manifest
        manifest = {"fonts": [{"match": "*Whatever*"}]}
        action, ok = [], []
        failures = _process_manifest(
            manifest, "linux", str(tmp_path / "data"), str(tmp_path / "root"),
            action, ok, plugin_name="t",
        )
        assert failures == []
        assert any("skipped malformed entry" in a for a in action)

    def test_no_download_declared_logs_action(self, tmp_path, monkeypatch):
        from bootstrap_lib import engine as engine_mod
        # Force "not installed" so the no-download branch is exercised.
        monkeypatch.setattr(
            engine_mod, "_process_manifest", engine_mod._process_manifest
        )
        from bootstrap_lib.font_check import FontCheckResult
        import bootstrap_lib.font_check as fc
        monkeypatch.setattr(fc, "find_installed_font", lambda match: None)
        manifest = {"fonts": [{"name": "Ghost Font", "match": "*Ghost*"}]}
        action, ok = [], []
        failures = engine_mod._process_manifest(
            manifest, "linux", str(tmp_path / "data"), str(tmp_path / "root"),
            action, ok, plugin_name="t",
        )
        assert failures == []
        assert any("no download declared" in a for a in action)


class TestRegisterFonts:
    def test_empty_list_is_noop(self):
        # Must not raise regardless of platform.
        font_check.register_fonts([])

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX/macOS path")
    def test_non_windows_register_does_not_raise(self, tmp_path):
        f = tmp_path / "X-Regular.ttf"
        f.write_bytes(b"font")
        # On Linux this best-effort calls fc-cache (skipped if absent); on
        # macOS it's a no-op. Either way it must not raise.
        font_check.register_fonts([str(f)])
