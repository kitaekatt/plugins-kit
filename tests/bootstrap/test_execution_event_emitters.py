"""Join test for the shared execution-event vocabulary.

Acceptance: at least two plugins emit conforming events without importing each
other's store or domain model, with identity and ordering tests.

Part 1 reads each emitting plugin's source BY PATH (AST, no import of the
subject) and pins the import boundary. Part 2 produces a real job-kit stream
and a real content-pipeline-kit stream and checks they conform together.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Iterable

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGINS = REPO / "plugins"

for _lib in (
    PLUGINS / "job-kit" / "lib",
    PLUGINS / "content-pipeline-kit" / "lib",
    PLUGINS / "llm-scripting-kit" / "lib",
    PLUGINS / "bootstrap",
):
    if str(_lib) not in sys.path:
        sys.path.append(str(_lib))

from bootstrap_lib import execution_event as ee  # noqa: E402

JK_PKG = PLUGINS / "job-kit" / "lib" / "job_kit"
CPK_PKG = PLUGINS / "content-pipeline-kit" / "lib" / "content_pipeline"
LSK_PKG = PLUGINS / "llm-scripting-kit" / "lib" / "llm_scripting_kit"

# Top-level package of each plugin that owns a store or domain model.
PLUGIN_PACKAGES = {
    "job_kit": "job-kit",
    "content_pipeline": "content-pipeline-kit",
    "llm_scripting_kit": "llm-scripting-kit",
    "workflow_kit": "workflow-kit",
    "workflow_kit_lib": "workflow-kit",
}

# Emitter source, by path -> the plugin packages it may import (its own, plus
# declared edges). bootstrap_lib is the shared vocabulary, not a plugin store.
EMITTERS: dict[str, tuple[Path, frozenset[str]]] = {
    "job_kit/events.py": (JK_PKG / "events.py", frozenset({"job_kit"})),
    "job_kit/store.py": (JK_PKG / "store.py", frozenset({"job_kit"})),
    "job_kit/interrupts.py": (
        JK_PKG / "interrupts.py",
        frozenset({"job_kit", "llm_scripting_kit"}),
    ),
    "content_pipeline/execution/events.py": (
        CPK_PKG / "execution" / "events.py",
        frozenset({"content_pipeline"}),
    ),
    "llm_scripting_kit/observer.py": (LSK_PKG / "observer.py", frozenset({"llm_scripting_kit"})),
    "llm_scripting_kit/declaration.py": (
        LSK_PKG / "declaration.py",
        frozenset({"llm_scripting_kit"}),
    ),
    "workflow-kit/scripts/openrouter_run.py": (
        PLUGINS / "workflow-kit" / "scripts" / "openrouter_run.py",
        frozenset({"llm_scripting_kit", "workflow_kit_lib"}),
    ),
    "workflow-kit/scripts/check_artifact.py": (
        PLUGINS / "workflow-kit" / "scripts" / "check_artifact.py",
        frozenset({"llm_scripting_kit", "workflow_kit_lib"}),
    ),
}


def imported_roots(source: str) -> set[str]:
    """Absolute module names imported anywhere in source (lazy imports included),
    plus constant module names passed to import_module / __import__."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            first = node.args[0]
            if (
                called in {"import_module", "__import__"}
                and isinstance(first, ast.Constant)
                and isinstance(first.value, str)
            ):
                names.add(first.value)
    return names


def foreign_plugin_imports(source: str, allowed: Iterable[str]) -> set[str]:
    allowed = set(allowed)
    return {
        name
        for name in imported_roots(source)
        if name.split(".")[0] in PLUGIN_PACKAGES and name.split(".")[0] not in allowed
    }


def package_sources(package: Path) -> dict[str, str]:
    return {
        str(p.relative_to(package)): p.read_text(encoding="utf-8")
        for p in sorted(package.rglob("*.py"))
    }


# ---------------------------------------------------------------------------
# Part 1: import boundary
# ---------------------------------------------------------------------------


