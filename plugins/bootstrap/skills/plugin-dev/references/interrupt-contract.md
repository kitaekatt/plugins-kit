# Interrupt contract

A durable interrupt is a healthy wait: running work asks a typed question, its
store records the request, and an answer, a rejection, or a lapse is recorded
once. A plugin in this marketplace that implements such a wait keeps it in its
own store and checks every request and every resolution with the one contract
specified here. The contract is shared; no store, database, or runtime is.

The audience is a plugin author implementing a wait on the plugin's own store,
and a reviewer checking one. The implementation is
`bootstrap_lib.interrupt_contract`; this file specifies the rules it enforces.

## What is shared and what stays in your plugin

Shared, and the same for every store:

- the request's key set, field rules, and limits;
- the canonical JSON form;
- the decision words, the outcome words, and the argument rules;
- input validation against the request's own schema, under one named JSON
  Schema subset;
- the replay test, the expiry arithmetic, and the lapse rule;
- the resolution document's seven keys and its bytes.

Yours, and outside the contract:

- **The store.** Tables, transactions, ids, and which attempt or claim owns a
  request. The module opens no file and no database.
- **How a request arrives.** A file your plugin reads, or a function your
  caller invokes. `parse_request_document` takes bytes; reading them, and
  wording a read failure, is yours.
- **Continuation.** What runs after an answer, and how often.
- **Outcome policy.** What a unit does after a rejection or a lapse: stop,
  return to a claimable state, or anything else. The contract records which
  outcome happened and says nothing about what follows.
- **When a lapse is recorded.** `lapsed` answers whether an interrupt has
  lapsed at a given time. Deciding when to ask, and writing the `expired`
  outcome, is yours.
- **Event phases.** The module has no event function. An `interrupt` event and
  its phases belong to the execution-event vocabulary
  ([execution-events](execution-events.md), "Schema v2"); each store emits the
  phase for the fact it recorded.
- **Error classes and exit codes** your callers see. Catch the contract's
  errors and raise your own if your callers depend on your class names.

## Request

```yaml
schema: plugins-kit.interrupt-request/v1   # one of the literals the store accepts
kind: approval                             # [a-z][a-z0-9-]*, at most 64 characters
request_schema: {type: object}             # the JSON Schema an answer must satisfy
payload: {target: v1.2.0}                  # what the person answering needs to see
expires_in_s: 3600                         # optional; an int from 1 to 2147483647
```

Rules, each enforced by `check_request`, in this order:

1. **The validator is usable** (see "Validator"). An unusable validator is
   refused with `ValidatorError` before any field is read.
2. **`schema` is one of `accepted_envelopes`**, the literals the calling store
   accepts. Any other value, or a non-string, is refused; the refusal names
   `owner` and the accepted literals.
3. **`kind`** is a string matching `[a-z][a-z0-9-]*`, at most `KIND_LIMIT`
   (64) characters.
4. **`request_schema` and `payload` are JSON objects.**
5. **Both are JSON-native**: string, int, finite float, bool, null, list, or
   object with string keys. A tuple, a set, NaN, infinity, or a non-string key
   is refused, and the refusal names the JSON pointer of the first fault.
6. **`expires_in_s`**, when present, is an int from 1 to `EXPIRES_IN_S_MAX`
   (2147483647). A bool or a float is refused.
7. **`request_schema` is inside the validator's subset**: the contract calls
   `validator.check_schema(request_schema, subset=VALIDATOR_SUBSET)` and
   reports a `ValueError` from it as a refused request.

`check_request` returns a dict with the keys `envelope`, `kind`,
`request_schema`, `payload`, and `expires_in_s`. `request_schema` and
`payload` are canonical deep copies, so the caller may store them without
holding a reference into the request it was given.

`check_request_mapping` takes the request as a mapping with the request's own
keys. **The key set is closed**: exactly `REQUEST_KEYS` (`schema`, `kind`,
`request_schema`, `payload`) plus any of `OPTIONAL_REQUEST_KEYS`
(`expires_in_s`). An unknown or a missing key is refused before any field is
checked.

`parse_request_document` takes the bytes of a request document:

- at most `REQUEST_DOCUMENT_LIMIT` (131072) bytes;
- UTF-8;
- JSON, with `NaN`, `Infinity`, and `-Infinity` refused;
- no key repeated in any object;
- one JSON object at the top level.

It then applies `check_request_mapping`.

## The three per-store inputs

