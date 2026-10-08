"""Records and YAML shapes for the job-kit runner.

The package consumes a small YAML document with one flat list of jobs::

    jobs:
      - id: lint
        prompt:
          system: "You are a coding assistant."
          user: "Fix the lint errors."
        models: [local-codex]
        requirements:
          params: [cwd]
        directory: .
        contract:
          command: [python, -m, pytest, tests/lint]

``models`` is the job's model declaration: an ordered list of llm-scripting-kit
registry ids, in the one format specified by bootstrap's plugin-dev skill
(``references/model-declaration.md``) and validated structurally by
``bootstrap_lib.model_declaration``. A scalar is read as a one-element list.
The pre-declaration keys ``endpoint_preference``, ``endpoint_preferences``,
``endpoints`` and ``endpoint`` are no longer accepted (declaration-format
migration step 12); a job file using one of them fails loading with an error
naming ``models``.

The optional ``model_efforts`` mapping states the reasoning effort for each
declared entry, keyed by the same id ``models`` lists::

    models: [luna, sonnet]
    model_efforts: {luna: high, sonnet: low}

It is a SIDECAR to ``models`` rather than part of it, so the shared
declaration grammar (``bootstrap_lib.model_declaration``) stays a plain list
of ids. When present it must name every declared id and nothing else, and it
excludes ``options.effort``: effort is stated in one place. The runner sends
the selected entry's effort as that attempt's effort, and refuses an entry
whose adapter delivers no effort, or an attempt whose seam reports the effort
dropped, rather than running it at some other effort.

The optional ``model_requirements`` and ``model_options`` mappings are also
exhaustive sidecars keyed by ``models``. Each entry's effective requirements
and options are its sidecar mapping merged with the disjoint job-level mapping.
``model_options`` accepts only completion controls: ``max_tokens``,
``temperature``, ``effort`` and ``extras``. Tool controls and system-prompt
mode remain job-level because they are run policy, not model-specific knobs.

The job's directory is the declared working directory. Git repositories use
that directory as the starting point for per-attempt isolation. A contract
accepts only when its command exits with code zero.

The optional ``workspace`` mapping accepts ``directory``, ``base_ref`` and
``isolate``. ``base_ref`` defaults to the repository HEAD captured at run
start. Isolation defaults to false; set ``isolate: true`` when a job should
run each attempt in a detached worktree instead of its declared directory.

The optional job ``options`` mapping accepts ``allowed_tools``,
``disallowed_tools``, ``effort``, ``system_prompt_mode``, ``max_tokens``,
``temperature`` and ``extras``. ``max_tokens`` defaults to 4096 and
``temperature`` is unset by default, so the endpoint/model default applies;
set it per job to override. ``effort`` is unset by default, which keeps whatever
the endpoint registry entry carries. The top-level
``disallowed_tools`` job-file key sets a deny floor for every job in the run.
The option defaults are ``None`` for both tool lists, ``"replace"`` for
``system_prompt_mode``, and an empty mapping for ``extras``. The floor defaults
to ``None`` and does not change the adapter default.

Usage is nullable because a transport can complete without exposing token
counts. Unknown usage is represented by ``None`` rather than zero.
"""

from __future__ import annotations

import math
import os
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

_MODEL_OPTION_KEYS = frozenset(
    {"max_tokens", "temperature", "effort", "extras"}
)
_JOB_LEVEL_ONLY_OPTION_KEYS = _JOB_OPTION_KEYS - _MODEL_OPTION_KEYS


def _split_command(value: str) -> tuple[str, ...]:
    """Split a scalar command using the host platform's quoting rules."""
    return tuple(shlex.split(value, posix=os.name != "nt"))


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


