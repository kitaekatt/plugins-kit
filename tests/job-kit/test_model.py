"""Tests for the documented jobs YAML shape."""

from __future__ import annotations

from pathlib import Path

import pytest

import job_kit.workspace as workspace_module
import job_kit.model as model_module
from job_kit import SharedLibTooOldError
from job_kit.model import (
    Acceptance,
    Contract,
    Job,
    Prompt,
    WorkspaceSpec,
    load_job_file,
    validate_max_parallel,
)


def test_workspace_statuses_have_one_definition_and_shared_error_is_public() -> None:
    """The package shares workspace status vocabulary and exports its error."""
    assert model_module.WORKSPACE_STATUSES is workspace_module.WORKSPACE_STATUSES
    assert SharedLibTooOldError.__name__ == "SharedLibTooOldError"


def test_windows_scalar_contract_command_preserves_backslashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows command parsing is tested through its platform-specific path."""
    monkeypatch.setattr(model_module.os, "name", "nt")

    contract = Contract.from_mapping(
        {"command": r"C:\Tools\verify.exe --check"}
    )

    assert contract.command == (r"C:\Tools\verify.exe", "--check")


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


def test_workspace_isolate_option_defaults_false_and_round_trips_true(
    tmp_path: Path,
) -> None:
    """The job-file workspace opt-in is a durable boolean setting."""
    default = WorkspaceSpec.from_value({"directory": "."}, base_dir=tmp_path)
    requested = WorkspaceSpec.from_value(
        {"directory": ".", "isolate": True}, base_dir=tmp_path
    )

    assert default is not None
    assert default.isolate is False
    assert requested is not None
    assert requested.isolate is True
    assert requested.to_mapping()["isolate"] is True
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
            models=("fake",),
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
            models=("fake",),
            directory=tmp_path,
            contract=Contract(command=("true",), directory=tmp_path),
            options=options,
        )


def test_job_max_attempts_accepts_positive_retry_budgets(tmp_path: Path) -> None:
    """A job can opt into multiple recorded attempts while zero is invalid."""
    job = Job(
        id="retryable",
        prompt=Prompt(user="run"),
        models=("fake",),
        directory=tmp_path,
        contract=Contract(command=("true",), directory=tmp_path),
        max_attempts=3,
    )

    assert job.max_attempts == 3
    with pytest.raises(ValueError, match="positive integer"):
        Job(
            id="invalid-retries",
            prompt=Prompt(user="run"),
            models=("fake",),
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


def test_job_file_accepts_a_positive_max_parallel_and_defaults_to_one(
    tmp_path: Path,
) -> None:
    """A worker-pool bound above one is loaded; the default stays sequential."""
    document = """jobs:
  - id: lint
    prompt:
      user: fix lint
    endpoint_preference: [fake]
    contract:
      command: [true]
"""
    sequential = tmp_path / "sequential.yaml"
    sequential.write_text(document, encoding="utf-8")
    parallel = tmp_path / "parallel.yaml"
    parallel.write_text(f"max_parallel: 4\n{document}", encoding="utf-8")

    assert load_job_file(sequential).max_parallel == 1
    assert load_job_file(parallel).max_parallel == 4


@pytest.mark.parametrize(
    "value", ["0", "-1", "true", "1.5", "'3'", "null"]
)
def test_job_file_rejects_a_non_positive_integer_max_parallel(
    tmp_path: Path, value: str
) -> None:
    """Zero, negatives, booleans, floats, strings and null are refused."""
    jobs_path = tmp_path / "jobs.yaml"
    jobs_path.write_text(
        f"""max_parallel: {value}
jobs:
  - id: lint
    prompt:
      user: fix lint
    endpoint_preference: [fake]
    contract:
      command: [true]
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="max_parallel must be a positive integer"):
        load_job_file(jobs_path)


@pytest.mark.parametrize("value", [0, -3, True, False, 1.0, "2", None])
def test_validate_max_parallel_rejects_every_non_positive_integer(
    value: object,
) -> None:
    """The shared validator is the single gate both the file and store use."""
    with pytest.raises(ValueError, match="max_parallel must be a positive integer"):
        validate_max_parallel(value)