Exactly three values vary between stores. Everything else in this file is the
same for every store.

| Input | Passed to | Meaning |
| --- | --- | --- |
| `owner` | the three request functions | the accepting plugin's name, used only in the refusal of an unaccepted `schema` literal |
| `accepted_envelopes` | the three request functions | the collection of `schema` literals this store accepts for the v1 request shape; a bare string is refused with `TypeError` |
| `resolution_envelope` | `resolution_document` | the one literal this store writes as a resolution document's `schema`; it must match `[a-z][a-z0-9-]*\.interrupt-resolution/v1` |

A store with no literals of its own uses the shared ones:
`REQUEST_ENVELOPE_V1` (`plugins-kit.interrupt-request/v1`) and
`RESOLUTION_ENVELOPE_V1` (`plugins-kit.interrupt-resolution/v1`). A store that
already has frozen literals keeps them and passes them instead; job-kit's
`job-kit.interrupt-request/v1` is such a literal. Pass fixed literals from
your own module. Do not take any of the three from your caller.

## Validator

The contract validates schemas and answers with a validator the CALLER passes
in. `bootstrap_lib` is stdlib-only and imports no plugin library, so the
module names the validator and imports nothing:

- `VALIDATOR_MODULE` is `llm_scripting_kit.completion.json_schema`, the module
  that is meant. It is a string. Your plugin imports that module and passes it
  as `validator=`.
- `VALIDATOR_SUBSET` is `llm-scripting-kit.json-schema-subset/v1`, the frozen
  JSON Schema subset `CONTRACT_V1` requires.

`check_validator(validator)` refuses, with `ValidatorError`:

- a validator with no `SUPPORTED_SUBSETS` collection (a set, frozenset, tuple,
  or list; a bare string does not count);
- a validator whose `SUPPORTED_SUBSETS` does not contain `VALIDATOR_SUBSET`;
- a validator whose `check_schema` or `validate` is not callable.

The message names the required literal and what the validator advertises.
**Matching signatures are not enough.** A validator that accepts the same
arguments and carries no marker is refused, because nothing says which keyword
set it applies.

Every call the contract makes selects the subset by name:

```python
validator.check_schema(request_schema, subset=VALIDATOR_SUBSET)
validator.validate(request_schema, value, subset=VALIDATOR_SUBSET)
```

So a request accepted under this contract is validated under the same subset
when it is recorded and at every later resolution, whichever validator release
is linked. The contract cannot observe a validator's behaviour beyond its
marker; what the subset means is frozen by the validator's owner.

## Resolving

**Decisions and outcomes.** `DECISIONS` maps the word a resolver names to the
outcome it records: `answer` to `answered`, `reject` to `rejected`. `OUTCOMES`
is `answered`, `rejected`, `expired`; no decision produces `expired`, which a
store records when it finds an interrupt lapsed.

`decision_outcome(decision, *, input=None, reason=None)` returns the outcome
and refuses, with `DecisionError`:

- any other decision word;
- a rejection that carries an input;
- an answer that carries a reason.

**Input validation.** `validate_input(request_schema, value, *, validator)`
returns the canonical JSON text of an accepted answer, which is what a store
records. It raises `InputError`:

- with empty `errors`, when the value is not JSON-native or its canonical text
  is over `INPUT_LIMIT` (65536) bytes; the validator is not called;
- with the validator's `(json_pointer, keyword)` tuples verbatim in `errors`,
  when the value fails the request schema.

**Reason.** `bound_reason(reason)` is `str(reason)` cut to `REASON_LIMIT`
(2000) characters, or `None` for no reason. Record the bounded value.

**Replay.** A resolution is recorded once. A second resolve call is either a
replay of the stored resolution, which changes nothing, or a conflict.
`same_resolution(*, stored_outcome, stored_input_json, stored_reason, outcome,
input=None, reason=None)` is the test:

- the outcomes are equal, and
- for `answered`, the canonical JSON of `input` equals `stored_input_json`;
  otherwise, `bound_reason(reason)` equals `stored_reason`.

Key order does not matter, because both sides are canonical. An input with no
canonical form compares unequal.

**Expiry.** `expiry(created_at, expires_in_s)` is `created_at + expires_in_s`,
or `None` when the request set no expiry. `lapsed(expires_at, now)` is true
when `now >= expires_at`: an interrupt lapses AT its `expires_at`. An
interrupt with no expiry never lapses.

## Resolution document

`resolution_document` returns the canonical JSON text a store hands to the
work that continues after a resolution:

