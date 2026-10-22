"""Shared, standard-library-only contracts of the plugins-kit marketplace.

Three modules, each importable on its own:

* ``katsoft_contracts.execution_event`` -- the execution-event envelope:
  schema revisions, validation, emitters and JSONL sinks.
* ``katsoft_contracts.interrupt_contract`` -- the interrupt request and
  resolution contract.
* ``katsoft_contracts.model_declaration`` -- the structural parser for a
  model declaration.

The package imports none of them eagerly. ``__all__`` lists the modules, and
removing a name from it is a breaking change that needs a major version.
"""

__all__ = [
    "execution_event",
    "interrupt_contract",
    "model_declaration",
]
