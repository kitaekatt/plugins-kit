"""Tests for plugins/bootstrap/lib/pypi_check.py."""

import io
import json
import zipfile
from unittest.mock import MagicMock, patch

import pytest

from bootstrap_lib.pypi_check import check_pypi_package, download_and_extract, _get_wheel_url


class TestCheckPypiPackage:
    def test_file_exists(self, tmp_path):
        target = tmp_path / "stubs" / "unreal.py"
        target.parent.mkdir()
        target.write_text("# stub")
        result = check_pypi_package("unreal-stub", str(target))
        assert result.passed is True

    def test_file_missing(self, tmp_path):
        target = tmp_path / "stubs" / "unreal.py"
        result = check_pypi_package("unreal-stub", str(target))
        assert result.passed is False
        assert "not found" in result.message


class TestDownloadAndExtract:
    def _make_wheel_bytes(self, files: dict[str, bytes]) -> bytes:
        """Create a minimal wheel (zip) in memory."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, content in files.items():
                zf.writestr(name, content)
        return buf.getvalue()

    @patch("bootstrap_lib.pypi_check.urlopen")
    @patch("bootstrap_lib.pypi_check._get_wheel_url")
    def test_download_and_extract_success(self, mock_get_url, mock_urlopen, tmp_path):
        wheel_data = self._make_wheel_bytes({
            "unreal/__init__.py": b"# init",
            "unreal/unreal.py": b"# " + b"x" * 5000,  # Largest file
        })

        mock_get_url.return_value = ("https://example.com/unreal_stub-1.0-py3-none-any.whl", "")

        mock_resp = MagicMock()
        mock_resp.read.return_value = wheel_data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        target = tmp_path / "stubs" / "unreal.py"
        result = download_and_extract("unreal-stub", str(target))
        assert result.passed is True
        assert target.is_file()
        assert target.stat().st_size > 100

    @patch("bootstrap_lib.pypi_check.urlopen")
    @patch("bootstrap_lib.pypi_check._get_wheel_url")
    def test_extract_pattern_filters_files(self, mock_get_url, mock_urlopen, tmp_path):
        """extract_pattern selects files matching the glob instead of largest."""
        wheel_data = self._make_wheel_bytes({
            "pkg/big_file.py": b"x" * 10000,  # Largest but wrong pattern
            "pkg/data.json": b'{"key": "value"}',
            "pkg/small.pyi": b"# stub",
        })

        mock_get_url.return_value = ("https://example.com/pkg-1.0.whl", "")
        mock_resp = MagicMock()
        mock_resp.read.return_value = wheel_data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        target = tmp_path / "out" / "data.json"
        result = download_and_extract("some-pkg", str(target), extract_pattern="*.json")
        assert result.passed is True
        assert target.is_file()
        content = target.read_text()
        assert "key" in content

    @patch("bootstrap_lib.pypi_check._get_wheel_url")
    def test_download_fails_no_url(self, mock_get_url, tmp_path):
        mock_get_url.return_value = (None, "")
        target = tmp_path / "stubs" / "unreal.py"
        result = download_and_extract("nonexistent-pkg", str(target))
        assert result.passed is False
        assert "failed to find wheel" in result.message

    @patch("bootstrap_lib.pypi_check.urlopen")
    @patch("bootstrap_lib.pypi_check._get_wheel_url")
    def test_bad_zip_data(self, mock_get_url, mock_urlopen, tmp_path):
        mock_get_url.return_value = ("https://example.com/bad.whl", "")

        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not a zip file"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        target = tmp_path / "stubs" / "unreal.py"
        result = download_and_extract("unreal-stub", str(target))
        assert result.passed is False
        assert "not a valid wheel" in result.message

    @patch("bootstrap_lib.pypi_check.urlopen")
    @patch("bootstrap_lib.pypi_check._get_wheel_url")
    def test_wheel_with_no_py_files(self, mock_get_url, mock_urlopen, tmp_path):
        wheel_data = self._make_wheel_bytes({
            "data.txt": b"not a python file",
        })

        mock_get_url.return_value = ("https://example.com/pkg-1.0.whl", "")

        mock_resp = MagicMock()
        mock_resp.read.return_value = wheel_data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        target = tmp_path / "stubs" / "unreal.py"
        result = download_and_extract("some-pkg", str(target))
        assert result.passed is False
        assert "no Python files" in result.message

    @patch("bootstrap_lib.pypi_check.urlopen")
    @patch("bootstrap_lib.pypi_check._get_wheel_url")
    def test_sha256_mismatch_fails(self, mock_get_url, mock_urlopen, tmp_path):
        """A wheel whose bytes don't match the PyPI-published digest is rejected (B18)."""
        wheel_data = self._make_wheel_bytes({"pkg/mod.py": b"# code"})
        mock_get_url.return_value = ("https://example.com/pkg-1.0.whl", "0" * 64)

        mock_resp = MagicMock()
        mock_resp.read.return_value = wheel_data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        target = tmp_path / "out" / "mod.py"
        result = download_and_extract("some-pkg", str(target))
        assert result.passed is False
        assert "sha256 mismatch" in result.message
        assert not target.exists()

    @patch("bootstrap_lib.pypi_check.urlopen")
    @patch("bootstrap_lib.pypi_check._get_wheel_url")
    def test_sha256_match_passes(self, mock_get_url, mock_urlopen, tmp_path):
        """A wheel matching the published digest extracts normally."""
        import hashlib
        wheel_data = self._make_wheel_bytes({"pkg/mod.py": b"# code " + b"y" * 500})
        digest = hashlib.sha256(wheel_data).hexdigest()
        mock_get_url.return_value = ("https://example.com/pkg-1.0.whl", digest.upper())

        mock_resp = MagicMock()
        mock_resp.read.return_value = wheel_data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        target = tmp_path / "out" / "mod.py"
        result = download_and_extract("some-pkg", str(target))
        assert result.passed is True
        assert target.is_file()


class TestGetWheelUrl:
    @patch("bootstrap_lib.pypi_check.urlopen")
    def test_returns_wheel_url(self, mock_urlopen):
        pypi_response = {
            "info": {"version": "1.0"},
            "urls": [
                {"packagetype": "bdist_wheel", "url": "https://example.com/pkg.whl",
                 "digests": {"sha256": "abc123"}},
                {"packagetype": "sdist", "url": "https://example.com/pkg.tar.gz",
                 "digests": {"sha256": "def456"}},
            ],
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(pypi_response).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        url, sha = _get_wheel_url("some-pkg")
        assert url == "https://example.com/pkg.whl"
        assert sha == "abc123"

    @patch("bootstrap_lib.pypi_check.urlopen")
    def test_falls_back_to_sdist(self, mock_urlopen):
        pypi_response = {
            "info": {"version": "1.0"},
            "urls": [
                {"packagetype": "sdist", "url": "https://example.com/pkg.tar.gz"},
            ],
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(pypi_response).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        url, sha = _get_wheel_url("some-pkg")
        assert url == "https://example.com/pkg.tar.gz"
        assert sha == ""

    @patch("bootstrap_lib.pypi_check.urlopen", side_effect=Exception("network error"))
    def test_returns_none_on_error(self, mock_urlopen):
        url, sha = _get_wheel_url("some-pkg")
        assert url is None
        assert sha == ""
