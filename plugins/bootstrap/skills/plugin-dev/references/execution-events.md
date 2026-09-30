# Execution events

An execution event is one fact about running work: an executor was chosen, a
call started, usage was observed, an attempt ended, a unit finished. A plugin
in this marketplace that records such facts must write them in the one
envelope specified here. An emitting plugin keeps its events in its own store;
the envelope and the event names are shared, and no store or database is.

The audience is a plugin author emitting events, and a reviewer checking a
stream. The validator is `bootstrap_lib.execution_event`; this file specifies
the format it enforces.

## Envelope

```yaml
schema: plugins-kit.execution-event/v1      # exact literal
seq: 17                                     # int >= 0; bool is refused
identity: {run_id: str, unit_id: str?, attempt_id: str?}
event: dispatch-selected | call-started | usage | result | terminal | <plugin>:<name>
at: "2026-09-29T20:00:00Z"                  # ISO-8601 UTC, "Z", optional .fff to .ffffff
source: {plugin: str, adapter: str?, model: str?}
payload: {}                                 # JSON-native mapping, <= 16384 bytes serialized
```

Rules, each enforced by `validate_event`:

- **Exactly seven top-level keys.** An unknown or missing key is refused.
- **Absent optional sub-keys are omitted, never null.** `unit_id: null` is
  refused; leave the key out.
- **`schema` must be a revision the module supports** (`SUPPORTED_SCHEMAS`).
  Any other value is refused and the error names it.
- **Identity strings** (`run_id`, `unit_id`, `attempt_id`) are non-empty, at
  most 200 characters, and contain no control characters. `attempt_id`
  requires `unit_id`.
- **`source.plugin`** matches `[a-z][a-z0-9-]*`. `source.adapter` and
  `source.model`, when present, are non-empty strings.
- **`at`** is `YYYY-MM-DDTHH:MM:SSZ`, optionally with 3 to 6 fractional digits
  before the `Z`. It is informational: no consumer sorts by it.
- **`payload`** is a mapping with string keys whose values are JSON-native:
  string, int, finite float, bool, null, list, or mapping. A tuple, a set, NaN,
  infinity, or a non-string key is refused. Its compact, sorted-key, ASCII
  serialization is at most `MAX_PAYLOAD_BYTES` (16384) bytes.
- **The payload carries metadata, never content.** Put status, counts, reasons,
  and identifiers in it; never a prompt, a response body, or model output. The
  size cap is the mechanical guard.

## Vocabulary

The v1 core names:

| Event | Scope | Required payload | Meaning |
| --- | --- | --- | --- |
| `dispatch-selected` | attempt | none | the executor for this attempt was chosen, before invocation |
| `call-started` | attempt | none | execution began; cost or side effects may occur after this point |
| `usage` | attempt | exactly `input_tokens`, `output_tokens`, `cache_hit_tokens`, `total_tokens`; each an int >= 0 or null (null = unknown); at least one non-null | resource usage observed; build the payload with `usage_payload()` |
| `result` | attempt | `status`: non-empty string | the attempt ended; at most one per attempt |
| `terminal` | unit, or run when `unit_id` is absent; `attempt_id` forbidden | `state`: non-empty string | the unit or run reached a terminal state; at most one per unit |

Attempt scope means `unit_id` and `attempt_id` are both present. A core event
may carry payload keys beyond the required ones, except `usage`, whose payload
is exactly the four fields.

**Extensions.** Any other name is `<source.plugin>:<name>`, where the prefix
EQUALS `source.plugin` and the name part matches `[a-z][a-z0-9-]*`. A plugin
extends only its own namespace: `job-kit:run-created` is valid only in an event
whose `source.plugin` is `job-kit`. An unprefixed unknown name, or a prefix
naming another plugin, is refused. An extension may be run-, unit-, or
attempt-scoped. Extensions are never promoted to core names.

**`contract` and `interrupt` are not v1 names.** A v1 validator refuses them
with an error saying they are defined by a later schema revision
(`LATER_REVISION_NAMES`). Both describe execution: a contract event is emitted
while a run executes, under its run identity, never for a compile. A compile
error is reported by the compiler itself and needs no event.

## Revisions

Schema v1 (`SCHEMA_V1 = "plugins-kit.execution-event/v1"`) is frozen: its
names and validation rules never change. A later name enters only through a
later revision:

