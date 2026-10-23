"""The jobs factory seam: jobs receive backends through an injected BackendFactory.

``llm_kit.jobs`` takes ``backend_factory`` as a
``llm_kit.completion.factory_protocols.BackendFactory`` (default
``dispatch.ConfiguredBackendFactory``, which is also the CapabilityProvider).
Selection wraps it in ``dispatch.endpoint_resolver.EndpointResolver``, which
keeps the ``endpoint -> BackendSelection`` shape ``describe`` consumes and
constructs the chosen backend through ``factory.create(BackendSpec(...))``.

These tests pin the seam from the jobs side with a RECORDING factory: which
specs jobs asks for, that each candidate is built once, that the injected
factory is also where capabilities come from, that the legacy selection
callable still works as a named compatibility surface, and that jobs import no
adapter. Behavioural parity with legacy job-kit over the same scenarios is in
``test_jobs_parity.py`` (``TestFactorySeamParity``).
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

import pytest

from llm_kit.completion.capabilities import Capabilities
from llm_kit.completion.factory_protocols import BackendFactory, BackendSpec, CapabilityProvider
from llm_kit.completion.types import LLMResponse
from llm_kit.dispatch import endpoint_resolver as resolver_module
from llm_kit.dispatch import factory as factory_module
from llm_kit.dispatch.endpoint_resolver import EndpointResolver
from llm_kit.dispatch.factory import BackendSelection
from llm_kit.dispatch.pipeline_backends import ConfiguredBackendFactory
from llm_kit.jobs import run as jobs_run
from llm_kit.jobs import select as jobs_select
from llm_kit.jobs.model import load_job_file
from llm_kit.registry.model_endpoints import EndpointEntry
from llm_kit.registry.models import EndpointResolveError

_LIB = Path(__file__).resolve().parents[2] / "packages" / "katsoft-llm-kit" / "src" / "llm_kit"
_PASS = [sys.executable, "-c", "pass"]

_CONFIG = {
    "endpoints": {
        "h-claude": {"harness": "claude", "model": "m-claude", "effort": "high"},
        "h-bad": {"harness": "zed", "model": "m-z"},
    }
}
_ENTRIES = {
    "t-effort": EndpointEntry(
        id="t-effort", base_url="http://x.invalid/v1", model="t-effort-model",
        reasoning_effort="medium", effort_style="top-level", effort_style_declared=True,
    ),
    "t-plain": EndpointEntry(id="t-plain", base_url="http://x.invalid/v1", model="t-plain-model"),
    "h-claude": EndpointEntry(
        id="h-claude", base_url=None, model="m-claude", kind="harness", harness="claude", effort="high"
    ),
}
_RESOLVED = {
    "t-effort": {
        "name": "t-effort", "base_url": "http://x.invalid/v1",
        "request_defaults": {"reasoning_effort": "medium"}, "effort_style": "top-level",
        "effort_style_source": "endpoint",
    },
    "t-plain": {"name": "t-plain", "base_url": "http://x.invalid/v1", "request_defaults": {}},
}
_NAMES = {"openrouter": "openrouter", "claude-cli": "claude-cli", "codex-cli": "codex-cli"}


class FakeBackend:
    """A hermetic completion backend that answers every call."""

    def __init__(self, name: str, capabilities: Optional[Capabilities] = None) -> None:
        self.name = name
        if capabilities is not None:
            self.capabilities = capabilities
        self.calls: list[tuple] = []

    def complete(self, system: str, user: str, *, model: str, options: object = None) -> LLMResponse:
        self.calls.append((system, user, model))
        return LLMResponse(
            text="answer", model="fake-model", input_tokens=1, output_tokens=1,
            dropped_params=(), execution_controls_applied=(),
            started_at="2026-09-01T00:00:00Z", ended_at="2026-09-01T00:00:01Z",
        )

    def classify_halt(self, exc: BaseException) -> Optional[str]:
        return None


class RecordingFactory:
    """A BackendFactory and CapabilityProvider that records what jobs asks of it."""

    def __init__(self, table: Optional[Mapping[str, Capabilities]] = None) -> None:
        self.specs: list[BackendSpec] = []
        self.backends: list[FakeBackend] = []
        self.record_reads = 0
        self._table = table

    def _records(self) -> Mapping[str, Capabilities]:
        return self._table if self._table is not None else ConfiguredBackendFactory().capability_records()

    def create(self, spec: BackendSpec) -> FakeBackend:
        self.specs.append(spec)
        backend = FakeBackend(_NAMES[spec.backend], self._records().get(_NAMES[spec.backend]))
        self.backends.append(backend)
        return backend

    def capability_records(self) -> Mapping[str, Capabilities]:
        self.record_reads += 1
        return dict(self._records())

    def capability_record(self, backend: str) -> Optional[Capabilities]:
        return self._records().get(backend)


@pytest.fixture(autouse=True)
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fake registry, no probes, no verdict write-back, a scratch HOME."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CLAUDE_BOOTSTRAP_DATA_ROOT", raising=False)

    def resolve_endpoint(name: str, **_kw: object) -> dict:
        if name not in _RESOLVED:
            raise EndpointResolveError(f"no endpoint {name!r}")
        return dict(_RESOLVED[name])

    for module in (factory_module, resolver_module):
        monkeypatch.setattr(module, "load_model_config", lambda project_root=None: _CONFIG, raising=False)
        monkeypatch.setattr(module, "discover_model_entries", lambda **_kw: dict(_ENTRIES), raising=False)
    monkeypatch.setattr(resolver_module, "default_endpoint_name", lambda cfg: "h-claude")
    monkeypatch.setattr(resolver_module, "resolve_endpoint", resolve_endpoint)
    monkeypatch.setattr(
        resolver_module, "resolve_model",
        lambda model, *, cheap, config, endpoint, project_root: model or f"{endpoint}-model",
    )
    import llm_kit.dispatch.declaration as declaration
    import llm_kit.dispatch.usage_budget as usage
    import llm_kit.registry.models as models

    monkeypatch.setattr(models, "discover_model_entries", lambda **_kw: dict(_ENTRIES))
    monkeypatch.setattr(declaration, "check_many", lambda *_a, **_k: {})
    monkeypatch.setattr(usage, "record_observed_halt", lambda *a, **k: None)
    monkeypatch.setattr(
        jobs_run.BackpressurePolicy, "from_environment",
        staticmethod(lambda: jobs_run.BackpressurePolicy(cap_s=0.0)),
    )


def _job_file(tmp_path: Path, models: list, **extra: Any) -> Path:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    doc = {
        "id": "j", "prompt": {"user": "x"}, "models": models, "directory": str(work),
        "contract": {"command": _PASS, "directory": str(work)}, **extra,
    }
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps({"max_parallel": 1, "workspace_root": str(tmp_path / "ws"), "jobs": [doc]}),
                    encoding="utf-8")
    return path


def _run(tmp_path: Path, models: list, factory: Any, **extra: Any):
    path = _job_file(tmp_path, models, **extra)
    snapshot = jobs_run.run_job_file(
        path, store_path=tmp_path / "ledger" / "runs.sqlite3", run_id="r", backend_factory=factory
    )
    return snapshot, load_job_file(path).jobs[0]


class TestJobsCallCreateWithSpecs:
    def test_a_transport_and_a_harness_are_requested_as_specs(self, tmp_path):
        factory = RecordingFactory()
        snapshot, job = _run(tmp_path, ["t-plain", "h-claude"], factory)
        assert [j.state.value for j in snapshot.jobs] == ["accepted"]
        # One create per declared candidate: describe resolves every id once.
        assert factory.specs == [
            BackendSpec(backend="openrouter", endpoint="t-plain", project_root=Path(str(job.declared_directory))),
            BackendSpec(backend="claude-cli"),
        ]
        # The chosen candidate (the first usable entry) is the one that was called.
        assert [len(b.calls) for b in factory.backends] == [1, 0]

    def test_the_spec_carries_no_selection_policy(self, tmp_path):
        factory = RecordingFactory()
        _run(tmp_path, ["t-effort"], factory, requirements={"params": ["effort"]},
             model_efforts={"t-effort": "high"})
        (spec,) = factory.specs
        # Ranking, requirements, model and effort stay selection policy, never constructor state.
        assert {name for name, value in vars(spec).items() if value is not None} == {
            "backend", "endpoint", "project_root"
        }

    def test_the_selected_model_and_effort_come_from_the_registry_not_the_spec(self, tmp_path):
        factory = RecordingFactory()
        _run(tmp_path, ["h-claude"], factory)
        assert [call[2] for call in factory.backends[0].calls] == ["m-claude"]

    def test_each_candidate_is_created_once_per_selection_even_for_sidecar_jobs(self, tmp_path):
        factory = RecordingFactory()
        _run(tmp_path, ["t-plain", "t-effort"], factory,
             model_requirements={"t-plain": {"params": ["effort"]}, "t-effort": {}})
        endpoints = [spec.endpoint for spec in factory.specs]
        assert sorted(endpoints) == ["t-effort", "t-plain"]

    def test_an_unresolvable_endpoint_is_skipped_without_creating_a_backend(self, tmp_path):
        factory = RecordingFactory()
        snapshot, _ = _run(tmp_path, ["nope", "t-plain"], factory)
        assert [j.state.value for j in snapshot.jobs] == ["accepted"]
        assert [spec.endpoint for spec in factory.specs] == ["t-plain"]

    def test_an_unsupported_harness_is_refused_before_create(self, tmp_path):
        factory = RecordingFactory()
        snapshot, _ = _run(tmp_path, ["h-bad", "t-plain"], factory)
        assert [j.state.value for j in snapshot.jobs] == ["accepted"]
        assert [spec.endpoint for spec in factory.specs] == ["t-plain"]


class TestTheFactoryIsAlsoTheCapabilityProvider:
    def test_the_injected_factory_supplies_the_advertisement(self, tmp_path):
        factory = RecordingFactory()
        _run(tmp_path, ["t-plain"], factory)
        assert factory.record_reads >= 1

    def test_requirements_match_against_the_injected_providers_records(self, tmp_path):
        table = dict(ConfiguredBackendFactory().capability_records())
        table.pop("claude-cli")
        factory = RecordingFactory(table)
        # With no claude-cli record advertised, a harness entry matches no requirement.
        snapshot, _ = _run(tmp_path, ["h-claude"], factory, requirements={"params": ["cwd"]})
        assert [j.state.value for j in snapshot.jobs] == ["unroutable"]
        assert factory.specs  # it was resolved and built, then refused on its record

    def test_an_injected_capabilities_provider_overrides_the_factorys(self, tmp_path):
        factory = RecordingFactory()
        empty = lambda: {}  # noqa: E731 - an advertisement with no records matches nothing
        path = _job_file(tmp_path, ["t-plain"])
        snapshot = jobs_run.run_job_file(
            path, store_path=tmp_path / "ledger" / "runs.sqlite3", run_id="r",
            backend_factory=factory, capabilities_provider=empty,
        )
        assert [j.state.value for j in snapshot.jobs] == ["unroutable"]
        assert factory.record_reads == 0

    def test_a_factory_that_is_not_a_provider_takes_the_callable_provider(self, tmp_path):
        class Bare:
            def __init__(self) -> None:
                self.inner = RecordingFactory()

            def create(self, spec: BackendSpec) -> FakeBackend:
                return self.inner.create(spec)

        bare = Bare()
        assert isinstance(bare, BackendFactory) and not isinstance(bare, CapabilityProvider)
        table = ConfiguredBackendFactory().capability_records()
        path = _job_file(tmp_path, ["t-plain"])
        snapshot = jobs_run.run_job_file(
            path, store_path=tmp_path / "ledger" / "runs.sqlite3", run_id="r",
            backend_factory=bare, capabilities_provider=lambda: table,
        )
        assert [j.state.value for j in snapshot.jobs] == ["accepted"]
        assert [spec.endpoint for spec in bare.inner.specs] == ["t-plain"]


class TestTheDefaultAndTheLegacyAdapter:
    def test_the_default_is_the_configured_factory_behind_a_resolver(self):
        resolver = jobs_select.selection_callable(None)
        assert isinstance(resolver, EndpointResolver)
        assert isinstance(resolver._factory, ConfiguredBackendFactory)
        assert isinstance(resolver._factory, BackendFactory)
        assert isinstance(resolver._factory, CapabilityProvider)
        assert jobs_select.advertised_capabilities(None) == ConfiguredBackendFactory().capability_records()

    def test_a_backend_factory_is_wrapped_and_never_called_as_a_selection(self):
        factory = RecordingFactory()
        resolver = jobs_select.selection_callable(factory)
        assert isinstance(resolver, EndpointResolver) and resolver._factory is factory
        assert factory.specs == []

    def test_the_legacy_selection_callable_is_used_as_given(self, tmp_path):
        """The named compatibility surface job-kit's re-pointed tests inject."""
        backend = FakeBackend("fake")
        seen: list[str] = []

        def legacy(endpoint: str, **_kw: object) -> BackendSelection:
            seen.append(endpoint)
            return BackendSelection(endpoint, "fake", backend, "fake-model")

        assert jobs_select.selection_callable(legacy) is legacy
        table = {"fake": Capabilities(adapter="fake")}
        path = _job_file(tmp_path, ["ep"])
        snapshot = jobs_run.run_job_file(
            path, store_path=tmp_path / "ledger" / "runs.sqlite3", run_id="r",
            backend_factory=legacy, capabilities_provider=lambda: table,
        )
        assert [j.state.value for j in snapshot.jobs] == ["accepted"]
        assert seen == ["ep"] and len(backend.calls) == 1

    def test_a_legacy_callable_without_a_provider_reads_the_default_advertisement(self):
        assert jobs_select.advertised_capabilities(lambda endpoint, **_kw: None) == (
            ConfiguredBackendFactory().capability_records()
        )

    def test_a_replaced_run_hook_redirects_runs_that_inject_no_factory(self, tmp_path, monkeypatch):
        """``run.create_backend`` / ``run.adapter_capabilities`` stay the module-level default hooks."""
        backend = FakeBackend("fake")
        seen: list[str] = []

        def hook(endpoint: str, **_kw: object) -> BackendSelection:
            seen.append(endpoint)
            return BackendSelection(endpoint, "fake", backend, "fake-model")

        monkeypatch.setattr(jobs_run, "create_backend", hook)
        monkeypatch.setattr(jobs_run, "adapter_capabilities", lambda: {"fake": Capabilities(adapter="fake")})
        path = _job_file(tmp_path, ["ep"])
        snapshot = jobs_run.run_job_file(path, store_path=tmp_path / "ledger" / "runs.sqlite3", run_id="r")
        assert [j.state.value for j in snapshot.jobs] == ["accepted"]
        assert seen == ["ep"] and len(backend.calls) == 1

    def test_an_injected_factory_wins_over_a_replaced_hook(self, tmp_path, monkeypatch):
        def hook(endpoint: str, **_kw: object) -> BackendSelection:
            raise AssertionError("the replaced hook must not be consulted")

        monkeypatch.setattr(jobs_run, "create_backend", hook)
        factory = RecordingFactory()
        snapshot, _ = _run(tmp_path, ["t-plain"], factory)
        assert [j.state.value for j in snapshot.jobs] == ["accepted"]
        assert [spec.endpoint for spec in factory.specs] == ["t-plain"]

    def test_with_nothing_injected_or_replaced_the_default_factory_builds_through_create(
        self, tmp_path, monkeypatch
    ):
        built: list[BackendSpec] = []

        def create(self: ConfiguredBackendFactory, spec: BackendSpec) -> FakeBackend:
            built.append(spec)
            return FakeBackend(_NAMES[spec.backend], self.capability_record(_NAMES[spec.backend]))

        monkeypatch.setattr(ConfiguredBackendFactory, "create", create)
        path = _job_file(tmp_path, ["t-plain"])
        snapshot = jobs_run.run_job_file(path, store_path=tmp_path / "ledger" / "runs.sqlite3", run_id="r")
        assert [j.state.value for j in snapshot.jobs] == ["accepted"]
        assert [spec.backend for spec in built] == ["openrouter"]

    def test_an_object_that_is_neither_is_refused(self):
        with pytest.raises(TypeError, match="backend_factory must be a BackendFactory"):
            jobs_select.selection_callable(object())  # type: ignore[arg-type]


