"""Records and YAML shapes for the job-kit runner.

The package consumes a small YAML document with one flat list of jobs::

    jobs:
      - id: lint
        prompt:
          system: "You are a coding assistant."
          user: "Fix the lint errors."
        endpoint_preference: [local-codex]
        requirements:
          params: [cwd]
        directory: .
        contract:
          command: [python, -m, pytest, tests/lint]

The job's directory is the declared working directory. Git repositories use
that directory as the starting point for per-attempt isolation. A contract
accepts only when its command exits with code zero.

The optional ``workspace`` mapping accepts ``directory``, ``base_ref`` and
``isolate``. ``base_ref`` defaults to the repository HEAD captured at run
start. Isolation defaults to true; set ``isolate: false`` when a job must run
in its declared directory.

The optional job ``options`` mapping accepts ``allowed_tools``,
``disallowed_tools``, ``system_prompt_mode`` and ``extras``. The top-level
``disallowed_tools`` job-file key sets a deny floor for every job in the run.
The option defaults are ``None`` for both tool lists, ``"replace"`` for
``system_prompt_mode``, and an empty mapping for ``extras``. The floor defaults
to ``None`` and does not change the adapter default.

Usage is nullable because a transport can complete without exposing token
counts. Unknown usage is represented by ``None`` rather than zero.
"""

from __future__ import annotations

import math
import shlex
from dataclasses import dataclass, field
from numbers import Real
from enum import Enum
from pathlib import Path
from typing import Mapping, Optional, Sequence


PathLike = str | Path


_JOB_OPTION_KEYS = frozenset(
    {
        "allowed_tools",
        "disallowed_tools",
        "effort",
        "system_prompt_mode",
        "extras",
        "max_tokens",
        "temperature",
    }
)


def _normalize_job_options(value: object) -> dict[str, object]:
    """Validate and copy a job's completion options mapping."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("job options must be a mapping")

    unknown = [
        key for key in value if not isinstance(key, str) or key not in _JOB_OPTION_KEYS
    ]
    if unknown:
        raise ValueError(f"job options contain unknown keys: {unknown!r}")

    options = {str(key): option for key, option in value.items()}
    for name in ("allowed_tools", "disallowed_tools"):
        option = options.get(name)
        if option is not None and not isinstance(option, str):
            raise ValueError(f"job option {name} must be a string or null")

    if "system_prompt_mode" in options and not isinstance(
        options["system_prompt_mode"], str
    ):
        raise ValueError("job option system_prompt_mode must be a string")

    effort = options.get("effort")
    if effort is not None and not isinstance(effort, str):
        raise ValueError("job option effort must be a string or null")

    max_tokens = options.get("max_tokens")
    if "max_tokens" in options and (
        isinstance(max_tokens, bool)
        or not isinstance(max_tokens, int)
        or max_tokens < 1
    ):
        raise ValueError("job option max_tokens must be an integer >= 1")

    temperature = options.get("temperature")
    if "temperature" in options and (
        isinstance(temperature, bool)
        or not isinstance(temperature, Real)
        or not math.isfinite(float(temperature))
        or not 0 <= temperature <= 2
    ):
        raise ValueError("job option temperature must be a number in [0, 2]")

    extras = options.get("extras")
    if extras is not None and not isinstance(extras, Mapping):
        raise ValueError("job option extras must be a mapping")
    if extras is not None:
        options["extras"] = dict(extras)
    return options


class JobState(str, Enum):
    """State of one job in a durable run."""

    PENDING = "pending"
    RUNNING = "running"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    FAILED = "failed"
    HALTED = "halted"
    UNROUTABLE = "unroutable"


TERMINAL_STATES = frozenset(
    {
        JobState.ACCEPTED,
        JobState.REJECTED,
        JobState.FAILED,
        JobState.HALTED,
        JobState.UNROUTABLE,
    }
)


COMPLETED = "completed"
TIMEOUT = "timeout"
ERROR = "error"


class RunState(str, Enum):
    """Derived state of the set of jobs in a run."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"


