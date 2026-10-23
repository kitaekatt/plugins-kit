"""Measured, exact-model reasoning-effort menus.

An effort menu is a fact about ONE model reached over ONE route, not about a
harness or a wire convention. ``gpt-6-luna`` stops at ``max`` while
``gpt-6.1-sol`` also takes ``ultra``; the Claude CLI takes no ``none``; one
OpenRouter model takes ``none`` and another does not. This module is the single
library-owned table of those menus. Four consumers share the one validator
below, so no path can bypass it: catalogue writes and selection resolution
(``project_models``), ``CodexAdapter`` and ``CodexCliBackend``.

The data is MEASURED AT BUILD TIME and shipped as data. Nothing here (or in any
module of the library) reads a user cache, ``~/.codex``, ``~/.claude`` or the
network at runtime: a plain ``pip install`` user with no AI harness gets the
same menus as a developer with every CLI installed. Each profile records where
its menu came from and the day it was taken; a model that is not listed has NO
exact menu, and the library invents none for it.

Measurement sources (no model call was made for any of them):

``codex-models-cache``
    The ``supported_reasoning_levels`` of each model in the Codex CLI's own
    model catalogue (``models_cache.json``, ``client_version`` 0.164.0,
    ``fetched_at`` 2026-10-23T15:06:47Z, etag ``W/"fe5e2fa610e3b662921d8017e51d5938"``).
    It is the CLI's statement of what it accepts for ``model_reasoning_effort``;
    it is not a live call per level. Codex exposes no ``none``.

``claude-code-model-table``
    The ``runtime.effort_levels`` of each model in the static model table that
    Claude Code 2.1.309 ships (the table behind ``claude --effort``; the flag's
    help lists ``low, medium, high, xhigh, max``). A table row is per model:
    ``claude-opus-4-6`` and ``claude-sonnet-4-6`` omit ``xhigh``. The CLI takes
    no ``none``.

``openrouter-models-api``
    The ``reasoning.supported_efforts`` of ``GET https://openrouter.ai/api/v1/models``
    for the model (a public, unauthenticated listing), taken 2026-10-23. The
    model also lists ``reasoning`` and ``reasoning_effort`` in
    ``supported_parameters``. The vendor's list is unordered; the menu is stored
    low to high on the scale below. ``none`` is an explicit disable and appears
    only where the vendor lists it. A model that lists ``reasoning`` but neither
    ``reasoning_effort`` nor any ``supported_efforts`` has NO effort dial: its
    profile has an empty menu (``no_effort_dial=True``), which is a measured
    "send no effort", distinct from an unmeasured model (no profile at all).

Stdlib only; imports nothing outside ``registry``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .effort import HarnessAdapterError

#: The routes a profile is keyed on. ``codex`` and ``claude`` are the registry
#: harness names; ``openrouter`` is a transport whose base URL is openrouter.ai.
ROUTE_CODEX = "codex"
ROUTE_CLAUDE = "claude"
ROUTE_OPENROUTER = "openrouter"
ROUTES = (ROUTE_CODEX, ROUTE_CLAUDE, ROUTE_OPENROUTER)

#: Every effort token a menu may carry, low to high. A profile is validated
#: against this scale when the module is imported, so a typo fails loudly there.
EFFORT_SCALE = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")

SOURCE_CODEX = "codex-models-cache"
SOURCE_CLAUDE = "claude-code-model-table"
SOURCE_OPENROUTER = "openrouter-models-api"

#: How a catalogue row's menu was derived (``catalog`` reports it).
MENU_EXACT_MODEL = "library-model"
MENU_ENDPOINT_STYLE = "library-endpoint-style"
MENU_NONE = "none"


@dataclass(frozen=True)
class ModelProfile:
    """One measured exact-model effort menu (low to high) and its provenance."""

    route: str
    model: str
    efforts: "tuple[str, ...]"
    source: str
    measured_on: str
    note: str = ""
    no_effort_dial: bool = False

    def __post_init__(self) -> None:
        if self.route not in ROUTES:
            raise ValueError(f"profile {self.model!r}: unknown route {self.route!r}")
        if not self.model:
            raise ValueError(f"profile {self.route}/{self.model!r}: model is required")
        if bool(self.efforts) == self.no_effort_dial:
            raise ValueError(
                f"profile {self.route}/{self.model!r}: efforts are required, or an empty menu "
                "must say no_effort_dial=True (the model has no effort dial); the two must agree"
            )
        indexes = []
        for effort in self.efforts:
            if effort not in EFFORT_SCALE:
                raise ValueError(
                    f"profile {self.route}/{self.model}: {effort!r} is not on the effort scale {list(EFFORT_SCALE)}"
                )
            indexes.append(EFFORT_SCALE.index(effort))
        if indexes != sorted(set(indexes)):
            raise ValueError(f"profile {self.route}/{self.model}: efforts must be unique and low to high")

    def to_json(self) -> dict:
        result = {
            "route": self.route,
            "model": self.model,
            "efforts": list(self.efforts),
            "source": self.source,
            "measured_on": self.measured_on,
        }
        if self.note:
            result["note"] = self.note
        return result


_MEASURED_ON = "2026-10-23"

_CODEX_THROUGH_MAX = ("low", "medium", "high", "xhigh", "max")
_CODEX_THROUGH_ULTRA = _CODEX_THROUGH_MAX + ("ultra",)
_CLAUDE_THROUGH_MAX = ("low", "medium", "high", "xhigh", "max")
_CLAUDE_NO_XHIGH = ("low", "medium", "high", "max")


def _codex(model: str, efforts: "tuple[str, ...]", note: str = "") -> ModelProfile:
    return ModelProfile(ROUTE_CODEX, model, efforts, SOURCE_CODEX, _MEASURED_ON, note)


def _claude(model: str, efforts: "tuple[str, ...]") -> ModelProfile:
    return ModelProfile(ROUTE_CLAUDE, model, efforts, SOURCE_CLAUDE, _MEASURED_ON)


PROFILES: "tuple[ModelProfile, ...]" = (
    # Codex: gpt-6.1-sol, gpt-6-sol, gpt-6-astra and the gpt-5.6 workhorses list
    # ``ultra`` (described by the CLI as maximum reasoning with automatic task
    # delegation); the luna models stop at ``max``.
    _codex("gpt-6.1-sol", _CODEX_THROUGH_ULTRA),
    _codex("gpt-6-sol", _CODEX_THROUGH_ULTRA),
    _codex("gpt-6-astra", _CODEX_THROUGH_ULTRA),
    _codex("gpt-6-luna", _CODEX_THROUGH_MAX),
    _codex("gpt-5.6-sol", _CODEX_THROUGH_ULTRA, "older generation in the Codex catalogue"),
    _codex("gpt-5.6-terra", _CODEX_THROUGH_ULTRA, "older generation in the Codex catalogue"),
    _codex("gpt-5.6-luna", _CODEX_THROUGH_MAX, "older generation in the Codex catalogue"),
    # Claude CLI, exact ids from the shipped model table.
    _claude("claude-opus-5-5", _CLAUDE_THROUGH_MAX),
    _claude("claude-sonnet-5-5", _CLAUDE_THROUGH_MAX),
    _claude("claude-fable-5-1", _CLAUDE_THROUGH_MAX),
    _claude("claude-haiku-5-5", _CLAUDE_THROUGH_MAX),
    _claude("claude-opus-5", _CLAUDE_THROUGH_MAX),
    _claude("claude-sonnet-5", _CLAUDE_THROUGH_MAX),
    _claude("claude-fable-5", _CLAUDE_THROUGH_MAX),
    _claude("claude-opus-4-8", _CLAUDE_THROUGH_MAX),
    _claude("claude-opus-4-7", _CLAUDE_THROUGH_MAX),
    _claude("claude-opus-4-6", _CLAUDE_NO_XHIGH),
    _claude("claude-sonnet-4-6", _CLAUDE_NO_XHIGH),
    # OpenRouter: the vendor lists [max, high, low, none]; no ``medium``.
    ModelProfile(
        ROUTE_OPENROUTER,
        "deepseek/deepseek-v4.1-flash",
        ("none", "low", "high", "max"),
        SOURCE_OPENROUTER,
        _MEASURED_ON,
        "vendor default effort is high; the endpoint must resolve an effort_style "
        "(top-level) for an effort to be sent",
    ),
    # Reasoning is mandatory for both models below (no ``none``); the vendor
    # lists three efforts each, unordered, stored low to high.
    ModelProfile(
        ROUTE_OPENROUTER,
        "google/gemini-3.8-flash",
        ("low", "medium", "high"),
        SOURCE_OPENROUTER,
        _MEASURED_ON,
        "reasoning is mandatory; vendor default effort is medium; the endpoint must "
        "resolve an effort_style (top-level) for an effort to be sent",
    ),
    ModelProfile(
        ROUTE_OPENROUTER,
        "z-ai/glm-5.3",
        ("low", "high", "max"),
        SOURCE_OPENROUTER,
        _MEASURED_ON,
        "reasoning is mandatory; vendor default effort is max; the endpoint must "
        "resolve an effort_style (top-level) for an effort to be sent",
    ),
    # No effort dial: the vendor lists ``reasoning`` and omits ``reasoning_effort``
    # and ``supported_efforts``. A row for it must not inherit the endpoint's menu.
    ModelProfile(
        ROUTE_OPENROUTER,
        "xiaomi/mimo-v2.6-pro",
        (),
        SOURCE_OPENROUTER,
        _MEASURED_ON,
        "no effort dial: the vendor lists reasoning but not reasoning_effort; any effort is "
        "refused and none is sent",
        no_effort_dial=True,
    ),
)

_INDEX = {(profile.route, profile.model): profile for profile in PROFILES}
if len(_INDEX) != len(PROFILES):
    raise ValueError("model_profiles: duplicate (route, model) profile")


def profile_for(route: Optional[str], model: Optional[str]) -> Optional[ModelProfile]:
    """The measured profile for ``model`` over ``route``, or None when none was measured."""
    if not route or not model:
        return None
    return _INDEX.get((route, model))


def exact_effort_menu(route: Optional[str], model: Optional[str]) -> "Optional[tuple[str, ...]]":
    """The measured menu (low to high) for the exact model, or None when unmeasured."""
    profile = profile_for(route, model)
    return None if profile is None else profile.efforts


def check_effort(
    effort: Optional[str],
    *,
    route: Optional[str],
    model: Optional[str],
    fallback: "Optional[Iterable[str]]",
    subject: str,
) -> None:
    """Refuse ``effort`` unless the exact-model menu (else ``fallback``) accepts it.

    The one validator every dispatch path shares. ``None`` (omit the effort) is
    always accepted. A model with a measured profile is held to that menu; a
    model with none falls back to ``fallback`` (the harness-wide menu the
    adapter already validated before profiles existed), and ``fallback`` None
    accepts any value. ``none`` is an explicit-disable wire value: it passes only
    where the menu lists it. Raises :class:`HarnessAdapterError`.
    """
    if effort is None:
        return
    profile = profile_for(route, model)
    if profile is not None:
        if effort in profile.efforts:
            return
        if not profile.efforts:
            raise HarnessAdapterError(
                f"{subject} accepts no effort for model {model!r} ({profile.source}, measured "
                f"{profile.measured_on}): the model has no effort dial; omit the effort (null)"
            )
        hint = ""
        if effort == "none":
            hint = (
                "; omit the effort (null) to send none -- an explicit 'none' is "
                "accepted only where the menu lists it"
            )
        raise HarnessAdapterError(
            f"{subject} does not accept effort {effort!r} for model {model!r} "
            f"({profile.source}, measured {profile.measured_on}); "
            f"accepted efforts: {'|'.join(profile.efforts)}{hint}"
        )
    if fallback is None:
        return
    menu = tuple(fallback)
    if effort in menu:
        return
    # No measured profile: the harness-wide menu, in the wording the adapter
    # used before profiles existed.
    raise HarnessAdapterError(
        f"{subject} does not accept effort {effort!r}; accepted efforts: {'|'.join(sorted(menu))}"
    )


__all__ = [
    "EFFORT_SCALE",
    "MENU_ENDPOINT_STYLE",
    "MENU_EXACT_MODEL",
    "MENU_NONE",
    "ModelProfile",
    "PROFILES",
    "ROUTES",
    "ROUTE_CLAUDE",
    "ROUTE_CODEX",
    "ROUTE_OPENROUTER",
    "SOURCE_CLAUDE",
    "SOURCE_CODEX",
    "SOURCE_OPENROUTER",
    "check_effort",
    "exact_effort_menu",
    "profile_for",
]
