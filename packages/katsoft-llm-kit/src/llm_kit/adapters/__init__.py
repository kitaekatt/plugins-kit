"""llm_kit.adapters: concrete integrations behind the completion contracts.

OpenAI-compatible, Claude, Codex and OpenCode backends; the CLI streaming
runner; the harness command adapters; the llama-swap swapper client and its
process layer; the raw Claude/Codex usage-source readers; the account and
endpoint probes; endpoint profiles; and the filled adapter capability tables.

Depends only on ``llm_kit.completion`` and ``llm_kit.registry``. Pacing policy,
backend factories and reachability live in ``llm_kit.dispatch``.

No root re-exports; import from the owning module (``llm_kit.adapters.account``,
``llm_kit.adapters.backends``, ``llm_kit.adapters.capabilities``,
``llm_kit.adapters.claude_runner``, ``llm_kit.adapters.client``,
``llm_kit.adapters.codex_backend``, ``llm_kit.adapters.endpoint_profile``,
``llm_kit.adapters.harness_adapters``, ``llm_kit.adapters.opencode_backend``,
``llm_kit.adapters.swapper``, ``llm_kit.adapters.swapper_acceptance``,
``llm_kit.adapters.usage_sources``).
"""
