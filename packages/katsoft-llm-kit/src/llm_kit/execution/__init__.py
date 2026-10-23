"""llm_kit.execution: the shared kernel of the two execution products.

Layer 1; imports only ``llm_kit.schema`` (and reaches bootstrap_lib by dotted
name at call time). No root re-exports; import from the submodules:

- ``llm_kit.execution.probe``: the failure vocabulary every probe shares
  (CallShape, ProbeFailure, the kind and stage words).
- ``llm_kit.execution.event_probe``: the bootstrap_lib.execution_event
  probe (absent / too old / stale) and the make_event call.
- ``llm_kit.execution.contract_probe``: the bootstrap_lib.interrupt_contract
  probe.
- ``llm_kit.execution.schema_validator``: the JSON Schema validator probe;
  llm-kit names ``llm_kit.schema.json_schema``.
- ``llm_kit.execution.interrupts``: the interrupt contract adapters.
- ``llm_kit.execution.values``: the interrupt resolution value, the lapse rule
  and canonical JSON, all standard library only.

State machines, stores, runners, error classes and message wording stay with
the products (jobs, pipeline.execution). A product describes itself with a
spec: the error class a refusal raises and the function that words it. The
probe decides WHAT is wrong; the product says it.
"""
