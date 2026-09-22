"""Tests for the real fix-up redirector commandlet entrypoint."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from _redirector_fakes import make_harness


@pytest.mark.parametrize(
    ("kwargs", "failed_phase", "affected"),
    [
        ({"load_results": [None]}, "load", "/Game/A"),
        ({"load_results": [object(), None]}, "save", "/Game/A"),
        ({"load_results": [object(), object()], "save_result": False}, "save", "/Game/A"),
        (
            {"soft_rewrite_error": RuntimeError("rewrite failed")},
            "soft_rewrite",
            "/Game/A",
        ),
    ],
)
def test_failed_rewrite_phase_blocks_all_redirector_deletion(
    tmp_path: Path, kwargs: dict, failed_phase: str, affected: str
) -> None:
    harness = make_harness(tmp_path, **kwargs)

    code, _stdout, stderr, manifest_path = harness.run()

    assert code != 0
    assert "FAIL" in stderr
    assert not any(event[0] == "ue_delete" for event in harness.events)
    assert not any(event[0] == "p4_delete" for event in harness.events)
    assert not any(event[0] == "reopen" for event in harness.events)
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert manifest["pre_delete_phase"] == failed_phase
    assert affected in manifest["pre_delete_failures"]


def test_collection_guard_failure_blocks_referencer_and_ue_mutations(
    tmp_path: Path,
) -> None:
    harness = make_harness(tmp_path, setter_error=RuntimeError("setter failed"))

    code, _stdout, stderr, manifest_path = harness.run()

    assert code != 0
    assert "b_auto_commit_on_save" in stderr
    mutation_names = {"edit", "asset_tools", "load", "save", "ue_delete", "p4_delete"}
    assert not mutation_names.intersection(event[0] for event in harness.events)
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert manifest["pre_delete_phase"] == "collection_guard"
    assert manifest["pre_delete_failures"]


def test_all_success_rewrites_then_deletes_and_records_manifest(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)

    code, stdout, _stderr, manifest_path = harness.run()

    assert code == 0
    assert "Done. CL 123: deleted 1 redirectors" in stdout
    names = [event[0] for event in harness.events]
    assert names.index("edit") < names.index("ue_delete") < names.index("p4_delete")
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert manifest["redirectors_deleted"] == 1
    assert manifest["referencers_saved"] == 1


def test_delete_only_skips_referencer_work(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, mode="delete-only")

    code, _stdout, _stderr, manifest_path = harness.run()

    assert code == 0
    names = [event[0] for event in harness.events]
    assert "edit" not in names
    assert "load" not in names
    assert "ue_delete" not in names
    assert "p4_delete" in names
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert manifest["mode"] == "delete-only"
    assert manifest["redirectors_deleted"] == 1


def test_auto_opened_delete_is_confirmed_after_reopen(tmp_path: Path) -> None:
    redirector = tmp_path / "project" / "Content" / "R.uasset"
    harness = make_harness(
        tmp_path,
        fstat_output=f"... depotFile {redirector}\n... action delete\n",
    )

    code, _stdout, _stderr, manifest_path = harness.run()

    assert code == 0
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert manifest["redirectors_deleted"] == 1
    assert manifest["mutation_outcomes"] == {str(redirector): "confirmed"}


def test_manifest_write_failure_blocks_all_mutation(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, fail_manifest=True)

    code, _stdout, stderr, _manifest_path = harness.run()

    assert code != 0
    assert "pre-mutation recovery record" in stderr
    mutation_names = {"collection_set", "edit", "load", "save", "ue_delete", "p4_delete"}
    assert not mutation_names.intersection(event[0] for event in harness.events)


def test_preexisting_project_collection_edit_blocks_before_new_cl(tmp_path: Path) -> None:
    harness = make_harness(
        tmp_path,
        preexisting_collections=["//depot/project/Content/Collections/Existing.collection"],
    )

    code, _stdout, stderr, _manifest_path = harness.run()

    assert code != 0
    assert "pre-existing" in stderr.lower()
    assert not any(event[0] == "create_cl" for event in harness.events)
    assert not any(event[0] in {"edit", "ue_delete", "p4_delete"} for event in harness.events)


def test_collection_query_failure_is_incomplete_and_never_sweeps(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, collection_query_error=True)

    code, _stdout, stderr, manifest_path = harness.run()

    assert code != 0
    assert "collection" in stderr.lower()
    assert not any(event[0] == "reopen" for event in harness.events)
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert manifest["pre_delete_phase"] == "collection_reconciliation"


def test_map_native_candidate_must_match_classification_snapshot(tmp_path: Path) -> None:
    harness = make_harness(tmp_path)
    umap = harness.project / "Content" / "R.umap"
    umap.write_bytes(b"map")
    harness.safe_json.write_text(
        harness.safe_json.read_text(encoding="utf-8").replace(
            '"referencer_files": [',
            f'"mutation_files": ["{harness.redirector_file}"], "referencer_files": ['
        ),
        encoding="utf-8",
    )
    harness.redirector_file.unlink()

    code, _stdout, stderr, _manifest_path = harness.run()

    assert code != 0
    assert "candidate" in stderr.lower() or "mutation" in stderr.lower()
    assert not any(event[0] == "create_cl" for event in harness.events)


@pytest.mark.parametrize("mode", ["fixup", "delete-only"])
def test_fstat_failure_is_unknown_and_retains_cl_and_ue_result(
    tmp_path: Path, mode: str
) -> None:
    harness = make_harness(tmp_path, mode=mode, fstat_rc=1)

    code, stdout, stderr, manifest_path = harness.run()

    assert code != 0
    assert "Done. CL" not in stdout
    assert "fstat" in stderr.lower()
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert manifest["cl"] == "123"
    assert manifest["mutation_outcomes"] == {str(harness.redirector_file): "unknown"}
    assert manifest["redirectors_deleted"] == 0
    if mode == "fixup":
        assert manifest["ue_deleted"] == 1


def test_omitted_fstat_record_is_unknown_and_not_counted(tmp_path: Path) -> None:
    harness = make_harness(tmp_path, fstat_output="")

    code, _stdout, stderr, manifest_path = harness.run()

    assert code != 0
    assert "missing" in stderr.lower() or "omitted" in stderr.lower()
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert manifest["redirectors_deleted"] == 0
    assert manifest["mutation_outcomes"] == {str(harness.redirector_file): "unknown"}


def test_unexpected_fstat_action_is_unknown_and_not_deleted(tmp_path: Path) -> None:
    harness = make_harness(
        tmp_path,
        fstat_output=f"... depotFile {tmp_path / 'project' / 'Content' / 'R.uasset'}\n"
        "... action edit\n",
    )

    code, _stdout, stderr, manifest_path = harness.run()

    assert code != 0
    assert "unexpected" in stderr.lower() or "action" in stderr.lower()
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert manifest["redirectors_deleted"] == 0
    assert manifest["mutation_outcomes"] == {str(harness.redirector_file): "unknown"}


@pytest.mark.parametrize(
    ("delete_error", "expected"),
    [
        ("delete failed", "failed"),
        ("being used by another process", "pending-retry"),
    ],
)
def test_partial_delete_outcomes_are_persisted_and_fail(
    tmp_path: Path, delete_error: str, expected: str
) -> None:
    redirector = tmp_path / "project" / "Content" / "R.uasset"
    harness = make_harness(
        tmp_path,
        delete_results={str(redirector): (1, delete_error)},
    )

    code, stdout, stderr, manifest_path = harness.run()

    assert code != 0
    assert "Done. CL" not in stdout
    assert "FAIL" in stderr
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert manifest["mutation_outcomes"] == {str(redirector): expected}
    assert manifest["redirectors_deleted"] == 0
    if expected == "pending-retry":
        assert manifest["lock_retry_list"]
        assert "p4 -x - delete -c 123" in stdout


def test_failed_reopen_is_failed_and_does_not_report_complete(tmp_path: Path) -> None:
    redirector = tmp_path / "project" / "Content" / "R.uasset"
    harness = make_harness(
        tmp_path,
        fstat_output=f"... depotFile {redirector}\n... action delete\n",
        reopen_error=RuntimeError("reopen failed"),
    )

    code, stdout, stderr, manifest_path = harness.run()

    assert code != 0
    assert "Done. CL" not in stdout
    assert "reopen" in stderr.lower()
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert manifest["mutation_outcomes"] == {str(redirector): "failed"}


def test_final_cl_membership_must_confirm_delete_action(tmp_path: Path) -> None:
    redirector = tmp_path / "project" / "Content" / "R.uasset"
    harness = make_harness(
        tmp_path,
        opened_output=f"{redirector}#1 - edit change 123 (text) by alice@ws\n",
    )

    code, stdout, stderr, manifest_path = harness.run()

    assert code != 0
    assert "Done. CL" not in stdout
    assert "unknown" in stderr.lower()
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert manifest["redirectors_deleted"] == 0
    assert manifest["mutation_outcomes"] == {str(redirector): "unknown"}
