"""One side of a cross-state scenario, in its own process.

``tests/llm-kit/test_cross_state_parity.py`` starts this script once per side
and per stretch of a scenario: ``cross_state_worker.py <request.json> <result.json>``.
The request names the side (``legacy`` or ``final``), the scratch HOME and a
list of operations; the result holds one outcome per operation.

Why a process per side. The two sides have module-global state that must not
meet: the verdict cache path and the credential path are computed from HOME at
import, the legacy libraries and ``llm_kit`` define classes with the same
names, and the final side must never import a legacy package (nor the legacy
side an ``llm_kit`` one). A fresh interpreter whose ``sys.path`` holds only
one side's roots makes every one of those a checked fact: the result reports
any module of the other side that got imported (``leaks``), and the HOME the
interpreter resolved.

Nothing here makes a network call or starts a model process. Job backends are
the in-process fake below; the only child process is the ``python`` a job
contract runs, which the job runner's own tests use as well.

``legacy`` is job_kit, content_pipeline and llm_scripting_kit imported from the
three legacy plugins' ``lib`` directories. ``final`` is ``llm_kit`` from
packages/katsoft-llm-kit/src. Both also see plugins/bootstrap, whose
``bootstrap_lib.interrupt_contract`` both sides' interrupt adapters delegate to.
"""

from __future__ import annotations

import dataclasses
import enum
import importlib
import importlib.util
import json
import os
import sqlite3
import sys
import time
import traceback
import types
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

REPO = Path(__file__).resolve().parents[4]

LEGACY_PATHS = (
    REPO / "plugins" / "job-kit" / "lib",
    REPO / "plugins" / "content-pipeline-kit" / "lib",
    REPO / "plugins" / "llm-scripting-kit" / "lib",
    REPO / "plugins" / "bootstrap",
)
FINAL_PATHS = (
    Path(importlib.util.find_spec("llm_kit").origin).resolve().parent.parent,
    REPO / "plugins" / "bootstrap",
)
LEGACY_ROOTS = ("job_kit", "content_pipeline", "llm_scripting_kit")
FINAL_ROOTS = ("llm_kit",)

SIDE = ""
NS: dict[str, Any] = {}
PATCHED: list[str] = []


class StalePatchTarget(Exception):
    """A name a scenario patches no longer exists on the side being driven."""


def imp(name: str) -> Any:
    return importlib.import_module(name)


# --------------------------------------------------------------------------
# Plain data, so two sides' classes compare by shape
# --------------------------------------------------------------------------


def plain(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return {"enum": type(value).__name__, "value": plain(value.value)}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        out = {"__type__": type(value).__name__}
        for field in dataclasses.fields(value):
            out[field.name] = plain(getattr(value, field.name))
        return out
    if isinstance(value, BaseException):
        return exc_shape(value)
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (set, frozenset)):
        return sorted((plain(v) for v in value), key=repr)
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    return {"repr": repr(value), "type": type(value).__name__}


def exc_shape(exc: BaseException) -> dict:
    attrs = {}
    for key, val in sorted(vars(exc).items()):
        if not key.startswith("__"):
            attrs[key] = plain(val)
    return {
        "mro": [c.__name__ for c in type(exc).__mro__],
        "message": str(exc),
        "attrs": attrs,
    }


def patch(obj: Any, name: str, value: Any, label: str) -> None:
    """Replace ``obj.name``; a name that is gone is a stale patch target."""
    if not hasattr(obj, name):
        raise StalePatchTarget(f"{label}: {name!r} no longer exists on {obj!r}")
    setattr(obj, name, value)
    PATCHED.append(label)


# --------------------------------------------------------------------------
# Barriers (file based, so two processes can meet)
# --------------------------------------------------------------------------


def op_touch(a: dict) -> dict:
    path = Path(a["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1", encoding="ascii")
    return {"touched": path.name}


def op_wait_file(a: dict) -> dict:
    path = Path(a["path"])
    deadline = time.monotonic() + float(a.get("timeout", 60))
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f"barrier {path} never appeared")
        time.sleep(0.005)
    return {"seen": path.name}


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------

_JOB_MODULES = ("model", "run", "select", "store", "workspace", "events", "interrupts")


