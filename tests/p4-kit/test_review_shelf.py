"""review_shelf.py: which shelf of a CL did the review create?"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import review_shelf as rs

FP = {"//depot/a.py": "AB12", "//depot/b.py": ""}
ACTIONS = {"//depot/a.py": "edit", "//depot/b.py": "delete"}


def test_no_record_means_the_review_did_not_create_the_shelf(tmp_path):
    assert rs.is_review_created(tmp_path / "4242", FP, ACTIONS) is False


def test_a_shelf_equal_to_the_record_is_the_reviews(tmp_path):
    cl_dir = tmp_path / "4242"
    rs.record_created(cl_dir, "4242", FP, ACTIONS)
    assert rs.is_review_created(cl_dir, FP, ACTIONS) is True
    # Digest case is the server's business, not a reason to disown the shelf.
    assert rs.is_review_created(cl_dir, {"//depot/a.py": "ab12", "//depot/b.py": ""}, ACTIONS) is True


@pytest.mark.parametrize(
    "digests, actions",
    [
        ({"//depot/a.py": "FFFF", "//depot/b.py": ""}, ACTIONS),  # reshelved with new content
        ({"//depot/a.py": "AB12"}, {"//depot/a.py": "edit"}),  # a file dropped from the shelf
        ({**FP, "//depot/c.py": "01"}, {**ACTIONS, "//depot/c.py": "add"}),  # a file added
        (FP, {"//depot/a.py": "add", "//depot/b.py": "delete"}),  # same content, new action
    ],
)
def test_any_change_to_the_shelf_makes_it_the_users(tmp_path, digests, actions):
    cl_dir = tmp_path / "4242"
    rs.record_created(cl_dir, "4242", FP, ACTIONS)
    assert rs.is_review_created(cl_dir, digests, actions) is False


def test_forget_erases_the_record_and_is_idempotent(tmp_path):
    cl_dir = tmp_path / "4242"
    rs.record_created(cl_dir, "4242", FP, ACTIONS)
    rs.forget(cl_dir)
    rs.forget(cl_dir)
    assert not rs.marker_path(cl_dir).exists()
    assert rs.is_review_created(cl_dir, FP, ACTIONS) is False


def test_the_record_sits_in_the_cl_directory_beside_the_snapshots(tmp_path):
    cl_dir = tmp_path / "4242"
    rs.record_created(cl_dir, "4242", FP, ACTIONS)
    assert rs.marker_path(cl_dir) == cl_dir / "review-shelf.json"
    assert json.loads(rs.marker_path(cl_dir).read_text(encoding="utf-8"))["cl"] == "4242"


def test_an_unknown_schema_fails_loudly_instead_of_being_guessed_at(tmp_path):
    cl_dir = tmp_path / "4242"
    cl_dir.mkdir()
    rs.marker_path(cl_dir).write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported review-shelf record schema"):
        rs.is_review_created(cl_dir, FP, ACTIONS)


def test_the_module_is_stdlib_only():
    source = Path(rs.__file__).read_text(encoding="utf-8")
    assert "bootstrap_lib" not in source and "bootstrap_guard" not in source
