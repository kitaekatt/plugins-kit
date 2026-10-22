"""The model answerer chain of the task questions (summary-on-commit-plan.md D2; databench-integration-plan.md M5, U3 re-cut).

``ask`` answers one question on one task from its input bundle through llm-kit's declaration seam and returns
the validated answer with its provenance.

**Phases are cost-class ``allow`` sets.** Phase 1 allows ``{"free"}``. Phase 2 allows the caller's classes and
runs only when they hold more than ``free``. Free-before-paid therefore holds whatever order the declaration
lists, and an entry whose class is not allowed is never dispatched (llm-kit reports it switched off).
``resolve_allow`` decides the caller's classes: an explicit set (the host's ``--allow``) wins; otherwise
llm-kit's per-machine consent (``llm_kit.registry.consent.load_consent``). ``free`` is always allowed.

**Re-selection is persistent.** Within a phase llm-kit moves on by itself after a classified halt or a launch
failure. Whatever else ends a run (a task error, or an answer this module rejects), every entry the run
attempted joins one exclusion set that is passed to every later run in both phases, so a halted or rejected
entry is never selected again for this question.

**Validation.** The model text must be one JSON object (a code fence around it is tolerated). Typographic dashes,
curly quotes, the ellipsis and the right arrow become ASCII; any other non-ASCII character is rejected. The
object must match the question's answer schema (``questions.validate_value``); every string must be single-line
and non-empty; every ``grounding`` field must quote the inputs (a whitespace-normalised verbatim substring,
compared after the same ASCII conversion of the inputs). A rejected answer is a task error for the chain.

**llm-kit is optional (plugins/CLAUDE.md, optional plugin dependencies).** Q1: task-kit does its job without it
(script answers and every other verb work), so it is not REQUIRED. Q2: an answer would be read as a model's
answer and a missing model produces no answer to disclose in, so model answering REFUSES: ``ask`` raises
``LlmKitAbsent`` or ``LlmKitTooOld`` and the enclosing pass or dashboard names the failed unit. Nothing
substitutes. The probe targets the newest symbols used (llm-kit 0.4.0: ``declaration.run(allow=)``,
``registry.consent.load_consent``, ``registry.cost.classify``).
"""

from __future__ import annotations

import importlib
import inspect
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from . import questions
from .answers_config import AnswersConfig
from .answers_types import Answerer, InputBundle, Question, RunFn

FREE = "free"
COST_CLASSES = ("free", "usage", "spend")
LLM_KIT_MIN_VERSION = "0.4.0"
INSTALL_COMMAND = "claude plugin install llm-kit@plugins-kit"
UPDATE_COMMAND = "claude plugin update llm-kit@plugins-kit"
CALL_TIMEOUT_S = 180.0
CLIENT_ID = "task-kit:answers"
# A text-only call: the entry must not be given tools that write, execute or spawn.
GUARANTEES = ("filesystem-write", "shell-exec", "subagent-spawn")

_ASCII_MAP = {
    "\u2013": "--", "\u2014": "--",
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2026": "...",
    "\u2192": "->",
}
_FENCE_RE = re.compile(r"\A```[A-Za-z]*[ \t]*\n(.*)\n```\Z", re.S)


class ModelAnsweringUnavailable(RuntimeError):
    """llm-kit cannot answer. ``state`` is ``absent`` or ``too-old``; the message names the remedy command."""

    state = ""


class LlmKitAbsent(ModelAnsweringUnavailable):
    state = "absent"


class LlmKitTooOld(ModelAnsweringUnavailable):
    state = "too-old"


class AnswerRejected(ValueError):
    """The model's text is not an acceptable answer. The message states why, on one line."""


class NoAnswer(RuntimeError):
    """No entry produced an acceptable answer.

    ``reason``: ``no-usable-entry`` (every entry the allowed classes admit was exhausted or unusable; with
    nothing attempted this is the "waiting: no answerer usable" case), ``all-attempts-failed`` (entries were
    tried and none answered), or ``deadline``. ``attempts`` lists every ``(entry, outcome)`` tried, in order.
    """

    def __init__(self, reason: str, attempts: tuple[tuple[str, str], ...], detail: str = "") -> None:
        self.reason = reason
        self.attempts = attempts
        self.detail = detail
        text = f"no answer ({reason})"
        if attempts:
            text += "; tried " + ", ".join(f"{entry}={outcome}" for entry, outcome in attempts)
        if detail:
            text += f": {detail}"
        super().__init__(text)