class TestEndpointResolver:
    def test_it_memoizes_by_endpoint_model_cheap_and_root(self):
        factory = RecordingFactory()
        resolver = EndpointResolver(factory)
        first = resolver("t-plain", project_root="/p")
        assert resolver("t-plain", project_root="/p") is first
        assert len(factory.specs) == 1
        resolver("t-plain", project_root="/q")
        resolver("t-plain", project_root="/p", cheap=True)
        resolver("t-plain", project_root="/p", model="x")
        assert len(factory.specs) == 4

    def test_a_failed_resolution_is_not_cached_and_builds_nothing(self):
        factory = RecordingFactory()
        resolver = EndpointResolver(factory)
        for _ in range(2):
            with pytest.raises(EndpointResolveError, match="no endpoint 'nope'"):
                resolver("nope")
        assert factory.specs == []

    def test_the_selection_fields(self):
        resolver = EndpointResolver(RecordingFactory())
        transport = resolver("t-effort", project_root="/p")
        assert (transport.endpoint, transport.kind, transport.model, transport.effort) == (
            "t-effort", "transport", "t-effort-model", "medium"
        )
        assert "effort" in transport.capabilities.params  # specialized to this endpoint
        plain = resolver("t-plain")
        assert plain.effort is None and "effort" not in plain.capabilities.params
        harness = resolver("h-claude", model="override")
        assert (harness.kind, harness.model, harness.effort) == ("harness", "override", "high")
        assert harness.capabilities is harness.backend.capabilities

    def test_the_default_endpoint_is_used_when_none_is_named(self):
        assert EndpointResolver(RecordingFactory())().endpoint == "h-claude"

    def test_a_factory_that_provides_no_capabilities_is_refused_unless_one_is_passed(self):
        class Bare:
            def create(self, spec: BackendSpec) -> FakeBackend:
                return FakeBackend("openrouter")

        with pytest.raises(TypeError, match="needs a CapabilityProvider"):
            EndpointResolver(Bare())  # type: ignore[arg-type]
        EndpointResolver(Bare(), RecordingFactory())  # type: ignore[arg-type]

    def test_a_provider_without_the_transport_family_record_fails_loudly(self):
        table = dict(ConfiguredBackendFactory().capability_records())
        table.pop("openrouter")
        factory = RecordingFactory(table)
        with pytest.raises(LookupError, match="'openrouter'"):
            EndpointResolver(factory)("t-plain")
        assert factory.specs == []

    def test_an_unsupported_harness_names_the_endpoint(self):
        factory = RecordingFactory()
        with pytest.raises(EndpointResolveError, match="endpoint 'h-bad' uses unsupported harness 'zed'"):
            EndpointResolver(factory)("h-bad")
        assert factory.specs == []


