"""The caller-facing skill-context records of the completion seam.

A leaf module: it imports the standard library only -- never ``.types``, which
imports THIS module at runtime for the ``BackendOptions.skill_context`` and
``LLMResponse.skill_context`` annotations, and never ``bootstrap_lib``. That
second rule is what lets ``import llm_scripting_kit`` succeed on a machine
whose bootstrap predates ``bootstrap_lib.skill_material``: the library is
reached only when a caller materializes a selection (see :mod:`.skill_context`).

These records are llm-scripting-kit's own, adapted field for field from the
library's report by :func:`.skill_context.skill_context_from`. The library's
own document is carried verbatim in :attr:`SkillContextReport.provenance`.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Dict, Optional

#: ``SkillContextReport.resolver``: the library that produced the material.
RESOLVER = "bootstrap_lib.skill_material"

#: ``SkillContextReport.delivery`` and the advertised ``skill_context.delivery``
#: of an adapter that places the block in the system message.
DELIVERY_SYSTEM_MESSAGE = "system-message"


@dataclass(frozen=True)
class SkillContextReport:
    """What one skill-context block holds, and how it was delivered.

    - ``resolver`` -- :data:`RESOLVER`.
    - ``report_schema`` -- the library's report schema id.
    - ``format_version`` -- the rendered format the library used.
    - ``digest`` -- sha256 hex of :attr:`SkillContext.text` (UTF-8). It is
      known before dispatch, and a caller that caches responses keyed on the
      system and user text adds it to the key, because the block is composed
      inside the adapter.
    - ``estimated_tokens`` / ``token_budget`` / ``token_estimate`` -- the
      library's estimate of the block, the caller's budget, and the estimate's
      method (``"chars/4"``).
    - ``skills`` -- the number of skills rendered, after duplicate suppression.
    - ``provenance`` -- the library's ``report.to_json()`` document, verbatim:
      per-skill and per-resource paths, hashes and sizes.
    - ``adapter`` / ``delivery`` / ``emits`` -- set only on the copy a
      delivering adapter puts on ``LLMResponse.skill_context``; ``None`` on a
      record that has not been delivered.
    """

    resolver: str
    report_schema: str
    format_version: str
    digest: str
    estimated_tokens: int
    token_budget: int
    token_estimate: str
    skills: int
    provenance: Dict[str, Any]
    adapter: Optional[str] = None
    delivery: Optional[str] = None
    emits: Optional[str] = None

    def to_json(self) -> Dict[str, Any]:
        """A fresh JSON-native dict; ``provenance`` is copied, not shared."""
        return {
            "resolver": self.resolver,
            "report_schema": self.report_schema,
            "format_version": self.format_version,
            "digest": self.digest,
            "estimated_tokens": self.estimated_tokens,
            "token_budget": self.token_budget,
            "token_estimate": self.token_estimate,
            "skills": self.skills,
            "provenance": copy.deepcopy(self.provenance),
            "adapter": self.adapter,
            "delivery": self.delivery,
            "emits": self.emits,
        }


@dataclass(frozen=True)
class SkillContext:
    """The value of ``BackendOptions.skill_context``: the block and its report.

    Build one with :func:`llm_scripting_kit.completion.materialize_skill_context`.
    The adapter checks that ``sha256(text)`` equals ``report.digest`` before
    dispatch, so a record whose text was edited after materializing is refused.
    """

    text: str
    report: SkillContextReport

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError(
                f"SkillContext.text must be a str, got {type(self.text).__name__}"
            )
        if not isinstance(self.report, SkillContextReport):
            raise TypeError(
                "SkillContext.report must be a SkillContextReport, got "
                f"{type(self.report).__name__}"
            )


class SkillContextError(ValueError):
    """The skill-context request is refused; nothing was dispatched."""


class SkillContextUnsatisfiable(SkillContextError):
    """This adapter does not deliver skill context; nothing was dispatched."""


class SkillContextSupportError(SkillContextError):
    """The skill-material library cannot serve this call here.

    ``state`` is ``"absent"`` (``bootstrap_lib`` is not importable),
    ``"too-old"`` (the linked library lacks a call this seam makes, or its
    report has a shape this seam does not know) or ``"no-pyyaml"`` (the
    library is current and the running interpreter has no PyYAML). Each state
    has its own message and remedy.
    """

    def __init__(self, message: str, *, state: str) -> None:
        super().__init__(message)
        self.state = state


__all__ = [
    "RESOLVER",
    "DELIVERY_SYSTEM_MESSAGE",
    "SkillContextReport",
    "SkillContext",
    "SkillContextError",
    "SkillContextUnsatisfiable",
    "SkillContextSupportError",
]