1. a constant such as `SCHEMA_V2 = "plugins-kit.execution-event/v2"`, with the
   name and its payload rules;
2. `SUPPORTED_SCHEMAS` gaining that literal, while `SCHEMA_V1`, its rules, and
   the default of every `schema=` selector stay unchanged;
3. its emitters passing `schema=SCHEMA_V2` to `make_event` or `Emitter`, and
   probing for it as described under "Probing for the module".

A v1 caller never passes `schema=`, so a later revision does not affect it.
A process that holds a module without a later revision refuses an event
written under it at `schema`, naming the revision; it never accepts such an
event under weaker rules.

## Ordering and identity

- **Ordering group** G = (`source.plugin`, `identity.run_id`,
  `identity.unit_id` or none).
- Within G, `seq` strictly increases in the order the source RECORDED the
  underlying facts: commit order across transactions, insertion order within
  one transaction. `seq` need not be dense. (G, `seq`) is unique and is an
  event's identity for de-duplication.
- A source MAY document a stronger order, for example a `seq` unique and
  recorded-ordered across every unit of a run.
- No order across sources is promised, and `at` is never a sort key.

`validate_stream(events)` validates each event, then refuses:

- a duplicate (G, `seq`);
- a `seq` that does not increase within G, in the given order;
- a second `result` for one attempt;
- a second `terminal` for one unit (or for the run);
- an attempt-scoped event after that unit's `terminal`.

An extension event after a unit's `terminal` is allowed.

## Usage: zero versus unknown

Many providers report 0 for a token count they do not measure, so a 0 is
ambiguous. `usage_payload()` is the one place that ambiguity is resolved; route
every `usage` payload through it.

- An `input_tokens` or `output_tokens` of 0 is kept only when the OTHER
  directional field is non-zero (a provider that reports a split reports both);
  otherwise it becomes null.
- A `cache_hit_tokens` of 0 becomes null, and a `total_tokens` of 0 becomes
  null.
- When all four end up null, the result is `None`: emit no `usage` event.

| Reported (input, output, cache, total) | `usage_payload` result |
| --- | --- |
| (0, 0, 0, 1234): a total-only provider | `{total_tokens: 1234}`, others null |
| (120, 0, 0, 0) | `{input_tokens: 120, output_tokens: 0}`, others null |
| (0, 0, 0, 0) | `None` |

A genuine zero cache hit is therefore reported as unknown. That is deliberate:
the reported value cannot distinguish the two.

## API

| Name | Purpose |
| --- | --- |
| `OWNER` | `"bootstrap@plugins-kit"`, for remedy text |
| `SCHEMA_V1`, `SUPPORTED_SCHEMAS` | the frozen v1 literal, and the set of supported revisions (the capability marker; it only grows) |
| `CORE_EVENTS`, `ATTEMPT_SCOPED`, `LATER_REVISION_NAMES` | the vocabulary sets above |
| `MAX_PAYLOAD_BYTES` | 16384 |
| `EventError` | a `ValueError`; `.pointer` is the JSON pointer of the first fault (`"/identity/run_id"`, `"/3/seq"` inside a stream, `""` for the whole value) |
| `utc_timestamp(epoch=None)` | an `at` value from epoch seconds (int, float, or a decimal string such as `str(time.time())`); `None` means now |
| `usage_payload(*, input_tokens=None, output_tokens=None, cache_hit_tokens=None, total_tokens=None)` | the `usage` payload, or `None` |
| `make_event(*, seq, run_id, event, plugin, at=None, unit_id=None, attempt_id=None, adapter=None, model=None, payload=None, schema=SCHEMA_V1)` | build and validate one event; `at` defaults to now, `payload` to `{}` |
| `validate_event(value)` | validate one event; returns a normalized deep copy |
| `validate_stream(events)` | validate a stream; returns a tuple of events |
| `Emitter(plugin, run_id, *, unit_id=None, sinks=(), start_seq=0, schema=SCHEMA_V1)` | a stream bound to one plugin and run |
| `Emitter.emit(event, *, unit_id=None, attempt_id=None, adapter=None, model=None, payload=None, at=None)` | number, record, and return one event |
| `InMemorySink()` | collects events in `.events` |
| `JsonlSink(path, *, mode="create")` | writes one JSON line per event |
| `read_jsonl(path)` | read and validate a JSONL event file |