@dataclass(frozen=True)
class Prompt:
    """The system and user messages sent to one completion."""

    system: str = ""
    user: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "system", "" if self.system is None else str(self.system))
        object.__setattr__(self, "user", "" if self.user is None else str(self.user))

    @classmethod
    def from_value(cls, value: object) -> "Prompt":
        """Build a prompt from the mapping or scalar YAML forms."""
        if isinstance(value, Mapping):
            system = value.get("system", value.get("system_prompt", ""))
            user = value.get("user", value.get("user_prompt", ""))
            return cls(system="" if system is None else str(system), user="" if user is None else str(user))
        if value is None:
            return cls()
        return cls(user=str(value))

    def to_mapping(self) -> dict[str, str]:
        """Return the YAML-compatible prompt mapping."""
        return {"system": self.system, "user": self.user}


def _resolve_directory(value: object, base_dir: Optional[Path]) -> Optional[Path]:
    """Resolve a declared directory without using string path operations."""
    if value is None or value == "":
        return None
    path = Path(str(value)).expanduser()
    if not path.is_absolute() and base_dir is not None:
        path = base_dir / path
    return path.resolve()


@dataclass(frozen=True)
class Contract:
    """A command-shaped acceptance check."""

    command: tuple[str, ...]
    directory: Optional[Path] = None

    def __post_init__(self) -> None:
        command: tuple[str, ...]
        if isinstance(self.command, str):
            command = tuple(shlex.split(self.command))
        else:
            command = tuple(str(part) for part in self.command)
        if not command:
            raise ValueError("contract command must contain at least one argument")
        object.__setattr__(self, "command", command)
        if self.directory is not None:
            object.__setattr__(self, "directory", Path(self.directory).expanduser().resolve())

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object], *, base_dir: Optional[Path] = None
    ) -> "Contract":
        """Build a contract from a YAML mapping."""
        command = value.get("command")
        if command is None:
            raise ValueError("contract requires command")
        if isinstance(command, str):
            command_value: tuple[str, ...] = tuple(shlex.split(command))
        elif isinstance(command, Sequence) and not isinstance(command, (bytes, bytearray)):
            command_value = tuple(str(part) for part in command)
        else:
            raise ValueError("contract command must be a string or a list")
        directory = _resolve_directory(value.get("directory", value.get("cwd")), base_dir)
        return cls(command=command_value, directory=directory)

    def to_mapping(self) -> dict[str, object]:
        """Return the YAML-compatible contract mapping."""
        result: dict[str, object] = {"command": list(self.command)}
        if self.directory is not None:
            result["directory"] = str(self.directory)
        return result


@dataclass(frozen=True)
class ContractContext:
    """Attempt metadata exported to a contract subprocess."""

    run_id: str
    job_id: str
    attempt_no: int
    endpoint: str
    backend: str
    model: str


@dataclass(frozen=True)
class WorkspaceSpec:
    """Declared workspace inputs used by the runner's isolation policy."""

    directory: Optional[Path] = None
    base_ref: Optional[str] = None
    isolate: bool = True

    def __post_init__(self) -> None:
        if self.directory is not None:
            object.__setattr__(self, "directory", Path(self.directory).expanduser().resolve())
        if self.base_ref is not None:
            base_ref = str(self.base_ref).strip()
            if not base_ref:
                raise ValueError("workspace base_ref must not be empty")
            object.__setattr__(self, "base_ref", base_ref)
        if not isinstance(self.isolate, bool):
            raise ValueError("workspace isolate must be a boolean")

    @classmethod
    def from_value(
        cls, value: object, *, base_dir: Optional[Path] = None
    ) -> Optional["WorkspaceSpec"]:
        """Build a workspace record from a YAML mapping or path."""
        if value is None:
            return None
        if isinstance(value, Mapping):
            directory = _resolve_directory(
                value.get("directory", value.get("path", value.get("cwd"))), base_dir
            )
            base_ref_value = value.get("base_ref")
            base_ref = (
                str(base_ref_value).strip()
                if base_ref_value is not None
                else None
            )
            isolate = value.get("isolate", True)
            if not isinstance(isolate, bool):
                raise ValueError("workspace isolate must be a boolean")
            return cls(directory=directory, base_ref=base_ref, isolate=isolate)
        return cls(directory=_resolve_directory(value, base_dir))

    def to_mapping(self) -> dict[str, object]:
        """Return the YAML-compatible workspace mapping."""
        result: dict[str, object] = {}
        if self.directory is not None:
            result["directory"] = str(self.directory)
        if self.base_ref is not None:
            result["base_ref"] = self.base_ref
        result["isolate"] = self.isolate
        return result