def _sidecar_entries(
    value: object,
    models: tuple[str, ...],
    job_id: str,
    sidecar: str,
) -> dict[str, Mapping[object, object]]:
    """Validate one exhaustive per-model mapping and preserve model order."""
    where = f"job {job_id!r} {sidecar}"
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(
            f"{where} must be a mapping of declared model id -> mapping, got "
            f"{type(value).__name__}"
        )
    if not value:
        return {}
    entries: dict[str, Mapping[object, object]] = {}
    for key, entry in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{where} has a blank or non-string id {key!r}")
        name = key.strip()
        if name in entries:
            raise ValueError(f"{where} names {name!r} twice")
        if name not in models:
            raise ValueError(
                f"{where} names {name!r}, which models does not declare "
                f"(declared: {list(models)})"
            )
        if not isinstance(entry, Mapping):
            raise ValueError(
                f"{where}: {name!r} must have a mapping, got "
                f"{type(entry).__name__}"
            )
        entries[name] = entry
    missing = [name for name in models if name not in entries]
    if missing:
        raise ValueError(
            f"{where} states no entry for declared model(s) {missing}; state "
            "one mapping per declared entry"
        )
    return {name: entries[name] for name in models}


def _normalize_model_requirements(
    value: object,
    models: tuple[str, ...],
    requirements: Mapping[str, object],
    job_id: str,
) -> dict[str, dict[object, object]]:
    """Validate the exhaustive per-entry requirements sidecar."""
    entries = _sidecar_entries(value, models, job_id, "model_requirements")
    normalized: dict[str, dict[object, object]] = {}
    for name, entry in entries.items():
        collision = [key for key in entry if key in requirements]
        if collision:
            raise ValueError(
                f"job {job_id!r} sets requirement key(s) {collision!r} in both "
                f"requirements and model_requirements[{name!r}]; state it in "
                "one place"
            )
        normalized[name] = dict(entry)
    return normalized


def _normalize_model_options(
    value: object,
    models: tuple[str, ...],
    options: Mapping[str, object],
    model_efforts: Mapping[str, str],
    job_id: str,
) -> dict[str, dict[str, object]]:
    """Validate completion-only per-entry options and source exclusivity."""
    entries = _sidecar_entries(value, models, job_id, "model_options")
    normalized: dict[str, dict[str, object]] = {}
    for name, entry in entries.items():
        job_level_only = [key for key in entry if key in _JOB_LEVEL_ONLY_OPTION_KEYS]
        if job_level_only:
            raise ValueError(
                f"job {job_id!r} model_options[{name!r}] contains job-level only "
                f"key(s): {job_level_only!r}"
            )
        unknown = [
            key
            for key in entry
            if not isinstance(key, str) or key not in _MODEL_OPTION_KEYS
        ]
        if unknown:
            raise ValueError(
                f"job {job_id!r} model_options[{name!r}] contains unknown "
                f"keys: {unknown!r}"
            )
        collision = [key for key in entry if key in options]
        if collision:
            raise ValueError(
                f"job {job_id!r} sets option key(s) {collision!r} in both "
                f"options and model_options[{name!r}]; state it in one place"
            )
        normalized[name] = _normalize_job_options(entry)
    if model_efforts and any("effort" in entry for entry in normalized.values()):
        raise ValueError(
            f"job {job_id!r} sets both model_efforts and model_options.effort; "
            "state it in one place"
        )
    return normalized


class JobState(str, Enum):
    """State of one job in a durable run."""

    PENDING = "pending"
    RUNNING = "running"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    FAILED = "failed"
    HALTED = "halted"
    UNROUTABLE = "unroutable"
    # A healthy durable wait on an operator's answer to an interrupt. NOT
    # terminal: the job still moves on once the interrupt is resolved.
    WAITING = "waiting"
    # The operator rejected the job's interrupt.
    OPERATOR_REJECTED = "operator_rejected"
    # The job's interrupt lapsed before anyone answered it.
    EXPIRED = "expired"


TERMINAL_STATES = frozenset(
    {
        JobState.ACCEPTED,
        JobState.REJECTED,
        JobState.FAILED,
        JobState.HALTED,
        JobState.UNROUTABLE,
        JobState.OPERATOR_REJECTED,
        JobState.EXPIRED,
    }
)


WORKSPACE_STATUSES = frozenset({"isolated", "none", "removing", "removed"})


COMPLETED = "completed"
TIMEOUT = "timeout"
ERROR = "error"