```json
{"input":{"approved":true},"interrupt_id":"7","kind":"approval","outcome":"answered","payload":{"target":"v1.2.0"},"resolved_at":"2026-09-30T10:00:00Z","schema":"plugins-kit.interrupt-resolution/v1"}
```

- **Exactly seven keys**: `schema`, `interrupt_id`, `kind`, `outcome`,
  `input`, `payload`, `resolved_at`.
- `schema` is the store's `resolution_envelope`. A value that is not a v1
  resolution literal is refused with `RequestError`.
- `outcome` is one of `OUTCOMES`; any other value is refused with
  `ContractError`.
- `resolved_at` is given as the recorded epoch and rendered as ISO-8601 UTC
  ending in `Z`, with six fractional digits only when the epoch has a
  fractional part.
- Build it only from recorded values. The same recorded row then gives
  byte-identical text on every call, which is what lets a continuation that
  runs more than once use the document as a stable key.

## Canonical JSON

`canonical_json(value)` is the one text form the contract compares and
records: keys sorted, separators `,` and `:` with no spaces, ASCII only
(non-ASCII characters escaped). Equal values give byte-identical text whatever
their key order. NaN and infinity raise `ValueError`.

## API

| Name | Purpose |
| --- | --- |
| `OWNER` | `"bootstrap@plugins-kit"`, for remedy text |
| `CONTRACT_V1`, `SUPPORTED_CONTRACTS` | the frozen contract literal, `"plugins-kit.interrupt-contract/v1"`, and the set of supported revisions (the capability marker; it only grows) |
| `REQUEST_ENVELOPE_V1`, `RESOLUTION_ENVELOPE_V1` | the frozen shared envelope literals |
| `VALIDATOR_MODULE`, `VALIDATOR_SUBSET` | the validator's module name and the subset literal; two strings |
| `REQUEST_KEYS`, `OPTIONAL_REQUEST_KEYS` | the closed request key set |
| `REQUEST_DOCUMENT_LIMIT`, `INPUT_LIMIT`, `KIND_LIMIT`, `EXPIRES_IN_S_MAX`, `REASON_LIMIT` | 131072, 65536, 64, 2147483647, 2000 |
| `OUTCOMES`, `DECISIONS` | the outcome words, and the decision-to-outcome pairs |
| `ContractError` | a `ValueError`; the base of the four classes below |
| `RequestError`, `InputError`, `DecisionError`, `ValidatorError` | a refused request, input (with `.errors`), decision, or validator |
| `check_validator(validator)` | refuse a validator that cannot be held to `VALIDATOR_SUBSET` |
| `canonical_json(value)` | the canonical text of a JSON value |
| `check_request(*, envelope, kind, request_schema, payload, expires_in_s=None, accepted_envelopes, owner, validator)` | validate the fields of one request; returns a dict |
| `check_request_mapping(raw, *, accepted_envelopes, owner, validator)` | closed key set, then `check_request` |
| `parse_request_document(data, *, accepted_envelopes, owner, validator)` | document rules, then `check_request_mapping` |
| `validate_input(request_schema, value, *, validator)` | validate an answer; returns its canonical text |
| `expiry(created_at, expires_in_s)` | when a request lapses, or `None` |
| `lapsed(expires_at, now)` | whether it has lapsed; inclusive |
| `bound_reason(reason)` | the reason as recorded |
| `decision_outcome(decision, *, input=None, reason=None)` | the outcome a decision records |
| `same_resolution(*, stored_outcome, stored_input_json, stored_reason, outcome, input=None, reason=None)` | the replay test |
| `resolution_document(*, resolution_envelope, interrupt_id, kind, outcome, input, payload, resolved_at)` | the canonical resolution document |

## Probing for the module

A shared-lib link pins no version, and a long-running process keeps the copy
it imported. Probe before recording any wait, and hold two constants of your
own: the contract literal you need and the bootstrap version that carries it.
`CONTRACT_V1` is carried by bootstrap 0.137.0. The probe distinguishes three
states:

1. `import bootstrap_lib` raises `ModuleNotFoundError`: absent. Tell the user
   to run `claude plugin install bootstrap@plugins-kit`.
2. Too old or stale, when any of these holds:
   - `bootstrap_lib.interrupt_contract` does not import;
   - `SUPPORTED_CONTRACTS` is missing, or your literal is not in it;
   - a function you call is missing;
   - `inspect.signature(<function>).bind(<the exact arguments you pass>)`
     raises `TypeError`.

   Tell the user to run `claude plugin update bootstrap@plugins-kit` and
   restart, naming the version from YOUR constant.
