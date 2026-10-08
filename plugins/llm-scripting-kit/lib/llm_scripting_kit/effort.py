"""Reasoning-effort wire vocabulary shared by the front door and the direct seam.

OpenAI-compatible servers disagree on where a reasoning effort goes and which
values they accept. An *effort style* names one wire convention:

- ``top-level``: ``{"reasoning_effort": <effort>}``.
- ``ninfer``: top-level, with ``high`` remapped to ``xhigh`` (NInfer's menu is
  none|low|medium|xhigh and it rejects ``high`` with a 400).
- ``chat_template_kwargs``: ``{"chat_template_kwargs": {"reasoning_effort":
  <effort>}}`` (llama.cpp / mlx chat templates).
- ``unsupported``: the endpoint takes no effort; nothing is emitted.

One module owns the vocabulary so the front door (which translates effort
already in an inbound body) and ``OpenRouterBackend`` (which translates
``BackendOptions.effort`` on a direct call) cannot disagree about a style.
Stdlib-only and import-free within the package, so ``model_endpoints`` and the
completion seam can both depend on it without a cycle.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

#: The request key an effort rides under, at the top level or nested.
EFFORT_KEY = "reasoning_effort"

#: The nested object the ``chat_template_kwargs`` style places the effort in.
TEMPLATE_KEY = "chat_template_kwargs"

TOP_LEVEL = "top-level"
NINFER = "ninfer"
CHAT_TEMPLATE_KWARGS = "chat_template_kwargs"
UNSUPPORTED = "unsupported"

#: Every style the registry accepts.
EFFORT_STYLES = (TOP_LEVEL, NINFER, CHAT_TEMPLATE_KWARGS, UNSUPPORTED)

#: The styles that put an effort on the wire.
DELIVERING_STYLES = (TOP_LEVEL, NINFER, CHAT_TEMPLATE_KWARGS)

#: Where a resolved style came from (``EffortDelivery.source``).
SOURCE_ENDPOINT = "endpoint"
SOURCE_FRONTDOOR = "frontdoor"
SOURCE_ROUTING = "routing"
SOURCE_NONE = "none"

#: The one value remap a style applies; every other value passes unchanged.
_REMAPS: Dict[str, Dict[str, str]] = {NINFER: {"high": "xhigh"}}

# EffortPlan outcomes.
OUTCOME_TRANSLATED = "translated"
OUTCOME_CALLER_EXTRAS = "caller-extras"
OUTCOME_SUPPRESSED = "suppressed"
OUTCOME_UNDELIVERABLE = "undeliverable"
OUTCOME_UNSET = "unset"


def remap_effort(effort: str, style: Optional[str]) -> str:
    """The value ``style`` sends for ``effort`` (ninfer maps ``high`` to ``xhigh``)."""
    return _REMAPS.get(style or "", {}).get(effort, effort)


def _emits(style: Optional[str]) -> Optional[str]:
    if style in (TOP_LEVEL, NINFER):
        return EFFORT_KEY
    if style == CHAT_TEMPLATE_KWARGS:
        return f"{TEMPLATE_KEY}.{EFFORT_KEY}"
    return None


@dataclass(frozen=True)
class EffortDelivery:
    """How (and whether) an endpoint receives a reasoning effort.

    ``style`` is None when no style resolved, including a declared-but-invalid
    one: a typo must never pick a wire format. ``source`` names the registry
    field the style came from, or ``"none"``.
    """

    style: Optional[str]
    source: str = SOURCE_NONE

    @property
    def deliverable(self) -> bool:
        return self.style in DELIVERING_STYLES

    @property
    def emits(self) -> Optional[str]:
        return _emits(self.style)

    def to_json(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "deliverable": self.deliverable,
            "emits": self.emits,
            "style": self.style,
            "source": self.source,
        }
        remap = _REMAPS.get(self.style or "")
        if remap:
            result["remap"] = dict(remap)
        return result


def extract_effort(body: Dict[str, Any]) -> Optional[str]:
    """Remove and return the effort an INBOUND front-door body carries.

    Pops the top-level ``reasoning_effort`` (the key is removed even when its
    value is null); only when that yields None is the nested
    ``chat_template_kwargs.reasoning_effort`` popped instead. A dict
    ``chat_template_kwargs`` is replaced by a copy first, so the caller's
    nested object is never mutated. When both channels carry a value the
    top-level one is returned and the nested one is left in place -- the front
    door's long-standing wire behavior, preserved byte for byte.
    """
    template = body.get(TEMPLATE_KEY)
    if isinstance(template, dict):
        body[TEMPLATE_KEY] = dict(template)
    effort = body.pop(EFFORT_KEY, None)
    if effort is None:
        template = body.get(TEMPLATE_KEY)
        if isinstance(template, dict):
            effort = template.pop(EFFORT_KEY, None)
    return effort


def place_effort(body: Dict[str, Any], effort: str, style: Optional[str]) -> Optional[str]:
    """Put ``effort`` into ``body`` the way ``style`` requires; return what was placed.

    Applies the style's remap. A nested ``chat_template_kwargs`` dict is copied
    before it is written; a non-dict value under that key is replaced. A style
    that delivers nothing (``unsupported``, None, or an unknown string) leaves
    ``body`` untouched and returns None.
    """
    if style not in DELIVERING_STYLES:
        return None
    value = remap_effort(effort, style)
    if style in (TOP_LEVEL, NINFER):
        body[EFFORT_KEY] = value
    else:
        template = body.get(TEMPLATE_KEY)
        template = dict(template) if isinstance(template, dict) else {}
        template[EFFORT_KEY] = value
        body[TEMPLATE_KEY] = template
    return value


@dataclass(frozen=True)
class EffortPlan:
    """What a direct call puts on the wire for effort, and why.

    ``extra_body`` is exactly the request's extra body (possibly empty).
    ``applied`` is the value translation placed (post-remap), else None.
    ``outcome`` is one of ``translated``, ``caller-extras``, ``suppressed``,
    ``undeliverable`` or ``unset``.
    """

    extra_body: Dict[str, Any]
    applied: Optional[str]
    outcome: str

    @property
    def effort_delivered(self) -> bool:
        """True only when ``BackendOptions.effort`` itself reached the wire."""
        return self.outcome == OUTCOME_TRANSLATED


def _drop_nested(body: Dict[str, Any], template: Mapping[str, Any]) -> None:
    """Remove the nested effort key; drop ``chat_template_kwargs`` if that empties it."""
    remaining = {k: v for k, v in template.items() if k != EFFORT_KEY}
    if remaining:
        body[TEMPLATE_KEY] = remaining
    else:
        body.pop(TEMPLATE_KEY, None)


def plan_effort(
    extras: Optional[Mapping[str, Any]],
    effort: Optional[str],
    style: Optional[str],
) -> EffortPlan:
    """Decide the extra body for one direct call. Never mutates ``extras``.

    Precedence, first match wins:

    1. The caller's own extras name an effort, in either channel: top-level
       ``extras["reasoning_effort"]`` or nested
       ``extras["chat_template_kwargs"]["reasoning_effort"]``. The caller wins
       over ``effort`` and nothing is translated or remapped. When BOTH
       channels are present the TOP-LEVEL key wins, whatever either value is,
       and the nested duplicate is removed from the wire (an emptied
       ``chat_template_kwargs`` is removed with it). The winning value then
       decides:

       - non-None: sent verbatim (outcome ``caller-extras``);
       - None: an explicit opt-out -- the key is removed and no effort is sent
         (outcome ``suppressed``).
    2. ``effort`` is None: nothing to send (``unset``).
    3. ``style`` delivers nothing: ``effort`` is dropped (``undeliverable``).
    4. Otherwise ``effort`` is remapped and placed per ``style``
       (``translated``).

    The front door reads an inbound body differently (:func:`extract_effort`
    prefers a non-null top-level value and otherwise takes the nested one);
    that path translates what a caller sent to a deployment, while this one
    decides what a direct caller's request carries.
    """
    body: Dict[str, Any] = dict(extras or {})
    template = body.get(TEMPLATE_KEY)
    has_top = EFFORT_KEY in body
    has_nested = isinstance(template, Mapping) and EFFORT_KEY in template
    if has_top or has_nested:
        if has_top:
            if has_nested:
                _drop_nested(body, template)
            if body[EFFORT_KEY] is None:
                del body[EFFORT_KEY]
                return EffortPlan(body, None, OUTCOME_SUPPRESSED)
            return EffortPlan(body, None, OUTCOME_CALLER_EXTRAS)
        if template[EFFORT_KEY] is None:
            _drop_nested(body, template)
            return EffortPlan(body, None, OUTCOME_SUPPRESSED)
        return EffortPlan(body, None, OUTCOME_CALLER_EXTRAS)
    if effort is None:
        return EffortPlan(body, None, OUTCOME_UNSET)
    if style not in DELIVERING_STYLES:
        return EffortPlan(body, None, OUTCOME_UNDELIVERABLE)
    applied = place_effort(body, effort, style)
    return EffortPlan(body, applied, OUTCOME_TRANSLATED)


#: The accepted effort values per delivering style, low to high. NInfer's menu
#: is none|low|medium|xhigh and it rejects ``high`` with a 400; the other
#: styles take the conventional low|medium|high.
_MENUS: Dict[str, "tuple[str, ...]"] = {
    NINFER: ("none", "low", "medium", "xhigh"),
    TOP_LEVEL: ("low", "medium", "high"),
    CHAT_TEMPLATE_KWARGS: ("low", "medium", "high"),
}


def style_effort_menu(style: Optional[str]) -> "tuple[str, ...]":
    """The accepted efforts for ``style``, low to high; empty when it delivers none."""
    return _MENUS.get(style or "", ())


def _endpoint_style(endpoint: str) -> Optional[str]:
    # Lazy: model_endpoints imports this module, so a top-level import cycles.
    from .model_endpoints import resolve_effort_style, resolve_registry_entry  # noqa: PLC0415

    return resolve_effort_style(resolve_registry_entry(endpoint)).style


def effort_menu(endpoint: str) -> "tuple[str, ...]":
    """The efforts the registry endpoint ``endpoint`` accepts, low to high.

    Resolved from the endpoint's effort style (see :func:`style_effort_menu`).
    An endpoint that delivers no effort, or a harness entry, has an empty menu.
    Raises ``EndpointRegistryError`` for an unknown endpoint.
    """
    return style_effort_menu(_endpoint_style(endpoint))


def lower_effort(endpoint: str, effort: str) -> Optional[str]:
    """The next accepted effort below ``effort`` for ``endpoint``; None at the bottom.

    A value the style would remap (``high`` on ninfer) is remapped first. A
    value outside the menu, or an endpoint with an empty menu, gives None.
    """
    style = _endpoint_style(endpoint)
    menu = style_effort_menu(style)
    value = remap_effort(effort, style)
    if value not in menu:
        return None
    index = menu.index(value)
    return menu[index - 1] if index > 0 else None


__all__ = [
    "EFFORT_KEY",
    "TEMPLATE_KEY",
    "TOP_LEVEL",
    "NINFER",
    "CHAT_TEMPLATE_KWARGS",
    "UNSUPPORTED",
    "EFFORT_STYLES",
    "DELIVERING_STYLES",
    "SOURCE_ENDPOINT",
    "SOURCE_FRONTDOOR",
    "SOURCE_ROUTING",
    "SOURCE_NONE",
    "OUTCOME_TRANSLATED",
    "OUTCOME_CALLER_EXTRAS",
    "OUTCOME_SUPPRESSED",
    "OUTCOME_UNDELIVERABLE",
    "OUTCOME_UNSET",
    "EffortDelivery",
    "EffortPlan",
    "remap_effort",
    "extract_effort",
    "place_effort",
    "plan_effort",
    "style_effort_menu",
    "effort_menu",
    "lower_effort",
]