class RunState(str, Enum):
    """Derived state of the set of jobs in a run."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    # No job is running or pending, not every job is terminal, and at least
    # one job waits on an interrupt.
    WAITING = "waiting"


#: The acceptance outcomes a contract run can record. ``interrupt_requested``
#: is a contract that exited 0 after writing a valid interrupt request;
#: ``request_refused`` is a contract whose interrupt request job-kit refused
#: (an invalid request, or a request beside a non-zero exit). Neither is ever
#: an acceptance.
ACCEPTANCE_OUTCOMES = frozenset(
    {"observed", "timed_out", "not_run", "interrupt_requested", "request_refused"}
)


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
            command = _split_command(self.command)
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
            command_value = _split_command(command)
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
    isolate: bool = False

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
            isolate = value.get("isolate", False)
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


#: The pre-declaration spellings a job file may no longer use. Declared
#: separately from the one accepted key (``models``) so a job written under
#: one of these fails loading with a message naming the removed key rather
#: than a bare "requires models".
_LEGACY_DECLARATION_KEYS = (
    "endpoint_preference",
    "endpoint_preferences",
    "endpoints",
    "endpoint",
)

#: The bootstrap release that shipped ``bootstrap_lib.model_declaration``.
_MODEL_DECLARATION_BOOTSTRAP = "0.129.0"


def _normalize_model_efforts(
    value: object,
    models: tuple[str, ...],
    options: Mapping[str, object],
    job_id: str,
) -> dict[str, str]:
    """Validate a job's per-entry effort sidecar against its declaration.

    Empty (or absent) means no per-entry effort. Otherwise every declared id
    must have exactly one non-empty effort string and no other key may
    appear, and ``options.effort`` must be unset -- a partial map would leave
    some entries at an effort nobody stated, and two sources would leave the
    effort ambiguous. The result is ordered like ``models``.
    """
    where = f"job {job_id!r} model_efforts"
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(
            f"{where} must be a mapping of declared model id -> effort, got "
            f"{type(value).__name__}"
        )
    if not value:
        return {}
    efforts: dict[str, str] = {}
    for key, effort in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{where} has a blank or non-string id {key!r}")
        name = key.strip()
        if name in efforts:
            raise ValueError(f"{where} names {name!r} twice")
        if name not in models:
            raise ValueError(
                f"{where} names {name!r}, which models does not declare "
                f"(declared: {list(models)})"
            )
        if not isinstance(effort, str) or not effort.strip():
            raise ValueError(
                f"{where}: {name!r} must have a non-empty effort string, got {effort!r}"
            )
        efforts[name] = effort.strip()
    missing = [name for name in models if name not in efforts]
    if missing:
        raise ValueError(
            f"{where} states no effort for declared model(s) {missing}; state "
            "one effort per declared entry"
        )
    if options.get("effort") is not None:
        raise ValueError(
            f"job {job_id!r} sets both options.effort and model_efforts; state "
            "the effort in one place"
        )
    return {name: efforts[name] for name in models}


def _parse_declaration(value: object) -> tuple[str, ...]:
    """Validate a model declaration with the shared structural validator.

    ``bootstrap_lib`` is a REQUIRED shared lib of job-kit (its bootstrap.json
    links it, and bootstrap is a declared dependency of every plugin), so an
    absent or too-old copy is diagnosed by name rather than surfacing as a
    bare ImportError. It is imported here rather than at module load so that
    importing ``job_kit`` never needs it.
    """
    try:
        import bootstrap_lib  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "job-kit validates model declarations with bootstrap_lib, which is "
            "not linked into this environment: the plugins-kit:bootstrap plugin "
            "has not provisioned job-kit here. Install or enable bootstrap "
            "(`claude plugin install bootstrap@plugins-kit`) and start a new "
            "session so it links job-kit's shared libs."
        ) from exc
    try:
        from bootstrap_lib import model_declaration
    except ImportError as exc:
        raise ImportError(
            "job-kit validates model declarations with bootstrap_lib."
            "model_declaration, which the linked bootstrap_lib predates: update "
            f"the bootstrap plugin to >= {_MODEL_DECLARATION_BOOTSTRAP} "
            "(`claude plugin update bootstrap@plugins-kit`)."
        ) from exc
    return tuple(model_declaration.parse(value))


@dataclass(frozen=True)
class Job:
    """One heterogeneous job and its caller-supplied acceptance contract."""

    id: str
    prompt: Prompt
    models: tuple[str, ...]
    contract: Contract
    requirements: Mapping[str, object] = field(default_factory=dict)
    directory: Optional[Path] = None
    workspace: Optional[WorkspaceSpec] = None
    max_attempts: int = 1
    options: Mapping[str, object] = field(default_factory=dict)
    model_efforts: Mapping[str, str] = field(default_factory=dict)
    model_requirements: Mapping[str, Mapping[str, object]] = field(
        default_factory=dict
    )
    model_options: Mapping[str, Mapping[str, object]] = field(default_factory=dict)

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

        declared = self.models
        if isinstance(declared, Sequence) and not isinstance(declared, (str, list)):
            declared = list(declared)
        try:
            models = _parse_declaration(declared)
        except ValueError as exc:
            raise type(exc)(f"job {job_id!r} models: {exc}") from exc
        object.__setattr__(self, "models", models)

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
        object.__setattr__(
            self,
            "model_efforts",
            _normalize_model_efforts(self.model_efforts, models, self.options, job_id),
        )
        object.__setattr__(
            self,
            "model_requirements",
            _normalize_model_requirements(
                self.model_requirements,
                models,
                self.requirements,
                job_id,
            ),
        )
        object.__setattr__(
            self,
            "model_options",
            _normalize_model_options(
                self.model_options,
                models,
                self.options,
                self.model_efforts,
                job_id,
            ),
        )

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
    def uses_model_sidecars(self) -> bool:
        """Whether selection needs per-entry requirements or options."""
        return bool(self.model_requirements or self.model_options)

    def effective_requirements(self, model_id: str) -> dict[str, object]:
        """Return the disjoint job and per-entry requirements for ``model_id``."""
        if model_id not in self.models:
            raise ValueError(
                f"job {self.id!r} does not declare model {model_id!r}"
            )
        result = dict(self.requirements)
        result.update(self.model_requirements.get(model_id, {}))
        return result

    def effective_options(self, model_id: str) -> dict[str, object]:
        """Return the disjoint job and per-entry options for ``model_id``."""
        if model_id not in self.models:
            raise ValueError(
                f"job {self.id!r} does not declare model {model_id!r}"
            )
        result = dict(self.options)
        result.update(self.model_options.get(model_id, {}))
        return result

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

        declared: object = value.get("models")
        if declared is None:
            legacy = next(
                (key for key in _LEGACY_DECLARATION_KEYS if value.get(key) is not None),
                None,
            )
            if legacy is not None:
                raise ValueError(
                    f"job {value.get('id')!r} uses the removed `{legacy}` key -- "
                    "declare its model priority under `models` instead"
                )
            raise ValueError(f"job {value.get('id')!r} requires models")

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
            models=declared,
            contract=contract,
            requirements=requirements if requirements is not None else {},
            directory=directory,
            workspace=workspace,
            max_attempts=max_attempts,
            options=options if options is not None else {},
            model_efforts=value.get("model_efforts"),
            model_requirements=value.get("model_requirements"),
            model_options=value.get("model_options"),
        )

    def to_mapping(self) -> dict[str, object]:
        """Return the durable job definition mapping."""
        result: dict[str, object] = {
            "id": self.id,
            "prompt": self.prompt.to_mapping(),
            "models": list(self.models),
            "requirements": dict(self.requirements),
            "contract": self.contract.to_mapping(),
            "max_attempts": self.max_attempts,
            "options": dict(self.options),
        }
        # Written only when set, so a job without per-entry effort keeps the
        # exact definition JSON it always had in the ledger.
        if self.model_efforts:
            result["model_efforts"] = dict(self.model_efforts)
        if self.model_requirements:
            result["model_requirements"] = {
                name: dict(requirements)
                for name, requirements in self.model_requirements.items()
            }
        if self.model_options:
            result["model_options"] = {
                name: dict(options) for name, options in self.model_options.items()
            }
        if self.directory is not None:
            result["directory"] = str(self.directory)
        if self.workspace is not None:
            result["workspace"] = self.workspace.to_mapping()
        return result


def validate_max_parallel(value: object) -> int:
    """Return a positive worker-pool bound or raise a typed value error.

    ``bool`` is an ``int`` subclass and a YAML ``max_parallel: true`` would
    otherwise become a pool of one, so it is rejected explicitly. Floats and
    numeric strings are rejected rather than coerced: a jobs file that says
    ``"3"`` is a mistake worth reporting, not a bound worth guessing.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("max_parallel must be a positive integer")
    if value < 1:
        raise ValueError("max_parallel must be a positive integer")
    return int(value)


