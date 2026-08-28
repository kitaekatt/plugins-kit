from __future__ import annotations

from dataclasses import FrozenInstanceError
from io import StringIO
import json
import os
from pathlib import Path

import pytest

from bootstrap.link_compat import requires_symlinks
from bootstrap_lib.managed_state import (
    Declaration,
    Inspection,
    Operation,
    ResourceResult,
    ResourceApplyError,
    State,
    Status,
    Symlink,
    plan,
    run,
    run_cli,
)
import bootstrap_lib.managed_state.symlink as symlink_module


class FakeResource:
    def __init__(self, name: str, state: State):
        self.name = name
        self.state = state
        self.inspections = 0
        self.convergences = 0

    def inspect(self) -> Inspection:
        self.inspections += 1
        return Inspection(self.state, self.state.value)

    def converge(self, before: Inspection, operation: Operation) -> ResourceResult:
        self.convergences += 1
        self.state = State.CURRENT
        return ResourceResult(
            self.name, Status.CHANGED, before.state, State.CURRENT, "converged")


class FailingResource(FakeResource):
    def converge(self, before: Inspection, operation: Operation) -> ResourceResult:
        raise ResourceApplyError(
            "replacement failed",
            after=Inspection(State.DRIFTED, "original target remains"),
            backup="/recovery/target.backup",
            rollback="rollback failed: sharing violation",
        )


class TestRunner:
    def test_declaration_is_ordered_and_immutable(self):
        first = FakeResource("first", State.CURRENT)
        second = FakeResource("second", State.MISSING)
        declaration = Declaration([first, second])
        assert declaration.resources == (first, second)
        with pytest.raises(FrozenInstanceError):
            declaration.resources = ()

    def test_plan_is_read_only(self):
        resource = FakeResource("x", State.MISSING)
        result = plan([resource], Operation.UPDATE)
        assert result.items[0].will_change
        assert resource.inspections == 1
        assert resource.convergences == 0

    @pytest.mark.parametrize("state", [State.MISSING, State.DRIFTED])
    def test_check_reports_drift_without_mutation(self, state):
        resource = FakeResource("x", state)
        report = run([resource], "check")
        assert not report.ok
        assert not report.changed
        assert resource.convergences == 0

    def test_install_creates_missing_but_refuses_drift(self):
        missing = FakeResource("missing", State.MISSING)
        drifted = FakeResource("drifted", State.DRIFTED)
        report = run([missing, drifted], "install")
        assert missing.convergences == 1
        assert drifted.convergences == 0
        assert report.results[1].status is Status.BLOCKED
        assert not report.ok

    def test_update_converges_missing_and_drift(self):
        resources = [
            FakeResource("missing", State.MISSING),
            FakeResource("drifted", State.DRIFTED),
        ]
        report = run(resources, "update")
        assert report.ok and report.changed
        assert [resource.convergences for resource in resources] == [1, 1]

    def test_install_rechecks_before_mutating(self):
        resource = FakeResource("x", State.MISSING)
        original_inspect = resource.inspect

        def changing_inspect():
            result = original_inspect()
            if resource.inspections == 1:
                resource.state = State.DRIFTED
            return result

        resource.inspect = changing_inspect
        report = run([resource], "install")
        assert not report.ok
        assert resource.convergences == 0

    def test_current_resource_is_noop_for_every_operation(self):
        for operation in Operation:
            resource = FakeResource("current", State.CURRENT)
            report = run([resource], operation)
            assert report.ok and not report.changed
            assert report.results[0].status is Status.UNCHANGED
            assert resource.convergences == 0

    def test_cli_json_and_exit_status(self):
        output = StringIO()
        assert run_cli([FakeResource("x", State.CURRENT)],
                       ["check", "--json"], stdout=output) == 0
        assert json.loads(output.getvalue())["ok"] is True

        errors = StringIO()
        assert run_cli([FakeResource("x", State.DRIFTED)],
                       ["check"], stderr=errors) == 1
        assert "blocked: x" in errors.getvalue()

    def test_human_cli_renders_failed_recovery_facts(self):
        errors = StringIO()
        exit_code = run_cli(
            [FailingResource("x", State.DRIFTED)],
            ["update"],
            stderr=errors,
        )
        assert exit_code == 1
        assert errors.getvalue().splitlines() == [
            "failed: x: replacement failed",
            "  backup: /recovery/target.backup",
            "  rollback: rollback failed: sharing violation",
        ]


