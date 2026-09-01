"""Tests for the documented jobs YAML shape."""

from __future__ import annotations

from pathlib import Path

import pytest

from job_kit.model import Acceptance, load_job_file


def test_load_job_file_resolves_relative_job_paths(tmp_path: Path) -> None:
    """The YAML loader mirrors Job fields and anchors paths at the file."""
    jobs_path = tmp_path / "jobs.yaml"
    jobs_path.write_text(
        """jobs:
  - id: lint
    prompt:
      system: instructions
      user: fix lint
    endpoint_preference: [fake]
    requirements: [cwd]
    directory: workspace
    contract:
      command: [true]
""",
        encoding="utf-8",
    )

    job_file = load_job_file(jobs_path)

    assert len(job_file.jobs) == 1
    job = job_file.jobs[0]
    assert job.id == "lint"
    assert job.prompt.user == "fix lint"
    assert job.requirements == {"params": ["cwd"]}
    assert job.directory == (tmp_path / "workspace").resolve()
    assert job.contract.directory is None


def test_acceptance_outcome_is_validated_and_accepted_comes_from_exit_code(
    tmp_path: Path,
) -> None:
    """Acceptance serializes the three outcomes and never trusts a summary flag."""
    result = Acceptance(
        command=("true",),
        directory=tmp_path,
        exit_code=None,
        stdout="",
        stderr="launch failed",
        wall_ms=1,
        accepted=True,
        outcome="not_run",
    )

    assert result.accepted is False
    assert result.to_mapping()["outcome"] == "not_run"
    with pytest.raises(ValueError, match="acceptance outcome"):
        Acceptance(
            command=("true",),
            directory=tmp_path,
            exit_code=0,
            stdout="",
            stderr="",
            wall_ms=1,
            accepted=True,
            outcome="unknown",
        )