@dataclass(frozen=True)
class JobFile:
    """A loaded YAML document and run-level options."""

    jobs: tuple[Job, ...]
    max_parallel: int = 1
    workspace_root: Optional[Path] = None
    disallowed_tools: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "max_parallel", validate_max_parallel(self.max_parallel)
        )
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
        max_parallel=validate_max_parallel(document.get("max_parallel", 1)),
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
        if self.outcome not in ACCEPTANCE_OUTCOMES:
            raise ValueError(
                "acceptance outcome must be one of: "
                + ", ".join(sorted(ACCEPTANCE_OUTCOMES))
            )
        object.__setattr__(
            self, "accepted", self.exit_code == 0 and self.outcome == "observed"
        )

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
    # The rendered entries this attempt's endpoint was selected from, in pace
    # order: ``{"id", "pace", "usable"}`` each. Together with the job's
    # declared ``models`` list this is what makes an unattended choice
    # explainable afterwards. ``None`` on rows written before it was logged.
    pace_readings: Optional[tuple[Mapping[str, object], ...]] = None

    def __post_init__(self) -> None:
        if self.pace_readings is not None:
            object.__setattr__(
                self,
                "pace_readings",
                tuple(dict(reading) for reading in self.pace_readings),
            )
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
        if status not in WORKSPACE_STATUSES:
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
            "pace_readings": (
                [dict(reading) for reading in self.pace_readings]
                if self.pace_readings is not None
                else None
            ),
        }
        if self.id is not None:
            result["id"] = self.id
        return result