@dataclass(frozen=True)
class Asked:
    answer: Mapping[str, Any]
    answerer: Answerer


@dataclass(frozen=True)
class Allowed:
    """The cost classes a caller allows. ``source`` is ``explicit`` or ``consent``; ``note`` is llm-kit's consent
    note (empty unless consent was absent or unreadable), which the caller prints."""

    classes: frozenset[str]
    source: str
    note: str = ""


# --- llm-kit (optional) ---------------------------------------------------------


@dataclass(frozen=True)
class LlmKitApi:
    run: Callable[..., Any]
    RunRequest: Any
    BackendOptions: Any
    NoUsableRoutingTarget: type
    RUN_COMPLETED: str
    load_consent: Callable[..., Any]
    classify: Callable[..., Any]
    discover_model_entries: Callable[..., Any]
    probe_harness_auth: Callable[..., Any]
    HARNESS_KIND: str


_USED = (
    ("llm_kit.dispatch.declaration", ("run", "RunRequest", "NoUsableRoutingTarget", "RUN_COMPLETED")),
    ("llm_kit.completion.types", ("BackendOptions",)),
    ("llm_kit.registry.consent", ("load_consent",)),
    ("llm_kit.registry.cost", ("classify",)),
    ("llm_kit.registry.models", ("discover_model_entries",)),
    ("llm_kit.registry.model_endpoints", ("HARNESS_KIND",)),
    ("llm_kit.adapters.harness_auth", ("probe_harness_auth",)),
)


def _too_old(what: str) -> LlmKitTooOld:
    return LlmKitTooOld(
        f"llm-kit is too old or stale ({what}); model answers need llm-kit {LLM_KIT_MIN_VERSION} or later; "
        f"update it with: {UPDATE_COMMAND}"
    )


def probe_llm_kit() -> LlmKitApi:
    """Import the llm-kit symbols this module uses, telling an absent library from a too-old one."""
    try:
        importlib.import_module("llm_kit")
    except ModuleNotFoundError as exc:
        if exc.name == "llm_kit":
            raise LlmKitAbsent(
                "llm-kit is not installed, so model answers are unavailable (script answers and every other "
                f"task verb still work); install it with: {INSTALL_COMMAND}"
            ) from exc
        raise _too_old(str(exc)) from exc
    except ImportError as exc:
        raise _too_old(str(exc)) from exc
    modules: dict[str, Any] = {}
    for module_name, names in _USED:
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise _too_old(f"{module_name} is missing") from exc
        for name in names:
            if not hasattr(module, name):
                raise _too_old(f"{module_name}.{name} is missing")
        modules[module_name] = module
    declaration = modules["llm_kit.dispatch.declaration"]
    if "allow" not in inspect.signature(declaration.run).parameters:
        raise _too_old("llm_kit.dispatch.declaration.run has no allow parameter")
    return LlmKitApi(
        run=declaration.run,
        RunRequest=declaration.RunRequest,
        BackendOptions=modules["llm_kit.completion.types"].BackendOptions,
        NoUsableRoutingTarget=declaration.NoUsableRoutingTarget,
        RUN_COMPLETED=declaration.RUN_COMPLETED,
        load_consent=modules["llm_kit.registry.consent"].load_consent,
        classify=modules["llm_kit.registry.cost"].classify,
        discover_model_entries=modules["llm_kit.registry.models"].discover_model_entries,
        probe_harness_auth=modules["llm_kit.adapters.harness_auth"].probe_harness_auth,
        HARNESS_KIND=modules["llm_kit.registry.model_endpoints"].HARNESS_KIND,
    )


def parse_allow(text: str) -> tuple[str, ...]:
    """``free,usage`` -> ``("free", "usage")`` (the value of a host's ``--allow``); unknown classes are an error."""
    names = tuple(part.strip() for part in text.split(",") if part.strip())
    _check_classes(names)
    return names


def _check_classes(names: Iterable[str]) -> None:
    unknown = sorted({str(name) for name in names if name not in COST_CLASSES})
    if unknown:
        raise ValueError(f"unknown cost class {unknown}; valid classes: {', '.join(COST_CLASSES)}")


