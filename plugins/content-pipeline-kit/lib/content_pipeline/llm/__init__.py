"""llm -- LLM platform binding.

The pipeline-shaped layer above a raw OpenAI-compatible client: the
validate-until-valid loop, a content-addressed response cache, cost/budget
accounting, process-level backend routing (openrouter completion vs. a
claude-cli agent loop), a mock seam for tests, and the convergence gate. Key
resolution, the model registry, and the ready-made client are NOT
reimplemented here -- they are consumed from openrouter-kit via
``shared_lib_imports`` (reuse-by-availability). This package is the
domain-free machinery both source systems this plugin unifies share; it is
not "which provider / which key," which stays openrouter-kit's job.

Submodules:

- ``platform`` -- the transport-agnostic core: ``LLMResponse`` /
  ``BackendOptions`` / the ``LLMBackend`` protocol, the ``HaltError`` taxonomy,
  the content-addressed ``ResponseCache``, cost (``estimate_cost``, unknown
  model == hard ``KeyError``), the budget guards, ``call_llm`` (the single
  entry point), and ``submit_validated`` (the validate-until-valid loop over
  ``validate.contract`` validators).
- ``backends`` -- the three transports (``OpenRouterBackend``,
  ``ClaudeCliBackend``, ``MockBackend``) and process-level ``route``.
- ``convergence`` -- the ``CONVERGED`` / ``STALLED`` / ``CONTINUE`` gate.
- ``yaml_extract`` -- fenced-block-tolerant YAML extraction from LLM output.

Permitted cross-package imports (per the plugin's dependency rules): this
package may import ``validate.contract`` and ``freshness.hashing`` and nothing
else from ``content_pipeline``; it never reaches into ``store`` or ``vcs``.

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
   ``complete()`` parameters.** gen-ops carries these as ``complete()`` keyword
   args and the transport-specific knobs on ``BackendOptions``. This library
   folds all per-call knobs onto ``BackendOptions`` so the ``LLMBackend``
   protocol signature is uniform; the behavioral effect (temperature /
   max-tokens flow to the provider and into the cache key) is unchanged.

5. **Convergence over an explicit ``Round`` history, not substrate/log
   scans.** loc's ``trial_status`` derives its verdict by inspecting the
   candidate store and per-cycle ``metrics.yaml`` files on disk.
   ``ProgressEvaluator`` folds an in-memory sequence of ``(produced,
   outstanding)`` rounds the caller supplies -- the same CONVERGED
   (outstanding drained) and STALLED (no progress across a window) semantics,
   with the disk I/O left to the caller. loc's shell-driven FAILED verdict is
   out of scope (it classifies non-zero stage exits from a log, not a generic
   convergence signal).
"""
