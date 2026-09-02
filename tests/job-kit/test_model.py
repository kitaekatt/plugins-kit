"""Tests for the documented jobs YAML shape."""

from __future__ import annotations

from pathlib import Path

import pytest

from job_kit.model import (
    Acceptance,
    Contract,
    Job,
    Prompt,
    WorkspaceSpec,
    load_job_file,
)


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


def test_workspace_isolate_option_defaults_true_and_round_trips_false(
    tmp_path: Path,
) -> None:
    """The job-file workspace opt-out is a durable boolean setting."""
    default = WorkspaceSpec.from_value({"directory": "."}, base_dir=tmp_path)
    declined = WorkspaceSpec.from_value(
        {"directory": ".", "isolate": False}, base_dir=tmp_path
    )

    assert default is not None
    assert default.isolate is True
    assert declined is not None
    assert declined.isolate is False
    assert declined.to_mapping()["isolate"] is False
    with pytest.raises(ValueError, match="workspace isolate"):
        WorkspaceSpec.from_value({"isolate": "false"})


def test_job_options_and_run_floor_load_and_round_trip(tmp_path: Path) -> None:
    """The job options and run deny floor are durable YAML fields."""
    jobs_path = tmp_path / "jobs.yaml"
    jobs_path.write_text(
        """disallowed_tools: Bash
jobs:
  - id: options
    prompt: run
    endpoint_preference: [fake]
    options:
      allowed_tools: Read
      disallowed_tools: Edit
      system_prompt_mode: append
      max_tokens: 40
      temperature: 0
      extras:
        sandbox: read-only
    contract:
      command: [true]
""",
        encoding="utf-8",
    )

    job_file = load_job_file(jobs_path)

    assert job_file.disallowed_tools == "Bash"
    assert job_file.jobs[0].options == {
        "allowed_tools": "Read",
        "disallowed_tools": "Edit",
        "system_prompt_mode": "append",
        "max_tokens": 40,
        "temperature": 0,
        "extras": {"sandbox": "read-only"},
    }
    assert job_file.to_mapping()["disallowed_tools"] == "Bash"
    assert job_file.jobs[0].to_mapping()["options"] == job_file.jobs[0].options


def test_job_options_reject_unknown_keys(tmp_path: Path) -> None:
    """An option outside the four-key allow-list fails at job construction."""
    with pytest.raises(ValueError, match="unknown keys"):
        Job(
            id="invalid-options",
            prompt=Prompt(user="run"),
            endpoint_preference=("fake",),
            directory=tmp_path,
            contract=Contract(command=("true",), directory=tmp_path),
            options={"unexpected": True},
        )


@pytest.mark.parametrize(
    "options",
    [
        {"max_tokens": 0},
        {"max_tokens": True},
        {"max_tokens": 2.5},
        {"max_tokens": None},
        {"temperature": -0.1},
        {"temperature": 2.1},
        {"temperature": True},
        {"temperature": None},
    ],
)
def test_job_options_reject_invalid_completion_controls(
    tmp_path: Path, options: dict[str, object]
) -> None:
    """Completion controls enforce the seam's supported value ranges."""
    with pytest.raises(ValueError, match="max_tokens|temperature"):
        Job(
            id="invalid-completion-options",
            prompt=Prompt(user="run"),
            endpoint_preference=("fake",),
            directory=tmp_path,
            contract=Contract(command=("true",), directory=tmp_path),
            options=options,
        )


def test_job_max_attempts_accepts_positive_retry_budgets(tmp_path: Path) -> None:
    """A job can opt into multiple recorded attempts while zero is invalid."""
    job = Job(
        id="retryable",
        prompt=Prompt(user="run"),
        endpoint_preference=("fake",),
        directory=tmp_path,
        contract=Contract(command=("true",), directory=tmp_path),
        max_attempts=3,
    )

    assert job.max_attempts == 3
    with pytest.raises(ValueError, match="positive integer"):
        Job(
            id="invalid-retries",
            prompt=Prompt(user="run"),
            endpoint_preference=("fake",),
            directory=tmp_path,
            contract=Contract(command=("true",), directory=tmp_path),
            max_attempts=0,
        )


@pytest.mark.parametrize("max_attempts", [True, 2.5, "2"])
def test_job_mapping_rejects_non_integer_retry_budgets(
    tmp_path: Path, max_attempts: object
) -> None:
    """The YAML shape does not coerce non-integer retry budgets."""
    with pytest.raises(ValueError, match="positive integer"):
        Job.from_mapping(
            {
                "id": "invalid-mapping-retries",
                "prompt": "run",
                "endpoint_preference": ["fake"],
                "directory": str(tmp_path),
                "contract": {"command": ["true"]},
                "max_attempts": max_attempts,
            }
        )


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