3. Otherwise: usable.

Never read the version for a message from the module: a stale module cannot
know the version that replaced it.

```python
import inspect

REQUIRED_CONTRACT = "plugins-kit.interrupt-contract/v1"
INTERRUPT_CONTRACT_BOOTSTRAP = "0.137.0"

# Every call your plugin makes, with the exact arguments it passes.
_CALLS = (
    ("check_request", (), {
        "envelope": "", "kind": "", "request_schema": {}, "payload": {},
        "expires_in_s": None, "accepted_envelopes": (), "owner": "",
        "validator": None,
    }),
    ("validate_input", ({}, None), {"validator": None}),
    ("decision_outcome", ("answer",), {"input": None, "reason": None}),
    ("same_resolution", (), {
        "stored_outcome": "", "stored_input_json": None, "stored_reason": None,
        "outcome": "", "input": None, "reason": None,
    }),
    ("resolution_document", (), {
        "resolution_envelope": "", "interrupt_id": "", "kind": "",
        "outcome": "", "input": None, "payload": {}, "resolved_at": 0.0,
    }),
)


def _interrupt_contract():
    try:
        import bootstrap_lib  # noqa: F401
    except ModuleNotFoundError as exc:
        raise MySupportError(
            "interrupts need bootstrap: "
            "claude plugin install bootstrap@plugins-kit"
        ) from exc
    too_old = (
        f"interrupts need bootstrap >= {INTERRUPT_CONTRACT_BOOTSTRAP}: "
        "claude plugin update bootstrap@plugins-kit, then restart"
    )
    try:
        from bootstrap_lib import interrupt_contract as module
    except ImportError as exc:
        raise MySupportError(too_old) from exc
    if REQUIRED_CONTRACT not in getattr(module, "SUPPORTED_CONTRACTS", ()):
        raise MySupportError(too_old)
    for name, args, kwargs in _CALLS:
        function = getattr(module, name, None)
        if not callable(function):
            raise MySupportError(too_old)
        try:
            inspect.signature(function).bind(*args, **kwargs)
        except (TypeError, ValueError) as exc:
            raise MySupportError(too_old) from exc
    return module
```

Probe the validator the same way, with a constant of your own for the
llm-scripting-kit version that carries the subset (0.56.0 for
`llm-scripting-kit.json-schema-subset/v1`):

- `llm_scripting_kit` does not import: absent. Tell the user to run
  `claude plugin install llm-scripting-kit@plugins-kit`.
- `llm_scripting_kit.completion.json_schema` does not import, its
  `SUPPORTED_SUBSETS` is missing or lacks the literal, `check_schema` or
  `validate` is not callable, or `check_schema({}, subset=...)` or
  `validate({}, None, subset=...)` does not bind: too old or stale. Tell the
  user to run `claude plugin update llm-scripting-kit@plugins-kit`.

Hold the subset literal in your own module as a string; do not read it from
either module for a message. Run both probes before the first write, so that
no wait is recorded under rules your plugin could not load.

Choose the edge class (REQUIRED, REFUSE, or DEGRADE) for your plugin with
[optional-plugin-dependencies](optional-plugin-dependencies.md). A wait
recorded without the contract would be read as a checked wait, so a plugin
that can work without waiting REFUSES the waiting verbs, and a plugin that
cannot is REQUIRED. A plugin that calls the module declares
`"shared_lib_imports": ["bootstrap_lib"]` in its `bootstrap.json`, and a
REQUIRED edge sets `requires_bootstrap` to the version that carries the call
shapes it uses.

## Revisions

`CONTRACT_V1`, both shared envelope literals, and `VALIDATOR_SUBSET` are
frozen: the rules, limits, and message text specified here never change under
them. A later rule enters only through a later revision:

1. a constant such as `CONTRACT_V2 = "plugins-kit.interrupt-contract/v2"`,
   with its rules;
2. `SUPPORTED_CONTRACTS` gaining that literal, while `CONTRACT_V1` and its
   rules stay unchanged;
3. a keyword selector on the affected functions that defaults to v1, so a
   caller that passes no selector is unaffected.

This is the procedure [execution-events](execution-events.md) sets for its
schemas ("Revisions"). A process that holds a module without a later revision
finds it missing at its own probe, before any wait is recorded.
