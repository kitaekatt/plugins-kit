"""Tests for registry_repair.py -- the chimera and orphan-project self-repairs.

Every test targets an explicit registry path under tmp_path. Overriding HOME
does not sandbox os.path.expanduser on Windows (it reads USERPROFILE), so the
functions take the path as a parameter and the real registry is never touched.
"""

import json
import os

import pytest

from bootstrap_lib.registry_repair import (
    BACKUP_SUFFIX,
    apply_repair,
    describe_repair,
    describe_unrepairable,
    find_unrepairable,
    plan_repair,
)

HEALTHY = {"scope": "user", "version": "0.52.0", "installPath": "/cache/0.52.0"}
CHIMERA = {
    "scope": "user",
    "projectPath": "D:/dev/env-config",
    "version": "0.45.0",
    "installPath": "/cache/0.45.0",
}

# Defect 2: a project-scope record naming no project.
ORPHAN = {"scope": "project", "version": "0.3.4", "installPath": "/cache/0.3.4"}
PROJECT_A = dict(ORPHAN, projectPath="C:/dev/plugins-kit")
PROJECT_B = dict(ORPHAN, projectPath="C:/dev/spiritcrossing/main")


def _write_registry(tmp_path, plugins_data, wrapper=True):
    path = tmp_path / "plugins" / "installed_plugins.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"version": 2, "plugins": plugins_data} if wrapper else dict(plugins_data)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def _records(path, ref):
    return json.loads(path.read_text(encoding="utf-8"))["plugins"][ref]


class TestPlanRepair:
    def test_plans_drop_for_chimera_shape(self):
        plan = plan_repair({"plugins": {"bootstrap@plugins-kit": [CHIMERA, HEALTHY]}})
        keep, dropped = plan["bootstrap@plugins-kit"]
        assert dropped == [CHIMERA]
        assert keep == [HEALTHY]

    def test_project_scope_records_untouched(self):
        project = {"scope": "project", "projectPath": "D:/dev/x", "version": "0.45.0"}
        plan = plan_repair({"plugins": {"p4-kit@plugins-kit": [project, HEALTHY]}})
        assert plan == {}

    def test_no_healthy_survivor_refuses(self):
        only_chimeras = [CHIMERA, dict(CHIMERA, version="0.46.0")]
        plan = plan_repair({"plugins": {"bootstrap@plugins-kit": only_chimeras}})
        assert plan == {}

    def test_single_record_no_op(self):
        assert plan_repair({"plugins": {"bootstrap@plugins-kit": [CHIMERA]}}) == {}
        assert plan_repair({"plugins": {"bootstrap@plugins-kit": [HEALTHY]}}) == {}

    def test_dict_shaped_entry_no_op(self):
        assert plan_repair({"plugins": {"bootstrap@plugins-kit": dict(HEALTHY)}}) == {}

    def test_bare_mapping_registry_shape(self):
        plan = plan_repair({"bootstrap@plugins-kit": [CHIMERA, HEALTHY]})
        assert plan["bootstrap@plugins-kit"][1] == [CHIMERA]

    def test_unrecognized_shape_no_op(self):
        assert plan_repair([]) == {}
        assert plan_repair("nonsense") == {}

    def test_generalizes_beyond_bootstrap(self):
        plan = plan_repair({"plugins": {"unreal-kit@plugins-kit": [CHIMERA, HEALTHY]}})
        assert plan["unreal-kit@plugins-kit"][1] == [CHIMERA]


