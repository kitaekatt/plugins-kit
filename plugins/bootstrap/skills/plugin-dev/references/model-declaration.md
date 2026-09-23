# Model declarations

A model declaration answers one question for a unit of work: which model or
models may do it. Every plugin in this marketplace writes that answer in the
one format specified here. A plugin's own configuration documentation cites
this file by path instead of restating the grammar.

The audience is a plugin author adding a field that names a model, and a
reviewer checking one. The validator is
`bootstrap_lib.model_declaration`; this file specifies the format it enforces.

## Syntax

A declaration is a list of strings. Each string is a registry id: the name of
one model entry.

```yaml
models: [fable]                                        # one model
models: [astra, fable]                                 # two models, in preference order
models: [qwen3.8-5090, opus, astra, qwen3.8-m5pro]     # several providers
```

- **A one-model declaration is a one-element list.** The written form is always
  a list.
- **A bare string is accepted on read** as a one-element list, so `model: opus`
  and `model: [opus]` mean the same thing.
- **Order is preference order.** The first entry is the declared first choice.
- **Any id may appear**, including several ids served by one provider.

The carrier does not change the format. A YAML list, a JSON list, a JavaScript
array literal, and a repeated command-line flag all carry the same list.

## One namespace

An id names WHAT model runs. A provider is what serves a set of ids: the
Claude harness serves `fable`, `opus`, `sonnet`, and `haiku`; other providers
serve the ids that the llm-scripting-kit model registry declares. HOW an id is
driven -- an Agent subagent, a `claude -p` process, a CLI harness, an HTTP
endpoint -- is decided by the id's registry entry and by the calling site,
never by the spelling of the id.

## Core ids

`fable`, `opus`, `sonnet`, and `haiku` are the CORE ids
(`bootstrap_lib.model_declaration.CORE_IDS`). The harness itself defines them,
so they are valid on every installation and every plugin can route them
without llm-scripting-kit. The set is closed and case-sensitive: `Opus` and
`sonnett` are ordinary ids, not core ids.

The core ids are reserved for the Claude harness. Do not define a registry
entry under a core id with another harness or a `base_url`.

## Prefixes

Declarations name ids directly. Two prefixes predate this format and are
deprecated:

| Prefix | Meaning when accepted |
|---|---|
| `agent:<id>` | the same model as `<id>` |
| `peer:<id>` | a reachable peer model of `<id>`: same tier, different model family |

A site that accepted a prefix before this format still accepts it and rewrites
it to an id before dispatch. A new site does not accept either prefix. To the
validator a prefixed entry is an ordinary string.

## Validation

`bootstrap_lib.model_declaration` checks SHAPE and nothing else. It is
stdlib-only, reads no file, and never imports llm-scripting-kit.

| Call | Result |
|---|---|
| `parse(value)` | the declaration as a normalized `list[str]` |
| `validate(value)` | a `Declaration` whose `ids` is that list as a tuple |
| `is_core_id(value)` | whether `value` is a core id |

Both `parse` and `validate` raise `DeclarationError` for:

- a value that is neither a string nor a list (or tuple) of strings;
- a literally-empty list;
- an entry that is not a string, or is blank;
- the same id twice. Surrounding whitespace is stripped before the comparison.

`DeclarationError.index` gives the position of the offending entry, or `None`
when the fault is in the whole value. A calling site re-raises it in its own
error type with its own location.

The validator does NOT decide whether an id exists, resolves, or can be routed.
An id that matches no registry entry passes validation. Resolution is a runtime
question, answered where the declaration is dispatched.

A site may add a constraint of its own on top of the shape, for example "names
exactly one id" for a field that has no failover. The site states that
constraint in its own documentation and error message.

## Using the validator from a plugin

`bootstrap_lib` is a shared library. A plugin whose own code calls the
validator declares `"shared_lib_imports": ["bootstrap_lib"]` in its
`bootstrap.json`. The edge is REQUIRED: every plugin already depends on
bootstrap. See [optional-plugin-dependencies](optional-plugin-dependencies.md)
for the edge contract.

```python
from bootstrap_lib.model_declaration import DeclarationError, parse

try:
    models = parse(raw_value)
except DeclarationError as exc:
    raise MyConfigError(f"{path}: models: {exc}") from exc
```