def resolve_allow(allow: "Allowed | Iterable[str] | None" = None, *, api: LlmKitApi | None = None) -> Allowed:
    """The caller's allowed cost classes. An explicit set wins and consent is not read; ``None`` reads llm-kit's
    consent. ``free`` is always allowed."""
    if isinstance(allow, Allowed):
        return allow
    if allow is not None:
        if isinstance(allow, str):
            raise ValueError(f"allow must be an iterable of cost classes, not the string {allow!r}")
        names = tuple(allow)
        _check_classes(names)
        return Allowed(classes=frozenset(names) | {FREE}, source="explicit")
    consent = (api or probe_llm_kit()).load_consent()
    return Allowed(classes=frozenset(consent.allowed) | {FREE}, source="consent", note=consent.note)


# --- answer validation -----------------------------------------------------------


def to_ascii(text: str) -> str:
    """Typographic dashes, curly quotes, the ellipsis and the right arrow as ASCII; everything else unchanged."""
    return "".join(_ASCII_MAP.get(char, char) for char in text)


def parse_answer(text: str) -> dict[str, Any]:
    """The one JSON object in the model text (a surrounding code fence is tolerated)."""
    body = text.strip()
    fenced = _FENCE_RE.match(body)
    if fenced:
        body = fenced.group(1).strip()
    try:
        value = json.loads(body)
    except ValueError as exc:
        raise AnswerRejected("the text is not a JSON object") from exc
    if not isinstance(value, dict):
        raise AnswerRejected("the text is not a JSON object")
    return value


