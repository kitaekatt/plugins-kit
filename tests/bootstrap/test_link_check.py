"""Tests for bootstrap_lib/link_check.py."""

import os

import pytest

from bootstrap.link_compat import requires_symlinks
from bootstrap_lib.link_check import is_link


def test_regular_file_is_not_a_link(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("hello")
    assert is_link(target) is False


def test_directory_is_not_a_link(tmp_path):
    target = tmp_path / "dir"
    target.mkdir()
    assert is_link(target) is False


@requires_symlinks
def test_symlink_to_file_is_a_link(tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("hello")
    link = tmp_path / "link"
    link.symlink_to(target)
    assert is_link(link) is True


@requires_symlinks
def test_symlink_to_directory_is_a_link(tmp_path):
    target = tmp_path / "dir"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    assert is_link(link) is True


@requires_symlinks
def test_dangling_symlink_is_still_a_link(tmp_path):
    """A missing target must not raise, and must still read as a link."""
    missing = tmp_path / "does-not-exist"
    link = tmp_path / "dangling"
    link.symlink_to(missing)
    assert not missing.exists()
    assert is_link(link) is True


def test_nonexistent_path_is_not_a_link(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert is_link(missing) is False


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows-only")
def test_junction_is_a_link(tmp_path):
    import _winapi

    target = tmp_path / "dir"
    target.mkdir()
    link = tmp_path / "junction"
    _winapi.CreateJunction(str(target), str(link))
    assert is_link(link) is True