def jobs_ns() -> types.SimpleNamespace:
    if "jobs" in NS:
        return NS["jobs"]
    if SIDE == "legacy":
        mods = {m: imp(f"job_kit.{m}") for m in _JOB_MODULES}
        completion = imp("llm_scripting_kit.completion")
        ns = types.SimpleNamespace(
            LLMResponse=completion.LLMResponse,
            BackendSelection=completion.BackendSelection,
            Capabilities=completion.Capabilities,
            declaration=imp("llm_scripting_kit.declaration"),
            models=imp("llm_scripting_kit.models"),
            usage=imp("llm_scripting_kit.usage_budget"),
            **mods,
        )
    else:
        mods = {m: imp(f"llm_kit.jobs.{m}") for m in _JOB_MODULES}
        ns = types.SimpleNamespace(
            LLMResponse=imp("llm_kit.completion.types").LLMResponse,
            BackendSelection=imp("llm_kit.dispatch.factory").BackendSelection,
            Capabilities=imp("llm_kit.completion.capabilities").Capabilities,
            declaration=imp("llm_kit.dispatch.declaration"),
            models=imp("llm_kit.registry.models"),
            usage=imp("llm_kit.dispatch.usage_budget"),
            **mods,
        )
    NS["jobs"] = ns
    return ns


class Transport:
    """A hermetic backend: every call answers with the same canned response."""

    name = "fake"

    def __init__(self, ns: Any) -> None:
        self.ns = ns
        self.calls: list = []

    def complete(self, system: str, user: str, *, model: str, options: object = None) -> Any:
        self.calls.append((system, user, model))
        return self.ns.LLMResponse(
            text="model answer",
            model="fake-model",
            input_tokens=3,
            output_tokens=5,
            dropped_params=(),
            execution_controls_applied=(),
            started_at="2026-09-01T00:00:00Z",
            ended_at="2026-09-01T00:00:01Z",
        )

    def classify_halt(self, exc: BaseException) -> Optional[str]:
        return None


