"""p4-kit snapshot_review.py (freeze / verify / drop / prune) against a fake p4.

The fake models the server state the script reads: the CL's open files, its
shelf (per-file digest of LF-normalized content, as the server normalizes text),
the client view, and `p4 shelve -c`. Nothing here talks to a real server.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import importlib.util
import sys

import pytest

# Load under a unique name: git-kit ships a different snapshot_review.py and
# both suites run in one session. conftest puts p4-kit/scripts on sys.path for
# the vendored bootstrap_guard import.
_SCRIPT = Path(__file__).resolve().parents[2] / "plugins" / "p4-kit" / "scripts" / "snapshot_review.py"
_SPEC = importlib.util.spec_from_file_location("p4_kit_snapshot_review", _SCRIPT)
sr = importlib.util.module_from_spec(_SPEC)
sys.modules["p4_kit_snapshot_review"] = sr
_SPEC.loader.exec_module(sr)

CL = "4242"


def _digest(data: bytes) -> str:
    return hashlib.md5(data.replace(b"\r\n", b"\n")).hexdigest().upper()


class FakeP4:
    """Just enough of p4 for snapshot_review.py; records every call."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.opened: dict[str, str] = {}
        self.shelf: dict[str, tuple[str, str]] = {}  # depot -> (digest, action)
        self.calls: list[list[str]] = []
        self.types: dict[str, str] = {}

    def local(self, depot: str) -> Path:
        return self.workspace / depot[len("//depot/"):]

    def write(self, depot: str, data: bytes, action: str = "edit") -> None:
        path = self.local(depot)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.opened[depot] = action

    def __call__(self, args: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(args))
        if args == ["-ztag", "info"]:
            return 0, "... clientName ws1\n... clientRoot " + str(self.workspace) + "\n", ""
        if args[:3] == ["-ztag", "opened", "-c"]:
            out = "".join(
                f"... depotFile {d}\n... action {a}\n... haveRev 3\n"
                f"... type {self.types.get(d, 'text')}\n\n"
                for d, a in sorted(self.opened.items())
            )
            return 0, out, ""
        if args[:3] == ["-ztag", "fstat", "-Ol"]:
            if not self.shelf:
                return 1, "", f"{args[3]} - no such file(s).\n"
            out = "".join(
                f"... depotFile {d}\n... action {a}\n" + (f"... digest {g}\n" if g else "") + "\n"
                for d, (g, a) in sorted(self.shelf.items())
            )
            return 0, out, ""
        if args[:3] == ["shelve", "-d", "-c"]:
            self.shelf.clear()
            return 0, f"Shelved files deleted from change {CL}.\n", ""
        if args[:2] == ["shelve", "-c"]:
            for depot, action in self.opened.items():
                path = self.local(depot)
                digest = "" if action == "delete" else _digest(path.read_bytes())
                self.shelf[depot] = (digest, action)
            return 0, f"Change {CL} files shelved.\n", ""
        if args[:2] == ["-ztag", "where"]:
            out = "".join(
                f"... depotFile {d}\n... clientFile //ws1/{d[8:]}\n... path {self.local(d)}\n\n"
                for d in args[2:]
            )
            return 0, out, ""
        raise AssertionError(f"unexpected p4 call: {args}")


@pytest.fixture
def p4(tmp_path, monkeypatch) -> FakeP4:
    monkeypatch.setenv("CLAUDE_BOOTSTRAP_DATA_ROOT", str(tmp_path / "data"))
    fake = FakeP4(tmp_path / "ws")
    monkeypatch.setattr(sr, "run_p4", fake)
    return fake


def _reviews(tmp_path: Path) -> Path:
    return tmp_path / "data" / "plugins-kit" / "p4-kit" / "reviews"


def test_freeze_without_a_shelf_shelves_and_records(p4, tmp_path):
    p4.write("//depot/a.py", b"print(1)\n")
    result = sr.freeze(CL)
    assert ["shelve", "-c", CL] in p4.calls
    assert result["auto_shelved"] is True
    assert result["files"] == ["//depot/a.py"]
    record = json.loads(Path(result["record"]).read_text(encoding="utf-8"))
    assert record["fingerprint"] == {"//depot/a.py": _digest(b"print(1)\n")}
    assert Path(result["bundle_dir"]).parent == _reviews(tmp_path) / CL
    assert Path(result["bundle_dir"]).name == "snap-" + result["snapshot"]