class TestPlanOrphanProject:
    """Rule 2: scope=="project" with no projectPath."""

    def test_drops_orphans_alongside_well_formed_project_records(self):
        """The live 2026-07-27 engineer@spryfox-plugins shape: 4 records, 2 orphans."""
        records = [ORPHAN, PROJECT_A, ORPHAN, PROJECT_B]
        plan = plan_repair({"plugins": {"engineer@spryfox-plugins": records}})
        keep, dropped = plan["engineer@spryfox-plugins"]
        assert dropped == [ORPHAN, ORPHAN]
        assert keep == [PROJECT_A, PROJECT_B]

    def test_drops_orphan_at_index_zero(self):
        """The live prototyping@spryfox-plugins shape: orphan is entries[0]."""
        plan = plan_repair({"plugins": {"prototyping@spryfox-plugins": [ORPHAN, PROJECT_A, PROJECT_B]}})
        keep, dropped = plan["prototyping@spryfox-plugins"]
        assert dropped == [ORPHAN]
        assert keep[0] == PROJECT_A

    def test_well_formed_project_records_untouched(self):
        assert plan_repair({"plugins": {"x@m": [PROJECT_A, PROJECT_B]}}) == {}

    def test_null_and_empty_project_path_are_orphans(self):
        for bad in (None, ""):
            records = [dict(ORPHAN, projectPath=bad), PROJECT_A]
            plan = plan_repair({"plugins": {"x@m": records}})
            assert plan["x@m"][1] == [dict(ORPHAN, projectPath=bad)]

    def test_user_scope_record_counts_as_survivor(self):
        plan = plan_repair({"plugins": {"x@m": [ORPHAN, HEALTHY]}})
        keep, dropped = plan["x@m"]
        assert dropped == [ORPHAN]
        assert keep == [HEALTHY]

    def test_healthy_survivor_guard_refuses_when_all_records_are_orphans(self):
        assert plan_repair({"plugins": {"x@m": [ORPHAN, dict(ORPHAN, version="0.3.5")]}}) == {}

    def test_healthy_survivor_guard_refuses_lone_orphan(self):
        assert plan_repair({"plugins": {"x@m": [ORPHAN]}}) == {}

    def test_both_rules_apply_to_one_ref_in_a_single_pass(self):
        records = [CHIMERA, ORPHAN, HEALTHY, PROJECT_A]
        keep, dropped = plan_repair({"plugins": {"x@m": records}})["x@m"]
        assert dropped == [CHIMERA, ORPHAN]
        assert keep == [HEALTHY, PROJECT_A]


class TestFindUnrepairable:
    def test_reports_ref_with_only_orphans(self):
        assert find_unrepairable({"plugins": {"x@m": [ORPHAN, ORPHAN]}}) == {"x@m": 2}

    def test_repairable_ref_is_not_reported(self):
        assert find_unrepairable({"plugins": {"x@m": [ORPHAN, PROJECT_A]}}) == {}

    def test_healthy_registry_reports_nothing(self):
        assert find_unrepairable({"plugins": {"x@m": [HEALTHY], "y@m": [PROJECT_A]}}) == {}

    def test_unreadable_registry_reports_nothing(self):
        assert find_unrepairable(None) == {}
        assert find_unrepairable("nonsense") == {}
        assert find_unrepairable({"plugins": {"x@m": []}}) == {}

    def test_describe_names_refs_and_counts(self):
        assert describe_unrepairable({}) == ""
        msg = describe_unrepairable({"x@m": 2})
        assert "x@m (2)" in msg
        assert "deregister" in msg