def test_emitter_paths_exist() -> None:
    for label, (path, _) in EMITTERS.items():
        assert path.is_file(), label


@pytest.mark.parametrize("label", sorted(EMITTERS))
def test_emitter_imports_no_other_plugins_store_or_model(label: str) -> None:
    path, allowed = EMITTERS[label]
    found = foreign_plugin_imports(path.read_text(encoding="utf-8"), allowed)
    assert not found, f"{label} imports {sorted(found)}"


def test_no_job_kit_module_imports_content_pipeline() -> None:
    sources = package_sources(JK_PKG)
    assert len(sources) > 3
    for name, text in sources.items():
        bad = {n for n in imported_roots(text) if n.split(".")[0] == "content_pipeline"}
        assert not bad, f"job_kit/{name} imports {sorted(bad)}"


def test_no_content_pipeline_module_imports_job_kit() -> None:
    sources = package_sources(CPK_PKG)
    assert len(sources) > 3
    for name, text in sources.items():
        bad = {n for n in imported_roots(text) if n.split(".")[0] == "job_kit"}
        assert not bad, f"content_pipeline/{name} imports {sorted(bad)}"


def test_observer_imports_no_bootstrap_lib() -> None:
    text = EMITTERS["llm_scripting_kit/observer.py"][0].read_text(encoding="utf-8")
    bad = {n for n in imported_roots(text) if n.split(".")[0] == "bootstrap_lib"}
    assert not bad


