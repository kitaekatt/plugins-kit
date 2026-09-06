"""Tests for bootstrap_lib/downloader.py.

Uses file:// URLs to avoid network dependence. Each test builds the
artifact it wants the downloader to fetch in a tmp_path, then points the
downloader at it via a file:// URI.
"""

import hashlib
import io
import os
import pathlib
import sys
import tarfile
import zipfile
from unittest.mock import MagicMock

import pytest

from bootstrap_lib import downloader
from bootstrap_lib import pypi_check


def _sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_uri(path):
    # pathlib.Path.as_uri normalizes Windows backslashes and adds the
    # right drive-letter prefix for file:// URIs.
    return pathlib.Path(path).resolve().as_uri()


class TestSingleFileDownload:
    def test_single_file_download_installs_at_target(self, tmp_path):
        src = tmp_path / "jq-binary"
        src.write_bytes(b"#!/bin/sh\necho jq stub\n")
        sha = _sha256_of(src)

        target_dir = tmp_path / "bin"
        result = downloader.download_and_install(
            "jq",
            _file_uri(src),
            sha,
            target_dir=str(target_dir),
        )
        assert result.ok, result.message
        assert os.path.isfile(result.path)
        # Filename: "jq" on Unix, "jq.exe" on Windows.
        expected = "jq.exe" if sys.platform == "win32" else "jq"
        assert os.path.basename(result.path) == expected

    def test_hash_mismatch_reports_failure(self, tmp_path):
        src = tmp_path / "bad"
        src.write_bytes(b"different bytes than expected")

        target_dir = tmp_path / "bin"
        result = downloader.download_and_install(
            "tool",
            _file_uri(src),
            "0" * 64,  # known-wrong hash
            target_dir=str(target_dir),
        )
        assert not result.ok
        assert "sha256 mismatch" in result.message
        # Nothing should land in target_dir.
        assert not target_dir.exists() or list(target_dir.iterdir()) == []

    def test_explicit_binary_name(self, tmp_path):
        src = tmp_path / "raw"
        src.write_bytes(b"payload")
        sha = _sha256_of(src)

        target_dir = tmp_path / "bin"
        result = downloader.download_and_install(
            "jqlang",
            _file_uri(src),
            sha,
            binary_name="jq",
            target_dir=str(target_dir),
        )
        assert result.ok
        expected = "jq.exe" if sys.platform == "win32" else "jq"
        assert os.path.basename(result.path) == expected

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits only")
    def test_installed_file_is_executable(self, tmp_path):
        src = tmp_path / "x"
        src.write_bytes(b"#!/bin/sh\n")
        sha = _sha256_of(src)
        result = downloader.download_and_install(
            "x", _file_uri(src), sha, target_dir=str(tmp_path / "bin"),
        )
        assert result.ok
        mode = os.stat(result.path).st_mode
        assert mode & 0o111  # user/group/other execute set


class TestZipArchive:
    def test_extracts_inner_path_from_zip(self, tmp_path):
        # Build a zip containing bin/jq.
        inner_payload = b"jq archive payload"
        archive = tmp_path / "jq.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("bin/jq", inner_payload)
            z.writestr("README.md", b"unused")
        sha = _sha256_of(archive)

        target_dir = tmp_path / "bin"
        result = downloader.download_and_install(
            "jq",
            _file_uri(archive),
            sha,
            archive_path="bin/jq",
            target_dir=str(target_dir),
        )
        assert result.ok, result.message
        with open(result.path, "rb") as f:
            assert f.read() == inner_payload

    def test_missing_archive_path_for_archive_url_fails(self, tmp_path):
        archive = tmp_path / "x.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("anything", b"x")
        sha = _sha256_of(archive)
        result = downloader.download_and_install(
            "x", _file_uri(archive), sha,
            target_dir=str(tmp_path / "bin"),
            archive_type="zip",
        )
        assert not result.ok
        assert "no archive_path" in result.message


class TestTarGzArchive:
    def test_extracts_inner_path_from_tar_gz(self, tmp_path):
        inner_payload = b"gh archive payload"
        archive = tmp_path / "gh.tar.gz"
        # Build a real tar.gz with an inner file.
        inner_file = tmp_path / "_staging" / "gh_2.0_linux_amd64" / "bin" / "gh"
        inner_file.parent.mkdir(parents=True)
        inner_file.write_bytes(inner_payload)
        with tarfile.open(archive, "w:gz") as t:
            t.add(inner_file, arcname="gh_2.0_linux_amd64/bin/gh")
        sha = _sha256_of(archive)

        target_dir = tmp_path / "bin"
        result = downloader.download_and_install(
            "gh",
            _file_uri(archive),
            sha,
            archive_path="gh_2.0_linux_amd64/bin/gh",
            target_dir=str(target_dir),
        )
        assert result.ok, result.message
        with open(result.path, "rb") as f:
            assert f.read() == inner_payload