`Emitter.emit` assigns the next `seq` (starting at `start_seq`) and writes the
event to every sink while holding ONE lock, so each sink receives events in
`seq` order even when several threads emit. `unit_id` passed to `emit`
overrides the bound unit for that event. An invalid event raises `EventError`,
consumes no `seq`, and reaches no sink. An exception raised by a sink
propagates to the caller.

## Sinks

- **`InMemorySink`** appends each event to its `events` list.
- **`JsonlSink`** writes one line per event: sorted keys, compact separators,
  ASCII only, flushed per write. It validates each event before writing it.
  - `mode="create"` (the default) refuses an existing file with
    `FileExistsError`.
  - `mode="truncate"` replaces any existing file when the sink is constructed,
    so the file records its writer's LAST execution.
  - Appending to an earlier stream is not supported: a continued stream would
    hold two `terminal` events for one unit, which `validate_stream` refuses.
  - A file has one writer. Concurrent writers to one file are not supported.
- **`read_jsonl(path)`** validates every line as an event and the whole file
  as one stream. Its `EventError` message starts with the 1-based line number
  of the first fault (`line 2: ...`).

No sink is a database, and no sink is shared between plugins.

## Probing for the module

A shared-lib link pins no version, and a long-running process keeps the copy
it imported. Probe before writing any fact or dispatching any call, and hold
two constants of your own: `REQUIRED_SCHEMA` (`SCHEMA_V1`, or a later literal
if you emit a later revision) and the bootstrap version that shipped it. The
probe distinguishes three states:

1. `import bootstrap_lib` raises `ModuleNotFoundError`: absent. Tell the user
   to run `claude plugin install bootstrap@plugins-kit`.
2. Too old or stale, when any of these holds:
   - `bootstrap_lib.execution_event` does not import;
   - `SUPPORTED_SCHEMAS` is missing, or `REQUIRED_SCHEMA` is not in it;
   - a callable you invoke is missing;
   - `inspect.signature(<constructor>).bind(**<the exact keywords you pass>)`
     raises `TypeError`, where the constructor is `make_event` or `Emitter`.

   Tell the user to run `claude plugin update bootstrap@plugins-kit` and
   restart, naming the version from YOUR constant.
3. Otherwise: usable.

Never read the version for a message from the module: a stale module cannot
know the version that replaced it.

```python
import inspect

REQUIRED_SCHEMA = "plugins-kit.execution-event/v1"
EXECUTION_EVENT_BOOTSTRAP = "0.135.0"


def _execution_event():
    try:
        import bootstrap_lib  # noqa: F401
    except ModuleNotFoundError as exc:
        raise MySupportError(
            "execution events need bootstrap: "
            "claude plugin install bootstrap@plugins-kit"
        ) from exc
    too_old = (
        f"execution events need bootstrap >= {EXECUTION_EVENT_BOOTSTRAP}: "
        "claude plugin update bootstrap@plugins-kit, then restart"
    )
    try:
        from bootstrap_lib import execution_event as module
    except ImportError as exc:
        raise MySupportError(too_old) from exc
    if REQUIRED_SCHEMA not in getattr(module, "SUPPORTED_SCHEMAS", ()):
        raise MySupportError(too_old)
    make_event = getattr(module, "make_event", None)
    if not callable(make_event):
        raise MySupportError(too_old)
    try:
        inspect.signature(make_event).bind(
            seq=0, run_id="r", event="result", plugin="p", at=None,
            unit_id=None, attempt_id=None, payload=None,
        )
    except TypeError as exc:
        raise MySupportError(too_old) from exc
    return module
```

Choose the edge class (REQUIRED, REFUSE, or DEGRADE) for your plugin with
[optional-plugin-dependencies](optional-plugin-dependencies.md). A plugin that
calls the module declares `"shared_lib_imports": ["bootstrap_lib"]` in its
`bootstrap.json`, and a REQUIRED edge sets `requires_bootstrap` to the version
that shipped the call shape it uses.

## Why a stale module is safe

- A process that imported a v1-only module validates every v1 event exactly as
  any later module does, because v1 is frozen.
- The same process refuses an event written under a later revision at
  `schema`, with a named error.
- An emitter that needs a later revision finds it missing at its own probe,
  before any fact is written.
- `bootstrap_lib.execution_event` imports only the standard library and
  nothing from `bootstrap_lib`, so a process holding copies of two
  `bootstrap_lib` generations cannot break it internally.