@dataclass(frozen=True)
class AttemptReservation:
    """Durable write-ahead state for one possible seam invocation."""

    run_id: str
    job_id: str
    attempt_no: int
    budget_no: int
    endpoint: str
    backend: str
    model: str
    workspace_path: Optional[Path]
    reserved_at: str
    invoke_armed_at: Optional[str] = None
    disposition: Optional[str] = None
    resolved_at: Optional[str] = None
    lost_at: Optional[str] = None
    loss_reason: Optional[str] = None
    workspace_status: str = "none"
    workspace_reason: Optional[str] = None
    workspace_removed_at: Optional[float] = None
    workspace_removal_forced: bool = False
    id: Optional[int] = None

    def __post_init__(self) -> None:
        if self.workspace_path is not None:
            object.__setattr__(
                self, "workspace_path", Path(self.workspace_path).expanduser().resolve()
            )
        if self.workspace_status not in {"isolated", "none", "removing", "removed"}:
            raise ValueError(
                "reservation workspace_status must be one of: "
                "isolated, none, removing, removed"
            )
        if self.workspace_path is None and self.workspace_status != "none":
            raise ValueError("a reservation without a workspace must have status none")
        if self.workspace_path is not None and self.workspace_status == "none":
            raise ValueError("a reservation with a workspace must not have status none")
        if not isinstance(self.workspace_removal_forced, bool):
            raise ValueError("workspace_removal_forced must be a boolean")

    @property
    def workspace(self) -> Optional[Path]:
        """Compatibility alias for workspace-bearing ledger records."""
        return self.workspace_path

    def to_mapping(self) -> dict[str, object]:
        """Return a JSON-compatible reservation mapping."""
        return {
            "id": self.id,
            "run_id": self.run_id,
            "job_id": self.job_id,
            "attempt_no": self.attempt_no,
            "budget_no": self.budget_no,
            "endpoint": self.endpoint,
            "backend": self.backend,
            "model": self.model,
            "workspace_path": (
                str(self.workspace_path) if self.workspace_path is not None else None
            ),
            "reserved_at": self.reserved_at,
            "invoke_armed_at": self.invoke_armed_at,
            "disposition": self.disposition,
            "resolved_at": self.resolved_at,
            "lost_at": self.lost_at,
            "loss_reason": self.loss_reason,
            "workspace_status": self.workspace_status,
            "workspace_reason": self.workspace_reason,
            "workspace_removed_at": self.workspace_removed_at,
            "workspace_removal_forced": self.workspace_removal_forced,
        }


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