class TestDownloadFonts:
    def test_extracts_all_font_faces_from_zip(self, tmp_path):
        archive = tmp_path / "FontFam.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("FontFam-Regular.ttf", b"regular face")
            z.writestr("FontFam-Bold.ttf", b"bold face")
            z.writestr("FontFam-Italic.otf", b"italic otf face")
            z.writestr("README.md", b"not a font")
            z.writestr("LICENSE", b"license text")
        sha = _sha256_of(archive)

        dest = tmp_path / "fonts"
        result = downloader.download_fonts(_file_uri(archive), sha, str(dest))
        assert result.ok, result.message
        names = sorted(os.path.basename(p) for p in result.files)
        assert names == ["FontFam-Bold.ttf", "FontFam-Italic.otf", "FontFam-Regular.ttf"]
        # Non-font files are not extracted.
        assert not (dest / "README.md").exists()
        assert not (dest / "LICENSE").exists()
        assert (dest / "FontFam-Regular.ttf").read_bytes() == b"regular face"

    def test_flattens_nested_members_from_tar_xz(self, tmp_path):
        # Some font archives nest faces in subdirs; extraction is by basename.
        staging = tmp_path / "_staging" / "ttf"
        staging.mkdir(parents=True)
        (staging / "Nested-Regular.ttf").write_bytes(b"nested regular")
        archive = tmp_path / "Nested.tar.xz"
        with tarfile.open(archive, "w:xz") as t:
            t.add(staging / "Nested-Regular.ttf", arcname="ttf/Nested-Regular.ttf")
        sha = _sha256_of(archive)

        dest = tmp_path / "fonts"
        result = downloader.download_fonts(_file_uri(archive), sha, str(dest))
        assert result.ok, result.message
        # Flattened to basename directly under dest.
        assert (dest / "Nested-Regular.ttf").is_file()
        assert [os.path.basename(p) for p in result.files] == ["Nested-Regular.ttf"]

    def test_hash_mismatch_extracts_nothing(self, tmp_path):
        archive = tmp_path / "x.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("X-Regular.ttf", b"x")
        dest = tmp_path / "fonts"
        result = downloader.download_fonts(_file_uri(archive), "0" * 64, str(dest))
        assert not result.ok
        assert "sha256 mismatch" in result.message
        assert not dest.exists() or list(dest.iterdir()) == []

    def test_no_matching_members_fails(self, tmp_path):
        archive = tmp_path / "nofonts.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("README.md", b"docs only")
        sha = _sha256_of(archive)
        dest = tmp_path / "fonts"
        result = downloader.download_fonts(_file_uri(archive), sha, str(dest))
        assert not result.ok
        assert "no files matching" in result.message

    def test_custom_suffixes(self, tmp_path):
        archive = tmp_path / "woff.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("Face.woff2", b"woff2 bytes")
            z.writestr("Face.ttf", b"ttf bytes")
        sha = _sha256_of(archive)
        dest = tmp_path / "fonts"
        result = downloader.download_fonts(_file_uri(archive), sha, str(dest), suffixes=(".woff2",))
        assert result.ok, result.message
        assert [os.path.basename(p) for p in result.files] == ["Face.woff2"]


@pytest.mark.parametrize("installer", ["binary", "font", "pypi"])
def test_destination_parent_file_returns_failed_result(installer, tmp_path, monkeypatch):
    """A regular file where the destination directory should be is reported."""
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_bytes(b"blocker")

    if installer == "binary":
        source = tmp_path / "payload"
        source.write_bytes(b"payload")
        result = downloader.download_and_install(
            "tool", _file_uri(source), _sha256_of(source),
            target_dir=str(blocked_parent),
        )
        assert not result.ok
    elif installer == "font":
        archive = tmp_path / "fonts.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("Family-Regular.ttf", b"font")
        result = downloader.download_fonts(
            _file_uri(archive), _sha256_of(archive), str(blocked_parent),
        )
        assert not result.ok
    else:
        wheel = io.BytesIO()
        with zipfile.ZipFile(wheel, "w") as zf:
            zf.writestr("pkg/module.py", b"# module")
        response = MagicMock()
        response.read.return_value = wheel.getvalue()
        response.__enter__ = MagicMock(return_value=response)
        response.__exit__ = MagicMock(return_value=False)
        monkeypatch.setattr(
            pypi_check, "_get_wheel_url",
            lambda package: ("https://example.invalid/pkg.whl", ""),
        )
        monkeypatch.setattr(pypi_check, "urlopen", lambda *args, **kwargs: response)
        result = pypi_check.download_and_extract(
            "pkg", str(blocked_parent / "module.py"),
        )
        assert not result.passed

    assert "failed" in result.message or "cannot" in result.message or "not a directory" in result.message


