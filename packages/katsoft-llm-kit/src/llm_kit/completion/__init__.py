"""llm_kit.completion: provider-neutral completion types and protocols.

Request options, results, usage, halt taxonomy, capability TYPES, requirement
matching, output contracts, skill context, prompt folding and the request
protocol. No filled adapter capability tables, no endpoint profiles, no
factories and no transports; those live in ``llm_kit.adapters`` and
``llm_kit.dispatch``.

No root re-exports; import from the owning module (``llm_kit.completion.types``,
``llm_kit.completion.halt``, ``llm_kit.completion.capabilities``,
``llm_kit.completion.requirements``, ``llm_kit.completion.contract``,
``llm_kit.completion.contract_types``, ``llm_kit.completion.skill_context``,
``llm_kit.completion.skill_context_types``, ``llm_kit.completion.results``,
``llm_kit.completion.prompt_fold``, ``llm_kit.completion.request_protocol``).
"""
