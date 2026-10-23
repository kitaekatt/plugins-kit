"""llm_kit.dispatch: declaration/run policy and configured construction.

Declaration describe/run, the backend factory and the configured construction
builder, endpoint reachability, seats, the pacing policy with its verdict
cache, the execution observer seam, the code-review lane service, and
policy-facing re-exports of the adapter endpoint profiles.

Depends only on ``llm_kit.completion``, ``llm_kit.registry`` and
``llm_kit.adapters``. The raw usage readers are ``llm_kit.adapters.usage_sources``;
the pacing policy that reads them is here.

No root re-exports; import from the owning module (``llm_kit.dispatch.declaration``,
``llm_kit.dispatch.factory``, ``llm_kit.dispatch.pipeline_backends``,
``llm_kit.dispatch.endpoint_resolver``,
``llm_kit.dispatch.reachability``, ``llm_kit.dispatch.seats``,
``llm_kit.dispatch.usage_budget``, ``llm_kit.dispatch.observer``,
``llm_kit.dispatch.review_lane``, ``llm_kit.dispatch.endpoint_profile``).
"""