def _asciify(value: Any, where: str) -> Any:
    if isinstance(value, str):
        converted = to_ascii(value)
        bad = sorted({char for char in converted if ord(char) > 127})
        if bad:
            raise AnswerRejected(f"{where}: non-ASCII character {bad[0]!r}")
        if "\n" in converted or "\r" in converted:
            raise AnswerRejected(f"{where}: more than one line")
        if not converted.strip():
            raise AnswerRejected(f"{where}: empty")
        return converted
    if isinstance(value, list):
        return [_asciify(item, f"{where}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, dict):
        return {key: _asciify(item, f"{where}.{key}") for key, item in value.items()}
    return value


def _squash(text: str) -> str:
    return " ".join(text.split())


def check_answer(question: Question, bundle: InputBundle, value: Any) -> dict[str, Any]:
    """The answer converted to ASCII, or ``AnswerRejected`` stating the first problem."""
    if not isinstance(value, dict):
        raise AnswerRejected("the answer is not a JSON object")
    answer = _asciify(value, "answer")
    errors = questions.validate_value(question.answer_schema, answer)
    if errors:
        raise AnswerRejected(errors[0])
    haystack = _squash(to_ascii("\n".join(text for _, text in bundle.texts)))
    for path in question.grounding:
        for quoted in questions.grounded_values(answer, path):
            if _squash(quoted) not in haystack:
                raise AnswerRejected(f"{path}: not quoted from the inputs: {_squash(quoted)[:60]!r}")
    return answer


# --- the chain -------------------------------------------------------------------


def effective_models(question: Question, config: AnswersConfig) -> tuple[str, ...]:
    """The question's own declaration when it has one, else the configured chain."""
    return tuple(question.models) if question.models else tuple(config.models)


def effective_effort(question: Question, config: AnswersConfig) -> str:
    return question.effort or config.effort


def _default_classifier(api: LlmKitApi, project_root: Path | str | None) -> Callable[[str], str]:
    root = None if project_root is None else str(project_root)
    entries = api.discover_model_entries(project_root=root)
    auth: dict[str, Any] = {}
    cwd = Path(project_root or Path.cwd()).resolve()

    def classify(entry_id: str) -> str:
        entry = entries.get(entry_id)
        if entry is not None and getattr(entry, "kind", None) == api.HARNESS_KIND:
            harness = (getattr(entry, "harness", None) or "").lower()
            if harness in ("claude", "codex") and harness not in auth:
                auth.update(api.probe_harness_auth([harness], cwd=cwd))
        return api.classify(entry, entries=entries, harness_auth=auth).cost_class

    return classify


def _outcome(attempt: Any) -> str:
    outcome = getattr(attempt, "outcome", "") or "failed"
    halt = getattr(attempt, "halt", None)
    return f"{outcome}:{halt}" if outcome == "halted" and halt else outcome


def ask(
    question: Question,
    bundle: InputBundle,
    config: AnswersConfig,
    *,
    allow: "Allowed | Iterable[str] | None" = None,
    deadline: float | None = None,
    reachability_cache: dict | None = None,
    run: RunFn | None = None,
    classify: Callable[[str], str] | None = None,
    project_root: Path | str | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Asked:
    """Answer ``question`` from ``bundle`` through the declaration seam.

    ``allow``: the caller's cost classes (see ``resolve_allow``); ``None`` reads llm-kit's consent on every call, so a
    pass resolves once, prints ``Allowed.note`` and passes the ``Allowed`` in.
    ``deadline``: absolute ``time.monotonic()`` seconds, or None. ``run``: replaces ``declaration.run``
    (called as ``run(names, request, project_root=, requirements=, exclude=, max_attempts=,
    reachability_cache=, allow=)``). ``classify``: replaces the entry-id -> cost-class lookup used for the
    answerer tier of a phase 2 answer. Raises ``LlmKitAbsent`` / ``LlmKitTooOld`` (REFUSE), ``NoAnswer``.
    """
    if not question.prompt:
        raise ValueError(f"question {question.id} has no prompt, so no model can answer it")
    api = probe_llm_kit()
    allowed = resolve_allow(allow, api=api)
    models = effective_models(question, config)
    effort = effective_effort(question, config)
    call = run if run is not None else api.run
    cache = reachability_cache if reachability_cache is not None else {}
    requirements = {"guarantees": list(GUARANTEES), "params": ["effort"]}
    phases = [frozenset({FREE})]
    if allowed.classes - {FREE}:
        phases.append(frozenset(allowed.classes))
    excluded: set[str] = set()
    attempts: list[tuple[str, str]] = []
    tier_of = classify

    for phase, phase_allow in enumerate(phases, start=1):
        while True:
            remaining = None if deadline is None else deadline - clock()
            if remaining is not None and remaining <= 0:
                raise NoAnswer("deadline", tuple(attempts), "the deadline passed before an answer")
            timeout = CALL_TIMEOUT_S if remaining is None else min(remaining, CALL_TIMEOUT_S)
            request = api.RunRequest(
                system=question.prompt,
                prompt=bundle.prompt_text(),
                options=api.BackendOptions(effort=effort, timeout_s=timeout, client_id=CLIENT_ID),
            )
            try:
                result = call(
                    list(models), request, project_root=project_root, requirements=requirements,
                    exclude=frozenset(excluded), max_attempts=max(1, len(models) - len(excluded)),
                    reachability_cache=cache, allow=phase_allow,
                )
            except api.NoUsableRoutingTarget:
                break  # this phase has nothing left to try
            tried = [getattr(a, "entry") for a in result.attempts]
            fresh = [entry for entry in tried if entry not in excluded]
            if not fresh:
                raise RuntimeError(f"llm-kit ran {models} with no new attempt; refusing to loop")
            outcomes = [(getattr(a, "entry"), _outcome(a)) for a in result.attempts]
            excluded.update(tried)
            if result.status == api.RUN_COMPLETED:
                try:
                    answer = check_answer(question, bundle, parse_answer(getattr(result.response, "text", "")))
                except AnswerRejected as exc:
                    entry, _ = outcomes[-1]
                    outcomes[-1] = (entry, f"rejected: {exc}")
                    attempts.extend(outcomes)
                    continue
                attempts.extend(outcomes)
                if phase == 1:
                    tier = FREE
                else:
                    if tier_of is None:
                        tier_of = _default_classifier(api, project_root)
                    tier = tier_of(result.entry)
                return Asked(
                    answer=answer,
                    answerer=Answerer(
                        kind="model", entry=result.entry, name=None, tier=tier, effort=effort,
                        attempts=tuple(attempts),
                    ),
                )
            attempts.extend(outcomes)
    reason = "all-attempts-failed" if attempts else "no-usable-entry"
    raise NoAnswer(reason, tuple(attempts), f"allowed classes: {', '.join(sorted(allowed.classes))}")