def _imports(path: Path) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append((node.lineno, node.module))
    return found


class TestImportEdges:
    def test_jobs_never_import_adapters(self):
        offenders = [
            (path.name, line, name)
            for path in sorted((_LIB / "jobs").glob("*.py"))
            for line, name in _imports(path)
            if name.startswith("llm_kit.adapters") or name.startswith("llm_kit._copy")
        ]
        assert offenders == []

    def test_jobs_do_not_import_the_selection_style_constructor(self):
        """``dispatch.factory.create_backend`` is neither imported nor re-exported by jobs."""
        imported = [
            (path.name, alias.name)
            for path in sorted((_LIB / "jobs").glob("*.py"))
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.name in ("create_backend", "create_transport_backend")
        ]
        assert imported == []
        # run.create_backend is jobs' own default hook, not dispatch.factory.create_backend.
        assert not hasattr(jobs_select, "create_backend")
        assert jobs_run.create_backend.__module__ == "llm_kit.jobs.run"

    def test_the_resolver_imports_only_completion_registry_adapters_and_dispatch(self):
        names = {name for _, name in _imports(_LIB / "dispatch" / "endpoint_resolver.py")}
        packages = {n.split(".")[1] for n in names if n.startswith("llm_kit.")}
        assert packages <= {"completion", "registry", "adapters", "dispatch"}, packages
