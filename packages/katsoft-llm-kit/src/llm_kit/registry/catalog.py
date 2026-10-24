"""The project model catalogue as a query: ordered rows, exact-model menus, modes and cost.

``build_catalog`` turns a validated project model file
(:mod:`llm_kit.registry.project_models`) into the JSON document behind
``llm-kit models catalog``. It answers "what can this project call, in the
project's own order, and what does each row accept" without dispatching a call,
probing a provider or reading any user cache:

- The ORDER and the selections are the project's, returned as stored; the
  library ships no roster and no order.
- ``efforts`` is the row's declared subset when it has one, else the full menu
  the library knows for that EXACT model (``model_profiles``), and
  ``effort_source`` says which. A harness model the library has not measured
  has an empty menu and the row says so in ``notes``.
- A row with NO effort dial reports ``"efforts": []`` (an empty list, never a missing
  key): either the library's measured profile for that exact model has an empty menu
  (``effort_source`` ``library-model``) or the project file declares ``efforts: []``
  (``effort_source`` ``project-no-effort``). Any effort for it is refused.
- ``effort_delivery`` says whether the endpoint can put an effort on the wire at
  all. A menu is the model's vendor menu; an endpoint that resolves no effort
  style (the shipped ``openrouter`` record until it declares one) would drop it,
  and a note names the remedy.
- ``selections`` are the tracked file's selections, as stored (the defaults).
  ``resolved_selections`` layers the personal file over them and reports, per
  key, ``{effective, default, source}`` (``source`` is ``default`` or
  ``override``); ``local`` names that file. Read ``resolved_selections`` for
  what a call should use.
- ``max_output_tokens`` is the row's declared output cap, present only when the row
  declares one. It is the cap a protocol 2 call to a transport row carries when the
  request names no ``max_tokens``; a harness row (claude, codex) may declare it, and a
  note says it is not applied there. A row without it sends no cap to openrouter.ai.
- ``modes`` come from the adapter family's advertisement, passed in by the
  caller (the registry cannot import adapters).
- ``cost`` is ``{class, reason}``: the registry classification, always reported
  with its reason and independent of any consent. A claude or codex harness is
  ``subscription`` only on subscription auth, which is learned by asking the CLI,
  so without a ``harness_auth`` mapping the class is ``null`` (the reason says why)
  rather than a guess.
- ``consent`` is the discovery note of the optional consent layer
  (:func:`llm_kit.registry.consent.discovery_note`): the catalogue never applies a
  policy, so ``in_use`` is false.

Stdlib only beyond the registry; no ``bootstrap_lib``, no plugin path, and no
read of ``~/.codex``, ``~/.claude`` or the network.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from .consent import discovery_note
from .cost import HarnessAuth, classify
from .effort import DELIVERING_STYLES
from .model_endpoints import HARNESS_KIND
from .model_profiles import MENU_NONE
from .project_models import (
    EndpointInfo,
    LocalOverrides,
    ProjectModels,
    _Endpoints,
    library_effort_menu_with_source,
    resolve_selections,
)

SCHEMA = "llm-kit.model-catalog/v1"

#: The adapter family that serves each harness; every other endpoint is served
#: by the ``openrouter`` transport adapter.
ADAPTER_BY_HARNESS = {
    "claude": "claude-cli",
    "codex": "codex-cli",
    "opencode": "opencode-cli",
}
TRANSPORT_ADAPTER = "openrouter"

#: ``effort_source`` of a row whose project file declares ``efforts: []`` (no effort dial).
EFFORT_SOURCE_PROJECT_NONE = "project-no-effort"

#: Harnesses whose cost class depends on how the CLI is authenticated.
_AUTH_DEPENDENT = ("claude", "codex")


def adapter_of(info: EndpointInfo) -> str:
    """The adapter family that serves ``info``."""
    if info.harness is not None:
        return ADAPTER_BY_HARNESS[info.harness.lower()]
    return TRANSPORT_ADAPTER


def _delivery(info: EndpointInfo) -> dict:
    if info.harness is not None:
        # Every harness adapter puts an effort on its command line.
        return {"deliverable": True, "style": None}
    return {"deliverable": info.effort_style in DELIVERING_STYLES, "style": info.effort_style}


def _cost(
    info: EndpointInfo,
    entry: Any,
    entries: Mapping[str, Any],
    harness_auth: Optional[Mapping[str, HarnessAuth]],
) -> dict:
    harness = (info.harness or "").lower()
    if harness in _AUTH_DEPENDENT and harness_auth is None:
        return {
            "class": None,
            "reason": f"{harness} cost class depends on its authentication, which was not probed",
        }
    verdict = classify(entry, entries=entries, harness_auth=harness_auth)
    return {"class": verdict.cost_class, "reason": verdict.reason}


def build_catalog(
    project: ProjectModels,
    *,
    project_root: Optional[str],
    modes_by_adapter: Mapping[str, Sequence[str]],
    harness_auth: Optional[Mapping[str, HarnessAuth]] = None,
    local: Optional[LocalOverrides] = None,
) -> dict:
    """The ``llm-kit.model-catalog/v1`` document for a validated project file.

    ``local`` is the validated personal layer (``project_models.read_local_overrides``);
    without it every selection resolves to its default and no ``local`` block is emitted.

    ``modes_by_adapter`` maps an adapter family name (see :func:`adapter_of`) to
    its call modes; a family missing from it raises ``KeyError``. ``harness_auth``
    is optional: without it a claude or codex row's cost class is ``null`` instead
    of a guess.
    """
    endpoints = _Endpoints(project_root)
    notes: list[str] = list(endpoints.notes())
    rows: list[dict] = []
    for row in project.document["catalog"]:
        info = endpoints.info(row["endpoint"])
        if info is None:  # validation resolved it a moment ago; a vanished endpoint is an error
            raise KeyError(f"catalog row {row['id']!r}: endpoint {row['endpoint']!r} no longer resolves")
        adapter = adapter_of(info)
        menu, menu_source = library_effort_menu_with_source(info, row["model"])
        declared = row.get("efforts")
        if declared is not None and not declared:
            effort_source = EFFORT_SOURCE_PROJECT_NONE
        elif menu_source == MENU_NONE:
            effort_source = MENU_NONE
        elif declared is not None:
            effort_source = f"{menu_source}+project-subset"
        else:
            effort_source = menu_source
        efforts = list(declared) if declared is not None else list(menu)
        delivery = _delivery(info)
        if info.harness is not None and menu_source == MENU_NONE:
            notes.append(
                f"{row['id']}: the library has no measured effort menu for {row['model']!r} on "
                f"{info.harness}; omit effort for this row"
            )
        if efforts and not delivery["deliverable"]:
            notes.append(
                f"{row['id']}: endpoint {row['endpoint']!r} resolves no effort style, so an effort "
                "would not be sent; declare effort_style (for example top-level) on the endpoint"
            )
        entry = endpoints.entry(row["endpoint"])
        item: dict = {
            "id": row["id"],
            "label": row["label"],
            "endpoint": row["endpoint"],
            "model": row["model"],
            "kind": "harness" if info.kind == HARNESS_KIND else "transport",
            "adapter": adapter,
            "efforts": efforts,
            "effort_source": effort_source,
            "effort_delivery": delivery,
            "modes": list(modes_by_adapter[adapter]),
            "cost": _cost(info, entry, endpoints.entries(), harness_auth),
        }
        if "max_output_tokens" in row:
            item["max_output_tokens"] = row["max_output_tokens"]
            if info.kind == HARNESS_KIND:
                notes.append(
                    f"{row['id']}: max_output_tokens is declared but {info.harness} accepts no output "
                    "cap, so it is not applied; the harness keeps its native behavior"
                )
        if "request_defaults" in row:
            item["request_defaults"] = row["request_defaults"]
        rows.append(item)
    result: dict = {
        "schema": SCHEMA,
        "path": project.path,
        "sha256": project.sha256,
        "models": rows,
        "selections": {key: dict(value) for key, value in project.document["selections"].items()},
        "resolved_selections": resolve_selections(
            project.document["selections"], local.selections if local is not None else {}
        ),
    }
    if local is not None:
        result["local"] = {"path": local.path, "present": local.present, "sha256": local.sha256}
    result["consent"] = discovery_note()
    result["notes"] = notes
    return result


__all__ = ["ADAPTER_BY_HARNESS", "SCHEMA", "TRANSPORT_ADAPTER", "adapter_of", "build_catalog"]