def test_validate_max_parallel_accepts_positive_integers() -> None:
    """A positive integer is returned unchanged."""
    assert validate_max_parallel(1) == 1
    assert validate_max_parallel(12) == 12


# ---------------------------------------------------------------------------
# The job's model declaration: `models:`, with the old keys still accepted
# ---------------------------------------------------------------------------

from bootstrap_lib.model_declaration import DeclarationError


def _job_mapping(**declaration: object) -> dict[str, object]:
    return {
        "id": "declared",
        "prompt": {"user": "hi"},
        "contract": {"command": ["true"]},
        **declaration,
    }


def test_models_is_the_declaration_key_and_round_trips(tmp_path: Path) -> None:
    """`models:` is read as a list of ids and written back under `models`."""
    job = Job.from_mapping(_job_mapping(models=["sonnet", "luna"]), base_dir=tmp_path)

    assert job.models == ("sonnet", "luna")
    mapping = job.to_mapping()
    assert mapping["models"] == ["sonnet", "luna"]
    assert "endpoint_preference" not in mapping
    assert Job.from_mapping(mapping).models == ("sonnet", "luna")


def test_a_scalar_models_value_is_a_one_element_declaration(tmp_path: Path) -> None:
    job = Job.from_mapping(_job_mapping(models="sonnet"), base_dir=tmp_path)

    assert job.models == ("sonnet",)


@pytest.mark.parametrize(
    "key", ["endpoint_preference", "endpoint_preferences", "endpoints", "endpoint"]
)
def test_the_old_declaration_keys_are_still_accepted(tmp_path: Path, key: str) -> None:
    """Old job files keep loading, with the same meaning, until they are retired."""
    job = Job.from_mapping(_job_mapping(**{key: ["sonnet", "luna"]}), base_dir=tmp_path)

    assert job.models == ("sonnet", "luna")
    assert job.endpoint_preference == ("sonnet", "luna")


def test_a_ledger_row_written_under_the_old_key_still_loads() -> None:
    """A definition persisted by an earlier job-kit carries endpoint_preference."""
    job = Job.from_mapping(
        {
            "id": "old",
            "prompt": {"system": "", "user": "hi"},
            "endpoint_preference": ["fake"],
            "requirements": {},
            "contract": {"command": ["true"]},
            "max_attempts": 1,
            "options": {},
        }
    )

    assert job.models == ("fake",)


def test_a_duplicate_id_is_a_declaration_error(tmp_path: Path) -> None:
    """The shared structural validator decides the shape, including duplicates."""
    with pytest.raises(DeclarationError):
        Job.from_mapping(_job_mapping(models=["sonnet", "sonnet"]), base_dir=tmp_path)


def test_an_empty_declaration_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        Job.from_mapping(_job_mapping(models=[]), base_dir=tmp_path)


def test_absent_and_too_old_bootstrap_lib_are_diagnosed_apart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent means install/provision; present-but-old means update."""
    import sys
    import types

    monkeypatch.setitem(sys.modules, "bootstrap_lib", None)
    with pytest.raises(ImportError) as absent:
        Job.from_mapping(_job_mapping(models=["sonnet"]), base_dir=tmp_path)
    assert "not linked" in str(absent.value)
    assert "claude plugin install bootstrap@plugins-kit" in str(absent.value)

    old = types.ModuleType("bootstrap_lib")
    old.__path__ = []
    monkeypatch.setitem(sys.modules, "bootstrap_lib", old)
    monkeypatch.setitem(sys.modules, "bootstrap_lib.model_declaration", None)
    with pytest.raises(ImportError) as too_old:
        Job.from_mapping(_job_mapping(models=["sonnet"]), base_dir=tmp_path)
    assert ">= 0.129.0" in str(too_old.value)
    assert "claude plugin update bootstrap@plugins-kit" in str(too_old.value)