@dataclass(frozen=True)
class Job:
    """One heterogeneous job and its caller-supplied acceptance contract."""

    id: str
    prompt: Prompt
    endpoint_preference: tuple[str, ...]
    contract: Contract
    requirements: Mapping[str, object] = field(default_factory=dict)
    directory: Optional[Path] = None
    workspace: Optional[WorkspaceSpec] = None
    max_attempts: int = 1
    options: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        job_id = str(self.id).strip()
        if not job_id:
            raise ValueError("job id must not be empty")
        object.__setattr__(self, "id", job_id)

        if not isinstance(self.prompt, Prompt):
            object.__setattr__(self, "prompt", Prompt.from_value(self.prompt))
        if not isinstance(self.contract, Contract):
            if not isinstance(self.contract, Mapping):
                raise ValueError("job contract must be a Contract or mapping")
            object.__setattr__(self, "contract", Contract.from_mapping(self.contract))

        if isinstance(self.endpoint_preference, str):
            endpoint_values: Sequence[str] = (self.endpoint_preference,)
        else:
            endpoint_values = self.endpoint_preference
        endpoints = tuple(str(endpoint).strip() for endpoint in endpoint_values)
        endpoints = tuple(endpoint for endpoint in endpoints if endpoint)
        if not endpoints:
            raise ValueError(f"job {job_id!r} requires endpoint_preference")
        object.__setattr__(self, "endpoint_preference", endpoints)

        if self.requirements is None:
            object.__setattr__(self, "requirements", {})
        elif isinstance(self.requirements, Mapping):
            object.__setattr__(self, "requirements", dict(self.requirements))
        elif isinstance(self.requirements, Sequence) and not isinstance(
            self.requirements, (str, bytes, bytearray)
        ):
            object.__setattr__(self, "requirements", {"params": list(self.requirements)})
        else:
            raise ValueError("job requirements must be a mapping or list")

        object.__setattr__(self, "options", _normalize_job_options(self.options))

        if self.directory is not None:
            object.__setattr__(self, "directory", Path(self.directory).expanduser().resolve())
        if self.workspace is not None and not isinstance(self.workspace, WorkspaceSpec):
            object.__setattr__(
                self, "workspace", WorkspaceSpec.from_value(self.workspace)
            )
        if self.directory is None:
            effective_directory = (
                self.workspace.directory
                if self.workspace is not None and self.workspace.directory is not None
                else self.contract.directory
            )
            object.__setattr__(
                self,
                "directory",
                (effective_directory or Path.cwd()).expanduser().resolve(),
            )
        if (
            not isinstance(self.max_attempts, int)
            or isinstance(self.max_attempts, bool)
            or self.max_attempts < 1
        ):
            raise ValueError("job max_attempts must be a positive integer")

    @property
    def system(self) -> str:
        """The system prompt text."""
        return self.prompt.system

    @property
    def user(self) -> str:
        """The user prompt text."""
        return self.prompt.user

    @property
    def declared_directory(self) -> Path:
        """The directory in which the contract command runs."""
        if self.directory is not None:
            return self.directory
        if self.workspace is not None and self.workspace.directory is not None:
            return self.workspace.directory
        if self.contract.directory is not None:
            return self.contract.directory
        return Path.cwd().resolve()

    @classmethod
    def from_mapping(
        cls, value: Mapping[str, object], *, base_dir: Optional[Path] = None
    ) -> "Job":
        """Build one job from its minimal YAML mapping."""
        if "id" not in value:
            raise ValueError("job requires id")
        prompt_value: object = value.get("prompt")
        if prompt_value is None and ("system" in value or "user" in value):
            prompt_value = {
                "system": value.get("system", ""),
                "user": value.get("user", ""),
            }
        prompt = Prompt.from_value(prompt_value)

        endpoint_value: object = value.get(
            "endpoint_preference",
            value.get("endpoint_preferences", value.get("endpoints", value.get("endpoint"))),
        )
        if isinstance(endpoint_value, str):
            endpoints = (endpoint_value,)
        elif isinstance(endpoint_value, Sequence) and not isinstance(
            endpoint_value, (bytes, bytearray)
        ):
            endpoints = tuple(str(item) for item in endpoint_value)
        else:
            raise ValueError(f"job {value.get('id')!r} requires endpoint_preference")

        workspace = WorkspaceSpec.from_value(value.get("workspace"), base_dir=base_dir)
        directory = _resolve_directory(
            value.get("directory", value.get("cwd")), base_dir
        )
        if directory is None and workspace is not None:
            directory = workspace.directory

        raw_contract = value.get("contract")
        if raw_contract is None:
            raw_contract = {"command": value.get("command"), "directory": value.get("contract_directory")}
        if not isinstance(raw_contract, Mapping):
            raise ValueError(f"job {value.get('id')!r} contract must be a mapping")
        contract = Contract.from_mapping(raw_contract, base_dir=base_dir)
        if directory is None:
            directory = contract.directory

        requirements = value.get("requirements", {})
        raw_max_attempts = value.get("max_attempts", 1)
        if isinstance(raw_max_attempts, bool) or not isinstance(raw_max_attempts, int):
            raise ValueError("job max_attempts must be a positive integer")
        max_attempts = raw_max_attempts
        options = value.get("options", {})
        return cls(
            id=str(value["id"]),
            prompt=prompt,
            endpoint_preference=endpoints,
            contract=contract,
            requirements=requirements if requirements is not None else {},
            directory=directory,
            workspace=workspace,
            max_attempts=max_attempts,
            options=options if options is not None else {},
        )

    def to_mapping(self) -> dict[str, object]:
        """Return the durable job definition mapping."""
        result: dict[str, object] = {
            "id": self.id,
            "prompt": self.prompt.to_mapping(),
            "endpoint_preference": list(self.endpoint_preference),
            "requirements": dict(self.requirements),
            "contract": self.contract.to_mapping(),
            "max_attempts": self.max_attempts,
            "options": dict(self.options),
        }
        if self.directory is not None:
            result["directory"] = str(self.directory)
        if self.workspace is not None:
            result["workspace"] = self.workspace.to_mapping()
        return result


