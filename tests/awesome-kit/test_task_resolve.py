"""Tests for ``task_system.resolve`` path normalization and containment."""

from pathlib import Path

import pytest

from task_system import resolve


class TestSymlinkedRootAbsolutePath:
    """Bug: ``_normalize_parts`` resolved BOTH the input and the project
    root with ``Path.resolve()`` before comparing, which follows a symlinked
    ``dev/tasks`` to its link target -- outside the (unresolved) project
    root -- and raised "path is outside the project root". ``init`` prints
    exactly this absolute form (``(project_root / canonical).absolute()``,
    no symlink resolution) as its success output, so feeding that output
    back into ``show``/``validate`` failed under the standard symlinked
    ``dev/tasks`` topology."""

    def test_absolute_path_under_symlinked_root_resolves(self, symlinked_root):
        project_root, _link_target = symlinked_root
        (project_root / "dev" / "tasks" / "durable").mkdir(parents=True)
        printed = (project_root / "dev/tasks/durable").absolute()

        resolved = resolve.resolve_path(str(printed), project_root)

        assert resolved.canonical == "dev/tasks/durable"

    def test_genuine_outside_path_still_rejected(self, symlinked_root, tmp_path):
        project_root, _link_target = symlinked_root
        outside = tmp_path / "elsewhere" / "dev" / "tasks" / "durable"
        outside.parent.mkdir(parents=True)

        with pytest.raises(resolve.RefResolutionError, match="outside the project root"):
            resolve.resolve_path(str(outside), project_root)


class TestDotDotSegmentsInAnAbsolutePath:
    """`..` must normalize the same way whether the path is absolute or not.

    The lexical-containment shortcut that lets an absolute path under a
    symlinked root resolve must not hand back `..` segments uncollapsed:
    `_classify_parts` would accept a stub of `..`.
    """

    def test_absolute_path_with_dotdot_collapses_to_the_project_root(
        self, tmp_path: Path
    ) -> None:
        project_root = tmp_path / "project"
        (project_root / "dev" / "tasks").mkdir(parents=True)
        with pytest.raises(resolve.RefResolutionError):
            resolve.resolve_path(str(project_root / "dev" / "tasks" / ".."), project_root)

    def test_absolute_path_with_dotdot_traversal_still_resolves(
        self, tmp_path: Path
    ) -> None:
        project_root = tmp_path / "project"
        (project_root / "dev" / "tasks").mkdir(parents=True)
        ref = resolve.resolve_path(
            str(project_root / "dev" / "tasks" / ".." / "tasks" / "alpha"), project_root
        )
        assert ref.canonical == "dev/tasks/alpha"