def test_cpk_events_has_no_static_bootstrap_lib_import() -> None:
    """The shared vocabulary is probed at call time, so a foreign-project
    interpreter without bootstrap_lib can still import the module."""
    path = EMITTERS["content_pipeline/execution/events.py"][0]
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert not any(a.name.split(".")[0] == "bootstrap_lib" for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "bootstrap_lib"


def test_boundary_detector_sees_lazy_and_dynamic_imports() -> None:
    src = (
        "def f():\n"
        "    import content_pipeline.execution.store\n"
        "    importlib.import_module('job_kit.store')\n"
    )
    assert foreign_plugin_imports(src, {"job_kit"}) == {"content_pipeline.execution.store"}
    assert foreign_plugin_imports(src, set()) == {
        "content_pipeline.execution.store",
        "job_kit.store",
    }


# ---------------------------------------------------------------------------
# Part 2: streams produced by both plugins conform together
# ---------------------------------------------------------------------------

RUN = "shared-run"
T0 = "2026-09-01T00:00:0"


def _job_kit_stream(tmp_path: Path) -> tuple[dict, ...]:
    from job_kit.model import Acceptance, Attempt, Contract, Job, JobState, Prompt, Usage
    from job_kit.store import JobStore

    store = JobStore(tmp_path / "ledger.sqlite3")
    jobs = [
        Job(
            id=name,
            prompt=Prompt(user=name),
            models=("fake",),
            directory=tmp_path,
            max_attempts=2,
            contract=Contract(command=(sys.executable, "-c", "pass"), directory=tmp_path),
        )
        for name in ("a", "b")
    ]
    store.create_run(RUN, jobs)
    for name in ("a", "b"):
        number = store.reserve_attempt(
            RUN, name, endpoint="e", backend="b", model="m", reserved_at=T0 + "0Z"
        ).attempt_no
        store.arm_reservation(RUN, name, number, invoke_armed_at=T0 + "1Z")
        store.append_attempt(
            Attempt(
                run_id=RUN,
                job_id=name,
                attempt_no=number,
                endpoint="e",
                backend="b",
                model="m",
                status="completed",
                started_at=T0 + "1Z",
                ended_at=T0 + "2Z",
                usage=Usage(input_tokens=3, output_tokens=5),
                error=None,
                acceptance=Acceptance(
                    command=("true",),
                    directory=tmp_path,
                    exit_code=0,
                    stdout="",
                    stderr="",
                    wall_ms=1,
                    accepted=True,
                ),
            ),
            terminal_state=JobState.ACCEPTED,
        )
    return store.list_events(RUN)


def _content_pipeline_stream(tmp_path: Path) -> tuple[dict, ...]:
    from content_pipeline.execution.events import project_run
    from content_pipeline.execution.model import UsageRecord
    from content_pipeline.execution.store import ExecutionStore

    store = ExecutionStore(tmp_path / "run.db")
    store.create_run(
        RUN, driver="inline", backend="mock", model="m1", adapter_version="7", created_at=1000.0
    )
    store.register_units(RUN, ["a", "b"], at=1000.0)
    for i, unit in enumerate(("a", "b")):
        token = store.claim_unit(RUN, unit, "w", at=1001.0 + i).fencing_token
        store.accept_unit(RUN, unit, token, usage=UsageRecord(1, 2, 3), at=1002.0 + i)
    return project_run(store, RUN)


@pytest.fixture()
def streams(tmp_path: Path) -> tuple[tuple[dict, ...], tuple[dict, ...]]:
    (tmp_path / "jk").mkdir()
    (tmp_path / "cpk").mkdir()
    return _job_kit_stream(tmp_path / "jk"), _content_pipeline_stream(tmp_path / "cpk")


def test_both_streams_conform(streams) -> None:
    jk, cpk = streams
    assert jk and cpk
    assert ee.validate_stream(jk) == jk
    assert ee.validate_stream(cpk) == cpk
    assert {e["source"]["plugin"] for e in jk} == {"job-kit"}
    assert {e["source"]["plugin"] for e in cpk} == {"content-pipeline-kit"}
    for stream in (jk, cpk):
        assert {e["schema"] for e in stream} == {ee.SCHEMA_V1}


def test_identity_is_the_shared_shape_across_plugins(streams) -> None:
    for stream in streams:
        assert {e["identity"]["run_id"] for e in stream} == {RUN}
        units = {e["identity"]["unit_id"] for e in stream if "unit_id" in e["identity"]}
        assert units == {"a", "b"}
        for e in stream:
            if e["event"] in ee.ATTEMPT_SCOPED:
                assert e["identity"]["attempt_id"]
        for unit in ("a", "b"):
            terminals = [
                e for e in stream if e["event"] == "terminal" and e["identity"]["unit_id"] == unit
            ]
            assert len(terminals) == 1


def test_interleaved_streams_with_same_run_id_conform(streams) -> None:
    """Ordering is per (plugin, run_id, unit_id): two plugins sharing a run id
    and seq numbers do not collide."""
    jk, cpk = streams
    merged = [e for pair in zip(jk, cpk) for e in pair]
    merged += list(jk[len(cpk):]) + list(cpk[len(jk):])
    assert len(merged) == len(jk) + len(cpk)
    assert ee.validate_stream(merged) == tuple(merged)


def test_ordering_is_enforced_on_a_real_stream(streams) -> None:
    for stream in streams:
        unit_events = [e for e in stream if e["identity"].get("unit_id") == "a"]
        assert len(unit_events) >= 2
        swapped = list(stream)
        i, j = swapped.index(unit_events[0]), swapped.index(unit_events[-1])
        swapped[i], swapped[j] = swapped[j], swapped[i]
        with pytest.raises(ee.EventError):
            ee.validate_stream(swapped)


def test_seq_strictly_increases_per_group(streams) -> None:
    for stream in streams:
        last: dict[tuple, int] = {}
        for e in stream:
            group = (e["source"]["plugin"], e["identity"]["run_id"], e["identity"].get("unit_id"))
            assert e["seq"] > last.get(group, -1)
            last[group] = e["seq"]


def test_jsonl_round_trip_of_both_streams_in_one_file(streams, tmp_path: Path) -> None:
    jk, cpk = streams
    sink = ee.JsonlSink(tmp_path / "both.jsonl")
    for event in [*jk, *cpk]:
        sink.write(event)
    assert ee.read_jsonl(tmp_path / "both.jsonl") == (*jk, *cpk)
