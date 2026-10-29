"""prepare_review.py --snapshot <freeze record>: the background runner's reader surface."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import prepare_review as pr

CL = "4242"
SNAP = "0123456789ab"
FP = {"//depot/a.py": "AAAA", "//depot/doc.md": "BBBB", "//depot/old.py": ""}
ACTIONS = {"//depot/a.py": "edit", "//depot/doc.md": "edit", "//depot/old.py": "delete"}


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    reviews = tmp_path / "reviews"
    monkeypatch.setattr(pr, "DEFAULT_BUNDLE_ROOT", reviews)
    return reviews


def _record(root: Path, *, auto_shelved: bool = True, cl: str = CL) -> Path:
    directory = root / cl / f"snap-{SNAP}"
    directory.mkdir(parents=True)
    path = directory / "freeze.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cl": cl,
                "snapshot": SNAP,
                "client": "ws1",
                "fingerprint": FP,
                "actions": ACTIONS,
                "local": {},
                "auto_shelved": auto_shelved,
                "use_shelf": False,
                "frozen_at": 0,
            }
        ),
        encoding="utf-8",
    )
    return path


def _bundle(bundle_dir: Path) -> dict:
    return {
        "cl": CL,
        "project_root": "C:/ws",
        "bundle_dir": str(bundle_dir),
        "changed_files": [
            {"depot": "//depot/a.py", "local": "C:/ws/a.py", "chunk_index": 0, "claude_mds": []},
            {"depot": "//depot/old.py", "local": "C:/ws/old.py", "chunk_index": 0, "claude_mds": []},
        ],
        "claimed_files": [{"identifier": "//depot/doc.md", "depot": "//depot/doc.md", "local": "C:/ws/doc.md"}],
        "auto_shelved": False,
        "shelf_fingerprint": {},
    }


def _fake_print(calls: list):
    def run(args):
        calls.append(args)
        assert args[:3] == ["print", "-q", "-o"], args
        dest, spec = Path(args[3]), args[4]
        dest.write_text(f"shelved {spec}\n", encoding="utf-8")
        return 0, "", ""

    return run


def _built(bundle_dir, **_kw):
    return _bundle(bundle_dir)


def test_snapshot_bundle_points_generic_files_at_frozen_shelf_copies(root):
    record = _record(root)
    prints: list = []
    with patch.object(pr, "fetch_shelf_fingerprint", return_value=pr.ShelfScanResult(digests=FP, actions=ACTIONS)), \
            patch.object(pr, "build_bundle", side_effect=lambda cl, d, **kw: _built(d)) as build, \
            patch.object(pr, "run_p4", side_effect=_fake_print(prints)):
        bundle, bundle_dir = pr.build_snapshot_bundle(CL, str(record))

    assert bundle_dir == record.parent.resolve()
    assert build.call_args.args[1] == bundle_dir  # one directory per snapshot
    a = bundle["changed_files"][0]
    assert a["live_local"] == "C:/ws/a.py"
    assert Path(a["local"]) == bundle_dir / "tree" / "depot" / "a.py"
    assert Path(a["local"]).read_text(encoding="utf-8") == f"shelved //depot/a.py@={CL}\n"
    # A delete has no post-image to print; its entry is untouched.
    assert bundle["changed_files"][1]["local"] == "C:/ws/old.py"
    assert [p[4] for p in prints] == [f"//depot/a.py@={CL}", f"//depot/doc.md@={CL}"]
    # Claimed Markdown keeps its workspace path for md-domain and gains the frozen copy.
    doc = bundle["claimed_files"][0]
    assert doc["local"] == "C:/ws/doc.md"
    assert Path(doc["frozen"]) == bundle_dir / "tree" / "depot" / "doc.md"
    # The freeze created the shelf, so step 10's cleanup owns it.
    assert bundle["auto_shelved"] is True
    assert bundle["shelf_fingerprint"] == FP
    assert bundle["snapshot"]["id"] == SNAP
    assert bundle["snapshot"]["record"] == str(bundle_dir / "freeze.json")


def test_snapshot_bundle_keeps_a_users_shelf_unowned(root):
    record = _record(root, auto_shelved=False)
    with patch.object(pr, "fetch_shelf_fingerprint", return_value=pr.ShelfScanResult(digests=FP, actions=ACTIONS)), \
            patch.object(pr, "build_bundle", side_effect=lambda cl, d, **kw: _built(d)), \
            patch.object(pr, "run_p4", side_effect=_fake_print([])):
        bundle, _ = pr.build_snapshot_bundle(CL, str(record))
    assert bundle["auto_shelved"] is False
    assert bundle["shelf_fingerprint"] == {}


def test_snapshot_bundle_refuses_a_shelf_changed_since_the_freeze(root):
    record = _record(root)
    changed = dict(FP, **{"//depot/a.py": "CCCC"})
    with patch.object(pr, "fetch_shelf_fingerprint", return_value=pr.ShelfScanResult(digests=changed, actions=ACTIONS)), \
            patch.object(pr, "build_bundle") as build:
        with pytest.raises(ValueError, match="changed since snapshot 0123456789ab"):
            pr.build_snapshot_bundle(CL, str(record))
    build.assert_not_called()


def test_snapshot_bundle_refuses_a_reshelve_during_prepare(root):
    record = _record(root)
    scans = iter([
        pr.ShelfScanResult(digests=FP, actions=ACTIONS),
        pr.ShelfScanResult(digests=dict(FP, **{"//depot/a.py": "CCCC"}), actions=ACTIONS),
    ])
    with patch.object(pr, "fetch_shelf_fingerprint", side_effect=lambda cl: next(scans)), \
            patch.object(pr, "build_bundle", side_effect=lambda cl, d, **kw: _built(d)):
        with pytest.raises(ValueError, match="after prepare"):
            pr.build_snapshot_bundle(CL, str(record))


def test_record_for_another_cl_is_refused(root):
    record = _record(root, cl="7")
    with pytest.raises(ValueError, match="is for CL 7"):
        pr.load_freeze_record(str(record), CL)


def test_record_outside_the_reviews_root_is_refused(root, tmp_path):
    elsewhere = tmp_path / "other" / CL / f"snap-{SNAP}"
    elsewhere.mkdir(parents=True)
    (elsewhere / "freeze.json").write_text(
        json.dumps({"schema_version": 1, "cl": CL}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="is not under"):
        pr.load_freeze_record(str(elsewhere), CL)


def test_main_snapshot_without_a_value_exits_2(root, capsys):
    assert pr.main(["prepare_review.py", CL, "--snapshot"]) == 2
    assert "--snapshot requires a freeze record path" in capsys.readouterr().err


def test_main_snapshot_emits_into_the_record_directory(root):
    record = _record(root)
    with patch.object(pr, "build_snapshot_bundle", return_value=({"cl": CL}, record.parent)) as build, \
            patch.object(pr, "emit_bundle", return_value=0) as emit:
        assert pr.main(["prepare_review.py", CL, "--snapshot", str(record), "--claim", "**/*.md"]) == 0
    assert build.call_args.args == (CL, str(record))
    assert build.call_args.kwargs["claim_globs"] == ["**/*.md"]
    assert emit.call_args.args == ({"cl": CL}, record.parent)


# ------------------------------------------------ workspace snapshots (no shelf read)

WS_FILES = {
    "//depot/a.py": {"rev": "3", "type": "text"},
    "//depot/new.py": {"rev": "1", "type": "text"},
    "//depot/old.py": {"rev": "2", "type": "text"},
    "//depot/pic.png": {"rev": "4", "type": "binary"},
}
WS_ACTIONS = {
    "//depot/a.py": "edit",
    "//depot/new.py": "add",
    "//depot/old.py": "delete",
    "//depot/pic.png": "edit",
}
WS_FP = {"//depot/a.py": "AA", "//depot/new.py": "BB", "//depot/old.py": "", "//depot/pic.png": "CC"}
DESCRIBE_S = (
    "Change 4242 by dev@ws1 on 2026/10/29 09:00:00 *pending*\n"
    "\n"
    "\tTune the thing\n"
    "\n"
    "Affected files ...\n"
    "\n"
    "... //depot/stale.py#9 edit\n"
    "\n"
    "Shelved files ...\n"
    "\n"
    "... //depot/foreign-shelf-only.py#1 add\n"
)
HAVE = {"//depot/a.py#3": "keep\nold line\ntail\n", "//depot/old.py#2": "gone one\ngone two\n"}


def _workspace_record(root: Path) -> Path:
    directory = root / CL / f"snap-{SNAP}"
    tree = directory / "tree" / "depot"
    tree.mkdir(parents=True)
    # CRLF on disk: the diff must not report a line-ending change.
    (tree / "a.py").write_bytes(b"keep\r\nnew line\r\ntail\r\n")
    (tree / "new.py").write_bytes(b"x = 1\ny = 2\n")
    (tree / "pic.png").write_bytes(b"\x89PNG" + b"\0" * 8)
    path = directory / "freeze.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cl": CL,
                "snapshot": SNAP,
                "client": "ws1",
                "source": "workspace",
                "fingerprint": WS_FP,
                "actions": WS_ACTIONS,
                "files": WS_FILES,
                "local": {},
                "auto_shelved": False,
                "use_shelf": False,
                "frozen_at": 0,
            }
        ),
        encoding="utf-8",
    )
    return path


def _ws_p4(calls: list):
    def run(args):
        calls.append(list(args))
        if args == ["describe", "-s", CL]:
            return 0, DESCRIBE_S, ""
        if args[:2] == ["print", "-q"] and len(args) == 3:
            spec = args[2]
            if spec in HAVE:
                return 0, HAVE[spec], ""
        raise AssertionError(f"unexpected p4 call: {args}")

    return run


def test_workspace_view_builds_a_describe_from_the_record_and_the_frozen_copies(root):
    record_path = _workspace_record(root)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    calls: list = []
    with patch.object(pr, "run_p4", side_effect=_ws_p4(calls)):
        view = pr.workspace_view(CL, record, record_path.parent / "tree")

    text = view.describe
    assert pr._is_pending(text)
    assert pr.parse_description(text) == "Tune the thing"
    # The file list is the record's: neither the CL's affected list nor its shelf leaks in.
    assert pr.parse_file_actions(text) == {
        "//depot/a.py": ("3", "edit"),
        "//depot/new.py": ("1", "add"),
        "//depot/old.py": ("2", "delete"),
        "//depot/pic.png": ("4", "edit"),
    }
    assert "stale.py" not in text and "foreign-shelf-only" not in text
    diff = pr.extract_diff(text)  # raw: every section is already complete
    _, sections = pr.split_diff_sections(diff)
    by_depot = {s["depot"]: s["header"] + s["body"] for s in sections}
    assert by_depot["//depot/a.py"].endswith("\n@@ -1,3 +1,3 @@\n keep\n-old line\n+new line\n tail\n")
    assert by_depot["//depot/new.py"].endswith("@@ -0,0 +1,2 @@\n+x = 1\n+y = 2\n")
    assert by_depot["//depot/old.py"].endswith("@@ -1,2 +0,0 @@\n-gone one\n-gone two\n")
    assert "(binary file modified: 12 bytes)" in by_depot["//depot/pic.png"]
    assert view.digests == WS_FP and view.actions == WS_ACTIONS
    # Only the header, and the have revision of the files the diff needs, are read from p4.
    assert calls[0] == ["describe", "-s", CL]
    assert sorted(c[2] for c in calls[1:]) == ["//depot/a.py#3", "//depot/old.py#2"]
    assert not any("@=" in part for call in calls for part in call)


def test_workspace_view_refuses_a_submitted_cl_or_a_missing_copy(root):
    record_path = _workspace_record(root)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    tree = record_path.parent / "tree"
    submitted = DESCRIBE_S.replace("*pending*", "")
    with patch.object(pr, "run_p4", return_value=(0, submitted, "")):
        with pytest.raises(ValueError, match="no longer pending"):
            pr.workspace_view(CL, record, tree)
    (tree / "depot" / "a.py").unlink()
    with patch.object(pr, "run_p4", side_effect=_ws_p4([])):
        with pytest.raises(ValueError, match="frozen copy of //depot/a.py is missing"):
            pr.workspace_view(CL, record, tree)


def test_workspace_snapshot_bundle_never_reads_or_prints_the_shelf(root):
    record = _workspace_record(root)
    seen: dict = {}

    def build(cl, d, **kw):
        seen.update(kw)
        return _bundle(d)

    calls: list = []
    with patch.object(pr, "fetch_shelf_fingerprint", side_effect=AssertionError("shelf was read")), \
            patch.object(pr, "build_bundle", side_effect=build), \
            patch.object(pr, "run_p4", side_effect=_ws_p4(calls)):
        bundle, bundle_dir = pr.build_snapshot_bundle(CL, str(record))

    view = seen["workspace_view"]
    assert isinstance(view, pr.WorkspaceView) and view.digests == WS_FP
    a = bundle["changed_files"][0]
    assert a["live_local"] == "C:/ws/a.py"
    assert Path(a["local"]) == bundle_dir / "tree" / "depot" / "a.py"
    assert bundle["changed_files"][1]["local"] == "C:/ws/old.py"  # delete: nothing frozen
    assert not any("-o" in call for call in calls)  # nothing was printed from a shelf
    # Nobody owns a shelf here, so step 10's cleanup has nothing to delete.
    assert bundle["auto_shelved"] is False and bundle["shelf_fingerprint"] == {}
    assert bundle["snapshot"]["source"] == "workspace"
    assert bundle["snapshot"]["auto_shelved"] is False


def test_build_bundle_with_a_workspace_view_reads_no_shelf_and_chunks_the_workspace_diff(tmp_path):
    view = pr.WorkspaceView(
        describe=(
            "Change 4242 by dev@ws1 on 2026/10/29 09:00:00 *pending*\n\n\tTune\n\n"
            "Affected files ...\n\n... //depot/a.py#3 edit\n\n"
            "Differences ...\n\n==== //depot/a.py#3 (text) ====\n\n"
            "@@ -1 +1 @@\n-old\n+workspace line\n"
        ),
        digests={"//depot/a.py": "AA"},
        actions={"//depot/a.py": "edit"},
    )
    boom = AssertionError("the shelf must not be consulted")
    with patch.object(pr, "fetch_shelf_fingerprint", side_effect=boom), \
            patch.object(pr, "fetch_describe", side_effect=boom), \
            patch.object(pr, "auto_shelve_cl", side_effect=boom), \
            patch.object(pr, "fetch_opened_files", side_effect=boom), \
            patch.object(pr, "resolve_local_paths", return_value={"//depot/a.py": None}), \
            patch.object(pr, "get_workspace_root", return_value=(None, None)), \
            patch.object(pr, "find_unreconciled", return_value=([], [])), \
            patch.object(pr, "find_unresolved", return_value=([], [])):
        bundle = pr.build_bundle(CL, tmp_path / "bundle", workspace_view=view)

    chunk = (tmp_path / "bundle" / bundle["diff_chunks"][0]["path"]).read_text(encoding="utf-8")
    assert "+workspace line" in chunk
    assert [f["depot"] for f in bundle["changed_files"]] == ["//depot/a.py"]
    assert bundle["auto_shelved"] is False and bundle["shelf_fingerprint"] == {}
    assert bundle["shelf_drift"] == []
    assert bundle["hygiene_incomplete"] == []
    assert bundle["description"] == "Tune"


def test_an_unknown_record_source_is_refused(root):
    record = _record(root)
    data = json.loads(record.read_text(encoding="utf-8"))
    data["source"] = "mystery"
    record.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown source 'mystery'"):
        pr.build_snapshot_bundle(CL, str(record))


# ------------------------------------------- the review's own shelf ownership record


def test_cleanup_forgets_the_ownership_record_when_it_deletes_the_shelf(root):
    record = _record(root)
    bundle_dir = record.parent
    (bundle_dir / "bundle.json").write_text(
        json.dumps({"cl": CL, "auto_shelved": True, "shelf_fingerprint": FP}), encoding="utf-8"
    )
    marker = root / CL / "review-shelf.json"
    pr.review_shelf.record_created(root / CL, CL, FP, ACTIONS)
    assert marker.is_file()
    with patch.object(pr, "fetch_shelf_fingerprint", return_value=pr.ShelfScanResult(digests=FP, actions=ACTIONS)), \
            patch.object(pr, "run_p4", return_value=(0, "", "")) as p4:
        assert pr.cleanup_auto_shelve(bundle_dir) == 0
    assert p4.call_args.args[0] == ["shelve", "-d", "-c", CL]
    assert not marker.exists()


def test_cleanup_forgets_the_record_but_keeps_a_shelf_someone_changed(root):
    record = _record(root)
    bundle_dir = record.parent
    (bundle_dir / "bundle.json").write_text(
        json.dumps({"cl": CL, "auto_shelved": True, "shelf_fingerprint": FP}), encoding="utf-8"
    )
    pr.review_shelf.record_created(root / CL, CL, FP, ACTIONS)
    changed = dict(FP, **{"//depot/a.py": "CCCC"})
    with patch.object(pr, "fetch_shelf_fingerprint", return_value=pr.ShelfScanResult(digests=changed, actions=ACTIONS)), \
            patch.object(pr, "run_p4") as p4:
        assert pr.cleanup_auto_shelve(bundle_dir) == 0
    p4.assert_not_called()  # the user's reshelved content is never deleted
    assert not (root / CL / "review-shelf.json").exists()


def test_cleanup_leaves_the_record_when_the_delete_fails(root):
    record = _record(root)
    (record.parent / "bundle.json").write_text(
        json.dumps({"cl": CL, "auto_shelved": True, "shelf_fingerprint": FP}), encoding="utf-8"
    )
    pr.review_shelf.record_created(root / CL, CL, FP, ACTIONS)
    with patch.object(pr, "fetch_shelf_fingerprint", return_value=pr.ShelfScanResult(digests=FP, actions=ACTIONS)), \
            patch.object(pr, "run_p4", return_value=(1, "", "boom")):
        assert pr.cleanup_auto_shelve(record.parent) == 1
    assert (root / CL / "review-shelf.json").is_file()


def test_the_inline_auto_shelve_records_ownership_in_the_cl_directory(tmp_path):
    describe = (
        "Change 4242 by u@c on 2026/01/01 12:00:00 *pending*\n\n\tdesc\n\n"
        "Shelved files ...\n\n... //depot/a.cpp#1 edit\n\nDifferences ...\n"
        "==== //depot/a.cpp#1 (text) ====\n@@ -1 +1 @@\n+hello\n"
    )
    shelf = pr.ShelfScanResult(digests={"//depot/a.cpp": "a1"}, actions={"//depot/a.cpp": "edit"})
    answers = iter([pr.PendingUnshelvedError("no shelf"), (describe, True)])
    cl_dir = tmp_path / "reviews" / CL
    with patch.object(pr, "fetch_describe", side_effect=answers), \
            patch.object(pr, "fetch_shelf_fingerprint", return_value=pr.ShelfScanResult()), \
            patch.object(pr, "auto_shelve_cl", return_value=shelf), \
            patch.object(pr, "resolve_local_paths", return_value={"//depot/a.cpp": None}), \
            patch.object(pr, "get_workspace_root", return_value=(None, None)), \
            patch.object(pr, "find_unreconciled", return_value=([], [])), \
            patch.object(pr, "find_unresolved", return_value=([], [])):
        bundle = pr.build_bundle(CL, cl_dir)
    assert bundle["auto_shelved"] is True
    assert pr.review_shelf.is_review_created(cl_dir, {"//depot/a.cpp": "A1"}, {"//depot/a.cpp": "edit"})


def test_a_snapshots_ownership_record_lives_beside_it_not_inside(root):
    snap = root / CL / f"snap-{SNAP}"
    assert pr._review_cl_dir(snap) == root / CL
    assert pr._review_cl_dir(root / CL) == root / CL