class TestApplyOrphanProject:
    def test_live_shape_is_repaired_on_disk(self, tmp_path):
        path = _write_registry(tmp_path, {
            "engineer@spryfox-plugins": [ORPHAN, PROJECT_A, ORPHAN, PROJECT_B],
            "prototyping@spryfox-plugins": [ORPHAN, PROJECT_A, PROJECT_B],
            "bootstrap@plugins-kit": [PROJECT_B],
        })
        dropped = apply_repair(str(path))

        assert sorted(dropped) == ["engineer@spryfox-plugins", "prototyping@spryfox-plugins"]
        assert _records(path, "engineer@spryfox-plugins") == [PROJECT_A, PROJECT_B]
        assert _records(path, "prototyping@spryfox-plugins") == [PROJECT_A, PROJECT_B]
        assert _records(path, "bootstrap@plugins-kit") == [PROJECT_B]

    def test_idempotent_second_run_writes_nothing(self, tmp_path):
        path = _write_registry(tmp_path, {"x@m": [ORPHAN, PROJECT_A]})
        assert apply_repair(str(path))

        after_first = path.read_text(encoding="utf-8")
        os.utime(path, (1_000_000_000, 1_000_000_000))
        mtime_after_first = path.stat().st_mtime_ns

        assert apply_repair(str(path)) == {}

        assert path.read_text(encoding="utf-8") == after_first
        assert path.stat().st_mtime_ns == mtime_after_first

    def test_all_orphan_ref_leaves_file_untouched(self, tmp_path):
        path = _write_registry(tmp_path, {"x@m": [ORPHAN, dict(ORPHAN, version="0.3.5")]})
        before = path.read_text(encoding="utf-8")
        assert apply_repair(str(path)) == {}
        assert path.read_text(encoding="utf-8") == before

    def test_well_formed_project_registry_is_not_rewritten(self, tmp_path):
        """A registry of legitimate per-project installs must be a pure no-op."""
        path = _write_registry(tmp_path, {"x@m": [PROJECT_A, PROJECT_B], "y@m": [HEALTHY]})
        before_text = path.read_text(encoding="utf-8")
        os.utime(path, (1_000_000_000, 1_000_000_000))
        before_mtime = path.stat().st_mtime_ns

        assert apply_repair(str(path)) == {}

        assert path.read_text(encoding="utf-8") == before_text
        assert path.stat().st_mtime_ns == before_mtime

    def test_duplicate_well_formed_records_are_left_alone(self, tmp_path):
        """Dedupe was declined by design -- see the module docstring."""
        path = _write_registry(tmp_path, {"x@m": [PROJECT_A, PROJECT_A]})
        assert apply_repair(str(path)) == {}
        assert _records(path, "x@m") == [PROJECT_A, PROJECT_A]

    def test_version_and_install_path_are_never_rewritten(self, tmp_path):
        survivor = dict(PROJECT_A, version="0.1.0", installPath="/cache/0.1.0")
        path = _write_registry(tmp_path, {"x@m": [ORPHAN, survivor]})
        apply_repair(str(path))
        assert _records(path, "x@m") == [survivor]


