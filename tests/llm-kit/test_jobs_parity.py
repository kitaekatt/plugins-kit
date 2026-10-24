"""Differential parity: llm_kit.jobs vs the legacy job_kit it was taken from.

The legacy side is ``job_kit`` (plugins/job-kit/lib) over ``llm_scripting_kit``
(plugins/llm-scripting-kit/lib), imported with a sys.path insert scoped to a
module fixture. The final side is ``llm_kit.jobs`` over ``llm_kit.completion``,
``llm_kit.dispatch``, ``llm_kit.registry``, ``llm_kit.execution`` and
``llm_kit.storage``. Every scenario is a function of a "side" and runs on both
with the same inputs; the test asserts the two outcomes are equal.

Nothing here makes a network call or starts a real model: backends are fakes,
selection runs over an empty registry with every probe answering "no verdict",
verdict write-back is recorded instead of written, and HOME / USERPROFILE point
at a temp directory for every test. Workspace tests use a scratch git
repository in a temp directory.

What is compared
----------------

* ledger bytes: the product DDL (``_MIGRATIONS``), the fresh-create and
  every-migration-prefix ``sqlite_master`` / ``schema_version`` dumps (against
  committed fixtures under ``fixtures/job_schema/`` that must be byte-equal
  between the two sides), and the statement order a connection issues (the
  pragma order);
* runner behaviour: ledger rows, execution events (as the CLI renders them,
  byte for byte, and as the JSONL sink writes them), snapshots and the exit
  code the CLI derives from a snapshot, for accepted, retried, rejected,
  halted, unroutable, timed-out, interrupted-and-resumed and parallel runs;
* selection, job-file loading, interrupt documents, event building and
  workspace management: results, exception types and messages.

Deliberate differences, each pinned by name below:

* ``SharedLibTooOldError`` and the import-time version-skew probes are gone:
  ``llm_kit.jobs`` and the modules it probed ship in one package at one
  version, so the skew they diagnosed cannot occur (``TestDeliberateDifferences``);
* the default capability provider is ``ConfiguredBackendFactory`` and the
  default backend factory is ``dispatch.factory.create_backend``, so jobs
  import no adapter (``TestDeliberateDifferences``);
* the ``cancelled`` job state, ``llm-kit jobs cancel`` and ledger schema 13
  (user ruling 2026-10-19: llm-kit may change its frozen formats for a
  per-job timeout and ``jobs cancel``). Legacy job-kit stays at schema 12 and
  refuses a schema-13 ledger. The ledger-bytes comparisons stop at schema 12
  (``TestLedgerSchema``), the scenario dumps leave out the ``schema_version``
  table and a zero ``counts.cancelled``, and a rollback from llm-kit to
  job-kit is pinned as a refusal (``TestCrossVersionState``,
  ``TestDeliberateDifferences``).
"""

from __future__ import annotations

import importlib
import json
import os
import re
import sqlite3
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import types
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import pytest

from final_wording import claude_contract_deep, floors_in_text_to_final, max_tokens_deep, max_tokens_default_to_final, to_final

_REPO = Path(__file__).resolve().parents[2]
_LEGACY_PATHS = [
    _REPO / "plugins" / "job-kit" / "lib",
    _REPO / "plugins" / "llm-scripting-kit" / "lib",
]
_LEGACY_ROOTS = ("job_kit", "llm_scripting_kit", "bootstrap_guard")
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "job_schema"
_LIB = __import__("pathlib").Path(__import__("llm_kit").__file__).resolve().parent.parent

_JOB_MODULES = ("model", "run", "select", "store", "workspace", "events", "interrupts")


# --------------------------------------------------------------------------
# Loading both sides
# --------------------------------------------------------------------------


def _legacy_side() -> types.SimpleNamespace:
    ns = {name: importlib.import_module(f"job_kit.{name}") for name in _JOB_MODULES}
    completion = importlib.import_module("llm_scripting_kit.completion")
    return types.SimpleNamespace(
        name="legacy",
        package=importlib.import_module("job_kit"),
        LLMResponse=completion.LLMResponse,
        HaltError=completion.HaltError,
        AgentTimeoutError=completion.AgentTimeoutError,
        BackendSelection=completion.BackendSelection,
        Capabilities=completion.Capabilities,
        ExecutionControl=completion.ExecutionControl,
        declaration=importlib.import_module("llm_scripting_kit.declaration"),
        models=importlib.import_module("llm_scripting_kit.models"),
        usage=importlib.import_module("llm_scripting_kit.usage_budget"),
        **ns,
    )


def _final_side() -> types.SimpleNamespace:
    ns = {name: importlib.import_module(f"llm_kit.jobs.{name}") for name in _JOB_MODULES}
    return types.SimpleNamespace(
        name="final",
        package=importlib.import_module("llm_kit.jobs"),
        LLMResponse=importlib.import_module("llm_kit.completion.types").LLMResponse,
        HaltError=importlib.import_module("llm_kit.completion.halt").HaltError,
        AgentTimeoutError=importlib.import_module("llm_kit.completion.halt").AgentTimeoutError,
        BackendSelection=importlib.import_module("llm_kit.dispatch.factory").BackendSelection,
        Capabilities=importlib.import_module("llm_kit.completion.capabilities").Capabilities,
        ExecutionControl=importlib.import_module("llm_kit.completion.capabilities").ExecutionControl,
        declaration=importlib.import_module("llm_kit.dispatch.declaration"),
        models=importlib.import_module("llm_kit.registry.models"),
        usage=importlib.import_module("llm_kit.dispatch.usage_budget"),
        **ns,
    )


@pytest.fixture(scope="module")
def sides():
    """(legacy, final); legacy modules are removed from ``sys.modules`` afterwards."""
    before = set(sys.modules)
    for entry in map(str, _LEGACY_PATHS):
        sys.path.insert(0, entry)
    try:
        legacy = _legacy_side()
        final = _final_side()
        yield legacy, final
    finally:
        for entry in map(str, _LEGACY_PATHS):
            if entry in sys.path:
                sys.path.remove(entry)
        for name in list(sys.modules):
            if name not in before and name.split(".")[0] in _LEGACY_ROOTS:
                del sys.modules[name]


@pytest.fixture(autouse=True)
def _scratch_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """HOME and USERPROFILE point at a temp directory for every test."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CLAUDE_BOOTSTRAP_DATA_ROOT", raising=False)
    return home


def _ticking_clock() -> Callable[[], str]:
    """An ISO clock that advances one second per read (thread safe enough for a counter)."""
    import datetime
    import itertools

    ticks = itertools.count()
    start = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc)

    def now() -> str:
        moment = start + datetime.timedelta(seconds=next(ticks))
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    return now


@pytest.fixture()
def hermetic(sides, monkeypatch: pytest.MonkeyPatch):
    """Keep selection off this machine's registry, CLIs and pinned verdicts."""
    written: dict[str, list] = {}
    for side in sides:
        record: list = []
        written[side.name] = record
        monkeypatch.setattr(side.models, "discover_model_entries", lambda **_: {})
        monkeypatch.setattr(side.declaration, "check_many", lambda *_a, **_k: {})
        monkeypatch.setattr(
            side.usage,
            "record_observed_halt",
            lambda entry_id, spec, _record=record, **kwargs: _record.append(
                (entry_id, repr(spec), sorted(kwargs))
            ),
        )
        monkeypatch.setattr(
            side.run.BackpressurePolicy,
            "from_environment",
            staticmethod(lambda _run=side.run: _run.BackpressurePolicy(cap_s=0.0)),
        )
        # The unreachable-exclusion rule orders attempts by their ISO stamps, which
        # have one-second resolution; a ticking clock keeps that order deterministic.
        monkeypatch.setattr(side.run, "utc_now_iso", _ticking_clock())
    return written


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------

_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?")
_HEX32 = re.compile(r"\b[0-9a-f]{32}\b")
_EPOCH = re.compile(r"\b1[0-9]{9}(?:\.[0-9]+)?\b")
#: Wall-clock measurements: the only numbers a run produces that depend on the machine.
_TIMING_KEYS = ("wall_ms", "duration_ms", "elapsed_ms")
_TIMING = re.compile(r'("(?:' + "|".join(_TIMING_KEYS) + r')"\s*:\s*)[0-9.]+')


class Scrub:
    """Replace everything that legitimately differs between two runs."""

    def __init__(self, root: Path) -> None:
        resolved = root.resolve()
        self.roots = sorted(
            {
                str(resolved),
                resolved.as_posix(),
                str(root),
                root.as_posix(),
                str(resolved).replace("\\", "\\\\"),
            },
            key=len,
            reverse=True,
        )

    def text(self, value: str) -> str:
        for root in self.roots:
            value = value.replace(root, "<ROOT>")
        value = value.replace("\\", "/")
        value = _ISO.sub("<ISO>", value)
        value = _EPOCH.sub("<EPOCH>", value)
        value = _TIMING.sub(lambda m: m.group(1) + '"<MS>"', value)
        return _HEX32.sub("<HEX32>", value)

    def value(self, value: Any, *, column: str = "") -> Any:
        if isinstance(value, str):
            if column.endswith("_json") or column == "definition_json":
                try:
                    return self.value(json.loads(value))
                except ValueError:
                    pass
            return self.text(value)
        if isinstance(value, float):
            return "<EPOCH>" if value > 1e9 else value
        if isinstance(value, Mapping):
            return {
                str(k): ("<MS>" if str(k) in _TIMING_KEYS else self.value(v, column=str(k)))
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.value(v) for v in value]
        return value


def dump_ledger(db: Path, scrub: Scrub) -> dict[str, list]:
    """Every table of a ledger, in rowid order, scrubbed.

    The ``schema_version`` table is left out: legacy job-kit stays at schema 12
    and llm-kit is at 13 (``cancelled``), a deliberate difference pinned in
    ``TestLedgerSchema`` and ``TestDeliberateDifferences``. Every other table
    is compared as it is.
    """
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        tables = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            if row["name"] != "schema_version"
        ]
        out: dict[str, list] = {}
        for table in tables:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            out[table] = [
                {key: scrub.value(row[key], column=key) for key in row.keys()} for row in rows
            ]
        return out
    finally:
        conn.close()


