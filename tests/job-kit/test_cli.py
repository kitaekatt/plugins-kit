"""Tests for the status CLI surface without a model transport."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

import pytest

from llm_scripting_kit.completion import BackendSelection, Capabilities, LLMResponse

from job_kit import cli
from job_kit.model import (
    Contract,
    Job,
    JobRecord,
    JobState,
    Prompt,
    RunRecord,
    RunSnapshot,
    RunState,
    load_job_file,
)
import job_kit.run as run_module
from job_kit.store import JobStore


def test_status_reads_a_run_from_an_explicit_store(
    tmp_path: Path, capsys: Any
) -> None:
    """The status verb emits the durable run snapshot as JSON."""
    store_path = tmp_path / "status.sqlite3"
    job = Job(
        id="status-job",
        prompt=Prompt(user="status"),
        endpoint_preference=("fake",),
        directory=tmp_path,
        contract=Contract(command=("true",), directory=tmp_path),
    )
    JobStore(store_path).create_run("status-run", [job])

    assert cli.main(["status", "status-run", "--store", str(store_path)]) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["run"]["id"] == "status-run"
    assert payload["jobs"][0]["id"] == "status-job"
    assert payload["store"] == str(store_path.resolve())


def test_status_does_not_create_a_missing_store(tmp_path: Path, capsys: Any) -> None:
    """The read-only status verb reports a missing store without creating it."""
    store_path = tmp_path / "missing" / "status.sqlite3"

    assert cli.main(["status", "run-1", "--store", str(store_path)]) == cli.EXIT_RUNNER_FAILURE
    captured = capsys.readouterr()
    assert f"store does not exist: {store_path.resolve()}" in captured.err
    assert "unknown run" not in captured.err.lower()
    assert not store_path.exists()
    assert not store_path.parent.exists()


def test_package_reports_missing_bootstrap_before_cli_import(tmp_path: Path) -> None:
    """A bare package import gets the canonical provisioning message."""
    repo_root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(tmp_path / "home"),
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": os.pathsep.join(
                (
                    str(repo_root / "plugins" / "job-kit" / "lib"),
                    str(repo_root / "plugins" / "llm-scripting-kit" / "lib"),
                )
            ),
            "_BOOTSTRAP_GUARD_VENV_REEXEC": "1",
        }
    )
    result = subprocess.run(
        [sys.executable, "-S", "-c", "import job_kit"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
    )

    assert result.returncode == 3
    assert "the 'plugins-kit:bootstrap' plugin has not provisioned" in result.stderr
    assert "missing: bootstrap_lib" in result.stderr
    assert "No module named" not in result.stderr


def test_importing_package_does_not_reexec_under_an_arbitrary_interpreter(
    tmp_path: Path,
) -> None:
    """Importing the library never invokes the CLI interpreter guard."""
    repo_root = Path(__file__).resolve().parents[2]
    marker = tmp_path / "reexec-called"
    (tmp_path / "bootstrap_guard.py").write_text(
        "from pathlib import Path\n"
        "def reexec_under_plugin_venv(plugin: str) -> None:\n"
        f"    Path({str(marker)!r}).write_text('called', encoding='utf-8')\n"
        "    raise SystemExit(99)\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.pop("_BOOTSTRAP_GUARD_VENV_REEXEC", None)
    environment.pop("CLAUDE_BOOTSTRAP_DATA_ROOT", None)
    environment.update(
        {
            "HOME": str(tmp_path / "home"),
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": os.pathsep.join(
                (
                    str(tmp_path),
                    str(repo_root / "plugins" / "job-kit" / "lib"),
                    str(repo_root / "plugins" / "llm-scripting-kit" / "lib"),
                    str(repo_root / "plugins" / "bootstrap"),
                )
            ),
        }
    )
    result = subprocess.run(
        [sys.executable, "-S", "-c", "import job_kit; print('imported')"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "imported"
    assert not marker.exists()


def test_python_m_entrypoint_still_runs_the_cli(tmp_path: Path) -> None:
    """The shim remains usable through Python's module launcher."""
    repo_root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(tmp_path / "home"),
            "PYTHONNOUSERSITE": "1",
            "_BOOTSTRAP_GUARD_VENV_REEXEC": "1",
            "PYTHONPATH": os.pathsep.join(
                (
                    str(repo_root / "plugins" / "job-kit" / "lib"),
                    str(repo_root / "plugins" / "llm-scripting-kit" / "lib"),
                    str(repo_root / "plugins" / "bootstrap"),
                )
            ),
        }
    )
    result = subprocess.run(
        [sys.executable, "-S", "-m", "job_kit_entrypoint", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
    )

    assert result.returncode == 0
    assert "usage: job-kit" in result.stdout


def test_entrypoint_preserves_explicit_arguments_for_reexec(monkeypatch: Any) -> None:
    """The shim carries explicit arguments into a replacement interpreter."""
    import job_kit.cli
    import job_kit_entrypoint

    observed: list[tuple[str, ...]] = []
    cli_arguments: list[Sequence[str] | None] = []

    def fake_reexec(plugin: str) -> None:
        observed.append(tuple(sys.argv))

    def fake_cli_main(argv: Sequence[str] | None = None) -> int:
        cli_arguments.append(argv)
        return 0

    monkeypatch.setattr(sys, "argv", ["caller", "unrelated"])
    monkeypatch.setattr(job_kit_entrypoint, "reexec_under_plugin_venv", fake_reexec)
    monkeypatch.setattr(job_kit.cli, "main", fake_cli_main)

    assert job_kit_entrypoint.main(["status", "run-id"]) == 0
    assert observed == [(str(Path(job_kit_entrypoint.__file__).resolve()), "status", "run-id")]
    assert cli_arguments == [["status", "run-id"]]
    assert sys.argv == ["caller", "unrelated"]


def test_package_reraises_deep_import_error_when_provisioned(
    tmp_path: Path,
) -> None:
    """A provisioned install does not misreport a deep shared-lib defect."""
    repo_root = Path(__file__).resolve().parents[2]
    fake_root = tmp_path / "fake-libs"
    fake_root.mkdir()
    (fake_root / "bootstrap_lib.py").write_text("", encoding="utf-8")
    fake_llm = fake_root / "llm_scripting_kit"
    fake_llm.mkdir()
    (fake_llm / "__init__.py").write_text("", encoding="utf-8")
    (fake_llm / "completion.py").write_text(
        "from nonexistent_third_party import missing_symbol\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    log = home / ".claude" / "plugins" / "data" / "plugins-kit" / "job-kit"
    log.mkdir(parents=True)
    (log / "bootstrap.log").write_text("provisioned\n", encoding="utf-8")
    environment = os.environ.copy()
    environment.pop("_BOOTSTRAP_GUARD_VENV_REEXEC", None)
    environment.pop("CLAUDE_BOOTSTRAP_DATA_ROOT", None)
    environment.update(
        {
            "HOME": str(home),
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": os.pathsep.join(
                (
                    str(fake_root),
                    str(repo_root / "plugins" / "job-kit" / "lib"),
                )
            ),
        }
    )
    result = subprocess.run(
        [sys.executable, "-S", "-c", "import job_kit"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        check=False,
    )

    assert result.returncode not in {0, 3}
    assert "Traceback (most recent call last)" in result.stderr
    assert "nonexistent_third_party" in result.stderr
    assert "the 'plugins-kit:bootstrap' plugin has not provisioned" not in result.stderr


def _accepted_snapshot(tmp_path: Path) -> RunSnapshot:
    """Build a small accepted snapshot for CLI transport tests."""
    job = Job(
        id="cli-job",
        prompt=Prompt(user="run"),
        endpoint_preference=("fake",),
        directory=tmp_path,
        contract=Contract(command=("true",), directory=tmp_path),
    )
    record = JobRecord(job=job, state=JobState.ACCEPTED, created_at=0.0, updated_at=0.0)
    run = RunRecord(
        id="cli-run",
        created_at=0.0,
        jobs_path=None,
        max_parallel=1,
        workspace_root=None,
        status=RunState.COMPLETED,
    )
    return RunSnapshot(run=run, jobs=(record,), attempts=())


def test_run_and_resume_cli_delegate_to_runner(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    """The two mutating CLI verbs pass through without a live backend."""
    snapshot = _accepted_snapshot(tmp_path)
    store_path = tmp_path / "cli.sqlite3"
    calls: list[tuple[str, Path]] = []

    def fake_run(
        jobs_path: Path,
        *,
        store_path: Path,
        timeout_s: float,
        run_id: str | None,
        max_parallel: int | None = None,
    ) -> RunSnapshot:
        calls.append(("run", store_path))
        return snapshot

    def fake_resume(
        run_id: str, store: Path, *, timeout_s: float, max_parallel: int | None = None
    ) -> RunSnapshot:
        calls.append(("resume", store))
        return snapshot

    monkeypatch.setattr(cli, "run_job_file", fake_run)
    monkeypatch.setattr(cli, "resume_run", fake_resume)

    assert cli.main(["run", "jobs.yaml", "--store", str(store_path)]) == cli.EXIT_OK
    assert cli.main(["resume", "cli-run", "--store", str(store_path)]) == cli.EXIT_OK
    capsys.readouterr()
    assert calls == [("run", store_path.resolve()), ("resume", store_path.resolve())]


def test_cli_exit_codes_distinguish_job_outcome_from_runner_failure(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    """Accepted, not-accepted, and runner-error results have distinct codes."""
    accepted = _accepted_snapshot(tmp_path)
    rejected = replace(
        accepted,
        jobs=(replace(accepted.jobs[0], state=JobState.REJECTED),),
    )
    current = accepted
    store_path = tmp_path / "exit-codes.sqlite3"

    def fake_run(
        jobs_path: Path,
        *,
        store_path: Path,
        timeout_s: float,
        run_id: str | None,
        max_parallel: int | None = None,
    ) -> RunSnapshot:
        return current

    monkeypatch.setattr(cli, "run_job_file", fake_run)
    assert cli.main(["run", "jobs.yaml", "--store", str(store_path)]) == cli.EXIT_OK
    capsys.readouterr()

    current = rejected
    assert cli.main(["run", "jobs.yaml", "--store", str(store_path)]) == cli.EXIT_FAILURE
    capsys.readouterr()

    def broken_run(
        jobs_path: Path,
        *,
        store_path: Path,
        timeout_s: float,
        run_id: str | None,
        max_parallel: int | None = None,
    ) -> RunSnapshot:
        raise RuntimeError("runner boom")

    monkeypatch.setattr(cli, "run_job_file", broken_run)
    assert cli.main(["run", "jobs.yaml", "--store", str(store_path)]) == cli.EXIT_RUNNER_FAILURE
    assert "runner boom" in capsys.readouterr().err


def test_run_cli_preassigns_a_validated_run_id(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    """--run-id reaches the runner verbatim, and a malformed id is refused."""
    snapshot = _accepted_snapshot(tmp_path)
    seen: list[str | None] = []

    def fake_run(
        jobs_path: Path,
        *,
        store_path: Path,
        timeout_s: float,
        run_id: str | None,
        max_parallel: int | None = None,
    ) -> RunSnapshot:
        seen.append(run_id)
        return snapshot

    monkeypatch.setattr(cli, "run_job_file", fake_run)
    store = str(tmp_path / "cli.sqlite3")
    assert cli.main(["run", "jobs.yaml", "--store", store, "--run-id", "refresh-2026.09_01-a1"]) == cli.EXIT_OK
    assert cli.main(["run", "jobs.yaml", "--store", store]) == cli.EXIT_OK
    capsys.readouterr()
    assert seen == ["refresh-2026.09_01-a1", None]

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["run", "jobs.yaml", "--store", store, "--run-id", "bad id/with slash"])
    assert exit_info.value.code == cli.EXIT_USAGE
    assert "run id must be" in capsys.readouterr().err


class _CliBackend:
    """A hermetic backend so CLI verbs can run without a model transport."""

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(
        self, system: str, user: str, *, model: str, options: Any = None
    ) -> LLMResponse:
        """Return one fixed completion and record the job it served."""
        self.calls.append(user)
        return LLMResponse(text="answer", model="fake-model")

    def classify_halt(self, exc: BaseException) -> None:
        """Never classify a halt in CLI transport tests."""
        return None


def _install_fake_transport(monkeypatch: Any) -> _CliBackend:
    """Point the runner's default selection at a hermetic backend."""
    backend = _CliBackend()
    monkeypatch.setattr(
        run_module, "adapter_capabilities", lambda: {"fake": Capabilities(adapter="fake")}
    )
    monkeypatch.setattr(
        run_module,
        "create_backend",
        lambda endpoint, **_: BackendSelection(endpoint, "fake", backend, "fake-model"),
    )
    return backend


def _jobs_file(tmp_path: Path, job_ids: Sequence[str]) -> Path:
    """Write a jobs file whose contracts accept without a model transport."""
    entries = "".join(
        f"""  - id: {job_id}
    prompt:
      user: {job_id}
    endpoint_preference: [fake-endpoint]
    directory: {tmp_path}
    contract:
      command: [{sys.executable}, -c, "pass"]
      directory: {tmp_path}
"""
        for job_id in job_ids
    )
    jobs_path = tmp_path / "jobs.yaml"
    jobs_path.write_text(
        f"max_parallel: 1\nworkspace_root: {tmp_path / 'ws'}\njobs:\n{entries}",
        encoding="utf-8",
    )
    return jobs_path


def test_run_cli_max_parallel_overrides_the_file_and_is_persisted(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    """The run override wins over the file and becomes the ledger's record."""
    _install_fake_transport(monkeypatch)
    jobs_path = _jobs_file(tmp_path, ["first", "second"])
    store_path = tmp_path / "override.sqlite3"

    exit_code = cli.main(
        [
            "run",
            str(jobs_path),
            "--store",
            str(store_path),
            "--run-id",
            "cli-parallel",
            "--max-parallel",
            "2",
        ]
    )
    capsys.readouterr()

    assert exit_code == cli.EXIT_OK
    store = JobStore(store_path, create=False)
    assert store.get_run("cli-parallel").max_parallel == 2


def test_resume_cli_max_parallel_does_not_rewrite_the_ledger(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    """The resume override applies to one pass and leaves the record alone."""
    _install_fake_transport(monkeypatch)
    jobs_path = _jobs_file(tmp_path, ["first", "second"])
    store_path = tmp_path / "resume.sqlite3"
    job_file = load_job_file(jobs_path)
    store = JobStore(store_path)
    store.create_run(
        "cli-resume",
        job_file.jobs,
        jobs_path=jobs_path,
        max_parallel=1,
        workspace_root=tmp_path / "ws",
    )

    exit_code = cli.main(
        [
            "resume",
            "cli-resume",
            "--store",
            str(store_path),
            "--max-parallel",
            "2",
        ]
    )
    capsys.readouterr()

    assert exit_code == cli.EXIT_OK
    assert store.get_run("cli-resume").max_parallel == 1
    assert all(job.state is JobState.ACCEPTED for job in store.list_jobs("cli-resume"))


@pytest.mark.parametrize("value", ["0", "-1", "two", "1.5"])
def test_cli_rejects_a_non_positive_max_parallel(value: str) -> None:
    """Argparse refuses a bound that is not a positive integer."""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["run", "jobs.yaml", "--max-parallel", value])
    assert excinfo.value.code == cli.EXIT_USAGE