class TestApplyRepair:
    def test_repairs_bootstrap_ref(self, tmp_path):
        path = _write_registry(tmp_path, {"bootstrap@plugins-kit": [CHIMERA, HEALTHY]})
        dropped = apply_repair(str(path))
        assert dropped == {"bootstrap@plugins-kit": [CHIMERA]}
        assert _records(path, "bootstrap@plugins-kit") == [HEALTHY]

    def test_repairs_other_ref(self, tmp_path):
        path = _write_registry(tmp_path, {"git-kit@plugins-kit": [CHIMERA, HEALTHY]})
        dropped = apply_repair(str(path))
        assert list(dropped) == ["git-kit@plugins-kit"]
        assert _records(path, "git-kit@plugins-kit") == [HEALTHY]

    def test_multi_ref_repair_in_one_pass(self, tmp_path):
        path = _write_registry(tmp_path, {
            "bootstrap@plugins-kit": [CHIMERA, HEALTHY],
            "p4-kit@plugins-kit": [CHIMERA, HEALTHY],
            "cache-kit@plugins-kit": [HEALTHY],
        })
        dropped = apply_repair(str(path))
        assert sorted(dropped) == ["bootstrap@plugins-kit", "p4-kit@plugins-kit"]
        assert _records(path, "cache-kit@plugins-kit") == [HEALTHY]

    def test_project_scope_records_survive(self, tmp_path):
        project = {"scope": "project", "projectPath": "D:/dev/x", "version": "0.45.0"}
        path = _write_registry(tmp_path, {
            "bootstrap@plugins-kit": [project, CHIMERA, HEALTHY],
        })
        apply_repair(str(path))
        assert _records(path, "bootstrap@plugins-kit") == [project, HEALTHY]

    def test_no_healthy_survivor_leaves_file_untouched(self, tmp_path):
        path = _write_registry(tmp_path, {"bootstrap@plugins-kit": [CHIMERA, dict(CHIMERA, version="0.46.0")]})
        before = path.read_text(encoding="utf-8")
        assert apply_repair(str(path)) == {}
        assert path.read_text(encoding="utf-8") == before

    def test_clean_registry_is_not_rewritten(self, tmp_path):
        """No-op pass must not touch the file: its mtime arms the cooldown bypass."""
        path = _write_registry(tmp_path, {"bootstrap@plugins-kit": [HEALTHY]})
        before_text = path.read_text(encoding="utf-8")
        os.utime(path, (1_000_000_000, 1_000_000_000))
        before_mtime = path.stat().st_mtime_ns

        assert apply_repair(str(path)) == {}

        assert path.read_text(encoding="utf-8") == before_text
        assert path.stat().st_mtime_ns == before_mtime
        assert sorted(os.listdir(path.parent)) == ["installed_plugins.json"]

    def test_idempotent_second_run_is_byte_identical_and_writes_nothing(self, tmp_path):
        path = _write_registry(tmp_path, {"bootstrap@plugins-kit": [CHIMERA, HEALTHY]})
        assert apply_repair(str(path))

        after_first = path.read_text(encoding="utf-8")
        os.utime(path, (1_000_000_000, 1_000_000_000))
        mtime_after_first = path.stat().st_mtime_ns

        assert apply_repair(str(path)) == {}

        assert path.read_text(encoding="utf-8") == after_first
        assert path.stat().st_mtime_ns == mtime_after_first

    def test_backup_created_and_write_leaves_no_temp_files(self, tmp_path):
        path = _write_registry(tmp_path, {"bootstrap@plugins-kit": [CHIMERA, HEALTHY]})
        original = path.read_text(encoding="utf-8")

        apply_repair(str(path))

        backup = path.parent / ("installed_plugins.json" + BACKUP_SUFFIX)
        assert backup.read_text(encoding="utf-8") == original
        assert sorted(os.listdir(path.parent)) == [
            "installed_plugins.json",
            "installed_plugins.json" + BACKUP_SUFFIX,
        ]

    def test_backup_can_be_disabled(self, tmp_path):
        path = _write_registry(tmp_path, {"bootstrap@plugins-kit": [CHIMERA, HEALTHY]})
        apply_repair(str(path), backup=False)
        assert sorted(os.listdir(path.parent)) == ["installed_plugins.json"]

    def test_missing_registry_no_op(self, tmp_path):
        assert apply_repair(str(tmp_path / "nope" / "installed_plugins.json")) == {}

    def test_garbage_registry_no_op(self, tmp_path):
        path = tmp_path / "installed_plugins.json"
        path.write_text("{not json at all", encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        assert apply_repair(str(path)) == {}
        assert path.read_text(encoding="utf-8") == before

    def test_bare_mapping_registry_repaired(self, tmp_path):
        path = _write_registry(
            tmp_path, {"bootstrap@plugins-kit": [CHIMERA, HEALTHY]}, wrapper=False,
        )
        assert apply_repair(str(path)) == {"bootstrap@plugins-kit": [CHIMERA]}
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["bootstrap@plugins-kit"] == [HEALTHY]


class TestDescribeRepair:
    def test_empty_result_has_no_message(self):
        assert describe_repair({}) == ""

    def test_names_refs_and_versions(self):
        msg = describe_repair({"bootstrap@plugins-kit": [CHIMERA], "p4-kit@plugins-kit": [CHIMERA]})
        assert "bootstrap@plugins-kit [0.45.0]" in msg
        assert "p4-kit@plugins-kit [0.45.0]" in msg
        assert "dropped 2 malformed record(s)" in msg
        assert "next session" in msg


class TestEngineWiring:
    """The engine must log the repair -- a silent bootstrap operation is a bug."""

    def test_engine_imports_and_reports_repair(self, tmp_path):
        from bootstrap_lib.registry_repair import apply_repair as engine_apply
        path = _write_registry(tmp_path, {"bootstrap@plugins-kit": [CHIMERA, HEALTHY]})
        assert describe_repair(engine_apply(str(path)))

    def test_engine_reports_skipped_refs(self, tmp_path):
        """The healthy-survivor guard must log, not skip silently."""
        path = _write_registry(tmp_path, {"x@m": [ORPHAN, ORPHAN]})
        assert apply_repair(str(path)) == {}
        assert describe_unrepairable(find_unrepairable(json.loads(path.read_text(encoding="utf-8"))))

    def test_engine_step_wired_before_plugins_phase(self):
        engine_src = os.path.join(
            os.path.dirname(__file__), os.pardir, os.pardir,
            "plugins", "bootstrap", "bootstrap_lib", "engine.py",
        )
        with open(engine_src, "r", encoding="utf-8") as fh:
            src = fh.read()
        repair_at = src.index("from .registry_repair import")
        layered_at = src.index("# Step 3c: Process layered bootstrap manifests")
        assert repair_at < layered_at, "repair must run before any plugins phase"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