@requires_symlinks
class TestSymlink:
    def resource(self, tmp_path: Path, *, backup=True):
        source = tmp_path / "source file.txt"
        target = tmp_path / "target dir" / "target file.txt"
        source.write_text("source")
        return source, target, Symlink(source, target, "agents", backup)

    def test_missing_target_install_and_correct_noop(self, tmp_path):
        source, target, resource = self.resource(tmp_path)
        report = run([resource], "install")
        assert report.ok and report.changed
        assert target.is_symlink()
        assert os.path.samefile(target, source)
        mtime = os.lstat(target).st_mtime_ns

        second = run([resource], "update")
        assert second.ok and not second.changed
        assert os.lstat(target).st_mtime_ns == mtime

    def test_relative_source_is_encoded_as_an_absolute_link(
        self, tmp_path, monkeypatch
    ):
        source = tmp_path / "relative source"
        target = tmp_path / "elsewhere" / "target"
        source.write_text("source")
        monkeypatch.chdir(tmp_path)
        report = run([Symlink("relative source", target, "relative")], "install")
        assert report.ok
        assert Path(os.readlink(target)).is_absolute()
        assert os.path.samefile(target, source)

    def test_install_race_never_overwrites_new_target(
        self, tmp_path, monkeypatch
    ):
        _, target, resource = self.resource(tmp_path)
        real_symlink = os.symlink

        def racing_symlink(source, destination, **kwargs):
            Path(destination).write_text("arrived concurrently")
            return real_symlink(source, destination, **kwargs)

        monkeypatch.setattr(symlink_module.os, "symlink", racing_symlink)
        report = run([resource], "install")
        assert not report.ok
        assert target.read_text() == "arrived concurrently"
        assert report.results[0].after is State.DRIFTED

    def test_wrong_and_dangling_links_require_update(self, tmp_path):
        source, target, resource = self.resource(tmp_path)
        target.parent.mkdir(parents=True)
        wrong = tmp_path / "wrong"
        wrong.write_text("wrong")
        target.symlink_to(wrong)
        assert not run([resource], "install").ok
        assert run([resource], "update").ok
        assert os.path.samefile(target, source)

        target.unlink()
        target.symlink_to(tmp_path / "missing-destination")
        assert resource.inspect().state is State.DRIFTED
        assert run([resource], "update").ok
        assert os.path.samefile(target, source)

    def test_regular_file_is_backed_up_collision_safely(self, tmp_path):
        source, target, resource = self.resource(tmp_path)
        target.parent.mkdir(parents=True)
        target.write_text("local")
        target.with_name(target.name + ".backup").write_text("older")
        report = run([resource], "update")
        assert report.ok
        assert target.with_name(target.name + ".backup").read_text() == "older"
        assert target.with_name(target.name + ".backup.1").read_text() == "local"
        assert report.results[0].backup.endswith(".backup.1")

    def test_human_cli_renders_successful_backup(self, tmp_path):
        _, target, resource = self.resource(tmp_path)
        target.parent.mkdir(parents=True)
        target.write_text("local")
        output = StringIO()
        assert run_cli([resource], ["update"], stdout=output) == 0
        lines = output.getvalue().splitlines()
        assert lines[0].startswith("changed: agents: linked ")
        assert lines[1] == f"  backup: {target}.backup"

    def test_backup_reservation_retries_a_concurrent_collision(
        self, tmp_path, monkeypatch
    ):
        _, target, resource = self.resource(tmp_path)
        target.parent.mkdir(parents=True)
        target.write_text("local")
        first_backup = target.with_name(target.name + ".backup")
        real_link = os.link
        collided = False

        def racing_link(source, destination, **kwargs):
            nonlocal collided
            if not collided:
                collided = True
                Path(destination).write_text("concurrent backup")
                raise FileExistsError(destination)
            return real_link(source, destination, **kwargs)

        monkeypatch.setattr(symlink_module.os, "link", racing_link)
        report = run([resource], "update")
        assert report.ok
        assert first_backup.read_text() == "concurrent backup"
        assert target.with_name(target.name + ".backup.1").read_text() == "local"

    def test_regular_file_without_backup_is_atomically_replaced(self, tmp_path):
        source, target, resource = self.resource(tmp_path, backup=False)
        target.parent.mkdir(parents=True)
        target.write_text("stale")
        assert run([resource], "update").ok
        assert target.is_symlink() and os.path.samefile(target, source)

    def test_missing_source_blocks_without_writes(self, tmp_path):
        target = tmp_path / "target"
        resource = Symlink(tmp_path / "missing", target, "agents")
        report = run([resource], "update")
        assert not report.ok
        assert report.results[0].before is State.ERROR
        assert not os.path.lexists(target)

    def test_source_and_target_same_path_is_refused(self, tmp_path):
        source = tmp_path / "same"
        source.write_text("precious")
        report = run([Symlink(source, source, "same")], "update")
        assert not report.ok
        assert source.read_text() == "precious"

    def test_hardlink_alias_of_source_is_refused(self, tmp_path):
        source = tmp_path / "source"
        target = tmp_path / "hardlink"
        source.write_text("precious")
        os.link(source, target)
        report = run([Symlink(source, target, "alias")], "update")
        assert not report.ok
        assert source.read_text() == target.read_text() == "precious"

    def test_directory_is_never_replaced(self, tmp_path):
        _, target, resource = self.resource(tmp_path)
        target.mkdir(parents=True)
        report = run([resource], "update")
        assert not report.ok
        assert target.is_dir() and not target.is_symlink()

    def test_final_switch_uses_temporary_sibling_and_os_replace(
        self, tmp_path, monkeypatch
    ):
        _, target, resource = self.resource(tmp_path)
        target.parent.mkdir(parents=True)
        wrong = tmp_path / "wrong"
        wrong.write_text("wrong")
        target.symlink_to(wrong)
        calls = []
        real_replace = os.replace

        def recording_replace(source, destination):
            calls.append((Path(source), Path(destination)))
            return real_replace(source, destination)

        monkeypatch.setattr(symlink_module.os, "replace", recording_replace)
        assert run([resource], "update").ok
        assert calls[-1][1] == target
        assert calls[-1][0].parent == target.parent
        assert calls[-1][0].name.startswith(f".{target.name}.link.")

    def test_failed_final_replace_rolls_regular_file_back(
        self, tmp_path, monkeypatch
    ):
        _, target, resource = self.resource(tmp_path)
        target.parent.mkdir(parents=True)
        target.write_text("precious")
        real_replace = os.replace
        def fail_replace(source, destination):
            raise OSError("injected final replace failure")

        monkeypatch.setattr(symlink_module.os, "replace", fail_replace)
        report = run([resource], "update")
        assert not report.ok
        assert target.read_text() == "precious"
        assert report.results[0].after is State.DRIFTED
        assert "original target remained" in report.results[0].rollback
        assert not list(target.parent.glob(f".{target.name}.link.*"))

    def test_post_verification_failure_restores_backup(
        self, tmp_path, monkeypatch
    ):
        source, target, resource = self.resource(tmp_path)
        target.parent.mkdir(parents=True)
        target.write_text("precious")
        real_replace = os.replace

        def replace_then_invalidate(from_path, to_path):
            result = real_replace(from_path, to_path)
            if Path(to_path) == target and Path(from_path).name.startswith(
                f".{target.name}.link."
            ):
                source.unlink()
            return result

        monkeypatch.setattr(symlink_module.os, "replace", replace_then_invalidate)
        report = run([resource], "update")
        assert not report.ok
        assert target.read_text() == "precious"
        assert "restored original regular target" in report.results[0].rollback
        assert report.results[0].backup is None

    def test_rollback_failure_reports_backup_and_actual_state(
        self, tmp_path, monkeypatch
    ):
        _, target, resource = self.resource(tmp_path)
        target.parent.mkdir(parents=True)
        target.write_text("precious")
        real_unlink = os.unlink

        monkeypatch.setattr(
            symlink_module.os,
            "replace",
            lambda *args: (_ for _ in ()).throw(OSError("replace failed")),
        )

        def fail_backup_cleanup(path):
            if ".backup" in Path(path).name:
                raise OSError("cleanup failed")
            return real_unlink(path)

        monkeypatch.setattr(symlink_module.os, "unlink", fail_backup_cleanup)
        report = run([resource], "update")
        result = report.results[0]
        assert not report.ok
        assert result.after is State.DRIFTED
        assert result.backup is not None
        assert Path(result.backup).read_text() == "precious"
        assert "rollback failed: cleanup failed" in result.rollback
        encoded = report.to_dict()["results"][0]
        assert encoded["backup"] == result.backup
        assert encoded["rollback"] == result.rollback

    def test_symlink_creation_failure_preserves_existing_target(
        self, tmp_path, monkeypatch
    ):
        _, target, resource = self.resource(tmp_path)
        target.parent.mkdir(parents=True)
        target.write_text("precious")
        error = OSError("privilege not held")
        error.winerror = 1314
        monkeypatch.setattr(symlink_module.os, "symlink",
                            lambda *args, **kwargs: (_ for _ in ()).throw(error))
        report = run([resource], "update")
        assert not report.ok
        assert "privilege not held" in report.results[0].detail
        assert target.read_text() == "precious"

    def test_windows_canonical_comparison_is_case_insensitive(
        self, tmp_path, monkeypatch
    ):
        source, target, resource = self.resource(tmp_path)
        target.parent.mkdir(parents=True)
        target.symlink_to(source)
        real_realpath = symlink_module.os.path.realpath

        def different_case(value):
            resolved = real_realpath(value)
            return resolved.upper() if Path(value) == target else resolved.lower()

        monkeypatch.setattr(symlink_module, "_is_windows", lambda: True)
        monkeypatch.setattr(symlink_module.os.path, "realpath", different_case)
        assert resource.inspect().state is State.CURRENT

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            (r"C:\\Users\\Name\\File", r"c:/users/name/file"),
            (r"\\SERVER\\Share\\Dir\\File", r"\\server\\share\\dir\\file"),
        ],
    )
    def test_windows_drive_and_unc_canonical_spellings(
        self, tmp_path, monkeypatch, left, right
    ):
        values = iter([left, right])
        monkeypatch.setattr(symlink_module, "_is_windows", lambda: True)
        monkeypatch.setattr(symlink_module.os.path, "realpath",
                            lambda value: next(values))
        assert symlink_module._canonical(Path("left")) == (
            symlink_module._canonical(Path("right")))

    def test_directory_source_sets_windows_directory_flag(
        self, tmp_path, monkeypatch
    ):
        source = tmp_path / "source directory"
        target = tmp_path / "target directory link"
        source.mkdir()
        real_symlink = os.symlink
        flags = []

        def recording_symlink(source_path, target_path, *, target_is_directory):
            flags.append(target_is_directory)
            return real_symlink(source_path, target_path,
                                target_is_directory=target_is_directory)

        monkeypatch.setattr(symlink_module.os, "symlink", recording_symlink)
        assert run([Symlink(source, target, "directory")], "install").ok
        assert flags == [True]

    def test_windows_replace_failure_reports_actual_state_and_rollback(
        self, tmp_path, monkeypatch
    ):
        _, target, resource = self.resource(tmp_path)
        target.parent.mkdir(parents=True)
        target.write_text("precious")
        error = PermissionError("windows sharing violation")
        error.winerror = 32
        monkeypatch.setattr(symlink_module.os, "replace",
                            lambda *args: (_ for _ in ()).throw(error))
        report = run([resource], "update")
        result = report.results[0]
        assert not report.ok
        assert result.after is State.DRIFTED
        assert "windows sharing violation" in result.detail
        assert "original target remained" in result.rollback
        assert target.read_text() == "precious"