def test_two_freezes_never_share_a_directory(p4):
    p4.write("//depot/a.py", b"x\n")
    first = sr.freeze(CL)
    time.sleep(0.01)
    second = sr.freeze(CL)
    assert first["bundle_dir"] != second["bundle_dir"]


def test_freeze_with_nothing_open_and_no_shelf_refuses(p4):
    with pytest.raises(sr.SnapshotError, match="nothing to review"):
        sr.freeze(CL)


def test_freeze_pins_a_matching_existing_shelf_without_reshelving(p4):
    p4.write("//depot/a.py", b"line\r\n")  # CRLF on disk, LF-normalized digest on the server
    p4.shelf = {"//depot/a.py": (_digest(b"line\n"), "edit")}
    result = sr.freeze(CL)
    assert result["auto_shelved"] is False
    assert not any(call[:1] == ["shelve"] for call in p4.calls)


def test_freeze_refuses_a_shelf_that_differs_from_the_workspace(p4, tmp_path):
    p4.write("//depot/a.py", b"new\n")
    p4.shelf = {"//depot/a.py": (_digest(b"old\n"), "edit")}
    with pytest.raises(sr.SnapshotError) as exc:
        sr.freeze(CL)
    assert "//depot/a.py" in str(exc.value)
    assert f"p4 shelve -f -c {CL}" in str(exc.value)
    assert not any(call[:1] == ["shelve"] for call in p4.calls)
    assert not (_reviews(tmp_path) / CL).exists()
    # --use-shelf reviews the shelf as it stands instead.
    assert sr.freeze(CL, use_shelf=True)["auto_shelved"] is False


# ------------------------------------------- who created the shelf decides its fate


def _shelve_by_review(p4):
    """A freeze that shelves, leaving a review-created shelf behind (a cut-off run)."""
    p4.write("//depot/a.py", b"one\n")
    first = sr.freeze(CL)
    assert first["auto_shelved"] is True
    return first


def test_a_stale_shelf_the_review_created_is_reshelved_without_a_question(p4):
    _shelve_by_review(p4)
    p4.local("//depot/a.py").write_bytes(b"two\n")  # edited since; the shelf is stale
    p4.calls.clear()
    result = sr.freeze(CL)  # no flags: nothing to ask
    assert result["reshelved"] is True
    assert result["auto_shelved"] is True  # still the review's, so the runner's cleanup owns it
    assert result["source"] == "shelf"
    assert ["shelve", "-d", "-c", CL] in p4.calls and ["shelve", "-c", CL] in p4.calls
    assert not any(call[:2] == ["shelve", "-f"] for call in p4.calls)
    record = json.loads(Path(result["record"]).read_text(encoding="utf-8"))
    assert record["fingerprint"] == {"//depot/a.py": _digest(b"two\n")}
    # The shelf now equals the workspace, so the next freeze just pins it.
    assert sr.freeze(CL)["reshelved"] is False


def test_a_pinned_shelf_the_review_created_stays_the_reviews_to_clean_up(p4):
    _shelve_by_review(p4)
    second = sr.freeze(CL)  # unchanged workspace: pinned, not re-made
    assert second["reshelved"] is False and second["auto_shelved"] is True


def test_a_shelf_the_user_made_is_left_alone_and_freeze_asks(p4, tmp_path):
    p4.write("//depot/a.py", b"new\n")
    p4.shelf = {"//depot/a.py": (_digest(b"old\n"), "edit")}  # the user's own shelf
    with pytest.raises(sr.ShelfDecisionNeeded) as exc:
        sr.freeze(CL)
    message = str(exc.value)
    assert "this review did not create it" in message
    assert "--foreign-shelf workspace" in message and "--use-shelf" in message
    assert not any(call[:1] == ["shelve"] for call in p4.calls)
    assert p4.shelf == {"//depot/a.py": (_digest(b"old\n"), "edit")}
    assert not (_reviews(tmp_path) / CL).exists()
    assert sr.main(["freeze", CL]) == 3


