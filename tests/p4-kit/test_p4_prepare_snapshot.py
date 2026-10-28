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
