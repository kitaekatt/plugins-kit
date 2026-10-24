"""llm_kit.pipeline.runtime -- LLM platform binding.

The pipeline-shaped layer above a raw OpenAI-compatible client: the
validate-until-valid loop, a content-addressed response cache, cost/budget
accounting, process-level backend routing (OpenRouter, registry endpoints, or
CLI harnesses), a mock seam for tests, the adaptive gate, and the spend
ledger. Key resolution, the model registry, and the ready-made client are NOT
reimplemented here -- they are consumed from ``llm_kit.registry`` and
``llm_kit.dispatch``, and a concrete completion backend is built by an
injected ``llm_kit.completion.factory_protocols.BackendFactory``. This package
is the domain-free machinery both source systems this package unifies share;
it is not "which provider / which key," which stays the registry's job.

Submodules:

- ``platform`` -- the transport-agnostic core: ``LLMResponse`` /
  ``BackendOptions`` / the ``LLMBackend`` protocol, the ``PipelineHaltError`` taxonomy,
  the content-addressed ``ResponseCache``, cost (``estimate_cost``, unknown
  model == hard ``KeyError``), the budget guards, ``call_llm`` (the single
  entry point), and ``submit_validated`` (the validate-until-valid loop over
  ``validate.contract`` validators).
- ``backends`` -- the five live transports (``OpenRouterBackend``,
  ``ClaudeCliBackend``, ``CodexCliBackend``, ``OpencodeCliBackend``,
  ``ModelEndpointBackend``), the ``MockBackend``, and process-level ``route``.
  The live transports are THIN WRAPPERS: each keeps its constructor options
  and hands an immutable ``BackendSpec`` to the injected ``BackendFactory``
  (default ``llm_kit.dispatch.pipeline_backends.ConfiguredBackendFactory``),
  which owns the actual completion transport -- subprocess runners, retry,
  timeout, hard-stop detection, and the OpenAI-compatible client + prompt-cache
  message shaping. This layer keeps only the pipeline glue and adapts the seam
  types across the boundary; the factory is called on first use, so the
  ``MockBackend`` path and the import graph stay hermetic.
- ``gate`` -- the ``AdaptiveGate`` concurrency gate and ``call_llm_gated``.
- ``spend_ledger`` -- the cross-process spend ledger (SQLite, schema 1).
- ``budget`` -- the ``BudgetStop`` guard and auth-expiry preflight.
- ``yaml_extract`` -- fenced-block-tolerant YAML extraction from LLM output.

The convergence gate (``CONVERGED`` / ``STALLED`` / ``CONTINUE``) lives in
``llm_kit.pipeline.core.convergence``.

Permitted cross-package imports (the exact dependency graph in
``docs/reference/llm-kit-architecture.md``): ``completion``, ``registry``,
``dispatch``, ``storage``, ``pipeline.core`` and ``pipeline.artifacts``. No
adapters and no execution.

Deviations from the two source systems' semantics
--------------------------------------------------

The generic design preserves the behavioral cases the two source suites pin,
but several details differ deliberately.

1. **File-per-key cache, not SQLite.** The localization system's response
   cache is a WAL-mode SQLite store with a cross-process busy-timeout retry
   loop -- machinery sized for its dense ThreadPoolExecutor fan-out against one
   shared ``.sqlite``. This library takes a pluggable cache DIRECTORY and
   writes one JSON file per key (``ResponseCache``). The behavioral contract
   the source suite pins is preserved -- deterministic content-addressed key,
   hit/miss, empty/whitespace responses never cached, distinct static-prefix
   splits never collide -- but the storage substrate and its concurrency story
   are intentionally simpler; a consumer that needs cross-process write
   contention handling wraps its own store behind the same lookup/store shape.

2. **Cache key via ``freshness.hashing``, not a private SHA helper.** The
   source key is a hand-rolled ``hashlib.sha256`` over ``json.dumps(...,
   ensure_ascii=False)``. ``build_cache_key`` routes the same payload through
   ``freshness.hashing.content_hash`` (sorted-key, ``ensure_ascii=True``) --
   the one permitted cross-package reuse -- so digests are ASCII-canonical.
   Digests are not byte-identical to loc's, but every pinned property (stable,
   per-field-sensitive, whitespace-sensitive, prefix participates) holds.

3. **Cost/pricing over a Mapping, not a bound YAML path.** The source ``cost``
   module binds a project ``pricing.yaml`` in a per-pipeline shim.
   ``estimate_cost`` takes an already-loaded ``Mapping`` (``load_pricing``
   loads one from a file when wanted), keeping the pricing lookup pure and
   project-path-free. The unknown-model hard-``KeyError`` invariant is kept
   exactly.

4. **``max_tokens`` / ``temperature`` on ``BackendOptions``, not
   ``complete()`` parameters.** A prior transport API carried these as
   ``complete()`` keyword args and only transport-specific knobs on
   ``BackendOptions``. This library folds all per-call knobs onto
   ``BackendOptions`` so the ``LLMBackend`` protocol signature is uniform; the
   behavioral effect (temperature / max-tokens flow to the provider and into the
   cache key) is unchanged. ``max_tokens`` is optional too: ``None`` names no
   cap, and a transport wrapper then carries the selected model's configured
   ``max_output_tokens`` (project model file), else none on openrouter.ai and
   4096 elsewhere (``call_llm`` documents the spend reservation for an
   uncapped call). Temperature is optional: ``None`` inherits the
   server/model default and an explicit value is sent. The
   ``llm_scripting_kit.completion`` seam this layer now delegates its transport
   to shares this folded shape, so the adapter passes options across the
   boundary field-for-field.

5. **Convergence over an explicit ``Round`` history, not substrate/log
   scans.** loc's ``trial_status`` derives its verdict by inspecting the
   candidate store and per-cycle ``metrics.yaml`` files on disk.
   ``ProgressEvaluator`` folds an in-memory sequence of ``(produced,
   outstanding)`` rounds the caller supplies -- the same CONVERGED
   (outstanding drained) and STALLED (no progress across a window) semantics,
   with the disk I/O left to the caller. loc's shell-driven FAILED verdict is
   out of scope (it classifies non-zero stage exits from a log, not a generic
   convergence signal).

Convenience re-exports
----------------------

Unlike the root package (which re-exports nothing, keeping the cross-subpackage
import graph a strict DAG), this package re-exports its own advertised public
surface so a consumer writes ``from llm_kit.pipeline.runtime import call_llm``
rather than reaching into ``.platform`` / ``.backends``. Only names that do NOT
collide with a submodule name (``platform``, ``backends``, ``gate``,
``spend_ledger``, ``budget``, ``yaml_extract``) are re-exported -- the repo's
name-shadowing discipline -- so ``llm_kit.pipeline.runtime.platform`` always
resolves to the submodule, never a re-exported symbol. The re-exports are
import-safe: ``platform`` and ``backends`` load with no ``openai`` present (a
lazy import reached only when a live backend actually runs), so importing this
package never drags in an optional transport dependency.

The surface (both live under ``platform`` unless noted):

- ``call_llm`` / ``submit_validated`` -- the entry point and the
  validate-until-valid loop.
- ``LLMResponse`` / ``BackendOptions`` / ``LLMBackend`` -- the seam types.
- ``PipelineHaltError`` + ``HALT_AUTH`` / ``HALT_RATE_LIMIT`` /
  ``HALT_INSUFFICIENT_CREDIT`` -- the halt taxonomy.
- ``ResponseCache`` / ``CostBudget`` -- the cache and budget guard.
- ``StructuralOutputError`` / ``StructuredContractSupportError`` -- the
  output-contract failure types.
- ``OutputContract`` -- the completion layer's output-contract type,
  re-exported LAZILY through ``platform._contract_seam`` (not a duplicate
  type). It resolves on first attribute access and is deliberately absent from
  ``__all__``: a star import must never load the completion contract modules.
- ``CallAttempt`` / ``AttemptObserver`` -- the per-attempt record and the
  ``on_attempt`` callback type ``call_llm`` and ``submit_validated`` report to.
- ``OpenRouterBackend`` / ``ClaudeCliBackend`` / ``CodexCliBackend`` /
  ``OpencodeCliBackend`` / ``ModelEndpointBackend`` / ``MockBackend`` /
  ``route`` / ``routed_model`` -- the transports and process-level routing
  (from ``backends``).

The ``yaml_extract`` and ``budget`` submodules are imported directly (their
top-level names would be less discoverable flattened here).
"""

