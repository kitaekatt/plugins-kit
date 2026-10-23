# katsoft-contracts

Standard-library-only contracts shared by the plugins-kit marketplace:

- `katsoft_contracts.execution_event` -- the execution-event envelope (schema
  revisions v1 to v3), validation, emitters and JSONL sinks.
- `katsoft_contracts.interrupt_contract` -- the interrupt request and
  resolution contract. The schema validator is passed in by the caller.
- `katsoft_contracts.model_declaration` -- the structural parser for a model
  declaration.

The package has no dependencies and imports nothing outside the standard
library.

```python
from katsoft_contracts import model_declaration

model_declaration.parse(["opus", "sonnet"])  # ['opus', 'sonnet']
```

## Source

The canonical source lives in this directory. The marketplace's bootstrap
library aliases the three modules to these (module identity, no copies).

The package needs no AI harness, Claude Code, bootstrap or plugins-kit to
install and use.

Releases are tagged `katsoft-contracts-v<version>` in the repository named by
the `Source` project URL.
