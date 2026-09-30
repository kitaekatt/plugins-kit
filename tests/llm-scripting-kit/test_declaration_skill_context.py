"""``declaration.run`` with ``options.skill_context``: selection and provenance.

Skill contexts are built from real skill files through the real
``bootstrap_lib.skill_material`` (``materialize_skill_context``). Entries,
reachability and backends are injected, and selection reads the REAL adapter
advertisement (``capabilities`` is not passed), so the harness entries are
skipped because their records have no ``skill_context`` block, not because a
test said so.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import pytest

from bootstrap_lib import execution_event as ee
from llm_scripting_kit import declaration as decl
from llm_scripting_kit.completion import BackendOptions
from llm_scripting_kit.completion.contract import OutputContractViolation
from llm_scripting_kit.completion.halt import HALT_AUTH
from llm_scripting_kit.completion.skill_context import materialize_skill_context
from llm_scripting_kit.completion.types import LLMResponse, ResponseError
from llm_scripting_kit.model_endpoints import HARNESS_KIND, TRANSPORT_KIND, EndpointEntry
from llm_scripting_kit.models import EndpointResolveError
from llm_scripting_kit.reachability import STATUS_REACHABLE, Reachability

SENTINEL_SKILL = "sentinel-skill-zq7"
SENTINEL_BODY = "sentinel-body-text-kx9"


def _write_skill(root, name=SENTINEL_SKILL, body=SENTINEL_BODY):
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    text = f"---\nname: {name}\ndescription: The {name} skill.\n---\n{body}\n"
    (skill_dir / "SKILL.md").write_bytes(text.encode("utf-8"))
    return skill_dir


def _context(tmp_path, name=SENTINEL_SKILL, body=SENTINEL_BODY):
    _write_skill(tmp_path, name=name, body=body)
    return materialize_skill_context(
        {"skills": [{"path": name}], "token_budget": 10_000}, base_dir=tmp_path
    )


def _harness(entry_id, harness="claude"):
    return EndpointEntry(
        id=entry_id, base_url=None, model=f"{entry_id}-model", kind=HARNESS_KIND,
        harness=harness, tier=None, family=None, conserve_usage=None,
    )


def _transport(entry_id):
    return EndpointEntry(
        id=entry_id, base_url=f"http://{entry_id}.invalid/v1", model="test/slug"
    )


def _reach():
    return Reachability(status=STATUS_REACHABLE, checked="cli-version", detail=STATUS_REACHABLE)


class _Halt(Exception):
    def __init__(self, kind):
        super().__init__(f"halt {kind}")
        self.kind = kind


class _Backend:
    def __init__(self, name, outcomes, on_call=None):
        self.name = name
        self.outcomes = list(outcomes)
        self.calls = 0
        self.on_call = on_call
        self.texts = []

    def complete(self, system, user, *, model, options=None):
        self.calls += 1
        context = getattr(options, "skill_context", None)
        self.texts.append(None if context is None else context.text)
        if self.on_call:
            self.on_call()
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def classify_halt(self, exc):
        return getattr(exc, "kind", None)


@dataclass
class _Selection:
    endpoint: str
    kind: str
    backend: Any
    model: str
    effort: Optional[str] = None


def _factory_for(backends, kinds):
    def factory(name, **_kw):
        if name not in backends:
            raise EndpointResolveError(f"unknown endpoint '{name}'")
        return _Selection(name, kinds[name], backends[name], _entries()[name].model)

    return factory


def _entries():
    return {
        "opus": _harness("opus"),
        "sol": _harness("sol", "codex"),
        "or-a": _transport("or-a"),
        "or-b": _transport("or-b"),
    }


_KINDS = {"opus": HARNESS_KIND, "sol": HARNESS_KIND, "or-a": TRANSPORT_KIND, "or-b": TRANSPORT_KIND}


def _run(names, backends, options, *, observer=None, max_attempts=1, **kwargs):
    return decl.run(
        list(names), decl.RunRequest(system="s", prompt="p", options=options),
        entries=_entries(), backend_factory=_factory_for(backends, _KINDS),
        reachability_cache={name: _reach() for name in _entries()},
        max_attempts=max_attempts, observer=observer, **kwargs,
    )


@pytest.fixture
def sink():
    return ee.InMemorySink()


@pytest.fixture
def observer(sink):
    return ee.Emitter("job-kit", "run-1", unit_id="unit-1", sinks=[sink])


def _ok():
    return LLMResponse(text="ok", model="served-model")


def _selected(sink):
    return [e for e in sink.events if e["event"] == "dispatch-selected"]


class TestSelection:
    def test_run_skips_harness_entry_when_skill_context_set(self, tmp_path):
        context = _context(tmp_path)
        backends = {
            "opus": _Backend("claude-cli", []),
            "or-a": _Backend("openrouter", [_ok()]),
        }
        result = _run(["opus", "or-a"], backends, BackendOptions(skill_context=context))
        assert result.status == decl.RUN_COMPLETED and result.entry == "or-a"
        assert backends["opus"].calls == 0
        assert backends["or-a"].texts == [context.text]

    def test_run_harness_only_raises_floor_requirements_mismatch(self, tmp_path):
        context = _context(tmp_path)
        backends = {"opus": _Backend("claude-cli", []), "sol": _Backend("codex-cli", [])}
        with pytest.raises(decl.NoUsableRoutingTarget) as caught:
            _run(["opus", "sol"], backends, BackendOptions(skill_context=context))
        assert {d.id: d.disposition for d in caught.value.dispositions} == {
            "opus": decl.DISPOSITION_REQUIREMENTS_MISMATCH,
            "sol": decl.DISPOSITION_REQUIREMENTS_MISMATCH,
        }
        assert backends["opus"].calls == backends["sol"].calls == 0

    def test_run_without_skill_context_still_routes_to_the_harness_entry(self):
        backends = {"opus": _Backend("claude-cli", [_ok()]), "or-a": _Backend("openrouter", [])}
        result = _run(["opus", "or-a"], backends, BackendOptions())
        assert result.entry == "opus" and backends["or-a"].calls == 0

    def test_run_conflicting_skill_context_requirement_raises_value_error(self, tmp_path):
        context = _context(tmp_path)
        backends = {"or-a": _Backend("openrouter", [])}
        with pytest.raises(ValueError, match="conflicting requirements"):
            _run(
                ["or-a"], backends, BackendOptions(skill_context=context),
                requirements={"skill_context": {"delivery": "other"}},
            )
        assert backends["or-a"].calls == 0


class TestPayload:
    def test_dispatch_selected_payload_unchanged_without_skill_context(self, observer, sink):
        backends = {"or-a": _Backend("openrouter", [_ok()])}
        _run(["or-a"], backends, BackendOptions(), observer=observer)
        assert [e["payload"] for e in _selected(sink)] == [{"entry": "or-a"}]
        ee.validate_stream(sink.events)

    def test_skill_context_payload_is_exactly_digest_skills_estimated_tokens(
        self, tmp_path, observer, sink
    ):
        context = _context(tmp_path)
        backends = {"or-a": _Backend("openrouter", [_ok()])}
        _run(["or-a"], backends, BackendOptions(skill_context=context), observer=observer)
        payload = _selected(sink)[0]["payload"]
        assert payload == {
            "entry": "or-a",
            "skill_context": {
                "digest": context.report.digest,
                "skills": 1,
                "estimated_tokens": context.report.estimated_tokens,
            },
        }
        assert set(payload["skill_context"]) == {"digest", "skills", "estimated_tokens"}
        serialized = repr(sink.events)
        assert SENTINEL_SKILL not in serialized
        assert SENTINEL_BODY not in serialized
        assert str(tmp_path) not in serialized

    def test_skill_context_payload_validates_under_schema_v1(self, tmp_path, observer, sink):
        context = _context(tmp_path)
        backends = {"or-a": _Backend("openrouter", [_ok()])}
        _run(["or-a"], backends, BackendOptions(skill_context=context), observer=observer)
        for event in sink.events:
            ee.validate_event(event)
        ee.validate_stream(sink.events)
        assert "skill_context" in _selected(sink)[0]["payload"]

    def test_every_dispatch_selected_carries_the_key_with_the_same_digest(
        self, tmp_path, observer, sink
    ):
        context = _context(tmp_path)
        backends = {
            "or-a": _Backend("openrouter", [_Halt(HALT_AUTH)]),
            "or-b": _Backend("openrouter", [_ok()]),
        }
        _run(["or-a", "or-b"], backends, BackendOptions(skill_context=context),
             observer=observer, max_attempts=2)
        keys = [e["payload"]["skill_context"]["digest"] for e in _selected(sink)]
        assert keys == [context.report.digest, context.report.digest]


class TestAttempts:
    def test_every_attempt_sends_identical_skill_block(self, tmp_path):
        skill_dir = _write_skill(tmp_path)
        context = materialize_skill_context(
            {"skills": [{"path": SENTINEL_SKILL}], "token_budget": 10_000}, base_dir=tmp_path
        )

        def rewrite():
            _write_skill(tmp_path, body="a different body written between attempts")

        backends = {
            "or-a": _Backend("openrouter", [_Halt(HALT_AUTH)], on_call=rewrite),
            "or-b": _Backend("openrouter", [_ok()]),
        }
        result = _run(
            ["or-a", "or-b"], backends, BackendOptions(skill_context=context), max_attempts=2
        )
        assert result.status == decl.RUN_COMPLETED and result.entry == "or-b"
        assert "a different body" in (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        assert backends["or-a"].texts == backends["or-b"].texts == [context.text]
        assert SENTINEL_BODY in backends["or-b"].texts[0]

    def test_run_contract_violation_response_keeps_skill_context_report(self, tmp_path):
        # The REAL openrouter adapter under run(), over a fake SDK client: the
        # report is set before the contract is finalized, and run hands the
        # violation's response back unchanged.
        from llm_scripting_kit.completion import OutputContract
        from llm_scripting_kit.completion.backends import OpenRouterBackend
        from llm_scripting_kit.completion.contract import POLICY_VALIDATED_RESULT

        context = _context(tmp_path)
        backend = OpenRouterBackend(client=_Client("not json"))
        options = BackendOptions(
            skill_context=context,
            output_contract=OutputContract(
                "t.skill", POLICY_VALIDATED_RESULT, {"type": "object", "required": ["answer"]}
            ),
        )
        result = _run(["or-a"], {"or-a": backend}, options)
        assert result.status == decl.RUN_FAILED
        report = result.response.skill_context
        assert report is not None
        assert (report.adapter, report.delivery, report.digest) == (
            "openrouter", "system-message", context.report.digest,
        )


class _Client:
    """The OpenAI SDK seam, below ``OpenRouterBackend.complete``."""

    def __init__(self, answer):
        self.calls = []
        outer = self

        class _Message:
            content = answer
            reasoning_content = None

        class _Choice:
            message = _Message()
            finish_reason = "stop"

        class _Response:
            choices = [_Choice()]
            usage = None

        class _Completions:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                return _Response()

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()