from llm_kit.pipeline.runtime.backends import (
    ClaudeCliBackend,
    CodexCliBackend,
    MockBackend,
    ModelEndpointBackend,
    OpencodeCliBackend,
    OpenRouterBackend,
    declared_backend_and_model,
    declared_model_names,
    resolve_declaration,
    route,
    routed_model,
)
from llm_kit.pipeline.runtime.platform import (
    HALT_AUTH,
    HALT_INSUFFICIENT_CREDIT,
    HALT_RATE_LIMIT,
    HALT_BACKPRESSURE,
    HALT_UNREACHABLE,
    BackendOptions,
    CostBudget,
    EmptyCompletionError,
    HaltError,
    PipelineHaltError,
    LLMUnavailableError,
    ConfigurationError,
    LLMBackend,
    LLMResponse,
    ResponseCache,
    StructuralOutputError,
    StructuredContractSupportError,
    AttemptObserver,
    CallAttempt,
    call_llm,
    submit_validated,
)
from llm_kit.pipeline.runtime.gate import AdaptiveGate, EndpointBusyError, call_llm_gated
from llm_kit.pipeline.runtime.platform import _contract_seam

__all__ = [
    "call_llm",
    "submit_validated",
    "LLMResponse",
    "EmptyCompletionError",
    "BackendOptions",
    "LLMBackend",
    "PipelineHaltError",
    "HaltError",
    "LLMUnavailableError",
    "ConfigurationError",
    "CodexCliBackend",
    "OpencodeCliBackend",
    "ModelEndpointBackend",
    "HALT_AUTH",
    "HALT_RATE_LIMIT",
    "HALT_BACKPRESSURE",
    "AdaptiveGate",
    "EndpointBusyError",
    "call_llm_gated",
    "HALT_INSUFFICIENT_CREDIT",
    "HALT_UNREACHABLE",
    "ResponseCache",
    "CostBudget",
    "OpenRouterBackend",
    "ClaudeCliBackend",
    "MockBackend",
    "route",
    "routed_model",
    "declared_model_names",
    "resolve_declaration",
    "declared_backend_and_model",
    "StructuralOutputError",
    "StructuredContractSupportError",
    "CallAttempt",
    "AttemptObserver",
]


def __getattr__(name: str):
    # The lazy OutputContract re-export, resolved through the one probe.
    if name == "OutputContract":
        return _contract_seam().OutputContract
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
