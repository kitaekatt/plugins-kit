"""Download bootstrap-managed tool binaries into ~/.local/bin.

Phase 2 of the tool-resolution redesign (see
docs/planning/bootstrap/tool-resolution-redesign.md). The engine calls
download_and_install() when a tool entry has a `download` block; the
result is an absolute path on disk in a location bootstrap controls.

Stdlib-only. urllib for fetch, hashlib for verification, zipfile/tarfile
for archive extraction.
"""

import hashlib
import os
import shutil
import stat
import sys
import tarfile
import tempfile
import zipfile
from typing import NamedTuple, Optional
from urllib.request import urlopen


class DownloadResult(NamedTuple):
    ok: bool
    path: Optional[str]   # absolute path to the installed binary on success
    message: str          # human-readable status/error


class FontDownloadResult(NamedTuple):
    ok: bool
    files: list          # absolute paths of font files extracted on success
    message: str         # human-readable status/error


def install_dir():
    """The canonical install directory for bootstrap-managed binaries."""
    return os.path.expanduser("~/.local/bin")


def _detect_archive_type(url: str, explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit
    lower = url.lower().split("?", 1)[0]
    if lower.endswith(".zip"):
        return "zip"
    if lower.endswith((".tar.gz", ".tgz")):
        return "tar.gz"
    if lower.endswith((".tar.xz", ".txz")):
        return "tar.xz"
    if lower.endswith(".tar"):
        return "tar"
    return None  # treat as a raw single-file download


def _fetch(url: str, dest_path: str, timeout: float = 120) -> str:
    """Stream `url` to `dest_path`, returning the hex sha256 of the bytes.

    `timeout` is the socket timeout (connect + per-read). Without it a
    stalled connection hangs the background engine forever; 120s matches
    pypi_check's download timeout.
    """
    h = hashlib.sha256()
    with urlopen(url, timeout=timeout) as resp, open(dest_path, "wb") as out:
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            h.update(chunk)
            out.write(chunk)
    return h.hexdigest()


def _extract_from_archive(archive_path: str, archive_type: str, inner_path: str, dest_path: str):
    """Extract a single file `inner_path` from the archive to `dest_path`."""
    if archive_type == "zip":
        with zipfile.ZipFile(archive_path) as z:
            with z.open(inner_path) as src, open(dest_path, "wb") as out:
                shutil.copyfileobj(src, out)
        return
    if archive_type in ("tar", "tar.gz", "tar.xz"):
        mode = {
            "tar":    "r:",
            "tar.gz": "r:gz",
            "tar.xz": "r:xz",
        }[archive_type]
        with tarfile.open(archive_path, mode) as t:
            member = t.getmember(inner_path)
            extracted = t.extractfile(member)
            if extracted is None:
                raise ValueError(f"{inner_path!r} in archive is not a regular file")
            with extracted as src, open(dest_path, "wb") as out:
                shutil.copyfileobj(src, out)
        return
    raise ValueError(f"unsupported archive type: {archive_type!r}")


def _remove_quiet(path: str):
    try:
        os.remove(path)
    except OSError:
        pass


def _make_executable(path: str):
    if sys.platform == "win32":
        return  # Windows executable bit is implicit from extension
    st = os.stat(path)
    os.chmod(path, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def download_and_install(
    tool_name: str,
    url: str,
    sha256: str,
    *,
    binary_name: Optional[str] = None,
    archive_path: Optional[str] = None,
    archive_type: Optional[str] = None,
    target_dir: Optional[str] = None,
) -> DownloadResult:
    """Fetch `url`, verify sha256, install the binary at <target_dir>/<binary_name>.

    Single-file downloads: omit `archive_path`. The fetched bytes ARE the binary.
    Archive downloads: pass `archive_path` (path inside the archive). `archive_type`
    autodetects from the URL extension when omitted.

    On success: returns (ok=True, path=<absolute path on disk>, message=...).
    On failure: returns (ok=False, path=None, message=<reason>). Never raises
    for "expected" failures (network, hash mismatch, missing inner path);
    re-raises only for genuine internals (programmer errors).
    """
    bin_dir = target_dir or install_dir()
    try:
        os.makedirs(bin_dir, exist_ok=True)
    except OSError as e:
        return DownloadResult(False, None, f"create install directory failed: {e}")

    final_name = binary_name or tool_name
    if sys.platform == "win32" and not final_name.lower().endswith(".exe"):
        final_name += ".exe"
    final_path = os.path.join(bin_dir, final_name)

    detected_type = _detect_archive_type(url, archive_type)
    is_archive = bool(archive_path) or detected_type is not None

    with tempfile.TemporaryDirectory(prefix="bootstrap-dl-") as tmp:
        download_path = os.path.join(tmp, "download.bin")
        try:
            actual_hash = _fetch(url, download_path)
        except Exception as e:
            return DownloadResult(False, None, f"fetch failed: {e}")

        if actual_hash.lower() != sha256.lower():
            return DownloadResult(
                False, None,
                f"sha256 mismatch: expected {sha256}, got {actual_hash}",
            )

        if is_archive:
            if not archive_path:
                return DownloadResult(
                    False, None,
                    "download is an archive but no archive_path supplied",
                )
            if detected_type is None:
                return DownloadResult(
                    False, None,
                    "archive_path supplied but archive_type couldn't be detected from URL",
                )

        # Stage IN bin_dir itself (not the system temp dir) so the rename
        # into place is an atomic same-filesystem replace — on Linux /tmp is
        # often tmpfs, and a cross-device os.replace raises EXDEV.
        fd, staged = tempfile.mkstemp(prefix=f".{final_name}.", suffix=".staged", dir=bin_dir)
        os.close(fd)
        try:
            if is_archive:
                _extract_from_archive(download_path, detected_type, archive_path, staged)
            else:
                shutil.copyfile(download_path, staged)
            _make_executable(staged)
        except Exception as e:
            _remove_quiet(staged)
            return DownloadResult(False, None, f"extract failed: {e}")

        try:
            os.replace(staged, final_path)
        except OSError as e:
            _remove_quiet(staged)
            return DownloadResult(False, None, f"install to {final_path} failed: {e}")

    return DownloadResult(True, final_path, f"installed at {final_path}")


def _extract_fonts_from_archive(archive_path, archive_type, dest_dir, suffixes):
    """Extract every archive member whose basename ends with one of `suffixes`
    into `dest_dir` (flattened to basename). Returns the list of written paths.

    Unlike _extract_from_archive (single named member), this pulls a whole font
    family out of one archive. Members are written by basename only, so nested
    archive paths can't escape dest_dir (path-traversal safe).
    """
    written = []
    suffixes = tuple(s.lower() for s in suffixes)

    def _match(name):
        base = os.path.basename(name)
        return bool(base) and base.lower().endswith(suffixes)

    if archive_type == "zip":
        with zipfile.ZipFile(archive_path) as z:
            for info in z.infolist():
                if info.is_dir() or not _match(info.filename):
                    continue
                out_path = os.path.join(dest_dir, os.path.basename(info.filename))
                with z.open(info) as src, open(out_path, "wb") as out:
                    shutil.copyfileobj(src, out)
                written.append(out_path)
        return written

    if archive_type in ("tar", "tar.gz", "tar.xz"):
        mode = {"tar": "r:", "tar.gz": "r:gz", "tar.xz": "r:xz"}[archive_type]
        with tarfile.open(archive_path, mode) as t:
            for member in t.getmembers():
                if not member.isfile() or not _match(member.name):
                    continue
                extracted = t.extractfile(member)
                if extracted is None:
                    continue
                out_path = os.path.join(dest_dir, os.path.basename(member.name))
                with extracted as src, open(out_path, "wb") as out:
                    shutil.copyfileobj(src, out)
                written.append(out_path)
        return written

    raise ValueError(f"unsupported archive type: {archive_type!r}")


def download_fonts(
    url: str,
    sha256: str,
    dest_dir: str,
    *,
    archive_type: Optional[str] = None,
    suffixes=(".ttf", ".otf"),
) -> FontDownloadResult:
    """Fetch a font archive at `url`, verify sha256, and extract every font
    file (by `suffixes`) into `dest_dir`.

    Fonts ship as archives of many faces (Regular, Bold, Italic, …), so this
    extracts the whole family rather than a single named member. `dest_dir` is
    created if missing; existing files with the same basename are overwritten.

    On success: (ok=True, files=[<abs paths>], message=...).
    On failure: (ok=False, files=[], message=<reason>). Never raises for
    "expected" failures (network, hash mismatch, no matching members).
    """
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as e:
        return FontDownloadResult(False, [], f"create font directory failed: {e}")
    detected_type = _detect_archive_type(url, archive_type)
    if detected_type is None:
        return FontDownloadResult(False, [], f"could not detect archive type from {url!r}")

    with tempfile.TemporaryDirectory(prefix="bootstrap-font-") as tmp:
        download_path = os.path.join(tmp, "download.bin")
        try:
            actual_hash = _fetch(url, download_path)
        except Exception as e:
            return FontDownloadResult(False, [], f"fetch failed: {e}")

        if actual_hash.lower() != sha256.lower():
            return FontDownloadResult(
                False, [],
                f"sha256 mismatch: expected {sha256}, got {actual_hash}",
            )

        try:
            written = _extract_fonts_from_archive(download_path, detected_type, dest_dir, suffixes)
        except Exception as e:
            return FontDownloadResult(False, [], f"extract failed: {e}")

    if not written:
        return FontDownloadResult(
            False, [],
            f"no files matching {suffixes} found in archive",
        )

    return FontDownloadResult(True, written, f"extracted {len(written)} files to {dest_dir}")