def interrupt_lapsed(expires_at: Optional[float], now: float) -> bool:
    """Whether an interrupt expiring at ``expires_at`` has lapsed at ``now``.

    Inclusive: an interrupt lapses AT its ``expires_at``. An interrupt with
    no expiry never lapses.
    """
    return expires_at is not None and now >= expires_at


@dataclass(frozen=True)
class InterruptRequest:
    """A contract's validated request to wait for an operator's answer.

    ``envelope`` is the request-file schema literal it arrived under, and
    ``expires_in_s`` is relative, so a contract never supplies an absolute
    time. ``job_kit.interrupts.parse_request`` builds and validates one.
    """

    envelope: str
    kind: str
    request_schema: Mapping[str, object]
    payload: Mapping[str, object]
    expires_in_s: Optional[int] = None


@dataclass(frozen=True)
class InterruptResolution:
    """The one immutable resolution of one interrupt.

    ``outcome`` is ``answered``, ``rejected`` or ``expired``. ``input`` is
    the operator's answer (answered only) and ``reason`` the operator's free
    text (rejected only). ``replayed`` is not stored: it is true when a
    resolve call matched the existing resolution and wrote nothing.
    """

    interrupt_id: str
    outcome: str
    resolved_at: float
    input: object = None
    reason: Optional[str] = None
    replayed: bool = field(default=False, compare=False)

    def to_mapping(self) -> dict[str, object]:
        """Return a JSON-compatible resolution mapping."""
        return {
            "interrupt_id": self.interrupt_id,
            "outcome": self.outcome,
            "input": self.input,
            "reason": self.reason,
            "resolved_at": self.resolved_at,
        }


@dataclass(frozen=True)
class InterruptRecord:
    """One append-only interrupt row, with its resolution when it has one.

    ``id`` is the decimal string of the ledger row id. The owning attempt is
    ``(run_id, job_id, attempt_no)``; ``continuation_no`` names the contract
    run of that attempt that raised it (0: the attempt's first run).
    """

    id: str
    run_id: str
    job_id: str
    attempt_no: int
    continuation_no: int
    envelope: str
    kind: str
    request_schema: Mapping[str, object]
    payload: Mapping[str, object]
    created_at: float
    expires_at: Optional[float] = None
    resolution: Optional[InterruptResolution] = None

    def lapsed(self, now: float) -> bool:
        """Whether the interrupt is unresolved and past its expiry at ``now``."""
        return self.resolution is None and interrupt_lapsed(self.expires_at, now)

    def to_mapping(self) -> dict[str, object]:
        """Return a JSON-compatible interrupt mapping."""
        return {
            "id": self.id,
            "run_id": self.run_id,
            "job_id": self.job_id,
            "attempt_no": self.attempt_no,
            "continuation_no": self.continuation_no,
            "envelope": self.envelope,
            "kind": self.kind,
            "request_schema": self.request_schema,
            "payload": self.payload,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "resolution": (
                self.resolution.to_mapping() if self.resolution is not None else None
            ),
        }