class TestArchiveTypeDetection:
    @pytest.mark.parametrize("url,expected", [
        ("https://example.com/x.zip", "zip"),
        ("https://example.com/x.tar.gz", "tar.gz"),
        ("https://example.com/x.tgz", "tar.gz"),
        ("https://example.com/x.tar.xz", "tar.xz"),
        ("https://example.com/x.tar", "tar"),
        ("https://example.com/x.exe", None),
        ("https://example.com/x", None),
        ("https://example.com/x.zip?token=abc", "zip"),
    ])
    def test_archive_type_detection(self, url, expected):
        assert downloader._detect_archive_type(url, None) == expected

    def test_explicit_archive_type_overrides_detection(self):
        assert downloader._detect_archive_type("https://example.com/x.exe", "tar.gz") == "tar.gz"


class TestFetchTimeout:
    def test_fetch_passes_timeout_to_urlopen(self, tmp_path, monkeypatch):
        """_fetch must give urlopen a socket timeout (B11) — without one, a
        stalled connection hangs the background engine forever."""
        captured = {}

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, n=-1):
                return b""

        def fake_urlopen(url, timeout=None):
            captured["timeout"] = timeout
            return _FakeResp()

        monkeypatch.setattr(downloader, "urlopen", fake_urlopen)
        downloader._fetch("https://example.invalid/x", str(tmp_path / "out"))
        assert captured.get("timeout"), "urlopen must be called with a positive timeout"
        assert captured["timeout"] > 0


class TestStagingLocation:
    def test_staged_file_lives_in_target_dir(self, tmp_path, monkeypatch):
        """The pre-replace staging file must be created in bin_dir itself, so
        os.replace is a same-filesystem rename (B11) — staging in the system
        temp dir raises EXDEV when /tmp is tmpfs on Linux."""
        src = tmp_path / "payload"
        src.write_bytes(b"binary payload")
        sha = _sha256_of(src)
        target_dir = tmp_path / "bin"

        moves = []
        real_replace = os.replace

        def spy(s, d):
            moves.append((str(s), str(d)))
            return real_replace(s, d)

        monkeypatch.setattr("bootstrap_lib.downloader.os.replace", spy)
        result = downloader.download_and_install(
            "jq", _file_uri(src), sha, target_dir=str(target_dir),
        )
        assert result.ok, result.message
        final_moves = [m for m in moves if m[1] == result.path]
        assert final_moves, "expected an os.replace into the final path"
        staged_src = final_moves[-1][0]
        assert os.path.dirname(staged_src) == str(target_dir), (
            f"staging must happen in the destination dir, got {staged_src}"
        )

    def test_success_leaves_only_final_binary_in_target_dir(self, tmp_path):
        src = tmp_path / "payload"
        src.write_bytes(b"binary payload")
        sha = _sha256_of(src)
        target_dir = tmp_path / "bin"

        result = downloader.download_and_install(
            "jq", _file_uri(src), sha, target_dir=str(target_dir),
        )
        assert result.ok
        assert sorted(os.listdir(target_dir)) == [os.path.basename(result.path)]

    def test_extract_failure_leaves_no_staged_file(self, tmp_path):
        """A failed extract must clean up its staged temp file in bin_dir."""
        archive = tmp_path / "x.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("present/file", b"x")
        sha = _sha256_of(archive)
        target_dir = tmp_path / "bin"

        result = downloader.download_and_install(
            "x", _file_uri(archive), sha,
            archive_path="missing/inner/path",
            target_dir=str(target_dir),
        )
        assert not result.ok
        assert "extract failed" in result.message
        assert list(target_dir.iterdir()) == []


class TestAtomicReplace:
    def test_replaces_existing_file(self, tmp_path):
        target_dir = tmp_path / "bin"
        target_dir.mkdir()
        existing = target_dir / ("jq.exe" if sys.platform == "win32" else "jq")
        existing.write_bytes(b"old version")

        src = tmp_path / "new"
        new_payload = b"new version of jq"
        src.write_bytes(new_payload)
        sha = _sha256_of(src)

        result = downloader.download_and_install(
            "jq", _file_uri(src), sha, target_dir=str(target_dir),
        )
        assert result.ok
        with open(result.path, "rb") as f:
            assert f.read() == new_payload