def render_events(store: Any, run_id: str) -> list[str]:
    """The lines ``job-kit events`` prints, byte for byte."""
    return [
        json.dumps(event, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        for event in store.list_events(run_id)
    ]


def _in_final_wording(value: Any) -> Any:
    """``value`` with every string passed through ``final_wording.to_final``."""
    value = claude_contract_deep(value)
    if isinstance(value, dict):
        value = max_tokens_default_to_final(value)  # declared: the openrouter max_tokens default is unset
    if isinstance(value, str):
        return to_final(value)
    if isinstance(value, dict):
        # call_modes: declared difference, only the final Capabilities record carries it.
        return {key: _in_final_wording(item) for key, item in value.items() if key != "call_modes"}
    if isinstance(value, (list, tuple)):
        return type(value)(_in_final_wording(item) for item in value)
    return value


def first_difference(a: Any, b: Any, path: str = "$") -> Optional[str]:
    """The path and values of the first place two scrubbed outcomes differ."""
    if type(a) is not type(b):
        return f"{path}: {a!r} != {b!r}"
    if isinstance(a, dict):
        for key in sorted(set(a) | set(b), key=str):
            if key not in a or key not in b:
                return f"{path}.{key}: present on one side only ({a.get(key)!r} vs {b.get(key)!r})"
            found = first_difference(a[key], b[key], f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(a, (list, tuple)):
        if len(a) != len(b):
            return f"{path}: length {len(a)} != {len(b)}"
        for index, (x, y) in enumerate(zip(a, b)):
            found = first_difference(x, y, f"{path}[{index}]")
            if found:
                return found
        return None
    return None if a == b else f"{path}: {a!r} != {b!r}"


def exit_code(snapshot: Any) -> int:
    """The CLI's exit code for a snapshot (``cli._exit_for_snapshot``)."""
    states = [job.state.value for job in snapshot.jobs]
    terminal = {"accepted", "rejected", "failed", "halted", "unroutable", "operator_rejected", "expired"}
    if any(state in terminal and state != "accepted" for state in states):
        return 1
    if any(state == "waiting" for state in states):
        return 4
    if all(state == "accepted" for state in states):
        return 0
    return 1


# --------------------------------------------------------------------------
# Fake transports
# --------------------------------------------------------------------------


class Transport:
    """One hermetic backend whose outcomes are consumed in order (side-typed)."""

    name = "fake"

    def __init__(self, side: Any, outcomes: Optional[list] = None, label: Optional[str] = None) -> None:
        self.side = side
        self.outcomes = list(outcomes or [])
        self.calls: list[tuple] = []
        self.label = label
        self.before_call: Optional[Callable[[str], None]] = None
        #: Outcomes keyed by the user prompt, consumed before the shared sequence.
        self.by_user: dict[str, list] = {}

    def complete(self, system: str, user: str, *, model: str, options: object = None) -> Any:
        summary = {
            key: getattr(options, key, None)
            for key in ("allowed_tools", "disallowed_tools", "max_tokens", "temperature", "effort", "system_prompt_mode")
        }
        if self.side.name == "legacy" and summary["max_tokens"] == 4096:
            # Declared difference (final_wording.max_tokens_default_to_final): a job that names no
            # max_tokens carries the legacy default 4096 and llm-kit's unset default (None).
            summary["max_tokens"] = None
        self.calls.append((system, user, model, summary))
        if self.before_call is not None:
            self.before_call(user)
        queue = self.by_user.get(user)
        if queue:
            outcome = queue.pop(0)
        else:
            outcome = self.outcomes.pop(0) if self.outcomes else None
        if callable(outcome):
            outcome = outcome(self.side)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is None:
            return self.side.LLMResponse(
                text="model answer",
                model="fake-model",
                input_tokens=3,
                output_tokens=5,
                dropped_params=(),
                execution_controls_applied=(),
                started_at="2026-09-01T00:00:00Z",
                ended_at="2026-09-01T00:00:01Z",
            )
        return outcome

    def classify_halt(self, exc: BaseException) -> Optional[str]:
        return self.label if isinstance(exc, RuntimeError) else None


def _factory(side: Any, mapping: Mapping[str, Transport], *, with_capabilities: bool = False) -> Callable[..., Any]:
    def build(endpoint: str, **_kwargs: object) -> Any:
        if endpoint not in mapping:
            raise side.models.EndpointResolveError(f"no endpoint {endpoint!r}")
        return side.BackendSelection(
            endpoint,
            "fake",
            mapping[endpoint],
            "fake-model",
            capabilities=side.Capabilities(adapter="fake") if with_capabilities else None,
        )

    return build


def _provider(side: Any, *, deny: bool = False) -> Callable[[], Mapping[str, Any]]:
    """The fake advertisement; ``deny`` adds a control that can honour a deny floor."""
    if not deny:
        return lambda: {"fake": side.Capabilities(adapter="fake")}
    control = side.ExecutionControl(
        id="disallowed-tools",
        emits="fake deny control",
        effect="deny",
        subjects=("filesystem-write", "shell-exec", "subagent-spawn"),
        source="request",
        parameter="disallowed_tools",
    )
    return lambda: {"fake": side.Capabilities(adapter="fake", execution_controls=(control,))}


# --------------------------------------------------------------------------
# Runner scenarios
# --------------------------------------------------------------------------

_PASS = [sys.executable, "-c", "pass"]
_FAIL = [sys.executable, "-c", "import sys; sys.exit(1)"]

_FLAKY = textwrap.dedent(
    """
    import pathlib, sys
    marker = pathlib.Path(sys.argv[1])
    if marker.exists():
        sys.exit(0)
    marker.write_text("seen")
    sys.exit(1)
    """
)

_INTERRUPTING = textwrap.dedent(
    r"""
    import json, os, sys
    log_path = sys.argv[1]
    resolution_path = os.environ.get("JOB_KIT_INTERRUPT_RESOLUTION")
    if resolution_path is None:
        request = {
            "schema": "job-kit.interrupt-request/v1",
            "kind": "approval",
            "request_schema": {"type": "object", "required": ["approved"],
                               "properties": {"approved": {"const": True}}},
            "payload": {"action": "push release tag"},
        }
        with open(os.environ["JOB_KIT_INTERRUPT_REQUEST"], "w", encoding="utf-8") as handle:
            json.dump(request, handle)
    else:
        with open(resolution_path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(document) + "\n")
    """
)


def _job(job_id: str, work: Path, *, command: list, models: Optional[list] = None, **extra: Any) -> dict:
    return {
        "id": job_id,
        "prompt": {"user": job_id},
        "models": models or ["ep"],
        "directory": str(work),
        "contract": {"command": command, "directory": str(work)},
        **extra,
    }


def _write_jobs(path: Path, jobs: list, **top: Any) -> Path:
    path.write_text(json.dumps({"max_parallel": 1, "jobs": jobs, **top}), encoding="utf-8")
    return path


def comparable_snapshot(snapshot: Any) -> dict:
    """A snapshot mapping with the one key legacy job-kit lacks removed.

    ``counts`` gains ``cancelled`` with the ``cancelled`` job state (a
    deliberate difference). It is dropped only while it is zero, so a run that
    cancelled a job would still differ and is not a parity scenario.
    """
    mapping = snapshot.to_mapping()
    counts = mapping.get("counts")
    if isinstance(counts, dict) and counts.get("cancelled") == 0:
        counts = {key: value for key, value in counts.items() if key != "cancelled"}
        mapping = {**mapping, "counts": counts}
    return mapping


def _outcome(side: Any, base: Path, store_path: Path, run_id: str, snapshot: Any, transports: Mapping[str, Transport]) -> dict:
    scrub = Scrub(base)
    store = side.store.JobStore(store_path, create=False)
    return {
        "exit_code": exit_code(snapshot),
        "snapshot": scrub.value(comparable_snapshot(snapshot)),
        "ledger": dump_ledger(store_path, scrub),
        "events": [scrub.text(line) for line in render_events(store, run_id)],
        "calls": {name: [scrub.value(list(call)) for call in t.calls] for name, t in transports.items()},
    }


def _scenario_accepted(side: Any, base: Path) -> dict:
    work = base / "work"
    work.mkdir(parents=True)
    jobs = _write_jobs(
        base / "jobs.json",
        [_job("a", work, command=_PASS), _job("b", work, command=_PASS)],
        workspace_root=str(base / "ws"),
    )
    transports = {"ep": Transport(side)}
    snapshot = side.run.run_job_file(
        jobs,
        store_path=base / "ledger" / "runs.sqlite3",
        run_id="r",
        capabilities_provider=_provider(side),
        backend_factory=_factory(side, transports),
    )
    return _outcome(side, base, base / "ledger" / "runs.sqlite3", "r", snapshot, transports)


def _scenario_retry_then_accept(side: Any, base: Path) -> dict:
    work = base / "work"
    work.mkdir(parents=True)
    script = base / "flaky.py"
    script.write_text(_FLAKY, encoding="utf-8")
    marker = base / "marker"
    jobs = _write_jobs(
        base / "jobs.json",
        [_job("a", work, command=[sys.executable, str(script), str(marker)], max_attempts=3)],
        workspace_root=str(base / "ws"),
    )
    transports = {"ep": Transport(side)}
    snapshot = side.run.run_job_file(
        jobs,
        store_path=base / "ledger" / "runs.sqlite3",
        run_id="r",
        capabilities_provider=_provider(side),
        backend_factory=_factory(side, transports),
    )
    return _outcome(side, base, base / "ledger" / "runs.sqlite3", "r", snapshot, transports)


def _scenario_rejected(side: Any, base: Path) -> dict:
    work = base / "work"
    work.mkdir(parents=True)
    jobs = _write_jobs(
        base / "jobs.json",
        [_job("a", work, command=_FAIL, max_attempts=2), _job("b", work, command=_PASS)],
        workspace_root=str(base / "ws"),
    )
    transports = {"ep": Transport(side)}
    snapshot = side.run.run_job_file(
        jobs,
        store_path=base / "ledger" / "runs.sqlite3",
        run_id="r",
        capabilities_provider=_provider(side),
        backend_factory=_factory(side, transports),
    )
    return _outcome(side, base, base / "ledger" / "runs.sqlite3", "r", snapshot, transports)


def _scenario_halt_then_unroutable(side: Any, base: Path) -> dict:
    """A quota halt stops the endpoint for the run; the next job finds no usable entry."""
    work = base / "work"
    work.mkdir(parents=True)
    jobs = _write_jobs(
        base / "jobs.json",
        [_job("a", work, command=_PASS), _job("b", work, command=_PASS)],
        workspace_root=str(base / "ws"),
    )
    transports = {"ep": Transport(side, [lambda s: s.HaltError("quota", "pool spent")])}
    snapshot = side.run.run_job_file(
        jobs,
        store_path=base / "ledger" / "runs.sqlite3",
        run_id="r",
        capabilities_provider=_provider(side),
        backend_factory=_factory(side, transports),
    )
    return _outcome(side, base, base / "ledger" / "runs.sqlite3", "r", snapshot, transports)


def _scenario_fallback_after_halt(side: Any, base: Path) -> dict:
    """The first declared entry halts; the job falls to the second on its next attempt."""
    work = base / "work"
    work.mkdir(parents=True)
    jobs = _write_jobs(
        base / "jobs.json",
        [_job("a", work, command=_PASS, models=["one", "two"], max_attempts=3)],
        workspace_root=str(base / "ws"),
    )
    transports = {
        "one": Transport(side, [lambda s: s.HaltError("rate_limit", "slow down")]),
        "two": Transport(side),
    }
    snapshot = side.run.run_job_file(
        jobs,
        store_path=base / "ledger" / "runs.sqlite3",
        run_id="r",
        capabilities_provider=_provider(side),
        backend_factory=_factory(side, transports),
    )
    return _outcome(side, base, base / "ledger" / "runs.sqlite3", "r", snapshot, transports)


def _scenario_unreachable_then_fallback(side: Any, base: Path) -> dict:
    """Two sequential unreachable attempts exclude the entry for the rest of the run."""
    work = base / "work"
    work.mkdir(parents=True)
    jobs = _write_jobs(
        base / "jobs.json",
        [
            _job("a", work, command=_PASS, models=["one"]),
            _job("b", work, command=_PASS, models=["one"]),
            _job("c", work, command=_PASS, models=["one", "two"]),
        ],
        workspace_root=str(base / "ws"),
    )
    transports = {
        "one": Transport(
            side,
            [RuntimeError("connection refused"), RuntimeError("connection refused")],
            label="unreachable",
        ),
        "two": Transport(side),
    }
    snapshot = side.run.run_job_file(
        jobs,
        store_path=base / "ledger" / "runs.sqlite3",
        run_id="r",
        capabilities_provider=_provider(side),
        backend_factory=_factory(side, transports),
    )
    return _outcome(side, base, base / "ledger" / "runs.sqlite3", "r", snapshot, transports)


def _scenario_timeout_then_accept(side: Any, base: Path) -> dict:
    work = base / "work"
    work.mkdir(parents=True)
    jobs = _write_jobs(
        base / "jobs.json",
        [_job("a", work, command=_PASS, max_attempts=2)],
        workspace_root=str(base / "ws"),
    )
    transports = {"ep": Transport(side, [lambda s: s.AgentTimeoutError("deadline")])}
    snapshot = side.run.run_job_file(
        jobs,
        store_path=base / "ledger" / "runs.sqlite3",
        run_id="r",
        capabilities_provider=_provider(side),
        backend_factory=_factory(side, transports),
    )
    return _outcome(side, base, base / "ledger" / "runs.sqlite3", "r", snapshot, transports)


def _scenario_no_such_endpoint(side: Any, base: Path) -> dict:
    work = base / "work"
    work.mkdir(parents=True)
    jobs = _write_jobs(
        base / "jobs.json",
        [_job("a", work, command=_PASS, models=["ghost"])],
        workspace_root=str(base / "ws"),
    )
    transports: dict[str, Transport] = {}
    snapshot = side.run.run_job_file(
        jobs,
        store_path=base / "ledger" / "runs.sqlite3",
        run_id="r",
        capabilities_provider=_provider(side),
        backend_factory=_factory(side, transports),
    )
    return _outcome(side, base, base / "ledger" / "runs.sqlite3", "r", snapshot, transports)


def _scenario_effort_undeliverable(side: Any, base: Path) -> dict:
    work = base / "work"
    work.mkdir(parents=True)
    jobs = _write_jobs(
        base / "jobs.json",
        [_job("a", work, command=_PASS, models=["ep"], model_efforts={"ep": "high"})],
        workspace_root=str(base / "ws"),
    )
    transports = {"ep": Transport(side)}
    snapshot = side.run.run_job_file(
        jobs,
        store_path=base / "ledger" / "runs.sqlite3",
        run_id="r",
        capabilities_provider=_provider(side),
        backend_factory=_factory(side, transports),
    )
    return _outcome(side, base, base / "ledger" / "runs.sqlite3", "r", snapshot, transports)


def _scenario_interrupt_resume(side: Any, base: Path) -> dict:
    """Run to a waiting job, resolve its interrupt, resume to acceptance."""
    work = base / "work"
    work.mkdir(parents=True)
    script = base / "contract.py"
    script.write_text(_INTERRUPTING, encoding="utf-8")
    log = base / "continuations.jsonl"
    jobs = _write_jobs(
        base / "jobs.json",
        [_job("release", work, command=[sys.executable, str(script), str(log)])],
        workspace_root=str(base / "ws"),
    )
    store_path = base / "ledger" / "runs.sqlite3"
    transports = {"ep": Transport(side)}
    kwargs = dict(capabilities_provider=_provider(side), backend_factory=_factory(side, transports))
    first = side.run.run_job_file(jobs, store_path=store_path, run_id="r", **kwargs)
    store = side.store.JobStore(store_path, create=False)
    waiting = exit_code(first)
    resolution = store.resolve_interrupt(
        "r", 1, decision="answer", input={"approved": True}, now=1_900_000_000.0
    )
    second = side.run.resume_run("r", store_path, **kwargs)
    result = _outcome(side, base, store_path, "r", second, transports)
    result["waiting_exit_code"] = waiting
    result["first_states"] = [job.state.value for job in first.jobs]
    result["resolution"] = Scrub(base).value(resolution.to_mapping())
    result["continuations"] = [
        Scrub(base).value(json.loads(line)) for line in log.read_text(encoding="utf-8").splitlines()
    ]
    return result


def _scenario_parallel(side: Any, base: Path) -> dict:
    """A pool of two over four jobs: per-job event sequences and end states agree."""
    work = base / "work"
    work.mkdir(parents=True)
    ids = ["a", "b", "c", "d"]
    jobs = _write_jobs(
        base / "jobs.json",
        [_job(job_id, work, command=_PASS) for job_id in ids],
        workspace_root=str(base / "ws"),
        max_parallel=2,
    )
    store_path = base / "ledger" / "runs.sqlite3"
    transports = {"ep": Transport(side)}
    snapshot = side.run.run_job_file(
        jobs,
        store_path=store_path,
        run_id="r",
        capabilities_provider=_provider(side),
        backend_factory=_factory(side, transports),
    )
    scrub = Scrub(base)
    store = side.store.JobStore(store_path, create=False)
    per_job: dict[str, list] = {}
    for event in store.list_events("r"):
        per_job.setdefault(str((event.get("identity") or {}).get("unit_id")), []).append(
            scrub.text(json.dumps({k: v for k, v in event.items() if k != "seq"}, sort_keys=True))
        )
    ledger = dump_ledger(store_path, scrub)
    return {
        "exit_code": exit_code(snapshot),
        "states": sorted((job.id, job.state.value) for job in snapshot.jobs),
        "per_job_events": per_job,
        "attempts": sorted((row["job_id"], row["attempt_no"], row["status"]) for row in ledger["attempts"]),
        "calls": len(transports["ep"].calls),
        "max_parallel": ledger["runs"][0]["max_parallel"],
    }


def _simple(
    side: Any,
    base: Path,
    jobs: list,
    transports: Mapping[str, Transport],
    *,
    top: Optional[Mapping[str, Any]] = None,
    deny: bool = False,
    with_capabilities: bool = False,
) -> dict:
    """Run one jobs document to the end on ``side`` and return its scrubbed outcome."""
    path = _write_jobs(base / "jobs.json", jobs, workspace_root=str(base / "ws"), **(top or {}))
    store_path = base / "ledger" / "runs.sqlite3"
    snapshot = side.run.run_job_file(
        path,
        store_path=store_path,
        run_id="r",
        capabilities_provider=_provider(side, deny=deny),
        backend_factory=_factory(side, transports, with_capabilities=with_capabilities),
    )
    return _outcome(side, base, store_path, "r", snapshot, transports)


def _workdir(base: Path) -> Path:
    work = base / "work"
    work.mkdir(parents=True)
    return work


def _scenario_deny_floor_honoured(side: Any, base: Path) -> dict:
    work = _workdir(base)
    transports = {"ep": Transport(side)}
    return _simple(
        side, base, [_job("a", work, command=_PASS, options={"allowed_tools": "Bash", "disallowed_tools": "Edit"})],
        transports, top={"disallowed_tools": "Bash"}, deny=True,
    )


def _scenario_deny_floor_unmet(side: Any, base: Path) -> dict:
    """The endpoint cannot honour the floor: it is not selected at all."""
    work = _workdir(base)
    transports = {"ep": Transport(side)}
    return _simple(side, base, [_job("a", work, command=_PASS)], transports, top={"disallowed_tools": "Bash"})


def _scenario_error_with_capabilities(side: Any, base: Path) -> dict:
    """A seam failure on a selection that carries capabilities records the params reports."""
    work = _workdir(base)
    transports = {"ep": Transport(side, [RuntimeError("boom"), None], label=None)}
    return _simple(
        side, base, [_job("a", work, command=_PASS, max_attempts=2, options={"max_tokens": 77, "temperature": 0.25})],
        transports, with_capabilities=True,
    )


def _scenario_non_git_isolation_degrades(side: Any, base: Path) -> dict:
    """Isolation is git-only: a plain directory runs in place and the attempt says so."""
    work = _workdir(base)
    transports = {"ep": Transport(side)}
    job = _job("a", work, command=_PASS, workspace={"isolate": True})
    return _simple(side, base, [job], transports)


def _scenario_parallel_halt_never_cancels(side: Any, base: Path) -> dict:
    """Three jobs in flight together: one halts, the other two still finish."""
    import threading

    work = _workdir(base)
    barrier = threading.Barrier(3, timeout=20)
    transport = Transport(side, [])
    transport.before_call = lambda user: barrier.wait()
    transport.by_user["b"] = [lambda s: s.HaltError("quota", "pool spent")]
    jobs = [_job(job_id, work, command=_PASS) for job_id in "abc"]
    path = _write_jobs(base / "jobs.json", jobs, workspace_root=str(base / "ws"), max_parallel=3)
    store_path = base / "ledger" / "runs.sqlite3"
    snapshot = side.run.run_job_file(
        path, store_path=store_path, run_id="r",
        capabilities_provider=_provider(side), backend_factory=_factory(side, {"ep": transport}),
    )
    ledger = dump_ledger(store_path, Scrub(base))
    return {
        "exit_code": exit_code(snapshot),
        "states": sorted((job.id, job.state.value) for job in snapshot.jobs),
        "attempts": sorted((r["job_id"], r["attempt_no"], r["status"], r["halt_kind"]) for r in ledger["attempts"]),
        "calls": sorted(call[1] for call in transport.calls),
    }


def _scenario_backpressure_wait(side: Any, base: Path) -> dict:
    """A transient overload is waited out (retry_after honoured) before it counts as a halt."""
    work = _workdir(base)
    sleeps: list[float] = []
    mp = pytest.MonkeyPatch()
    mp.setattr(
        side.run.BackpressurePolicy,
        "from_environment",
        staticmethod(lambda: side.run.BackpressurePolicy(cap_s=30.0, sleeper=sleeps.append, rng=lambda: 0.5)),
    )
    try:
        transports = {
            "ep": Transport(
                side,
                [
                    lambda s: s.HaltError("backpressure", "busy", retry_after_s=1.5),
                    lambda s: s.HaltError("backpressure", "busy"),
                ],
            )
        }
        result = _simple(side, base, [_job("a", work, command=_PASS, max_attempts=2)], transports)
    finally:
        mp.undo()
    result["sleeps"] = sleeps
    return result


SCENARIOS = {
    "deny_floor_honoured": _scenario_deny_floor_honoured,
    "deny_floor_unmet": _scenario_deny_floor_unmet,
    "error_with_capabilities": _scenario_error_with_capabilities,
    "non_git_isolation_degrades": _scenario_non_git_isolation_degrades,
    "parallel_halt_never_cancels": _scenario_parallel_halt_never_cancels,
    "backpressure_wait": _scenario_backpressure_wait,
    "accepted": _scenario_accepted,
    "retry_then_accept": _scenario_retry_then_accept,
    "rejected": _scenario_rejected,
    "halt_then_unroutable": _scenario_halt_then_unroutable,
    "fallback_after_halt": _scenario_fallback_after_halt,
    "unreachable_then_fallback": _scenario_unreachable_then_fallback,
    "timeout_then_accept": _scenario_timeout_then_accept,
    "no_such_endpoint": _scenario_no_such_endpoint,
    "effort_undeliverable": _scenario_effort_undeliverable,
    "interrupt_resume": _scenario_interrupt_resume,
    "parallel": _scenario_parallel,
}


def _both(sides: Any, tmp_path: Path, scenario: Callable[[Any, Path], Any]) -> tuple[Any, Any]:
    out = []
    for side in sides:
        base = tmp_path / side.name
        base.mkdir()
        out.append(scenario(side, base))
    return out[0], out[1]


class TestRunnerParity:
    @pytest.mark.parametrize("name", sorted(SCENARIOS))
    def test_scenario_outcomes_are_equal(self, sides, hermetic, tmp_path, name):
        legacy, final = _both(sides, tmp_path, SCENARIOS[name])
        assert first_difference(final, legacy) is None, first_difference(final, legacy)
        assert final == legacy

    def test_the_scenarios_exercise_the_states_they_claim(self, sides, hermetic, tmp_path):
        """The comparisons above are not vacuous: each scenario reaches its state."""
        seen = {}
        for name in SCENARIOS:
            side = sides[0]
            base = tmp_path / name
            base.mkdir()
            result = SCENARIOS[name](side, base)
            seen[name] = result
        assert seen["accepted"]["exit_code"] == 0
        assert seen["retry_then_accept"]["exit_code"] == 0
        assert len(seen["retry_then_accept"]["ledger"]["attempts"]) == 2
        assert seen["rejected"]["exit_code"] == 1
        states = {row["id"]: row["state"] for row in seen["halt_then_unroutable"]["ledger"]["jobs"]}
        assert states == {"a": "halted", "b": "unroutable"}
        assert seen["fallback_after_halt"]["exit_code"] == 0
        assert [a["endpoint"] for a in seen["fallback_after_halt"]["ledger"]["attempts"]] == ["one", "two"]
        states = {row["id"]: row["state"] for row in seen["unreachable_then_fallback"]["ledger"]["jobs"]}
        assert states == {"a": "halted", "b": "halted", "c": "accepted"}
        assert [a["status"] for a in seen["timeout_then_accept"]["ledger"]["attempts"]] == ["error", "completed"]
        assert seen["no_such_endpoint"]["exit_code"] == 1
        assert seen["effort_undeliverable"]["exit_code"] == 1
        assert seen["interrupt_resume"]["waiting_exit_code"] == 4
        assert seen["interrupt_resume"]["exit_code"] == 0
        assert seen["parallel"]["states"] == [(i, "accepted") for i in "abcd"]
        assert [a["endpoint"] for a in seen["unreachable_then_fallback"]["ledger"]["attempts"]] == ["one", "one", "two"]
        assert [c[3]["disallowed_tools"] for c in seen["deny_floor_honoured"]["calls"]["ep"]] == ["Bash Edit"]
        assert seen["deny_floor_unmet"]["exit_code"] == 1
        assert seen["deny_floor_unmet"]["calls"]["ep"] == []
        errored = seen["error_with_capabilities"]["ledger"]["attempts"][0]
        assert errored["status"] == "error" and errored["dropped_params_json"] is not None
        assert [a["workspace_status"] for a in seen["non_git_isolation_degrades"]["ledger"]["attempts"]] == ["none"]
        assert seen["parallel_halt_never_cancels"]["states"] == [("a", "accepted"), ("b", "halted"), ("c", "accepted")]
        assert seen["backpressure_wait"]["sleeps"][0] == 1.5 and len(seen["backpressure_wait"]["sleeps"]) == 2
        assert seen["backpressure_wait"]["exit_code"] == 0

    def test_a_quota_halt_writes_the_pinned_verdict_back_through_the_budget_policy(
        self, sides, hermetic, tmp_path, monkeypatch
    ):
        """A halted entry that declares conserve_usage is recorded OUT_OF_QUOTA for its pool."""
        writes: dict[str, list] = {}
        for side in sides:
            record: list = []
            writes[side.name] = record
            entry = types.SimpleNamespace(conserve_usage="spec-sentinel")
            # The runner holds the registry and the budget policy as module aliases; the
            # alias names differ between the two sides, the modules they point at do not.
            models_alias, usage_alias = (
                ("_lsk_models", "_lsk_usage_budget") if side.name == "legacy" else ("_models", "_usage_budget")
            )
            monkeypatch.setattr(
                side.run, models_alias, types.SimpleNamespace(discover_model_entries=lambda **_kw: {"ep": entry})
            )
            monkeypatch.setattr(
                side.run,
                usage_alias,
                types.SimpleNamespace(
                    record_observed_halt=lambda entry_id, spec, _r=record, **kw: _r.append(
                        (entry_id, spec, sorted(kw), kw.get("resets_at"), sorted(kw["entries"]))
                    )
                ),
            )
            base = tmp_path / side.name
            base.mkdir()
            work = _workdir(base)
            halt = lambda s: s.HaltError("quota", "pool spent")  # noqa: E731
            _simple(side, base, [_job("a", work, command=_PASS)], {"ep": Transport(side, [halt])})
        assert writes["legacy"] == writes["final"]
        assert writes["final"] == [("ep", "spec-sentinel", ["entries", "resets_at"], None, ["ep"])]

    def test_the_cli_exit_mapping_matches_the_legacy_cli(self, sides, hermetic, tmp_path):
        """``exit_code`` above is the CLI's mapping: pin it to the legacy CLI's own function."""
        legacy_cli = importlib.import_module("job_kit.cli")
        side = sides[0]
        real = side.run.run_job_file
        for name in ("accepted", "rejected", "halt_then_unroutable"):
            base = tmp_path / ("cli-" + name)
            base.mkdir()
            captured = {}

            def capture(*args, _real=real, _captured=captured, **kwargs):
                _captured["snapshot"] = _real(*args, **kwargs)
                return _captured["snapshot"]

            side.run.run_job_file = capture
            try:
                SCENARIOS[name](side, base)
            finally:
                side.run.run_job_file = real
            assert legacy_cli._exit_for_snapshot(captured["snapshot"]) == exit_code(captured["snapshot"])

    def test_event_jsonl_sink_bytes_are_equal(self, sides, hermetic, tmp_path):
        outputs = []
        for side in sides:
            base = tmp_path / side.name
            base.mkdir()
            _scenario_retry_then_accept(side, base)
            store = side.store.JobStore(base / "ledger" / "runs.sqlite3", create=False)
            out = base / "events.jsonl"
            sink = side.events.jsonl_sink(out)
            for event in store.list_events("r"):
                sink.write(event)
            outputs.append(Scrub(base).text(out.read_text(encoding="utf-8")))
        assert outputs[0] == outputs[1] and outputs[0]

    def test_unknown_run_and_duplicate_ids_fail_alike(self, sides, hermetic, tmp_path):
        results = []
        for side in sides:
            base = tmp_path / side.name
            base.mkdir()
            store_path = base / "ledger" / "runs.sqlite3"
            errors = []
            try:
                side.run.resume_run("missing", store_path)
            except Exception as exc:  # noqa: BLE001 - the type and text are the datum
                errors.append((type(exc).__name__, str(exc).replace(str(base), "<ROOT>")))
            work = base / "work"
            work.mkdir()
            doc = _write_jobs(
                base / "dup.json", [_job("a", work, command=_PASS), _job("a", work, command=_PASS)]
            )
            try:
                side.model.load_job_file(doc)
            except Exception as exc:  # noqa: BLE001
                errors.append((type(exc).__name__, str(exc).replace(str(base), "<ROOT>")))
            results.append(errors)
        assert results[0] == results[1]
        assert len(results[0]) == 2


# --------------------------------------------------------------------------
# Ledger bytes
# --------------------------------------------------------------------------


def _schema_dump(db: Path) -> dict:
    """The ``sqlite_master`` rows (rowid order), the version rows and the journal mode."""
    conn = sqlite3.connect(str(db))
    try:
        master = [
            list(row)
            for row in conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY rowid"
            )
        ]
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_version ORDER BY rowid")]
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    return {"sqlite_master": master, "schema_version": versions, "journal_mode": mode}


def _encode(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


def _at_prefix(side: Any, db: Path, steps: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """Create ``db`` as a ledger migrated through the first ``steps`` migrations only."""
    full = list(side.store._MIGRATIONS)
    monkeypatch.setattr(side.store, "_MIGRATIONS", full[:steps])
    try:
        side.store.JobStore(db)
    finally:
        monkeypatch.setattr(side.store, "_MIGRATIONS", full)


def build_schema_fixtures(side: Any, work: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    """The fixture files one side produces: fresh create, every prefix, every upgrade."""
    files: dict[str, bytes] = {}
    fresh = work / "fresh.sqlite3"
    side.store.JobStore(fresh)
    files["fresh.json"] = _encode(_schema_dump(fresh))
    steps = len(side.store._MIGRATIONS)
    for prefix in range(1, steps + 1):
        db = work / f"prefix-{prefix:02d}.sqlite3"
        _at_prefix(side, db, prefix, monkeypatch)
        files[f"prefix-{prefix:02d}.json"] = _encode(_schema_dump(db))
        side.store.JobStore(db)
        files[f"upgrade-from-{prefix:02d}.json"] = _encode(_schema_dump(db))
    return files


def _master_sql(dump: Mapping[str, Any], name: str) -> str:
    return next(row[3] for row in dump["sqlite_master"] if row[1] == name)


class TestLedgerSchema:
    def test_the_product_ddl_is_byte_identical_through_schema_twelve(self, sides):
        """Deliberate difference: llm-kit adds step 13 (``cancelled``); legacy stays at 12."""
        legacy, final = sides
        assert len(legacy.store._MIGRATIONS) == 12
        assert len(final.store._MIGRATIONS) == final.store.CANCEL_SCHEMA == 13
        assert final.store._MIGRATIONS[:12] == legacy.store._MIGRATIONS
        assert (final.store.READER_MIN_SCHEMA, final.store.INTERRUPT_SCHEMA) == (
            legacy.store.READER_MIN_SCHEMA,
            legacy.store.INTERRUPT_SCHEMA,
        ) == (10, 12)
        assert final.store.DEFAULT_BUSY_TIMEOUT_MS == legacy.store.DEFAULT_BUSY_TIMEOUT_MS

    def test_version_one_is_inserted_inside_the_first_migration(self, sides):
        """The migration loop skips the UPDATE for step one: the DDL inserts the row."""
        for side in sides:
            first = side.store._MIGRATIONS[0]
            assert first[-1] == "INSERT INTO schema_version(version) VALUES (1)"
            assert not any("UPDATE schema_version" in s for step in side.store._MIGRATIONS for s in step)

    def test_fixtures_are_byte_equal_through_schema_twelve(self):
        """Every prefix dump through 12 is shared; fresh and every upgrade end at 13 on llm-kit.

        Deliberate difference: ``fresh.json`` and ``upgrade-from-*.json`` differ
        (legacy ends at schema 12, llm-kit at 13), and llm-kit has the extra
        ``prefix-13`` and ``upgrade-from-13`` dumps.
        """
        legacy = sorted(p.name for p in (_FIXTURES / "legacy").glob("*.json"))
        final = sorted(p.name for p in (_FIXTURES / "llm-kit").glob("*.json"))
        assert len(legacy) == 1 + 12 + 12
        assert len(final) == 1 + 13 + 13
        assert set(final) - set(legacy) == {"prefix-13.json", "upgrade-from-13.json"}
        shared = [name for name in legacy if name.startswith("prefix-")]
        assert len(shared) == 12
        for name in shared:
            assert (_FIXTURES / "legacy" / name).read_bytes() == (_FIXTURES / "llm-kit" / name).read_bytes(), name
        for name in legacy:
            if not name.startswith("prefix-"):
                assert (_FIXTURES / "legacy" / name).read_bytes() != (_FIXTURES / "llm-kit" / name).read_bytes(), name

    def test_each_side_regenerates_its_own_fixtures_byte_for_byte(self, sides, tmp_path, monkeypatch):
        for side, directory in zip(sides, ("legacy", "llm-kit")):
            work = tmp_path / directory
            work.mkdir()
            produced = build_schema_fixtures(side, work, monkeypatch)
            committed = {p.name: p.read_bytes() for p in (_FIXTURES / directory).glob("*.json")}
            assert produced == committed, directory

    def test_fresh_create_ends_at_schema_thirteen_with_one_version_row(self):
        legacy = json.loads((_FIXTURES / "legacy" / "fresh.json").read_text(encoding="ascii"))
        assert legacy["schema_version"] == [12]
        fresh = json.loads((_FIXTURES / "llm-kit" / "fresh.json").read_text(encoding="ascii"))
        assert fresh["schema_version"] == [13]
        assert fresh["journal_mode"] == "wal"
        names = [row[1] for row in fresh["sqlite_master"] if row[0] == "table"]
        assert "schema_version" in names and "interrupts" in names and "events" in names

    def test_every_upgrade_converges_on_the_fresh_create_bytes(self):
        fresh = json.loads((_FIXTURES / "llm-kit" / "fresh.json").read_text(encoding="ascii"))
        for prefix in range(1, 14):
            upgraded = json.loads(
                (_FIXTURES / "llm-kit" / f"upgrade-from-{prefix:02d}.json").read_text(encoding="ascii")
            )
            assert upgraded["schema_version"] == [13]
            assert {row[1] for row in upgraded["sqlite_master"] if row[0] == "table"} == {
                row[1] for row in fresh["sqlite_master"] if row[0] == "table"
            }, prefix
            assert _master_sql(upgraded, "interrupt_resolutions") == _master_sql(
                fresh, "interrupt_resolutions"
            ), prefix
            assert "'cancelled'" in _master_sql(upgraded, "interrupt_resolutions"), prefix

    def test_statement_order_on_open_is_identical(self, sides, tmp_path, monkeypatch):
        """Pragma order: every statement a fresh open, a reopen and a write verb issue."""
        traces = []
        for side in sides:
            base = tmp_path / side.name
            base.mkdir()
            log: list[str] = []
            real_connect = sqlite3.connect

            def traced(*args: Any, _log: list = log, **kwargs: Any) -> sqlite3.Connection:
                conn = real_connect(*args, **kwargs)
                _log.append("-- connect")
                conn.set_trace_callback(_log.append)
                return conn

            monkeypatch.setattr(sqlite3, "connect", traced)
            try:
                store = side.store.JobStore(base / "runs.sqlite3")
                log.append("== reopen")
                side.store.JobStore(base / "runs.sqlite3", create=False)
                log.append("== read")
                store.list_run_ids()
                log.append("== reader")
                side.store.LedgerReader(base / "runs.sqlite3").schema_version()
            finally:
                monkeypatch.setattr(sqlite3, "connect", real_connect)
            traces.append([line.replace(str(base), "<ROOT>") for line in log])
        # Deliberate difference: llm-kit's extra migration step (schema 13) and
        # its version UPDATE are the only statements legacy does not issue.
        final_trace = traces[1]
        start = final_trace.index("DROP TRIGGER interrupts_one_open_per_job")
        stop = final_trace.index("UPDATE schema_version SET version = 13")
        step_13 = [" ".join(statement.split()) for statement in sides[1].store._MIGRATIONS[12]]
        assert [" ".join(line.split()) for line in final_trace[start:stop]] == step_13
        traces[1] = final_trace[:start] + final_trace[stop + 1 :]
        assert traces[0] == traces[1]
        text = "\n".join(traces[1])
        # The documented order: busy_timeout, then WAL, then foreign_keys, then BEGIN IMMEDIATE.
        assert text.index("PRAGMA busy_timeout") < text.index("journal_mode") < text.index(
            "PRAGMA foreign_keys = ON"
        ) < text.index("BEGIN IMMEDIATE")

    def test_a_newer_ledger_is_refused_before_anything_is_written(self, sides, tmp_path):
        outcomes = []
        for side in sides:
            base = tmp_path / side.name
            base.mkdir()
            db = base / "runs.sqlite3"
            side.store.JobStore(db)
            conn = sqlite3.connect(str(db))
            conn.execute(
                "UPDATE schema_version SET version = ?", (len(side.store._MIGRATIONS) + 1,)
            )
            conn.commit()
            conn.close()
            before = db.read_bytes()
            wal_before = sorted(p.name for p in base.iterdir())
            with pytest.raises(side.store.StoreError) as excinfo:
                side.store.JobStore(db)
            newest = len(side.store._MIGRATIONS)
            message = (
                str(excinfo.value)
                .replace(str(base), "<ROOT>")
                .replace(f"version {newest + 1} ", "version <NEXT> ")
                .replace(f"max {newest})", "max <NEWEST>)")
            )
            outcomes.append((to_final(message), db.read_bytes() == before, wal_before == sorted(p.name for p in base.iterdir())))
        # The version numbers differ by design (legacy 12, llm-kit 13); the refusal is the same.
        assert outcomes[0] == outcomes[1]
        assert outcomes[0][1] is True

    def test_legacy_job_kit_refuses_a_ledger_llm_kit_has_written_at_schema_thirteen(
        self, sides, tmp_path
    ):
        """The deliberate old-reader refusal: schema 13 is not readable by frozen job-kit."""
        legacy, final = sides
        db = tmp_path / "runs.sqlite3"
        final.store.JobStore(db)
        before = db.read_bytes()
        with pytest.raises(legacy.store.StoreError, match=r"schema version 13 is newer .*max 12"):
            legacy.store.JobStore(db, create=False)
        with pytest.raises(legacy.store.StoreError, match=r"schema version 13 is newer .*max 12"):
            legacy.store.LedgerReader(db).schema_version()
        assert db.read_bytes() == before

    def test_llm_kit_upgrades_a_schema_twelve_ledger_legacy_wrote(self, sides, tmp_path):
        legacy, final = sides
        db = tmp_path / "runs.sqlite3"
        legacy.store.JobStore(db)
        assert final.store.LedgerReader(db).schema_version() == 12
        final.store.JobStore(db, create=False)
        assert final.store.LedgerReader(db).schema_version() == 13

    def test_the_reader_refuses_a_ledger_older_than_its_floor(self, sides, tmp_path, monkeypatch):
        outcomes = []
        for side in sides:
            base = tmp_path / side.name
            base.mkdir()
            db = base / "runs.sqlite3"
            _at_prefix(side, db, 9, monkeypatch)
            with pytest.raises(side.store.StoreError) as excinfo:
                side.store.LedgerReader(db).schema_version()
            outcomes.append(str(excinfo.value))
        # the stale-ledger hint names `llm-kit jobs resume` (final_wording.RENAMES)
        assert to_final(outcomes[0]) == outcomes[1]

    def test_opening_a_missing_ledger_without_create_fails_alike(self, sides, tmp_path):
        outcomes = []
        for side in sides:
            base = tmp_path / side.name
            base.mkdir()
            with pytest.raises(side.store.StoreNotFoundError) as excinfo:
                side.store.JobStore(base / "absent.sqlite3", create=False)
            outcomes.append(str(excinfo.value).replace(str(base), "<ROOT>"))
        assert outcomes[0] == outcomes[1]

    def test_no_lock_lease_or_ownership_artifact_is_added(self, sides, tmp_path):
        """One process per ledger stays an unenforced rule: no lock file, no refusal."""
        listings = []
        for side in sides:
            base = tmp_path / side.name
            base.mkdir()
            db = base / "runs.sqlite3"
            first = side.store.JobStore(db)
            second = side.store.JobStore(db, create=False)
            assert first.list_run_ids() == second.list_run_ids() == []
            listings.append(sorted(p.name for p in base.iterdir()))
        assert listings[0] == listings[1]
        assert all(not name.endswith((".lock", ".lease", ".pid")) for name in listings[1])

    def test_the_job_posture_is_foreign_keys_on_and_wal_reraise(self, sides):
        final = sides[1]
        source = Path(final.store.__file__).read_text(encoding="utf-8")
        assert 'post_wal_pragmas=(("foreign_keys", "ON"),)' in source
        assert "wal_failure=_connection.WAL_RERAISE" in source
        assert "CREATE TABLE" in source  # product DDL stays in jobs, not in storage
        storage = (_LIB / "llm_kit" / "storage")
        assert not any("CREATE TABLE" in p.read_text(encoding="utf-8") for p in storage.glob("*.py"))


# --------------------------------------------------------------------------
# Cross-version state: either runner reads and continues what the other wrote
# --------------------------------------------------------------------------


class TestCrossVersionState:
    @pytest.mark.parametrize("writer,reader", [(0, 1), (1, 0)])
    def test_a_waiting_run_resumes_under_the_other_implementation(
        self, sides, hermetic, tmp_path, writer, reader
    ):
        """Rollback and handover: a run left waiting by one side finishes under the other."""
        results = []
        for first, second in ((sides[writer], sides[reader]), (sides[writer], sides[writer])):
            base = tmp_path / f"{first.name}-{second.name}-{len(results)}"
            base.mkdir()
            work = base / "work"
            work.mkdir()
            script = base / "contract.py"
            script.write_text(_INTERRUPTING, encoding="utf-8")
            log = base / "continuations.jsonl"
            jobs = _write_jobs(
                base / "jobs.json",
                [_job("release", work, command=[sys.executable, str(script), str(log)])],
                workspace_root=str(base / "ws"),
            )
            store_path = base / "ledger" / "runs.sqlite3"
            tr1 = {"ep": Transport(first)}
            snap = first.run.run_job_file(
                jobs,
                store_path=store_path,
                run_id="r",
                capabilities_provider=_provider(first),
                backend_factory=_factory(first, tr1),
            )
            assert exit_code(snap) == 4
            if first is sides[1] and second is sides[0]:
                # Rollback is the deliberate difference: llm-kit wrote a schema-13
                # ledger and legacy job-kit (schema 12) refuses it, loudly and
                # before it writes anything. The same-side run still finishes.
                before = store_path.read_bytes()
                with pytest.raises(second.store.StoreError, match=r"newer than this .*max 12"):
                    second.store.JobStore(store_path, create=False)
                with pytest.raises(second.store.StoreError, match=r"newer than this .*max 12"):
                    second.store.LedgerReader(store_path).snapshot("r")
                assert store_path.read_bytes() == before
                second = first
            store2 = second.store.JobStore(store_path, create=False)
            store2.resolve_interrupt("r", 1, decision="answer", input={"approved": True}, now=1_900_000_000.0)
            tr2 = {"ep": Transport(second)}
            done = second.run.resume_run(
                "r",
                store_path,
                capabilities_provider=_provider(second),
                backend_factory=_factory(second, tr2),
            )
            scrub = Scrub(base)
            results.append(
                {
                    "exit": exit_code(done),
                    "ledger": dump_ledger(store_path, scrub),
                    "events": [scrub.text(line) for line in render_events(store2, "r")],
                    "calls": (len(tr1["ep"].calls), len(tr2["ep"].calls)),
                }
            )
        crossed, same = results
        assert crossed == same
        assert crossed["exit"] == 0 and crossed["calls"] == (1, 0)


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------


class TestSelectionParity:
    def _job(self, side: Any, work: Path, **extra: Any) -> Any:
        doc = {"id": "j", "prompt": {"user": "x"}, "models": ["one", "two"], "directory": str(work),
               "contract": {"command": _PASS}, **extra}
        path = work / "job.json"
        path.write_text(json.dumps({"jobs": [doc]}), encoding="utf-8")
        return side.model.load_job_file(path).jobs[0]

    def _select(self, side: Any, work: Path, job: Any, **kwargs: Any) -> dict:
        transports = {"one": Transport(side), "two": Transport(side)}
        try:
            selection, readings = side.select.select_endpoint_with_readings(
                job,
                capabilities={"fake": side.Capabilities(adapter="fake")},
                backend_factory=_factory(side, transports),
                project_root=str(work),
                **kwargs,
            )
        except Exception as exc:  # noqa: BLE001 - type and text are the datum
            return {
                "error": type(exc).__mro__[0].__name__,
                "mro": [c.__name__ for c in type(exc).__mro__],
                "text": str(exc),
                "lines": getattr(exc, "dispositions_text", lambda: None)(),
            }
        return {"endpoint": selection.endpoint, "model": selection.model, "readings": [dict(r) for r in readings]}

    def test_first_declared_usable_entry_wins(self, sides, hermetic, tmp_path):
        outcomes = []
        for side in sides:
            work = tmp_path / side.name
            work.mkdir()
            outcomes.append(self._select(side, work, self._job(side, work)))
        assert outcomes[0] == outcomes[1]
        assert outcomes[0]["endpoint"] == "one"

    def test_halted_endpoints_are_skipped(self, sides, hermetic, tmp_path):
        outcomes = []
        for side in sides:
            work = tmp_path / side.name
            work.mkdir()
            outcomes.append(self._select(side, work, self._job(side, work), halted_endpoints=["one"]))
        assert outcomes[0] == outcomes[1]
        assert outcomes[0]["endpoint"] == "two"

    def test_the_floor_itemises_every_declared_entry(self, sides, hermetic, tmp_path):
        outcomes = []
        for side in sides:
            work = tmp_path / side.name
            work.mkdir()
            outcomes.append(
                self._select(side, work, self._job(side, work), halted_endpoints=["one", "two"])
            )
        assert outcomes[0] == outcomes[1]
        assert outcomes[0]["error"] == "NoCompatibleEndpointError"
        assert outcomes[0]["mro"][:2] == ["NoCompatibleEndpointError", "SelectionError"]

    def test_sidecar_jobs_rank_once_then_match_each_entry(self, sides, hermetic, tmp_path):
        outcomes = []
        for side in sides:
            work = tmp_path / side.name
            work.mkdir()
            job = self._job(
                side, work, model_requirements={"one": {"params": ["cwd"]}, "two": {}}
            )
            outcomes.append(self._select(side, work, job))
        assert outcomes[0] == outcomes[1]

    def test_requirements_match_and_choose_endpoint(self, sides, hermetic, tmp_path):
        outcomes = []
        for side in sides:
            work = tmp_path / side.name
            work.mkdir()
            caps = side.Capabilities(adapter="fake")
            outcomes.append(
                (
                    side.select.requirements_match(caps, {"params": []}),
                    side.select.requirements_match(caps, {"params": ["cwd"]}),
                    side.select.choose_endpoint(
                        self._job(side, work),
                        capabilities={"fake": caps},
                        backend_factory=_factory(side, {"one": Transport(side), "two": Transport(side)}),
                        project_root=str(work),
                    ),
                )
            )
        assert outcomes[0] == outcomes[1] == (True, False, "one")


# --------------------------------------------------------------------------
# Model, events, interrupts
# --------------------------------------------------------------------------


_JOB_DOCUMENTS = {
    "minimal": {"jobs": [{"id": "a", "prompt": {"user": "u"}, "models": ["x"], "contract": {"command": ["true"]}}]},
    "scalar_models": {"jobs": [{"id": "a", "prompt": {"user": "u"}, "models": "x", "contract": {"command": "echo hi"}}]},
    "sidecars": {
        "disallowed_tools": "write",
        "jobs": [
            {
                "id": "a",
                "prompt": {"system": "s", "user": "u"},
                "models": ["x", "y"],
                "model_efforts": {"x": "high", "y": "low"},
                "model_options": {"x": {"temperature": 0.1}, "y": {}},
                "options": {"max_tokens": 100},
                "max_attempts": 3,
                "contract": {"command": ["true"], "timeout_s": 5},
            }
        ],
    },
    "duplicate": {"jobs": [{"id": "a", "prompt": {"user": "u"}, "models": ["x"], "contract": {"command": ["t"]}}] * 2},
    "old_key": {"jobs": [{"id": "a", "prompt": {"user": "u"}, "endpoint": "x", "contract": {"command": ["t"]}}]},
    "empty_models": {"jobs": [{"id": "a", "prompt": {"user": "u"}, "models": [], "contract": {"command": ["t"]}}]},
    "bad_attempts": {"jobs": [{"id": "a", "prompt": {"user": "u"}, "models": ["x"], "max_attempts": 0, "contract": {"command": ["t"]}}]},
    "effort_and_option": {
        "jobs": [
            {
                "id": "a",
                "prompt": {"user": "u"},
                "models": ["x"],
                "model_efforts": {"x": "high"},
                "options": {"effort": "low"},
                "contract": {"command": ["t"]},
            }
        ]
    },
    "not_a_mapping": [],
    "max_parallel_zero": {"max_parallel": 0, "jobs": [{"id": "a", "prompt": {"user": "u"}, "models": ["x"], "contract": {"command": ["t"]}}]},
}


class TestModelParity:
    @pytest.mark.parametrize("name", sorted(_JOB_DOCUMENTS))
    def test_load_job_file_agrees(self, sides, tmp_path, name):
        outcomes = []
        for side in sides:
            base = tmp_path / side.name
            base.mkdir()
            path = base / "jobs.json"
            path.write_text(json.dumps(_JOB_DOCUMENTS[name]), encoding="utf-8")
            try:
                loaded = side.model.load_job_file(path)
            except Exception as exc:  # noqa: BLE001
                outcomes.append(("error", type(exc).__name__, str(exc).replace(str(base), "<ROOT>")))
                continue
            outcomes.append(
                (
                    "ok",
                    loaded.max_parallel,
                    loaded.disallowed_tools,
                    [
                        json.dumps(job.to_mapping(), sort_keys=True).replace(
                            json.dumps(str(base))[1:-1], "<ROOT>"
                        )
                        for job in loaded.jobs
                    ],
                )
            )
        assert outcomes[0] == outcomes[1]

    def test_the_error_documents_actually_error(self, sides, tmp_path):
        base = tmp_path
        for name in ("duplicate", "old_key", "empty_models", "bad_attempts", "effort_and_option", "max_parallel_zero"):
            path = base / f"{name}.json"
            path.write_text(json.dumps(_JOB_DOCUMENTS[name]), encoding="utf-8")
            with pytest.raises(Exception):
                sides[1].model.load_job_file(path)

    def test_validate_max_parallel_agrees(self, sides):
        outcomes = []
        for side in sides:
            row = []
            for value in (1, 4, 0, -1, True, 2.5, "3", None):
                try:
                    row.append(side.model.validate_max_parallel(value))
                except Exception as exc:  # noqa: BLE001
                    row.append((type(exc).__name__, str(exc)))
            outcomes.append(row)
        assert outcomes[0] == outcomes[1]


class TestEventsParity:
    def test_build_event_bytes_agree(self, sides):
        outcomes = []
        for side in sides:
            row = []
            for schema in (None, "bootstrap.execution-event/v1"):
                for payload in ({"state": "accepted"}, None):
                    try:
                        event = side.events.build_event(
                            seq=3, run_id="r", event="terminal", at="2026-09-01T00:00:00Z",
                            job_id="a", attempt_no=1, adapter="fake", model="m",
                            payload=payload, schema=schema,
                        )
                        row.append(json.dumps(event, sort_keys=True, ensure_ascii=True, separators=(",", ":")))
                    except Exception as exc:  # noqa: BLE001
                        row.append((type(exc).__name__, str(exc)))
            outcomes.append(row)
        assert outcomes[0] == outcomes[1]

    def test_event_constants_agree(self, sides):
        legacy, final = sides
        for name in ("PLUGIN", "SCHEMA_V1", "SCHEMA_V2", "REQUIRED_SCHEMAS"):
            assert getattr(final.events, name) == getattr(legacy.events, name), name
        assert final.events.PLUGIN == "job-kit"  # the frozen source literal


class TestInterruptsParity:
    _REQUEST = {
        "schema": "job-kit.interrupt-request/v1",
        "kind": "approval",
        "request_schema": {"type": "object", "required": ["approved"], "properties": {"approved": {"const": True}}},
        "payload": {"action": "push"},
    }

    def _call(self, fn: Callable[[], Any]) -> Any:
        try:
            return ("ok", repr(fn()))
        except Exception as exc:  # noqa: BLE001
            return ("error", type(exc).__name__, str(exc))

    def test_request_parsing_and_validation_agree(self, sides, tmp_path):
        outcomes = []
        for side in sides:
            interrupts = side.interrupts
            bad = dict(self._REQUEST, kind="")
            good_file, bad_file, junk_file = (
                tmp_path / f"{side.name}-{label}.json" for label in ("good", "bad", "junk")
            )
            good_file.write_text(json.dumps(self._REQUEST), encoding="utf-8")
            bad_file.write_text(json.dumps(bad), encoding="utf-8")
            junk_file.write_text("{not json", encoding="utf-8")
            row = [
                self._call(lambda: interrupts.parse_request(good_file)),
                self._call(lambda: interrupts.parse_request(bad_file)),
                self._call(lambda: interrupts.parse_request(junk_file)),
                self._call(lambda: interrupts.validate_input(self._REQUEST["request_schema"], {"approved": True})),
                self._call(lambda: interrupts.validate_input(self._REQUEST["request_schema"], {"approved": False})),
                self._call(lambda: interrupts.canonical_json({"b": 1, "a": [1, 2]})),
                self._call(lambda: interrupts.canonical_json({"a": float("nan")})),
                self._call(lambda: interrupts.decision_outcome("answer", input={"approved": True}, reason=None)),
                self._call(lambda: interrupts.decision_outcome("reject", input=None, reason="no")),
                self._call(lambda: interrupts.decision_outcome("maybe", input=None, reason=None)),
                self._call(lambda: interrupts.bound_reason("x" * 5000)),
                self._call(lambda: interrupts.expiry(1000.0, 60)),
            ]
            outcomes.append(row)
        assert outcomes[0] == outcomes[1]
        assert outcomes[0][0][0] == "ok"

    def test_the_resolution_document_bytes_agree(self, sides, hermetic, tmp_path):
        documents = []
        for side in sides:
            base = tmp_path / side.name
            base.mkdir()
            out = _scenario_interrupt_resume(side, base)
            documents.append(out["continuations"])
        assert documents[0] == documents[1] and documents[0]

    def test_envelope_constants_agree(self, sides):
        legacy, final = sides
        for name in ("REQUEST_ENVELOPE_V1", "RESOLUTION_ENVELOPE_V1", "REQUEST_ENVELOPES"):
            assert getattr(final.interrupts, name) == getattr(legacy.interrupts, name), name

    def test_unusable_validator_wording_agrees(self, sides, monkeypatch, tmp_path):
        """A too-old validator is refused with the same words on both sides."""
        outcomes = []
        for side, module_name in zip(sides, ("llm_scripting_kit.completion.json_schema", "llm_kit.schema.json_schema")):
            stub = types.ModuleType(module_name)
            stub.SUPPORTED_SUBSETS = frozenset()
            monkeypatch.setitem(sys.modules, module_name, stub)
            try:
                request = tmp_path / f"{side.name}.json"
                request.write_text(json.dumps(self._REQUEST), encoding="utf-8")
                with pytest.raises(side.interrupts.JsonSchemaSupportError) as excinfo:
                    side.interrupts.parse_request(request)
                outcomes.append(str(excinfo.value))
            finally:
                monkeypatch.undo()
        # named deliberate wording and floor differences (final_wording)
        assert floors_in_text_to_final(to_final(outcomes[0])) == outcomes[1]


# --------------------------------------------------------------------------
# Workspaces (a scratch git repository)
# --------------------------------------------------------------------------


_GIT_ENV = {
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
    ).stdout.strip()


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "f.txt").write_text("one\n", encoding="utf-8")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _force_remove(func: Callable[..., Any], path: str, _exc: object) -> None:
    """``shutil.rmtree`` handler: git object files are read-only on Windows."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


class TestWorkspaceParity:
    def _isolated_job(self, side: Any, repo: Path, base: Path) -> Any:
        doc = {
            "jobs": [
                {
                    "id": "iso",
                    "prompt": {"user": "x"},
                    "models": ["ep"],
                    "directory": str(repo),
                    "workspace": {"isolate": True},
                    "contract": {"command": _PASS, "directory": str(repo)},
                }
            ]
        }
        path = base / "jobs.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return side.model.load_job_file(path)

    def test_an_isolated_job_runs_in_a_worktree_and_the_ledgers_agree(
        self, sides, hermetic, tmp_path, monkeypatch
    ):
        # A short base, HOME included (the default workspace root lives under it):
        # under xdist, pytest's popen-gwN tmp_path pushes the worktree paths past
        # the Windows path limit for ``git worktree add``.
        short = Path(tempfile.mkdtemp(prefix="lk"))
        try:
            home = short / "h"
            home.mkdir()
            monkeypatch.setenv("HOME", str(home))
            monkeypatch.setenv("USERPROFILE", str(home))
            outcomes = []
            for side in sides:
                base = short / side.name[0]
                base.mkdir()
                repo = _make_repo(base / "repo")
                self._isolated_job(side, repo, base)
                transports = {"ep": Transport(side)}
                store_path = base / "ledger" / "runs.sqlite3"
                snap = side.run.run_job_file(
                    base / "jobs.json",
                    store_path=store_path,
                    run_id="r",
                    capabilities_provider=_provider(side),
                    backend_factory=_factory(side, transports),
                )
                outcome = _outcome(side, base, store_path, "r", snap, transports)
                outcome["head"] = _git(repo, "rev-parse", "--short=0", "HEAD") and "head"
                outcome["worktrees"] = len(_git(repo, "worktree", "list").splitlines())
                outcomes.append(outcome)
            assert outcomes[0] == outcomes[1]
            assert outcomes[0]["exit_code"] == 0, [j["terminal_reason"] if "terminal_reason" in j else j for j in outcomes[0]["ledger"]["jobs"]]
        finally:
            shutil.rmtree(short, onerror=_force_remove)

    def test_workspace_gc_reports_agree(self, sides, hermetic, tmp_path):
        outcomes = []
        for side in sides:
            base = tmp_path / side.name
            base.mkdir()
            repo = _make_repo(base / "repo")
            self._isolated_job(side, repo, base)
            transports = {"ep": Transport(side)}
            store_path = base / "ledger" / "runs.sqlite3"
            side.run.run_job_file(
                base / "jobs.json",
                store_path=store_path,
                run_id="r",
                capabilities_provider=_provider(side),
                backend_factory=_factory(side, transports),
            )
            store = side.store.JobStore(store_path, create=False)
            report = side.workspace.gc_workspaces(store, "r", accepted_only=True, force=False)
            scrub = Scrub(base)
            outcomes.append(
                {
                    "report": scrub.value(report.to_mapping()),
                    "ledger": dump_ledger(store_path, scrub),
                    "listing": sorted(p.name for p in (base / "repo").iterdir() if p.name != ".git"),
                    "worktrees": len(_git(repo, "worktree", "list").splitlines()),
                }
            )
        assert first_difference(outcomes[0], outcomes[1]) is None, first_difference(outcomes[0], outcomes[1])

    def test_detect_repo_and_base_ref(self, sides, tmp_path):
        outcomes = []
        for side in sides:
            base = tmp_path / side.name
            base.mkdir()
            repo = _make_repo(base / "repo")
            plain = base / "plain"
            plain.mkdir()
            row = []
            for target in (repo, plain, base / "missing"):
                try:
                    root = side.workspace.detect_repo_root(target)
                    row.append(("root", str(root).replace(str(base.resolve()), "<ROOT>")))
                except Exception as exc:  # noqa: BLE001
                    row.append((type(exc).__name__, str(exc).replace(str(base.resolve()), "<ROOT>").replace(str(base), "<ROOT>")))
            try:
                row.append(("ref", len(side.workspace.resolve_base_ref(repo, None))))
            except Exception as exc:  # noqa: BLE001
                row.append((type(exc).__name__, str(exc)))
            outcomes.append(row)
        assert outcomes[0] == outcomes[1]

    def test_the_two_fixed_git_time_budgets_are_unchanged(self, sides):
        legacy, final = sides
        assert (final.workspace.GIT_PROBE_TIMEOUT_S, final.workspace.GIT_TREE_TIMEOUT_S) == (
            legacy.workspace.GIT_PROBE_TIMEOUT_S,
            legacy.workspace.GIT_TREE_TIMEOUT_S,
        ) == (30.0, 900.0)

    def test_the_workspace_module_is_the_legacy_source_but_for_the_cancelled_disposition(self, sides):
        """Deliberate difference: ``gc`` also reclaims a reservation's worktree after a cancel.

        The one changed line adds the ``cancelled`` reservation disposition (ledger
        schema 13) to the dispositions whose worktree ``gc`` may reclaim.
        """
        legacy, final = sides
        legacy_source = Path(legacy.workspace.__file__).read_text(encoding="utf-8")
        old = '                    "pre_invoke_failure",\n                }\n'
        new = '                    "pre_invoke_failure",\n                    "cancelled",\n                }\n'
        assert legacy_source.count(old) == 1
        assert Path(final.workspace.__file__).read_text(encoding="utf-8") == legacy_source.replace(old, new)


# --------------------------------------------------------------------------
# The injected-factory seam: spec factory + EndpointResolver vs create_backend
# --------------------------------------------------------------------------
#
# Both sides resolve endpoints against the same fake registry and build only
# fake backends. Legacy runs its default ``create_backend``; final runs jobs'
# default path, an injected ``ConfiguredBackendFactory`` (as BackendFactory and
# CapabilityProvider) behind ``dispatch.EndpointResolver``. Constructor calls
# are counted per (family, endpoint).

_SEAM_CONFIG = {
    "endpoints": {
        "h-claude": {"harness": "claude", "model": "m-claude", "effort": "high"},
        "h-codex": {"harness": "codex", "model": "m-codex"},
    }
}
_SEAM_RESOLVED = {
    "t-effort": {
        "name": "t-effort",
        "base_url": "http://x.invalid/v1",
        "request_defaults": {"reasoning_effort": "medium"},
        "effort_style": "top-level",
        "effort_style_source": "endpoint",
    },
    "t-plain": {"name": "t-plain", "base_url": "http://x.invalid/v1", "request_defaults": {}},
}
_SEAM_MODELS = ["t-plain", "t-effort", "h-claude"]


def _install_seam_world(side: Any, mp: pytest.MonkeyPatch, outcomes: Mapping[str, list]) -> tuple:
    """Patch ``side``'s registry and constructors; return (factory, counts, transports).

    ``factory`` is None for legacy (its default ``create_backend`` is the subject)
    and a ``ConfiguredBackendFactory`` over the same fake constructors for final.
    """
    from collections import Counter

    counts: Counter = Counter()
    transports: dict[str, Transport] = {}

    def resolve_endpoint(name: str, **_kw: object) -> dict:
        if name not in _SEAM_RESOLVED:
            raise side.models.EndpointResolveError(f"no endpoint {name!r}")
        return dict(_SEAM_RESOLVED[name])

    def make_ctor(label: str, name: str, capabilities: Any) -> Callable[..., Transport]:
        transport = Transport(side, outcomes.get(label))
        transport.name = name  # type: ignore[misc]
        if capabilities is not None:
            transport.capabilities = capabilities  # type: ignore[attr-defined]
        transports[label] = transport

        def ctor(**kwargs: object) -> Transport:
            counts[(label, kwargs.get("endpoint"))] += 1
            return transport

        return ctor

    entry_module = importlib.import_module(
        "llm_scripting_kit.model_endpoints" if side.name == "legacy" else "llm_kit.registry.model_endpoints"
    )
    entry = entry_module.EndpointEntry
    entries = {
        "t-effort": entry(
            id="t-effort", base_url="http://x.invalid/v1", model="t-effort-model",
            reasoning_effort="medium", effort_style="top-level", effort_style_declared=True,
        ),
        "t-plain": entry(id="t-plain", base_url="http://x.invalid/v1", model="t-plain-model"),
        "h-claude": entry(
            id="h-claude", base_url=None, model="m-claude", kind="harness", harness="claude", effort="high",
        ),
        "h-codex": entry(id="h-codex", base_url=None, model="m-codex", kind="harness", harness="codex"),
    }
    # describe() reads the merged registry through registry.models and specializes a
    # transport entry's capability record from its own profile.
    mp.setattr(side.models, "discover_model_entries", lambda **_kw: dict(entries))
    shared = {
        "load_model_config": lambda project_root=None: _SEAM_CONFIG,
        "default_endpoint_name": lambda cfg: "h-claude",
        "discover_model_entries": lambda **_kw: dict(entries),
        "resolve_endpoint": resolve_endpoint,
        "resolve_model": lambda model, *, cheap, config, endpoint, project_root: model or f"{endpoint}-model",
    }
    if side.name == "legacy":
        factory_mod = importlib.import_module("llm_scripting_kit.completion.factory")
        modules = [factory_mod]
        real_claude, real_codex = factory_mod.ClaudeCliBackend, factory_mod.CodexCliBackend
    else:
        from llm_kit.adapters.backends import ClaudeCliBackend as real_claude
        from llm_kit.adapters.codex_backend import CodexCliBackend as real_codex

        modules = [
            importlib.import_module("llm_kit.dispatch.factory"),
            importlib.import_module("llm_kit.dispatch.endpoint_resolver"),
        ]
    ctor_claude = make_ctor("claude", "claude-cli", real_claude.capabilities)
    ctor_codex = make_ctor("codex", "codex-cli", real_codex.capabilities)
    ctor_or = make_ctor("openrouter", "openrouter", None)

    patched = set()
    for module in modules:
        for attr, value in shared.items():
            if hasattr(module, attr):
                mp.setattr(module, attr, value)
                patched.add(attr)
    assert patched == set(shared), patched
    if side.name == "legacy":
        mp.setattr(factory_mod, "ClaudeCliBackend", lambda: ctor_claude())
        mp.setattr(factory_mod, "CodexCliBackend", lambda: ctor_codex())
        mp.setattr(factory_mod, "OpenRouterBackend", lambda **kw: ctor_or(**kw))
        return None, counts, transports
    from llm_kit.dispatch.pipeline_backends import ConfiguredBackendFactory

    factory = ConfiguredBackendFactory(
        classes={"claude-cli": ctor_claude, "codex-cli": ctor_codex, "openrouter": ctor_or}
    )
    return factory, counts, transports


def _counted(counts: Mapping[tuple, int]) -> dict[str, int]:
    return {f"{label}:{endpoint}": n for (label, endpoint), n in sorted(counts.items(), key=str)}


def _halt(kind: str, message: str) -> Callable[[Any], Any]:
    return lambda s: s.HaltError(kind, message)


#: Each spec: the job ids, models, extra job keys, and per-family outcomes.
SEAM_RUNS: dict[str, dict] = {
    "declared_order_harness_first": {"models": ["h-claude", "t-effort"]},
    "transport_effort_delivered": {"models": ["t-effort"], "extra": {"model_efforts": {"t-effort": "high"}}},
    "effort_undeliverable_on_plain_transport": {
        "models": ["t-plain"], "extra": {"model_efforts": {"t-plain": "high"}},
    },
    "capability_matching_per_endpoint": {
        "models": _SEAM_MODELS, "extra": {"requirements": {"params": ["effort"]}},
    },
    "requirement_refusal_floor": {
        "models": ["t-plain", "t-effort"], "extra": {"requirements": {"params": ["cwd"]}},
    },
    "sidecar_requirements": {
        "models": ["t-plain", "t-effort"],
        "extra": {"model_requirements": {"t-plain": {"params": ["effort"]}, "t-effort": {}}},
    },
    "unresolved_entry_is_skipped": {"models": ["nope", "t-plain"]},
    "halt_narrowing": {
        "models": ["h-claude", "h-codex", "t-effort"],
        "extra": {"max_attempts": 3},
        "outcomes": {
            "claude": [_halt("rate_limit", "slow down")],
            "codex": [_halt("quota", "pool spent")],
        },
    },
    "quota_halt_then_next_job_unroutable": {
        "models": ["h-claude"], "jobs": ["a", "b"],
        "outcomes": {"claude": [_halt("quota", "pool spent")]},
    },
}


def _seam_run(side: Any, base: Path, spec: Mapping[str, Any]) -> dict:
    mp = pytest.MonkeyPatch()
    try:
        work = _workdir(base)
        factory, counts, transports = _install_seam_world(side, mp, spec.get("outcomes", {}))
        jobs = [
            _job(job_id, work, command=_PASS, models=spec["models"], **spec.get("extra", {}))
            for job_id in spec.get("jobs", ["a"])
        ]
        path = _write_jobs(base / "jobs.json", jobs, workspace_root=str(base / "ws"))
        store_path = base / "ledger" / "runs.sqlite3"
        kwargs = {} if factory is None else {"backend_factory": factory}
        snapshot = side.run.run_job_file(path, store_path=store_path, run_id="r", **kwargs)
        outcome = _outcome(side, base, store_path, "r", snapshot, transports)
        outcome["constructions"] = _counted(counts)
        return outcome
    finally:
        mp.undo()


SEAM_SELECTIONS: dict[str, dict] = {
    "plain": {},
    "requirement_effort": {"doc": {"requirements": {"params": ["effort"]}}},
    "requirement_refused": {"doc": {"requirements": {"params": ["cwd"]}}},
    "requirement_cwd_harness_only": {
        "doc": {"requirements": {"params": ["cwd"]}}, "models": ["t-plain", "t-effort", "h-claude"],
    },
    "sidecar_requirements": {
        "doc": {"model_requirements": {"t-plain": {"params": ["effort"]}, "t-effort": {}, "h-claude": {}}},
    },
    "sidecar_efforts": {"doc": {"model_efforts": {"t-plain": "low", "t-effort": "high", "h-claude": "low"}}},
    "halted_first": {"kwargs": {"halted_endpoints": ["t-plain"]}},
    "all_halted": {"kwargs": {"halted_endpoints": ["t-plain", "t-effort", "h-claude"]}},
    "unresolved_first": {"models": ["nope", "h-claude"]},
}


def _seam_select(side: Any, base: Path, spec: Mapping[str, Any]) -> dict:
    mp = pytest.MonkeyPatch()
    try:
        work = _workdir(base)
        factory, counts, _ = _install_seam_world(side, mp, {})
        doc = {
            "id": "j", "prompt": {"user": "x"}, "models": spec.get("models", _SEAM_MODELS),
            "directory": str(work), "contract": {"command": _PASS}, **spec.get("doc", {}),
        }
        path = work / "job.json"
        path.write_text(json.dumps({"jobs": [doc]}), encoding="utf-8")
        job = side.model.load_job_file(path).jobs[0]
        kwargs = dict(spec.get("kwargs", {}))
        if factory is not None:
            kwargs["backend_factory"] = factory
        try:
            selection, readings = side.select.select_endpoint_with_readings(
                job, project_root=str(work), **kwargs
            )
        except Exception as exc:  # noqa: BLE001 - type and text are the datum
            outcome: dict = {
                "error": type(exc).__name__,
                "text": Scrub(base).text(str(exc)),
                "lines": getattr(exc, "dispositions_text", lambda: None)(),
            }
        else:
            outcome = {
                "endpoint": selection.endpoint,
                "kind": selection.kind,
                "backend": selection.backend.name,
                "model": selection.model,
                "effort": selection.effort,
                "capabilities": (
                    None if selection.capabilities is None
                    else max_tokens_deep(  # declared: llm-kit's openrouter max_tokens default is unset
                        json.loads(json.dumps(selection.capabilities.to_json(), sort_keys=True, default=str))
                    )
                ),
                "readings": [dict(r) for r in readings],
            }
        return {"outcome": outcome, "constructions": _counted(counts)}
    finally:
        mp.undo()


class TestFactorySeamParity:
    """The injected BackendFactory path agrees with legacy create_backend selection."""

    @pytest.mark.parametrize("name", sorted(SEAM_RUNS))
    def test_runs_agree_on_endpoint_model_effort_attempts_and_pace(self, sides, hermetic, tmp_path, name):
        legacy, final = _both(sides, tmp_path, lambda side, base: _seam_run(side, base, SEAM_RUNS[name]))
        legacy_counts, final_counts = legacy.pop("constructions"), final.pop("constructions")
        assert first_difference(final, legacy) is None, first_difference(final, legacy)
        assert set(final_counts) == set(legacy_counts)
        assert all(final_counts[key] <= legacy_counts[key] for key in final_counts)

    def test_the_runs_exercise_what_they_claim(self, sides, hermetic, tmp_path):
        seen = {}
        for name, spec in SEAM_RUNS.items():
            base = tmp_path / name
            base.mkdir()
            seen[name] = _seam_run(sides[1], base, spec)

        def attempts(name: str) -> list:
            return seen[name]["ledger"]["attempts"]

        assert [(a["endpoint"], a["backend"]) for a in attempts("declared_order_harness_first")] == [
            ("h-claude", "claude-cli")
        ]
        delivered = seen["transport_effort_delivered"]
        assert delivered["exit_code"] == 0
        assert [a["endpoint"] for a in attempts("transport_effort_delivered")] == ["t-effort"]
        assert delivered["calls"]["openrouter"][0][3]["effort"] == "high"
        assert seen["effort_undeliverable_on_plain_transport"]["exit_code"] == 1
        assert attempts("effort_undeliverable_on_plain_transport") == []
        assert [a["endpoint"] for a in attempts("capability_matching_per_endpoint")] == ["t-effort"]
        floor = seen["requirement_refusal_floor"]
        assert floor["exit_code"] == 1 and attempts("requirement_refusal_floor") == []
        assert [row["state"] for row in floor["ledger"]["jobs"]] == ["unroutable"]
        assert [a["endpoint"] for a in attempts("sidecar_requirements")] == ["t-effort"]
        assert [a["endpoint"] for a in attempts("unresolved_entry_is_skipped")] == ["t-plain"]
        narrowed = attempts("halt_narrowing")
        assert [(a["endpoint"], a["halt_kind"]) for a in narrowed] == [
            ("h-claude", "rate_limit"), ("h-codex", "quota"), ("t-effort", None)
        ]
        states = {row["id"]: row["state"] for row in seen["quota_halt_then_next_job_unroutable"]["ledger"]["jobs"]}
        assert states == {"a": "halted", "b": "unroutable"}
        for name in seen:
            for attempt in attempts(name):
                assert attempt["pace_readings_json"], (name, attempt)

    @pytest.mark.parametrize("name", sorted(SEAM_SELECTIONS))
    def test_selections_agree_and_each_candidate_is_built_at_most_once(self, sides, hermetic, tmp_path, name):
        legacy, final = _both(sides, tmp_path, lambda side, base: _seam_select(side, base, SEAM_SELECTIONS[name]))
        legacy_outcome = _in_final_wording(legacy["outcome"])
        final_outcome = _in_final_wording(final["outcome"])  # drops the final-only call_modes
        assert first_difference(final_outcome, legacy_outcome) is None, first_difference(
            final_outcome, legacy_outcome
        )
        assert set(final["constructions"]) == set(legacy["constructions"])
        assert all(n == 1 for n in final["constructions"].values()), final["constructions"]
        assert all(final["constructions"][k] <= n for k, n in legacy["constructions"].items())

    def test_the_selections_exercise_what_they_claim(self, sides, hermetic, tmp_path):
        seen = {}
        for name, spec in SEAM_SELECTIONS.items():
            base = tmp_path / name
            base.mkdir()
            seen[name] = _seam_select(sides[1], base, spec)
        outcome = {name: result["outcome"] for name, result in seen.items()}
        assert outcome["plain"]["endpoint"] == "t-plain"
        assert outcome["plain"]["effort"] is None and outcome["plain"]["model"] == "t-plain-model"
        assert outcome["requirement_effort"]["endpoint"] == "t-effort"
        assert outcome["requirement_effort"]["effort"] == "medium"
        assert outcome["requirement_refused"]["endpoint"] == "h-claude"
        assert outcome["requirement_refused"]["model"] == "m-claude"
        assert outcome["requirement_refused"]["effort"] == "high"
        assert outcome["sidecar_requirements"]["endpoint"] == "t-effort"
        assert outcome["halted_first"]["endpoint"] == "t-effort"
        assert outcome["all_halted"]["error"] == "NoCompatibleEndpointError"
        assert outcome["unresolved_first"]["endpoint"] == "h-claude"
        # The transport that delivers effort advertises it; the plain one does not.
        by_params = {
            name: "effort" in (outcome[name]["capabilities"] or {}).get("params", {})
            for name in ("plain", "requirement_effort")
        }
        assert by_params == {"plain": False, "requirement_effort": True}

    def test_legacy_builds_a_sidecar_candidate_twice_and_final_does_not(self, sides, hermetic, tmp_path):
        legacy, final = _both(
            sides, tmp_path, lambda side, base: _seam_select(side, base, SEAM_SELECTIONS["sidecar_requirements"])
        )
        assert max(legacy["constructions"].values()) == 2
        assert max(final["constructions"].values()) == 1


# --------------------------------------------------------------------------
# Deliberate differences and boundaries
# --------------------------------------------------------------------------


class TestDeliberateDifferences:
    def test_the_cancelled_state_and_schema_thirteen_exist_only_on_llm_kit(self, sides):
        legacy, final = sides
        assert "CANCELLED" not in legacy.model.JobState.__members__
        assert final.model.JobState.CANCELLED.value == "cancelled"
        assert final.model.JobState.CANCELLED in final.model.TERMINAL_STATES
        assert not hasattr(legacy.store.JobStore, "mark_cancelled")
        assert hasattr(final.store.JobStore, "mark_cancelled")
        assert not hasattr(legacy.store, "CANCEL_SCHEMA")
        assert (len(legacy.store._MIGRATIONS), len(final.store._MIGRATIONS)) == (12, 13)

    def test_the_version_skew_probes_are_gone(self, sides):
        legacy, final = sides
        assert hasattr(legacy.select, "SharedLibTooOldError")
        assert not hasattr(final.select, "SharedLibTooOldError")
        assert not hasattr(final.package, "SharedLibTooOldError")
        assert "SharedLibTooOldError" not in final.package.__all__
        # The rest of the public surface is the legacy surface.
        assert set(legacy.package.__all__) - set(final.package.__all__) == {"SharedLibTooOldError"}
        assert set(final.package.__all__) <= set(legacy.package.__all__)

    def test_jobs_imports_no_adapter_and_no_old_package(self):
        import ast

        for path in sorted((_LIB / "llm_kit" / "jobs").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    names = [node.module]
                for name in names:
                    assert not name.startswith("llm_kit.adapters"), (path.name, node.lineno, name)
                    assert not name.startswith("llm_kit._copy"), (path.name, node.lineno, name)
                    assert name.split(".")[0] not in ("llm_scripting_kit", "job_kit", "content_pipeline"), (
                        path.name, node.lineno, name)

    def test_the_default_capability_provider_is_the_configured_factory(self, sides, monkeypatch):
        legacy, final = sides
        from llm_kit.dispatch.pipeline_backends import ConfiguredBackendFactory

        assert final.select.adapter_capabilities() == ConfiguredBackendFactory().capability_records()
        assert set(final.select.adapter_capabilities()) == set(legacy.select.adapter_capabilities())
        # The legacy default factory is the selection-style create_backend; the final
        # default is the injected ConfiguredBackendFactory behind an EndpointResolver.
        assert legacy.select.create_backend.__module__.endswith("completion.factory")
        assert not hasattr(final.select, "create_backend")
        from llm_kit.dispatch.endpoint_resolver import EndpointResolver

        assert isinstance(final.select.selection_callable(None), EndpointResolver)

    def test_the_source_literal_job_kit_is_frozen(self, sides):
        legacy, final = sides
        assert final.events.PLUGIN == legacy.events.PLUGIN == "job-kit"
        assert final.package.__version__ == legacy.package.__version__

    def test_an_unprovisioned_import_failure_names_bootstrap(self, tmp_path):
        """The package guard: an import failure in an unprovisioned install exits 3."""
        home = tmp_path / "bare-home"
        home.mkdir()
        probe = textwrap.dedent(
            """
            import sys
            class Block:
                def find_spec(self, name, path=None, target=None):
                    if name == "llm_kit.dispatch.declaration":
                        raise ModuleNotFoundError("blocked", name="yaml")
                    return None
            sys.meta_path.insert(0, Block())
            import llm_kit.jobs
            """
        )
        env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), PYTHONPATH=str(_LIB))
        env.pop("CLAUDE_BOOTSTRAP_DATA_ROOT", None)
        result = subprocess.run([sys.executable, "-I", "-c", probe], env=env, capture_output=True, text=True, cwd=str(tmp_path))
        # -I ignores PYTHONPATH; re-run without it, from a neutral directory.
        result = subprocess.run(
            [sys.executable, "-c", f"import sys; sys.path.insert(0, {str(_LIB)!r})\n" + probe],
            env=env, capture_output=True, text=True, cwd=str(tmp_path),
        )
        assert result.returncode == 3, result.stderr
        assert "[llm-kit]" in result.stderr and "missing: yaml" in result.stderr

    def test_a_provisioned_import_failure_propagates(self, tmp_path):
        home = tmp_path / "provisioned-home"
        log = home / ".claude" / "plugins" / "data" / "plugins-kit" / "llm-kit" / "bootstrap.log"
        log.parent.mkdir(parents=True)
        log.write_text("ok", encoding="utf-8")
        probe = textwrap.dedent(
            """
            import sys
            class Block:
                def find_spec(self, name, path=None, target=None):
                    if name == "llm_kit.dispatch.declaration":
                        raise ModuleNotFoundError("blocked", name="yaml")
                    return None
            sys.meta_path.insert(0, Block())
            import llm_kit.jobs
            """
        )
        env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
        env.pop("CLAUDE_BOOTSTRAP_DATA_ROOT", None)
        result = subprocess.run(
            [sys.executable, "-c", f"import sys; sys.path.insert(0, {str(_LIB)!r})\n" + probe],
            env=env, capture_output=True, text=True, cwd=str(tmp_path),
        )
        assert result.returncode == 1
        assert "ModuleNotFoundError" in result.stderr and "blocked" in result.stderr


def _regenerate_fixtures() -> None:
    """Rewrite the committed fixtures from the live implementations."""
    import tempfile

    sys.path.insert(0, str(_LIB))
    for entry in map(str, _LEGACY_PATHS):
        sys.path.insert(0, entry)
    legacy, final = _legacy_side(), _final_side()
    mp = pytest.MonkeyPatch()
    try:
        for side, directory in ((legacy, "legacy"), (final, "llm-kit")):
            with tempfile.TemporaryDirectory() as tmp:
                produced = build_schema_fixtures(side, Path(tmp), mp)
            target = _FIXTURES / directory
            target.mkdir(parents=True, exist_ok=True)
            for old in target.glob("*.json"):
                old.unlink()
            for name, data in produced.items():
                (target / name).write_bytes(data)
            print(f"wrote {len(produced)} fixtures to {target}")
    finally:
        mp.undo()


if __name__ == "__main__":
    if sys.argv[1:] == ["--write-fixtures"]:
        os.environ.setdefault("_BOOTSTRAP_GUARD_SKIP_REEXEC", "1")
        _regenerate_fixtures()
    else:
        raise SystemExit("usage: test_jobs_parity.py --write-fixtures")