def test_a_shelf_reshelved_by_the_user_after_the_review_made_it_is_the_users(p4):
    _shelve_by_review(p4)
    p4.local("//depot/a.py").write_bytes(b"two\n")
    p4.shelf = {"//depot/a.py": (_digest(b"user's own\n"), "edit")}  # the user reshelved
    with pytest.raises(sr.ShelfDecisionNeeded):
        sr.freeze(CL)
    assert p4.shelf == {"//depot/a.py": (_digest(b"user's own\n"), "edit")}


def test_foreign_shelf_workspace_freezes_the_workspace_files_and_never_touches_the_shelf(p4):
    p4.write("//depot/a.py", b"new line\r\n")
    p4.write("//depot/b.py", b"fresh\n", action="add")
    p4.write("//depot/gone.py", b"bye\n")
    p4.opened["//depot/gone.py"] = "delete"
    p4.local("//depot/gone.py").unlink()
    user_shelf = {"//depot/a.py": (_digest(b"old\n"), "edit")}
    p4.shelf = dict(user_shelf)
    p4.calls.clear()

    result = sr.freeze(CL, foreign_shelf="workspace")

    assert result["source"] == "workspace"
    assert result["auto_shelved"] is False and result["reshelved"] is False
    assert result["files"] == ["//depot/a.py", "//depot/b.py", "//depot/gone.py"]
    # The shelf is untouched: no p4 call may write one.
    assert p4.shelf == user_shelf
    assert not any(call[:1] == ["shelve"] for call in p4.calls)
    bundle = Path(result["bundle_dir"])
    tree = bundle / "tree" / "depot"
    assert (tree / "a.py").read_bytes() == b"new line\r\n"
    assert (tree / "b.py").read_bytes() == b"fresh\n"
    assert not (tree / "gone.py").exists()  # a delete has no post-image
    record = json.loads(Path(result["record"]).read_text(encoding="utf-8"))
    assert record["source"] == "workspace"
    assert record["auto_shelved"] is False
    assert record["files"]["//depot/a.py"] == {"rev": "3", "type": "text"}
    assert record["actions"]["//depot/gone.py"] == "delete"
    assert record["fingerprint"]["//depot/gone.py"] == ""
    # The record fingerprints the frozen copies, so verify compares the workspace to them.
    assert sr.verify(result["record"])["matches"] is True


def test_a_workspace_snapshot_is_immune_to_later_edits_and_verify_reports_them(p4):
    p4.write("//depot/a.py", b"reviewed\n")
    p4.shelf = {"//depot/a.py": (_digest(b"old\n"), "edit")}
    result = sr.freeze(CL, foreign_shelf="workspace")
    frozen = Path(result["bundle_dir"]) / "tree" / "depot" / "a.py"
    p4.local("//depot/a.py").write_bytes(b"edited during the review\n")
    assert frozen.read_bytes() == b"reviewed\n"
    state = sr.verify(result["record"])
    assert state["matches"] is False and state["changed"] == ["//depot/a.py"]


def test_foreign_shelf_workspace_is_inert_without_a_differing_foreign_shelf(p4):
    # No shelf at all: the review shelves as ever.
    p4.write("//depot/a.py", b"x\n")
    assert sr.freeze(CL, foreign_shelf="workspace")["source"] == "shelf"
    # A foreign shelf that already equals the workspace is pinned, not copied.
    p4.shelf = {"//depot/a.py": (_digest(b"x\n"), "edit")}
    (sr._cl_dir(CL) / "review-shelf.json").unlink()
    again = sr.freeze(CL, foreign_shelf="workspace")
    assert again["source"] == "shelf" and again["auto_shelved"] is False


def test_workspace_freeze_refuses_a_missing_workspace_file_and_leaves_no_record(p4, tmp_path):
    p4.write("//depot/a.py", b"x\n")
    p4.shelf = {"//depot/a.py": (_digest(b"old\n"), "edit")}
    p4.local("//depot/a.py").unlink()
    with pytest.raises(sr.SnapshotError, match="workspace file is missing"):
        sr.freeze(CL, foreign_shelf="workspace")
    assert not (_reviews(tmp_path) / CL).exists()


