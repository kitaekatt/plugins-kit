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
"""