def _ticking_clock(epoch_s: int) -> Callable[[], str]:
    import datetime
    import itertools

    ticks = itertools.count()
    start = datetime.datetime(2026, 9, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(
        seconds=epoch_s
    )

    def now() -> str:
        moment = start + datetime.timedelta(seconds=next(ticks))
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    return now


def jobs_hermetic(ns: Any, clock_epoch_s: int) -> None:
    """Keep selection off this machine's registry, CLIs and pinned verdicts."""
    if NS.get("jobs_hermetic"):
        return
    NS["jobs_hermetic"] = True
    patch(ns.models, "discover_model_entries", lambda **_: {}, "models.discover_model_entries")
    patch(ns.declaration, "check_many", lambda *_a, **_k: {}, "declaration.check_many")
    patch(ns.usage, "record_observed_halt", lambda *_a, **_k: None, "usage.record_observed_halt")
    patch(
        ns.run.BackpressurePolicy,
        "from_environment",
        staticmethod(lambda: ns.run.BackpressurePolicy(cap_s=0.0)),
        "run.BackpressurePolicy.from_environment",
    )
    patch(ns.run, "utc_now_iso", _ticking_clock(clock_epoch_s), "run.utc_now_iso")


def jobs_providers(ns: Any) -> tuple[Callable, Callable]:
    transport = Transport(ns)

    def provider() -> Mapping[str, Any]:
        return {"fake": ns.Capabilities(adapter="fake")}

    def build(endpoint: str, **_kwargs: object) -> Any:
        if endpoint != "ep":
            raise ns.models.EndpointResolveError(f"no endpoint {endpoint!r}")
        return ns.BackendSelection(endpoint, "fake", transport, "fake-model", capabilities=None)

    return provider, build


def snapshot_summary(snapshot: Any) -> dict:
    states = {job.id: job.state.value for job in snapshot.jobs}
    terminal = {"accepted", "rejected", "failed", "halted", "unroutable", "operator_rejected", "expired"}
    values = list(states.values())
    if any(s in terminal and s != "accepted" for s in values):
        code = 1
    elif any(s == "waiting" for s in values):
        code = 4
    elif all(s == "accepted" for s in values):
        code = 0
    else:
        code = 1
    return {"states": states, "exit_code": code, "run_status": plain(snapshot.status)}


def op_jobs_run(a: dict) -> dict:
    ns = jobs_ns()
    jobs_hermetic(ns, int(a.get("clock_epoch_s", 0)))
    provider, build = jobs_providers(ns)
    snapshot = ns.run.run_job_file(
        Path(a["jobs_file"]),
        store_path=Path(a["store_path"]),
        run_id=a["run_id"],
        capabilities_provider=provider,
        backend_factory=build,
    )
    return snapshot_summary(snapshot)


def op_jobs_create(a: dict) -> dict:
    """Create a run and register its jobs without executing any of them."""
    ns = jobs_ns()
    path = Path(a["jobs_file"]).resolve()
    job_file = ns.model.load_job_file(path)
    store = ns.store.JobStore(Path(a["store_path"]))
    store.create_run(
        a["run_id"],
        job_file.jobs,
        jobs_path=path,
        max_parallel=job_file.max_parallel,
        workspace_root=job_file.workspace_root,
        workspace_base_refs={},
        disallowed_tools=job_file.disallowed_tools,
    )
    return snapshot_summary(store.snapshot(a["run_id"]))


def op_jobs_resume(a: dict) -> dict:
    ns = jobs_ns()
    jobs_hermetic(ns, int(a.get("clock_epoch_s", 0)))
    provider, build = jobs_providers(ns)
    snapshot = ns.run.resume_run(
        a["run_id"],
        Path(a["store_path"]),
        capabilities_provider=provider,
        backend_factory=build,
    )
    return snapshot_summary(snapshot)


def op_jobs_state(a: dict) -> dict:
    ns = jobs_ns()
    store = ns.store.JobStore(Path(a["store_path"]), create=False)
    return snapshot_summary(store.snapshot(a["run_id"]))


def _find_interrupt(store: Any, run_id: str, interrupt_id: str) -> Any:
    for record in store.list_interrupts(run_id):
        if str(record.id) == str(interrupt_id):
            return record
    raise LookupError(f"no interrupt {interrupt_id} in {run_id}")


def op_jobs_resolve(a: dict) -> dict:
    ns = jobs_ns()
    store = ns.store.JobStore(Path(a["store_path"]), create=False)
    resolution = store.resolve_interrupt(
        a["run_id"],
        str(a["interrupt_id"]),
        decision=a["decision"],
        input=a.get("input"),
        reason=a.get("reason"),
        now=a["now"],
    )
    record = _find_interrupt(store, a["run_id"], a["interrupt_id"])
    return {"resolution": plain(resolution), "document": ns.interrupts.resolution_document(record)}


def op_jobs_document(a: dict) -> dict:
    ns = jobs_ns()
    store = ns.store.JobStore(Path(a["store_path"]), create=False)
    record = _find_interrupt(store, a["run_id"], a["interrupt_id"])
    return {"document": ns.interrupts.resolution_document(record), "record": plain(record)}


def op_jobs_events(a: dict) -> dict:
    ns = jobs_ns()
    store = ns.store.JobStore(Path(a["store_path"]), create=False)
    lines = [
        json.dumps(event, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        for event in store.list_events(a["run_id"])
    ]
    return {"lines": lines}


def dump_tables(db: Path) -> dict:
    """Every table of a SQLite file, rows in rowid order, read without any product code."""
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        out: dict = {}
        for table in names:
            out[table] = [
                {k: plain(row[k]) for k in row.keys()}
                for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
        return out
    finally:
        conn.close()


def schema_dump(db: Path) -> dict:
    conn = sqlite3.connect(str(db))
    try:
        master = [
            list(r)
            for r in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY rowid")
        ]
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_version ORDER BY rowid")]
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    return {"sqlite_master": master, "schema_version": versions, "journal_mode": mode}


def op_dump(a: dict) -> dict:
    return {"tables": dump_tables(Path(a["db"])), "schema": schema_dump(Path(a["db"]))}


def op_files(a: dict) -> dict:
    root = Path(a["root"])
    return {"files": sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())}


# --------------------------------------------------------------------------
# Pipeline execution store
# --------------------------------------------------------------------------

_PIPE_MODULES = ("model", "store", "events", "interrupts", "status")


def pipe_ns() -> types.SimpleNamespace:
    if "pipe" in NS:
        return NS["pipe"]
    prefix = "content_pipeline.execution" if SIDE == "legacy" else "llm_kit.pipeline.execution"
    ns = types.SimpleNamespace(**{m: imp(f"{prefix}.{m}") for m in _PIPE_MODULES})
    NS["pipe"] = ns
    return ns


def _decode(ns: Any, value: Any) -> Any:
    if isinstance(value, dict) and "$interrupt_request" in value:
        spec = value["$interrupt_request"]
        return ns.model.InterruptRequest(
            kind=spec["kind"],
            request_schema=spec["request_schema"],
            payload=spec["payload"],
            expires_in_s=spec.get("expires_in_s"),
        )
    if isinstance(value, dict) and "$usage" in value:
        return ns.model.UsageRecord(**value["$usage"])
    if isinstance(value, dict):
        return {k: _decode(ns, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode(ns, v) for v in value]
    return value


def pipe_store(a: dict) -> Any:
    return pipe_ns().store.ExecutionStore(Path(a["db"]))


def op_pipe_seed(a: dict) -> dict:
    store = pipe_store(a)
    store.create_run(
        a["run_id"],
        driver="inline",
        backend="mock",
        model="m",
        adapter_version="1",
        created_at=1.0,
    )
    store.register_units(a["run_id"], list(a["units"]), at=1.0)
    return {"run": plain(store.get_run(a["run_id"]))}


def op_pipe_call(a: dict) -> Any:
    ns = pipe_ns()
    store = pipe_store(a)
    args = _decode(ns, a.get("args", []))
    kwargs = _decode(ns, a.get("kwargs", {}))
    return plain(getattr(store, a["method"])(*args, **kwargs))


def op_pipe_events(a: dict) -> dict:
    ns = pipe_ns()
    store = pipe_store(a)
    return {"events": [plain(dict(e)) for e in ns.events.project_run(store, a["run_id"])]}


def op_pipe_status(a: dict) -> dict:
    ns = pipe_ns()
    store = pipe_store(a)
    return {"status": plain(ns.status.compute_status(store, a["run_id"], now=a["now"]))}


def op_pipe_document(a: dict) -> dict:
    ns = pipe_ns()
    store = pipe_store(a)
    record = store.get_interrupt(a["run_id"], str(a["interrupt_id"]))
    if record is None:
        raise LookupError(f"no interrupt {a['interrupt_id']} in {a['run_id']}")
    return {"document": ns.interrupts.resolution_document(record), "record": plain(record)}


# --------------------------------------------------------------------------
# Spend ledger
# --------------------------------------------------------------------------


def spend_mod() -> Any:
    if "spend" not in NS:
        NS["spend"] = imp(
            "content_pipeline.llm.spend_ledger"
            if SIDE == "legacy"
            else "llm_kit.pipeline.runtime.spend_ledger"
        )
    return NS["spend"]


def op_spend_create(a: dict) -> dict:
    mod = spend_mod()
    ledger = mod.create_ledger(
        Path(a["path"]), cap_usd=a["cap_usd"], run_id=a["run_id"], meta=a.get("meta")
    )
    return {"run_id": ledger.run_id, "cap_usd": ledger.cap_usd, "schema_version": ledger.schema_version}


def op_spend_open(a: dict) -> dict:
    mod = spend_mod()
    ledger = mod.open_ledger(Path(a["path"]))
    return {"run_id": ledger.run_id, "cap_usd": ledger.cap_usd, "schema_version": ledger.schema_version}


def op_spend_reserve(a: dict) -> dict:
    mod = spend_mod()
    ledger = mod.open_ledger(Path(a["path"]))
    return plain(
        ledger.reserve(
            a["amount_usd"],
            scope=a.get("scope", ""),
            identifier=a.get("identifier", ""),
            model=a.get("model", ""),
        )
    )


def _reservation(mod: Any, fields: Mapping[str, Any]) -> Any:
    keep = {k: v for k, v in fields.items() if k != "__type__"}
    return mod.Reservation(**keep)


def op_spend_settle(a: dict) -> None:
    mod = spend_mod()
    mod.open_ledger(Path(a["path"])).settle(_reservation(mod, a["reservation"]), a["cost_usd"])


def op_spend_release(a: dict) -> None:
    mod = spend_mod()
    mod.open_ledger(Path(a["path"])).release(_reservation(mod, a["reservation"]))


def op_spend_halt(a: dict) -> None:
    spend_mod().open_ledger(Path(a["path"])).halt(a["reason"], a.get("detail", ""))


def op_spend_resume(a: dict) -> None:
    spend_mod().open_ledger(Path(a["path"])).resume(force=bool(a.get("force", False)))


def op_spend_status(a: dict) -> dict:
    return plain(spend_mod().open_ledger(Path(a["path"])).status())


# --------------------------------------------------------------------------
# Verdict cache
# --------------------------------------------------------------------------


def usage_mod() -> Any:
    if "usage" not in NS:
        NS["usage"] = imp(
            "llm_scripting_kit.usage_budget" if SIDE == "legacy" else "llm_kit.dispatch.usage_budget"
        )
    return NS["usage"]


def _environ(session: Optional[str]) -> dict:
    return {"LLM_SCRIPTING_KIT_USAGE_SESSION": session} if session else {}


def op_verdict_info(a: dict) -> dict:
    mod = usage_mod()
    return {"cache": str(mod.VERDICT_CACHE), "claude_snapshot": str(mod.CLAUDE_SNAPSHOT)}


def op_verdict_halt(a: dict) -> dict:
    mod = usage_mod()
    spec = mod.ConserveSpec(pool=a.get("pool", "seven_day"))
    budget = mod.record_observed_halt(
        a["entry"], spec, now=a["now"], environ=_environ(a["session"])
    )
    return {"budget": plain(budget)}


def op_verdict_pin(a: dict) -> dict:
    """``pinned_evaluate`` for one entry.

    ``mode`` is ``plain`` (evaluate as usual), ``forbid_evaluate`` (any
    recomputation is a failure, so a returned verdict was read from the cache),
    or ``pause`` (the fresh evaluation announces it has loaded the cache, then
    waits to be released: the writer that holds a stale snapshot).
    """
    mod = usage_mod()
    spec = mod.ConserveSpec(pool=a.get("pool", "seven_day"))
    mode = a.get("mode", "plain")
    original = mod.evaluate
    if mode == "forbid_evaluate":

        def forbidden(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("pinned_evaluate recomputed a verdict that should have been read")

        patch(mod, "evaluate", forbidden, "usage_budget.evaluate")
    elif mode == "pause":

        def paused(spec_: Any, harness_: Any, *, now: Optional[float] = None) -> Any:
            op_touch({"path": a["loaded_flag"]})
            op_wait_file({"path": a["go_flag"], "timeout": a.get("timeout", 60)})
            return original(spec_, harness_, now=now)

        patch(mod, "evaluate", paused, "usage_budget.evaluate")
    budget = mod.pinned_evaluate(
        a["entry"], spec, a.get("harness", "claude"), now=a["now"], environ=_environ(a["session"])
    )
    return {"budget": plain(budget), "usable": budget.usable, "deprioritized": budget.deprioritized}


def op_verdict_hammer(a: dict) -> dict:
    mod = usage_mod()
    spec = mod.ConserveSpec(pool="seven_day")
    for i in range(int(a["rounds"])):
        mod.record_observed_halt(
            f"{a['entry']}-{i % 3}", spec, now=a["now"], environ=_environ(a["session"])
        )
    return {"rounds": int(a["rounds"])}


def op_verdict_select(a: dict) -> dict:
    """Rank a two-entry declaration the way a dispatcher does, over the real cache.

    ``paced`` is a harness entry with ``conserve_usage``; ``backup`` is a
    harness entry with none. Both are pre-probed reachable, so no probe runs and
    the only input that differs between calls is the pinned verdict.
    """
    if SIDE == "legacy":
        me = imp("llm_scripting_kit.model_endpoints")
        reach = imp("llm_scripting_kit.reachability")
        decl = imp("llm_scripting_kit.declaration")
    else:
        me = imp("llm_kit.registry.model_endpoints")
        reach = imp("llm_kit.dispatch.reachability")
        decl = imp("llm_kit.dispatch.declaration")
    mod = usage_mod()
    os.environ["LLM_SCRIPTING_KIT_USAGE_SESSION"] = a["session"]
    entries = {
        "paced": me.EndpointEntry(
            id="paced",
            base_url=None,
            model="paced-model",
            kind=me.HARNESS_KIND,
            harness="claude",
            conserve_usage=mod.ConserveSpec(pool="seven_day"),
        ),
        "backup": me.EndpointEntry(
            id="backup", base_url=None, model="backup-model", kind=me.HARNESS_KIND, harness="claude"
        ),
    }
    cache = {
        name: reach.Reachability(status="reachable", checked="cli-version", detail="reachable")
        for name in entries
    }
    ranking = decl.describe(
        ["paced", "backup"], caller="process", entries=entries, reachability_cache=cache
    )
    return {
        "dispositions": {d.id: d.disposition for d in ranking.dispositions},
        "usability": {s.id: s.usability for s in ranking.rendered_entries},
        "default": ranking.default.id,
    }


# --------------------------------------------------------------------------
# Credentials and user config
# --------------------------------------------------------------------------


def registry_mods() -> types.SimpleNamespace:
    if "registry" not in NS:
        if SIDE == "legacy":
            p = "llm_scripting_kit"
            NS["registry"] = types.SimpleNamespace(
                env_file=imp(f"{p}.env_file"),
                api_key=imp(f"{p}.api_key"),
                constants=imp(f"{p}.constants"),
                models=imp(f"{p}.models"),
            )
        else:
            p = "llm_kit.registry"
            NS["registry"] = types.SimpleNamespace(
                env_file=imp(f"{p}.env_file"),
                api_key=imp(f"{p}.api_key"),
                constants=imp(f"{p}.constants"),
                models=imp(f"{p}.models"),
            )
    return NS["registry"]


def op_cred_paths(a: dict) -> dict:
    c = registry_mods().constants
    root = Path(a["project_root"])
    return {
        "user_env_file": str(c.USER_ENV_FILE),
        "project_env_file": str(c.project_env_file(root)),
        "legacy_project_env_file": str(c.legacy_project_env_file(root)),
        "project_env_files": [str(p) for p in c.project_env_files(root)],
        "api_key_env": c.API_KEY_ENV,
        "base_url": c.BASE_URL,
        "cli_command": c.CLI_COMMAND,
    }


def op_cred_write(a: dict) -> dict:
    m = registry_mods()
    path = Path(a["path"]) if a.get("path") else m.constants.USER_ENV_FILE
    m.env_file.write_env_file(path, dict(a["values"]))
    return {"bytes": path.read_bytes().decode("utf-8"), "path": str(path)}


def op_cred_read(a: dict) -> dict:
    m = registry_mods()
    path = Path(a["path"]) if a.get("path") else m.constants.USER_ENV_FILE
    return {"values": m.env_file.read_env_file(path)}


def op_cred_get_key(a: dict) -> dict:
    m = registry_mods()
    for name in ("OPENROUTER_API_KEY", "CROSS_STATE_KEY"):
        os.environ.pop(name, None)
    os.environ.update(a.get("env", {}))
    result = m.api_key.get_api_key(
        Path(a["project_root"]) if a.get("project_root") else None, endpoint=a.get("endpoint")
    )
    return plain(result)


def op_config_load(a: dict) -> dict:
    m = registry_mods()
    config = m.models.load_model_config(project_root=a.get("project_root"))
    resolved = m.models.resolve_endpoint(
        a.get("endpoint"), project_root=a.get("project_root"), config=None
    )
    return {"config": plain(config), "endpoint": plain(resolved)}


# --------------------------------------------------------------------------
# Self checks
# --------------------------------------------------------------------------


def op_patch_targets(a: dict) -> dict:
    """Run the jobs hermetic patching and report what it patched."""
    ns = jobs_ns()
    jobs_hermetic(ns, 0)
    return {"patched": list(PATCHED)}


OPS: dict[str, Callable[[dict], Any]] = {
    name[3:]: fn for name, fn in globals().items() if name.startswith("op_") and callable(fn)
}


def main(argv: list[str]) -> int:
    global SIDE
    request = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    SIDE = request["side"]
    if SIDE not in ("legacy", "final"):
        raise SystemExit(f"unknown side {SIDE!r}")
    roots = LEGACY_PATHS if SIDE == "legacy" else FINAL_PATHS
    for entry in reversed([str(p) for p in roots]):
        sys.path.insert(0, entry)
    home = Path(request["home"]).resolve()
    resolved_home = Path.home().resolve()
    if resolved_home != home:
        raise SystemExit(f"HOME resolved to {resolved_home}, expected the scratch {home}")
    results = []
    for step in request["ops"]:
        label = step.get("label", step["op"])
        try:
            value = OPS[step["op"]](step.get("args", {}))
            results.append({"label": label, "ok": value})
        except Exception as exc:  # noqa: BLE001 -- the raised exception IS the outcome
            shape = exc_shape(exc)
            shape["traceback"] = traceback.format_exc()
            results.append({"label": label, "raised": shape})
    other = FINAL_ROOTS if SIDE == "legacy" else LEGACY_ROOTS
    leaks = sorted(m for m in sys.modules if m.split(".")[0] in other)
    out = {"results": results, "leaks": leaks, "home": str(resolved_home), "patched": list(PATCHED)}
    Path(argv[2]).write_text(json.dumps(out, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