@dataclass(frozen=True)
class Continuation:
    """One re-run of an attempt's contract after its interrupt was answered.

    ``disposition`` is ``None`` while live, then ``completed``,
    ``interrupted`` or ``process_lost``. A continuation makes no model call
    and holds no reservation, so it never spends the attempt budget.
    """

    id: int
    run_id: str
    job_id: str
    attempt_no: int
    continuation_no: int
    interrupt_id: str
    started_at: float
    ended_at: Optional[float] = None
    disposition: Optional[str] = None
    acceptance: Optional[Acceptance] = None

    def to_mapping(self) -> dict[str, object]:
        """Return a JSON-compatible continuation mapping."""
        return {
            "id": self.id,
            "run_id": self.run_id,
            "job_id": self.job_id,
            "attempt_no": self.attempt_no,
            "continuation_no": self.continuation_no,
            "interrupt_id": self.interrupt_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "disposition": self.disposition,
            "acceptance": (
                self.acceptance.to_mapping() if self.acceptance is not None else None
            ),
        }


@dataclass(frozen=True)
class RunSnapshot:
    """A consistent read of one run, its jobs, attempts and interrupts.

    ``read_at`` is the epoch the snapshot was read. It derives each job's
    ``effective_state`` and each interrupt's ``lapsed`` flag without writing
    anything, so a lapse nobody has recorded yet is shown, never hidden.
    """

    run: RunRecord
    jobs: tuple[JobRecord, ...]
    attempts: tuple[Attempt, ...]
    reservations: tuple[AttemptReservation, ...] = ()
    interrupts: tuple[InterruptRecord, ...] = ()
    continuations: tuple[Continuation, ...] = ()
    read_at: Optional[float] = None

    @property
    def status(self) -> RunState:
        """The derived state of the run."""
        return self.run.status

    @property
    def counts(self) -> dict[str, int]:
        """Count jobs by recorded state."""
        result = {state.value: 0 for state in JobState}
        for record in self.jobs:
            result[record.state.value] += 1
        return result

    def open_interrupt(self, job_id: str) -> Optional[InterruptRecord]:
        """The job's one unresolved interrupt, or ``None``."""
        for record in self.interrupts:
            if record.job_id == job_id and record.resolution is None:
                return record
        return None

    def effective_state(self, job_id: str) -> JobState:
        """The recorded state, except ``expired`` for a waiting job whose open
        interrupt has lapsed at ``read_at``."""
        record = next(job for job in self.jobs if job.id == job_id)
        if record.state is JobState.WAITING and self.read_at is not None:
            open_record = self.open_interrupt(job_id)
            if open_record is not None and open_record.lapsed(self.read_at):
                return JobState.EXPIRED
        return record.state

    def to_mapping(self) -> dict[str, object]:
        """Return a JSON-compatible status payload."""
        jobs = []
        for job in self.jobs:
            mapping = job.to_mapping()
            mapping["effective_state"] = self.effective_state(job.id).value
            jobs.append(mapping)
        interrupts = []
        for record in self.interrupts:
            mapping = record.to_mapping()
            mapping["lapsed"] = (
                record.lapsed(self.read_at) if self.read_at is not None else False
            )
            interrupts.append(mapping)
        return {
            "run": self.run.to_mapping(),
            "jobs": jobs,
            "attempts": [attempt.to_mapping() for attempt in self.attempts],
            "reservations": [
                reservation.to_mapping() for reservation in self.reservations
            ],
            "interrupts": interrupts,
            "continuations": [
                continuation.to_mapping() for continuation in self.continuations
            ],
            "counts": self.counts,
            "read_at": self.read_at,
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
    "WORKSPACE_STATUSES",
    "COMPLETED",
    "TIMEOUT",
    "ERROR",
    "RunState",
    "ACCEPTANCE_OUTCOMES",
    "interrupt_lapsed",
    "InterruptRequest",
    "InterruptResolution",
    "InterruptRecord",
    "Continuation",
    "Prompt",
    "Contract",
    "ContractContext",
    "WorkspaceSpec",
    "Job",
    "JobFile",
    "load_job_file",
    "validate_max_parallel",
    "Usage",
    "AttemptError",
    "Acceptance",
    "Attempt",
    "AttemptReservation",
    "JobRecord",
    "RunRecord",
    "RunSnapshot",
]