@dataclass(frozen=True)
class JobFile:
    """A loaded YAML document and run-level options."""

    jobs: tuple[Job, ...]
    max_parallel: int = 1
    workspace_root: Optional[Path] = None
    disallowed_tools: Optional[str] = None

    def __post_init__(self) -> None:
        if self.max_parallel != 1:
            raise ValueError("job-kit job-core supports max_parallel=1 only")
        ids = [job.id for job in self.jobs]
        if len(ids) != len(set(ids)):
            raise ValueError("job ids must be unique within a run")
        if self.workspace_root is not None:
            object.__setattr__(
                self, "workspace_root", Path(self.workspace_root).expanduser().resolve()
            )
        if self.disallowed_tools is not None and not isinstance(
            self.disallowed_tools, str
        ):
            raise ValueError("job-file disallowed_tools must be a string or null")

    def to_mapping(self) -> dict[str, object]:
        """Return the YAML-compatible job-file mapping."""
        return {
            "jobs": [job.to_mapping() for job in self.jobs],
            "max_parallel": self.max_parallel,
            "workspace_root": (
                str(self.workspace_root) if self.workspace_root is not None else None
            ),
            "disallowed_tools": self.disallowed_tools,
        }


def load_job_file(path: PathLike) -> JobFile:
    """Load and validate a jobs YAML document."""
    job_path = Path(path).expanduser().resolve()
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - bootstrap owns this dependency
        raise RuntimeError("PyYAML is required to load a jobs file") from exc
    try:
        raw = yaml.safe_load(job_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read jobs file {job_path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"malformed jobs YAML {job_path}: {exc}") from exc

    if isinstance(raw, list):
        document: Mapping[str, object] = {"jobs": raw}
    elif isinstance(raw, Mapping):
        document = raw
    else:
        raise ValueError("jobs YAML must be a mapping or a list")
    raw_jobs = document.get("jobs")
    if not isinstance(raw_jobs, Sequence) or isinstance(raw_jobs, (str, bytes, bytearray)):
        raise ValueError("jobs YAML requires a jobs list")
    base_dir = job_path.parent
    jobs = tuple(
        Job.from_mapping(item, base_dir=base_dir)
        for item in raw_jobs
        if isinstance(item, Mapping)
    )
    if len(jobs) != len(raw_jobs):
        raise ValueError("each jobs entry must be a mapping")
    workspace_root = _resolve_directory(document.get("workspace_root"), base_dir)
    return JobFile(
        jobs=jobs,
        max_parallel=int(document.get("max_parallel", 1)),
        workspace_root=workspace_root,
        disallowed_tools=document.get("disallowed_tools"),
    )


@dataclass(frozen=True)
class Usage:
    """Nullable usage copied from one completion response."""

    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cache_hit_tokens: Optional[int] = None
    total_tokens: Optional[int] = None

    @classmethod
    def from_response(cls, response: object) -> Optional["Usage"]:
        """Copy response usage, treating an all-zero default as unknown."""
        values = {
            name: getattr(response, name, None)
            for name in (
                "input_tokens",
                "output_tokens",
                "cache_hit_tokens",
                "total_tokens",
            )
        }
        if not any(value not in (None, 0) for value in values.values()):
            return None
        return cls(**{name: int(value) if value is not None else None for name, value in values.items()})

    def to_mapping(self) -> dict[str, Optional[int]]:
        """Return a JSON-compatible usage mapping."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True)
class AttemptError:
    """Machine code and human detail for a failed completion."""

    code: str
    message: str = ""

    def to_mapping(self) -> dict[str, str]:
        """Return a JSON-compatible error mapping."""
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class Acceptance:
    """Observed result of one contract subprocess.

    ``outcome`` distinguishes a command that ran from a timeout or a command
    that could not be launched at all.
    """

    command: tuple[str, ...]
    directory: Path
    exit_code: Optional[int]
    stdout: str
    stderr: str
    wall_ms: int
    accepted: bool
    outcome: str = "observed"

    def __post_init__(self) -> None:
        object.__setattr__(self, "command", tuple(str(part) for part in self.command))
        object.__setattr__(self, "directory", Path(self.directory).expanduser().resolve())
        if self.outcome not in {"observed", "timed_out", "not_run"}:
            raise ValueError(
                "acceptance outcome must be one of: observed, timed_out, not_run"
            )
        object.__setattr__(self, "accepted", self.exit_code == 0)

    def to_mapping(self) -> dict[str, object]:
        """Return a JSON-compatible acceptance mapping."""
        return {
            "command": list(self.command),
            "directory": str(self.directory),
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "wall_ms": self.wall_ms,
            "accepted": self.accepted,
            "outcome": self.outcome,
        }


@dataclass(frozen=True)
class Attempt:
    """One append-only row: exactly one seam invocation and its check."""

    run_id: str
    job_id: str
    attempt_no: int
    endpoint: str
    backend: str
    model: str
    status: str
    started_at: Optional[str]
    ended_at: Optional[str]
    error: Optional[AttemptError] = None
    halt_kind: Optional[str] = None
    dropped_params: Optional[tuple[str, ...]] = None
    forwarded_params: Optional[tuple[str, ...]] = None
    execution_controls_applied: Optional[tuple[str, ...]] = None
    usage: Optional[Usage] = None
    response_text: Optional[str] = None
    workspace: Optional[Path] = None
    acceptance: Optional[Acceptance] = None
    id: Optional[int] = None
    base_ref: Optional[str] = None
    workspace_status: str = "none"
    workspace_reason: Optional[str] = None
    workspace_removed_at: Optional[float] = None
    workspace_removal_forced: bool = False
    reasoning: Optional[str] = None
    finish_reason: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "dropped_params", _optional_tuple(self.dropped_params))
        object.__setattr__(self, "forwarded_params", _optional_tuple(self.forwarded_params))
        object.__setattr__(
            self,
            "execution_controls_applied",
            _optional_tuple(self.execution_controls_applied),
        )
        if self.workspace is not None:
            object.__setattr__(self, "workspace", Path(self.workspace).expanduser().resolve())
        status = str(self.workspace_status)
        if status not in {"isolated", "none", "removing", "removed"}:
            raise ValueError(
                "workspace_status must be one of: isolated, none, removing, removed"
            )
        object.__setattr__(self, "workspace_status", status)
        if self.base_ref is not None:
            object.__setattr__(self, "base_ref", str(self.base_ref))
        if self.workspace_reason is not None:
            object.__setattr__(self, "workspace_reason", str(self.workspace_reason))
        if not isinstance(self.workspace_removal_forced, bool):
            raise ValueError("workspace_removal_forced must be a boolean")

    @property
    def error_code(self) -> Optional[str]:
        """The machine error code, when the completion failed."""
        return self.error.code if self.error is not None else None

    @property
    def error_message(self) -> Optional[str]:
        """The human error detail, when the completion failed."""
        return self.error.message if self.error is not None else None

    def to_mapping(self) -> dict[str, object]:
        """Return a JSON-compatible attempt mapping."""
        result: dict[str, object] = {
            "run_id": self.run_id,
            "job_id": self.job_id,
            "attempt_no": self.attempt_no,
            "endpoint": self.endpoint,
            "backend": self.backend,
            "model": self.model,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "error": self.error.to_mapping() if self.error is not None else None,
            "halt_kind": self.halt_kind,
            "dropped_params": list(self.dropped_params) if self.dropped_params is not None else None,
            "forwarded_params": (
                list(self.forwarded_params)
                if self.forwarded_params is not None
                else None
            ),
            "execution_controls_applied": (
                list(self.execution_controls_applied)
                if self.execution_controls_applied is not None
                else None
            ),
            "usage": self.usage.to_mapping() if self.usage is not None else None,
            "response_text": self.response_text,
            "reasoning": self.reasoning,
            "finish_reason": self.finish_reason,
            "workspace": str(self.workspace) if self.workspace is not None else None,
            "base_ref": self.base_ref,
            "workspace_status": self.workspace_status,
            "workspace_reason": self.workspace_reason,
            "workspace_removed_at": self.workspace_removed_at,
            "workspace_removal_forced": self.workspace_removal_forced,
            "acceptance": self.acceptance.to_mapping() if self.acceptance is not None else None,
        }
        if self.id is not None:
            result["id"] = self.id
        return result


@dataclass(frozen=True)
class JobRecord:
    """A persisted job definition and its durable state."""

    job: Job
    state: JobState
    created_at: float
    updated_at: float
    error: Optional[str] = None

    @property
    def id(self) -> str:
        """The job identifier."""
        return self.job.id

    @property
    def terminal(self) -> bool:
        """Whether the job is in a terminal state."""
        return self.state in TERMINAL_STATES

    @property
    def error_message(self) -> Optional[str]:
        """The durable human-readable reason for a job-level failure."""
        return self.error

    def to_mapping(self) -> dict[str, object]:
        """Return a JSON-compatible job record."""
        return {
            "id": self.id,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
            "job": self.job.to_mapping(),
        }


@dataclass(frozen=True)
class RunRecord:
    """A persisted run header."""

    id: str
    created_at: float
    jobs_path: Optional[Path]
    max_parallel: int
    workspace_root: Optional[Path]
    status: RunState = RunState.PENDING
    workspace_base_refs: Mapping[str, str] = field(default_factory=dict)
    disallowed_tools: Optional[str] = None

    def __post_init__(self) -> None:
        if self.jobs_path is not None:
            object.__setattr__(self, "jobs_path", Path(self.jobs_path).expanduser().resolve())
        if self.workspace_root is not None:
            object.__setattr__(
                self, "workspace_root", Path(self.workspace_root).expanduser().resolve()
            )
        if not isinstance(self.status, RunState):
            object.__setattr__(self, "status", RunState(str(self.status)))
        if isinstance(self.workspace_base_refs, Mapping):
            object.__setattr__(
                self,
                "workspace_base_refs",
                {
                    str(job_id): str(base_ref)
                    for job_id, base_ref in self.workspace_base_refs.items()
                },
            )
        else:
            raise ValueError("workspace_base_refs must be a mapping")
        if self.disallowed_tools is not None and not isinstance(
            self.disallowed_tools, str
        ):
            raise ValueError("run disallowed_tools must be a string or null")

    def to_mapping(self) -> dict[str, object]:
        """Return a JSON-compatible run mapping."""
        return {
            "id": self.id,
            "created_at": self.created_at,
            "jobs_path": str(self.jobs_path) if self.jobs_path is not None else None,
            "max_parallel": self.max_parallel,
            "workspace_root": str(self.workspace_root) if self.workspace_root is not None else None,
            "status": self.status.value,
            "workspace_base_refs": dict(self.workspace_base_refs),
            "disallowed_tools": self.disallowed_tools,
        }


@dataclass(frozen=True)
class RunSnapshot:
    """A consistent read of one run, its jobs, and its attempts."""

    run: RunRecord
    jobs: tuple[JobRecord, ...]
    attempts: tuple[Attempt, ...]

    @property
    def status(self) -> RunState:
        """The derived state of the run."""
        return self.run.status

    @property
    def counts(self) -> dict[str, int]:
        """Count jobs by state."""
        result = {state.value: 0 for state in JobState}
        for record in self.jobs:
            result[record.state.value] += 1
        return result

    def to_mapping(self) -> dict[str, object]:
        """Return a JSON-compatible status payload."""
        return {
            "run": self.run.to_mapping(),
            "jobs": [job.to_mapping() for job in self.jobs],
            "attempts": [attempt.to_mapping() for attempt in self.attempts],
            "counts": self.counts,
        }


def _optional_tuple(value: Optional[Sequence[str]]) -> Optional[tuple[str, ...]]:
    """Normalize a nullable sequence without turning unknown into empty."""
    if value is None:
        return None
    return tuple(str(item) for item in value)


__all__ = [
    "PathLike",
    "JobState",
    "TERMINAL_STATES",
    "COMPLETED",
    "TIMEOUT",
    "ERROR",
    "RunState",
    "Prompt",
    "Contract",
    "ContractContext",
    "WorkspaceSpec",
    "Job",
    "JobFile",
    "load_job_file",
    "Usage",
    "AttemptError",
    "Acceptance",
    "Attempt",
    "JobRecord",
    "RunRecord",
    "RunSnapshot",
]
