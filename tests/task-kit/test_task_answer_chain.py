"""The answer chain: phases by cost-class allow set, persistent re-selection, validation and grounding, and the
llm-kit probe (summary-on-commit-plan.md D2 and U3 checks; databench-integration-plan.md M5, U3 re-cut).

The chain runs against the real ``llm_kit`` library (its ``RunResult``, ``Attempt``, ``RunRequest``,
``BackendOptions`` and ``NoUsableRoutingTarget``) with a fake ``RunFn`` that stands in for ``declaration.run``:
it honours ``exclude`` and ``allow`` as llm-kit does, and replays scripted outcomes. No model is called.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from task_system import answer_chain
from task_system.answer_chain import (
    AnswerRejected, Allowed, LlmKitAbsent, LlmKitTooOld, NoAnswer, ask, check_answer, parse_allow, resolve_allow,
)
from task_system.answers_config import AnswersConfig
from task_system.answers_types import InputBundle, Question

LLM_KIT_LIB = (Path(__file__).resolve().parents[2] / "plugins" / "llm-kit" / "lib").resolve()


@pytest.fixture
def llm(monkeypatch):
    """The real llm-kit library on sys.path for this test (the autouse conftest fixture hides it otherwise)."""
    monkeypatch.syspath_prepend(str(LLM_KIT_LIB))
    from llm_kit.dispatch import declaration  # noqa: PLC0415
    from llm_kit.registry import consent  # noqa: PLC0415

    return SimpleNamespace(declaration=declaration, consent=consent)


ORIENTATION_SCHEMA = {
    "type": "object", "required": ["headline", "next"], "additionalProperties": False,
    "properties": {"headline": {"type": "string", "maxLength": 100}, "next": {"type": "string", "maxLength": 60}},
}
DECISIONS_SCHEMA = {
    "type": "object", "required": ["decisions"], "additionalProperties": False,
    "properties": {"decisions": {"type": "array", "maxItems": 5, "items": {
        "type": "object", "required": ["item", "evidence"], "additionalProperties": False,
        "properties": {"item": {"type": "string"}, "evidence": {"type": "string"}, "default": {"type": "string"}},
    }}},
}


def make_question(schema=ORIENTATION_SCHEMA, grounding=(), models=None, effort=None, prompt="Answer as JSON.") -> Question:
    return Question(
        id="q", version=1, title="Q", applies_to=("active",), inputs=("plan_md",), max_input_chars=1000,
        script=None, prompt=prompt, answer_schema=schema, grounding=tuple(grounding), models=models,
        effort=effort, source=Path("q.yaml"), digest="0" * 64,
    )


BUNDLE = InputBundle(
    task_id="t", texts=(("plan_md", "Use the blue deploy lane.\nDefault: ship on Friday."),), truncated=(),
)
GOOD = json.dumps({"headline": "A headline", "next": "Do the next thing"})
CONFIG = AnswersConfig(models=("qwen", "luna", "haiku"), effort="medium", sources=())
CLASSES = {"qwen": "free", "qwen2": "free", "luna": "usage", "haiku": "usage", "sp": "spend"}


class FakeRun:
    """A ``declaration.run`` stand-in over a fake registry of ``{entry id: cost class}``.

    ``script[entry]`` is a list of outcomes, one consumed per dispatch: ``("ok", text)``, ``("halt", kind)``
    (llm-kit moves on to the next entry within the same run), or ``("error", message)`` (a task error that ends
    the run). Dispatching an entry with nothing scripted fails the test.
    """

    def __init__(self, llm, script, classes=CLASSES):
        self.d = llm.declaration
        self.script = {entry: list(outcomes) for entry, outcomes in script.items()}
        self.classes = classes
        self.calls: list[dict] = []
        self.dispatched: list[str] = []
        self.efforts: list[tuple[str, str | None]] = []

    def __call__(self, names, request, *, project_root, requirements, exclude, max_attempts, reachability_cache, allow):
        d = self.d
        self.calls.append({
            "names": list(names), "exclude": set(exclude), "allow": None if allow is None else set(allow),
            "requirements": requirements, "max_attempts": max_attempts, "options": request.options,
        })
        run_excluded = set(exclude)
        attempts: list = []
        while True:
            candidates = [
                n for n in names
                if n not in run_excluded and (allow is None or self.classes[n] in allow)
            ]
            if not candidates:
                raise d.NoUsableRoutingTarget(names, [], "process")
            entry = candidates[0]
            assert self.script.get(entry), f"{entry} was dispatched with nothing scripted"
            kind, payload = self.script[entry].pop(0)
            self.dispatched.append(entry)
            self.efforts.append((entry, request.options.effort))
            number = len(attempts) + 1
            if kind == "halt":
                attempts.append(d.Attempt(entry, number, None, halt=payload, outcome="halted"))
                run_excluded.add(entry)
                if len(attempts) >= max_attempts:
                    return d.RunResult(d.RUN_ATTEMPT_LIMIT, entry, None, tuple(attempts), "limit")
                continue
            if kind == "error":
                attempts.append(d.Attempt(entry, number, None, error=payload, outcome="failed"))
                return d.RunResult(d.RUN_FAILED, entry, None, tuple(attempts), f"task error: {payload}")
            attempts.append(d.Attempt(entry, number, None, outcome="completed"))
            return d.RunResult(d.RUN_COMPLETED, entry, SimpleNamespace(text=payload), tuple(attempts))


def classifier(entry_id: str) -> str:
    return CLASSES[entry_id]


ALL = ("free", "usage", "spend")


class TestReselection:
    def test_a_halted_entry_is_never_selected_again(self, llm):
        # A halts inside run 1, B then fails with a task error (RUN_FAILED); run 2 must not return to A.
        run = FakeRun(llm, {"qwen": [("halt", "quota")], "qwen2": [("error", "bad")], "luna": [("ok", GOOD)]})
        config = AnswersConfig(models=("qwen", "qwen2", "luna"), effort="medium", sources=())
        asked = ask(make_question(), BUNDLE, config, allow=ALL, run=run, classify=classifier)
        assert run.dispatched == ["qwen", "qwen2", "luna"]
        assert run.calls[1]["exclude"] == {"qwen", "qwen2"}
        assert asked.answerer.entry == "luna" and asked.answerer.tier == "usage"
        assert asked.answerer.attempts == (
            ("qwen", "halted:quota"), ("qwen2", "failed"), ("luna", "completed"),
        )

    def test_the_exclusion_survives_the_phase_change(self, llm):
        run = FakeRun(llm, {"qwen": [("error", "bad")], "luna": [("ok", GOOD)]})
        config = AnswersConfig(models=("qwen", "luna"), effort="medium", sources=())
        ask(make_question(), BUNDLE, config, allow=ALL, run=run, classify=classifier)
        assert run.calls[0]["allow"] == {"free"}
        assert run.calls[1]["allow"] == {"free"} and run.calls[1]["exclude"] == {"qwen"}
        assert run.calls[2]["allow"] == set(ALL) and run.calls[2]["exclude"] == {"qwen"}

    def test_every_entry_failing_is_reported_with_its_attempts(self, llm):
        run = FakeRun(llm, {"qwen": [("error", "x")], "luna": [("halt", "quota")]})
        config = AnswersConfig(models=("qwen", "luna"), effort="medium", sources=())
        with pytest.raises(NoAnswer) as caught:
            ask(make_question(), BUNDLE, config, allow=ALL, run=run, classify=classifier)
        assert caught.value.reason == "all-attempts-failed"
        assert caught.value.attempts == (("qwen", "failed"), ("luna", "halted:quota"))

    def test_a_run_that_reports_no_attempt_cannot_loop(self, llm):
        def silent(names, request, **kwargs):
            return llm.declaration.RunResult(llm.declaration.RUN_FAILED, None, None, (), "nothing")

        with pytest.raises(RuntimeError, match="no new attempt"):
            ask(make_question(), BUNDLE, CONFIG, allow=ALL, run=silent)


class TestPhases:
    def test_a_free_entry_is_dispatched_before_a_paid_one_listed_earlier(self, llm):
        run = FakeRun(llm, {"qwen": [("ok", GOOD)], "luna": [("ok", GOOD)]})
        config = AnswersConfig(models=("luna", "qwen"), effort="medium", sources=())
        asked = ask(make_question(), BUNDLE, config, allow=ALL, run=run, classify=classifier)
        assert run.dispatched == ["qwen"]
        assert asked.answerer.entry == "qwen" and asked.answerer.tier == "free"
        assert run.calls[0]["allow"] == {"free"}

    def test_the_paid_phase_runs_only_when_the_free_phase_gave_no_answer(self, llm):
        run = FakeRun(llm, {"qwen": [("halt", "unreachable")], "luna": [("ok", GOOD)]})
        config = AnswersConfig(models=("luna", "qwen"), effort="medium", sources=())
        asked = ask(make_question(), BUNDLE, config, allow=ALL, run=run, classify=classifier)
        assert run.dispatched == ["qwen", "luna"]
        assert asked.answerer.tier == "usage"

    def test_free_only_runs_one_phase_and_never_dispatches_a_paid_entry(self, llm):
        run = FakeRun(llm, {"qwen": [("error", "x")], "luna": [("ok", GOOD)]})
        config = AnswersConfig(models=("qwen", "luna"), effort="medium", sources=())
        with pytest.raises(NoAnswer):
            ask(make_question(), BUNDLE, config, allow=["free"], run=run, classify=classifier)
        assert run.dispatched == ["qwen"]
        assert {frozenset(call["allow"]) for call in run.calls} == {frozenset({"free"})}

    def test_nothing_usable_under_the_allowed_classes_is_a_waiting_state(self, llm):
        run = FakeRun(llm, {"luna": [("ok", GOOD)]})
        config = AnswersConfig(models=("luna",), effort="medium", sources=())
        with pytest.raises(NoAnswer) as caught:
            ask(make_question(), BUNDLE, config, allow=["free"], run=run)
        assert caught.value.reason == "no-usable-entry" and caught.value.attempts == ()
        assert run.dispatched == []

    def test_consent_without_spend_never_dispatches_a_spend_entry(self, llm, monkeypatch, tmp_path):
        path = tmp_path / "consent.json"
        llm.consent.save_consent(usage=True, spend=False, path=path)
        monkeypatch.setattr(llm.consent, "default_consent_path", lambda: path)
        run = FakeRun(llm, {"sp": [("ok", GOOD)], "luna": [("ok", GOOD)]})
        config = AnswersConfig(models=("sp", "luna"), effort="medium", sources=())
        asked = ask(make_question(), BUNDLE, config, run=run, classify=classifier)
        assert run.dispatched == ["luna"] and asked.answerer.entry == "luna"
        spend_only = AnswersConfig(models=("sp",), effort="medium", sources=())
        with pytest.raises(NoAnswer):
            ask(make_question(), BUNDLE, spend_only, run=run, classify=classifier)
        assert run.dispatched == ["luna"]

    def test_an_absent_consent_file_means_free_only(self, llm, monkeypatch, tmp_path):
        monkeypatch.setattr(llm.consent, "default_consent_path", lambda: tmp_path / "missing.json")
        allowed = resolve_allow(None)
        assert allowed.classes == {"free"} and allowed.source == "consent" and "no consent file" in allowed.note

    def test_an_explicit_allow_overrides_consent(self, llm, monkeypatch, tmp_path):
        path = tmp_path / "consent.json"
        llm.consent.save_consent(usage=False, spend=True, path=path)
        monkeypatch.setattr(llm.consent, "default_consent_path", lambda: path)
        # consent allows spend; the host's explicit set does not
        run = FakeRun(llm, {"sp": [("ok", GOOD)]})
        config = AnswersConfig(models=("sp",), effort="medium", sources=())
        with pytest.raises(NoAnswer):
            ask(make_question(), BUNDLE, config, allow=["free", "usage"], run=run)
        assert run.dispatched == []
        # consent allows nothing beyond free; the host's explicit set allows usage
        llm.consent.save_consent(usage=False, spend=False, path=path)
        run = FakeRun(llm, {"luna": [("ok", GOOD)]})
        config = AnswersConfig(models=("luna",), effort="medium", sources=())
        asked = ask(make_question(), BUNDLE, config, allow=["usage"], run=run, classify=classifier)
        assert asked.answerer.entry == "luna"

    def test_an_explicit_allow_does_not_read_consent(self, llm, monkeypatch):
        def boom():
            raise AssertionError("consent was read")

        monkeypatch.setattr(llm.consent, "load_consent", boom)
        assert resolve_allow(["usage"]).classes == {"free", "usage"}

    def test_free_is_always_allowed_and_unknown_classes_fail_loudly(self, llm):
        assert resolve_allow([]).classes == {"free"}
        with pytest.raises(ValueError, match="unknown cost class"):
            resolve_allow(["bogus"])
        with pytest.raises(ValueError, match="not the string"):
            resolve_allow("usage")
        assert parse_allow("usage, spend") == ("usage", "spend")
        with pytest.raises(ValueError):
            parse_allow("usage,gold")
        given = Allowed(frozenset({"free"}), "explicit")
        assert resolve_allow(given) is given

    def test_the_default_classifier_names_the_class_of_a_paid_answer(self, llm, monkeypatch):
        entry = SimpleNamespace(kind="harness", harness="codex")
        api = answer_chain.probe_llm_kit()
        probed = []
        fake = SimpleNamespace(**{**api.__dict__})
        object.__setattr__(fake, "discover_model_entries", lambda project_root=None: {"luna": entry})
        object.__setattr__(fake, "probe_harness_auth", lambda names, cwd: probed.append(list(names)) or {"codex": "auth"})
        seen = {}

        def classify(e, *, entries, harness_auth):
            seen.update(entry=e, auth=dict(harness_auth))
            return SimpleNamespace(cost_class="usage")

        object.__setattr__(fake, "classify", classify)
        assert answer_chain._default_classifier(fake, None)("luna") == "usage"
        assert probed == [["codex"]] and seen["entry"] is entry and seen["auth"] == {"codex": "auth"}


class TestRequest:
    def test_the_configured_effort_reaches_luna(self, llm):
        run = FakeRun(llm, {"luna": [("ok", GOOD)]})
        config = AnswersConfig(models=("luna",), effort="medium", sources=())
        ask(make_question(), BUNDLE, config, allow=ALL, run=run, classify=classifier)
        assert run.efforts == [("luna", "medium")]
        assert run.calls[0]["requirements"]["params"] == ["effort"]
        assert set(run.calls[0]["requirements"]["guarantees"]) == {"filesystem-write", "shell-exec", "subagent-spawn"}

    def test_the_question_overrides_effort_and_models(self, llm):
        run = FakeRun(llm, {"haiku": [("ok", GOOD)]})
        question = make_question(models=("haiku",), effort="high")
        asked = ask(question, BUNDLE, CONFIG, allow=ALL, run=run, classify=classifier)
        assert run.calls[0]["names"] == ["haiku"] and run.efforts == [("haiku", "high")]
        assert asked.answerer.effort == "high"

    def test_the_prompt_and_inputs_are_the_request(self, llm):
        seen = []

        def run(names, request, **kwargs):
            seen.append(request)
            return llm.declaration.RunResult(
                llm.declaration.RUN_COMPLETED, "qwen", SimpleNamespace(text=GOOD),
                (llm.declaration.Attempt("qwen", 1, None, outcome="completed"),),
            )

        ask(make_question(prompt="SYSTEM"), BUNDLE, CONFIG, allow=["free"], run=run)
        assert seen[0].system == "SYSTEM" and seen[0].prompt == BUNDLE.prompt_text()
        assert seen[0].options.client_id == "task-kit:answers" and seen[0].options.timeout_s == 180.0

    def test_a_deadline_bounds_the_call_and_stops_the_chain(self, llm):
        run = FakeRun(llm, {"qwen": [("ok", GOOD)]})
        ask(make_question(), BUNDLE, CONFIG, allow=["free"], run=run, deadline=130.0, clock=lambda: 100.0)
        assert run.calls[0]["options"].timeout_s == 30.0
        with pytest.raises(NoAnswer) as caught:
            ask(make_question(), BUNDLE, CONFIG, allow=["free"], run=run, deadline=100.0, clock=lambda: 100.0)
        assert caught.value.reason == "deadline" and len(run.calls) == 1

    def test_a_script_only_question_cannot_be_asked(self, llm):
        with pytest.raises(ValueError, match="no prompt"):
            ask(make_question(prompt=None), BUNDLE, CONFIG, allow=["free"], run=FakeRun(llm, {}))


class TestValidation:
    def test_a_rejected_answer_re_selects_with_the_rejected_entry_excluded(self, llm):
        run = FakeRun(llm, {"qwen": [("ok", "not json")], "luna": [("ok", GOOD)]})
        config = AnswersConfig(models=("qwen", "luna"), effort="medium", sources=())
        asked = ask(make_question(), BUNDLE, config, allow=ALL, run=run, classify=classifier)
        assert run.dispatched == ["qwen", "luna"]
        assert asked.answerer.attempts == (("qwen", "rejected: the text is not a JSON object"), ("luna", "completed"))

    def test_an_ungrounded_default_is_rejected(self, llm):
        question = make_question(DECISIONS_SCHEMA, grounding=("decisions[].evidence", "decisions[].default"))
        invented = json.dumps({"decisions": [{"item": "d", "evidence": "Use the blue deploy lane.", "default": "ship on Monday"}]})
        grounded = json.dumps({"decisions": [{"item": "d", "evidence": "Use the blue deploy lane.", "default": "ship on Friday"}]})
        run = FakeRun(llm, {"qwen": [("ok", invented)], "luna": [("ok", grounded)]})
        config = AnswersConfig(models=("qwen", "luna"), effort="medium", sources=())
        asked = ask(question, BUNDLE, config, allow=ALL, run=run, classify=classifier)
        assert asked.answerer.entry == "luna"
        assert asked.answerer.attempts[0][1].startswith("rejected: decisions[].default: not quoted from the inputs")
        only = AnswersConfig(models=("qwen",), effort="medium", sources=())
        run = FakeRun(llm, {"qwen": [("ok", invented)]})
        with pytest.raises(NoAnswer) as caught:
            ask(question, BUNDLE, only, allow=["free"], run=run)
        assert caught.value.reason == "all-attempts-failed"

    def test_grounding_ignores_line_breaks_and_an_absent_optional_field(self):
        question = make_question(DECISIONS_SCHEMA, grounding=("decisions[].evidence", "decisions[].default"))
        value = {"decisions": [{"item": "d", "evidence": "deploy lane. Default:"}]}
        assert check_answer(question, BUNDLE, value) == value

    def test_grounding_survives_typographic_punctuation_in_the_inputs(self):
        bundle = InputBundle("t", (("plan_md", "It\u2019s a \u201cquoted\u201d line"),), ())
        question = make_question(DECISIONS_SCHEMA, grounding=("decisions[].evidence",))
        value = {"decisions": [{"item": "d", "evidence": "It\u2019s a \u201cquoted\u201d line"}]}
        assert check_answer(question, bundle, value)["decisions"][0]["evidence"] == "It's a \"quoted\" line"

    def test_typographic_punctuation_becomes_ascii(self):
        value = {"headline": "a \u2014 b \u2013 c \u2026 d \u2192 e \u2018f\u2019 \u201cg\u201d", "next": "x"}
        assert check_answer(make_question(), BUNDLE, value)["headline"] == "a -- b -- c ... d -> e 'f' \"g\""

    @pytest.mark.parametrize("value,fragment", [
        ({"headline": "caf\u00e9", "next": "x"}, "non-ASCII"),
        ({"headline": "two\nlines", "next": "x"}, "more than one line"),
        ({"headline": "   ", "next": "x"}, "empty"),
        ({"headline": "h"}, "missing required 'next'"),
        ({"headline": "h", "next": "x", "extra": "y"}, "unexpected key 'extra'"),
        ({"headline": "h" * 101, "next": "x"}, "longer than 100"),
        ({"headline": 3, "next": "x"}, "expected a string"),
        ([1], "not a JSON object"),
    ])
    def test_rejections(self, value, fragment):
        with pytest.raises(AnswerRejected, match=fragment):
            check_answer(make_question(), BUNDLE, value)

    def test_conversion_happens_before_the_length_check(self):
        with pytest.raises(AnswerRejected, match="longer than 60"):
            check_answer(make_question(), BUNDLE, {"headline": "h", "next": "\u2014" * 31})

    def test_a_code_fence_is_tolerated_and_prose_is_not(self):
        assert answer_chain.parse_answer("```json\n{\"a\": 1}\n```") == {"a": 1}
        assert answer_chain.parse_answer("  {\"a\": 1}  ") == {"a": 1}
        with pytest.raises(AnswerRejected):
            answer_chain.parse_answer("Here you go: {\"a\": 1}")
        with pytest.raises(AnswerRejected):
            answer_chain.parse_answer("[1]")


class TestLlmKitProbe:
    def _fake_library(self, monkeypatch, *, drop=None, allow_parameter=True):
        """A complete fake llm_kit; ``drop`` names one ``module.symbol`` to leave out."""
        def run(names, request, *, allow=None):  # noqa: ARG001
            return None

        def run_without_allow(names, request):  # noqa: ARG001
            return None

        symbols = {
            "llm_kit.dispatch.declaration": {
                "run": run if allow_parameter else run_without_allow, "RunRequest": object,
                "NoUsableRoutingTarget": Exception, "RUN_COMPLETED": "completed",
            },
            "llm_kit.completion.types": {"BackendOptions": object},
            "llm_kit.registry.consent": {"load_consent": object},
            "llm_kit.registry.cost": {"classify": object},
            "llm_kit.registry.models": {"discover_model_entries": object},
            "llm_kit.registry.model_endpoints": {"HARNESS_KIND": "harness"},
            "llm_kit.adapters.harness_auth": {"probe_harness_auth": object},
        }
        root = ModuleType("llm_kit")
        root.__path__ = []
        monkeypatch.setitem(sys.modules, "llm_kit", root)
        for name, attrs in symbols.items():
            if name == drop:
                continue
            module = ModuleType(name)
            for key, value in attrs.items():
                if f"{name}.{key}" != drop:
                    setattr(module, key, value)
            monkeypatch.setitem(sys.modules, name, module)
            parent, _, leaf = name.rpartition(".")
            package = sys.modules.get(parent)
            if package is None:
                package = ModuleType(parent)
                package.__path__ = []
                monkeypatch.setitem(sys.modules, parent, package)
            setattr(package, leaf, module)

    def test_absent_library(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "llm_kit", None)
        with pytest.raises(LlmKitAbsent) as caught:
            ask(make_question(), BUNDLE, CONFIG, allow=["free"], run=lambda *a, **k: None)
        assert "not installed" in str(caught.value)
        assert "claude plugin install llm-kit@plugins-kit" in str(caught.value)
        assert caught.value.state == "absent"

    @pytest.mark.parametrize("drop", [
        "llm_kit.dispatch.declaration.run",
        "llm_kit.registry.consent.load_consent",
        "llm_kit.registry.consent",
        "llm_kit.registry.cost.classify",
    ])
    def test_too_old_library_names_a_different_remedy(self, monkeypatch, drop):
        self._fake_library(monkeypatch, drop=drop)
        with pytest.raises(LlmKitTooOld) as caught:
            answer_chain.probe_llm_kit()
        message = str(caught.value)
        assert "too old or stale" in message and "not installed" not in message
        assert "claude plugin update llm-kit@plugins-kit" in message and "0.4.0" in message
        assert caught.value.state == "too-old"

    def test_a_run_without_the_allow_parameter_is_too_old(self, monkeypatch):
        self._fake_library(monkeypatch, allow_parameter=False)
        with pytest.raises(LlmKitTooOld, match="no allow parameter"):
            answer_chain.probe_llm_kit()

    def test_a_complete_library_probes_clean(self, monkeypatch):
        self._fake_library(monkeypatch)
        assert answer_chain.probe_llm_kit().RUN_COMPLETED == "completed"

    def test_the_two_failure_messages_differ(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "llm_kit", None)
        with pytest.raises(LlmKitAbsent) as absent:
            answer_chain.probe_llm_kit()
        self._fake_library(monkeypatch, drop="llm_kit.registry.cost.classify")
        with pytest.raises(LlmKitTooOld) as old:
            answer_chain.probe_llm_kit()
        assert str(absent.value) != str(old.value)

    def test_the_probe_accepts_the_real_library(self, llm):
        api = answer_chain.probe_llm_kit()
        assert api.run is llm.declaration.run
