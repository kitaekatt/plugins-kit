"""The filesystem receipt of a workspace call: scan, diff, expected files, path confinement."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from llm_kit.completion import workspace_manifest as wm


def _put(root: Path, name: str, text: str) -> None:
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(text, encoding="utf-8")


def test_scan_records_regular_files_by_relative_posix_path(tmp_path):
    _put(tmp_path, "a.txt", "aa")
    _put(tmp_path, "sub/deep/b.txt", "b")
    (tmp_path / "empty-dir").mkdir()
    found = wm.scan(tmp_path)
    assert sorted(found) == ["a.txt", "sub/deep/b.txt"]
    assert found["a.txt"] == wm.FileRecord("a.txt", 2, hashlib.sha256(b"aa").hexdigest())


def test_symlinks_are_neither_followed_nor_listed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    _put(outside, "secret.txt", "s")
    root = tmp_path / "root"
    root.mkdir()
    _put(root, "real.txt", "r")
    try:
        os.symlink(outside, root / "dirlink")
        os.symlink(outside / "secret.txt", root / "filelink")
    except OSError:  # pragma: no cover - symlinks unavailable
        pytest.skip("no symlinks")
    assert sorted(wm.scan(root)) == ["real.txt"]


def test_scan_of_a_missing_directory_raises(tmp_path):
    with pytest.raises(OSError):
        wm.scan(tmp_path / "nope")


def test_watch_scan_reads_only_the_listed_files(tmp_path, monkeypatch):
    _put(tmp_path, "listed.txt", "yes")
    _put(tmp_path, "unlisted.txt", "no")
    opened = []
    real_open = os.open

    def recording_open(path, *args, **kwargs):
        opened.append(Path(path).name)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(os, "walk", lambda *_a, **_k: pytest.fail("watch scan walked the tree"))
    got = wm.scan(tmp_path, ["listed.txt"])
    assert list(got) == ["listed.txt"]
    assert opened[-1] == "listed.txt"
    assert "unlisted.txt" not in opened


def test_diff_lists_created_and_modified_files_in_path_order(tmp_path):
    _put(tmp_path, "keep.txt", "k")
    _put(tmp_path, "edit.txt", "old")
    _put(tmp_path, "gone.txt", "g")
    before = wm.scan(tmp_path)
    _put(tmp_path, "zeta.txt", "z")
    _put(tmp_path, "alpha/new.txt", "n")
    _put(tmp_path, "edit.txt", "newer")
    (tmp_path / "gone.txt").unlink()
    after = wm.scan(tmp_path)
    assert [r.path for r in wm.diff(before, after)] == ["alpha/new.txt", "edit.txt", "zeta.txt"]
    # a same-size rewrite is a modification: the digest decides, not size or mtime
    _put(tmp_path, "keep.txt", "K")
    assert "keep.txt" in [r.path for r in wm.diff(after, wm.scan(tmp_path))]
    # identical bytes written again are not
    _put(tmp_path, "keep.txt", "K")
    assert wm.diff(wm.scan(tmp_path), wm.scan(tmp_path)) == []


def test_check_expected_reports_missing_directories_and_links(tmp_path):
    _put(tmp_path, "ok.png", "x")
    (tmp_path / "adir").mkdir()
    try:
        os.symlink(tmp_path / "ok.png", tmp_path / "link.png")
        os.symlink(tmp_path / "adir", tmp_path / "dirlink")
        _put(tmp_path / "adir", "inner.txt", "i")
    except OSError:  # pragma: no cover - symlinks unavailable
        pytest.skip("no symlinks")
    missing = wm.check_expected(tmp_path, ["ok.png", "absent.png", "adir", "link.png", "dirlink/inner.txt"])
    assert missing == ["absent.png", "adir", "link.png", "dirlink/inner.txt"]


@pytest.mark.parametrize("name", ["a.png", "sub/a.png", "./a.png", "a//b.png"])
def test_confine_relative_accepts_relative_names_and_normalizes(name):
    assert wm.confine_relative(name, what="x") == str(Path(name).as_posix()).replace("./", "", 1).replace("//", "/")


@pytest.mark.parametrize("name", ["", "/abs", "C:\\x", "C:/x", "../x", "a/../../x", "a\\b", "a/./..", ".", "a\0b", None, 3])
def test_confine_relative_refuses_everything_else(name):
    with pytest.raises(ValueError):
        wm.confine_relative(name, what="x")