def test_use_shelf_and_the_workspace_policy_are_alternatives(p4):
    p4.write("//depot/a.py", b"x\n")
    with pytest.raises(sr.SnapshotError, match="cannot be combined"):
        sr.freeze(CL, use_shelf=True, foreign_shelf="workspace")
    with pytest.raises(sr.SnapshotError, match="'ask' or 'workspace'"):
        sr.freeze(CL, foreign_shelf="sometimes")


def test_main_passes_the_foreign_shelf_policy_through(p4, capsys):
    p4.write("//depot/a.py", b"new\n")
    p4.shelf = {"//depot/a.py": (_digest(b"old\n"), "edit")}
    assert sr.main(["freeze", CL, "--foreign-shelf", "workspace"]) == 0
    assert json.loads(capsys.readouterr().out)["source"] == "workspace"


def test_verify_matches_until_a_reviewed_file_is_edited(p4):
    p4.write("//depot/a.py", b"one\n")
    record = sr.freeze(CL)["record"]
    assert sr.verify(record)["matches"] is True
    p4.local("//depot/a.py").write_bytes(b"two\n")  # post-freeze edit
    state = sr.verify(record)
    assert state["matches"] is False
    assert state["changed"] == ["//depot/a.py"]
    assert sr.main(["verify", record]) == 1


def test_verify_reports_a_file_opened_after_the_freeze(p4):
    p4.write("//depot/a.py", b"one\n")
    record = sr.freeze(CL)["record"]
    p4.write("//depot/b.py", b"late\n", action="add")
    state = sr.verify(record)
    assert state["opened_after"] == ["//depot/b.py"]
    assert state["matches"] is False


def test_verify_checks_a_delete_by_absence(p4):
    p4.write("//depot/gone.py", b"bye\n")
    p4.opened["//depot/gone.py"] = "delete"
    p4.local("//depot/gone.py").unlink()
    record = sr.freeze(CL)["record"]
    assert sr.verify(record)["matches"] is True
    p4.local("//depot/gone.py").write_bytes(b"back\n")
    assert sr.verify(record)["changed"] == ["//depot/gone.py"]


def test_drop_tree_only_then_whole_directory(p4):
    p4.write("//depot/a.py", b"x\n")
    result = sr.freeze(CL)
    bundle = Path(result["bundle_dir"])
    (bundle / "tree" / "depot").mkdir(parents=True)
    ro = bundle / "tree" / "depot" / "a.py"
    ro.write_bytes(b"x\n")
    os.chmod(ro, 0o444)  # p4 print -o leaves read-only files
    sr.drop(result["record"], tree_only=True)
    assert not (bundle / "tree").exists() and (bundle / "freeze.json").exists()
    sr.drop(result["record"])
    assert not bundle.exists()


def test_drop_refuses_a_directory_it_does_not_own(p4, tmp_path):
    foreign = tmp_path / "elsewhere" / "snap-0123456789ab"
    foreign.mkdir(parents=True)
    with pytest.raises(sr.SnapshotError, match="not a p4-kit review snapshot directory"):
        sr.drop(str(foreign))
    assert foreign.exists()


def test_prune_removes_only_old_snapshot_dirs(p4, tmp_path):
    p4.write("//depot/a.py", b"x\n")
    old = Path(sr.freeze(CL)["bundle_dir"])
    time.sleep(0.01)
    fresh = Path(sr.freeze(CL)["bundle_dir"])
    stale = time.time() - 30 * 86400
    os.utime(old / "freeze.json", (stale, stale))
    keep = _reviews(tmp_path) / CL / "chunks"  # an inline review's own files
    keep.mkdir(parents=True)
    os.utime(keep, (stale, stale))
    result = sr.prune(7)
    assert result["pruned"] == [str(old)]
    assert fresh.exists() and keep.exists()


def test_main_without_a_subcommand_exits_2(capsys):
    with pytest.raises(SystemExit) as exc:
        sr.main([])
    assert exc.value.code == 2


def test_the_script_never_submits():
    # Submitting stays the user's `p4 submit`; the review only reports via verify.
    source = Path(sr.__file__).read_text(encoding="utf-8")
    assert '"submit"' not in source
    assert '"-f"' not in source  # never replaces (force-shelves) an existing shelf


def test_the_script_is_stdlib_only():
    source = Path(sr.__file__).read_text(encoding="utf-8")
    assert "bootstrap_lib" not in source
    assert "from bootstrap_guard import data_dir" in source
