# The completion seam's per-call contract

What the seam GUARANTEES about one call: what an adapter advertises before it,
what the response reports after it, and what the CLI protocol carries in both
directions. Read this when changing an adapter, a capability record, the
per-call report, or the `complete` verb.

It is a reference rather than part of `CLAUDE.md` because it answers one
on-demand question -- what does this seam promise per call -- while the plugin's
`CLAUDE.md` carries what a reader needs on every load: the altitude rule, the
transports, and the one-line invariants that point here.

The invariants themselves stay ambient in `CLAUDE.md` because breaking one is
how the contract dies quietly:

- A capability describes what the adapter EMITS, never what the provider does
  with it.
- Suppressing a flag is not a control.
- A protocol error is not an endpoint error.

**The one rule the advertisement obeys: a capability describes what the adapter
EMITS, never what the provider or CLI does with it.** `ExecutionControl.emits`
names the exact argv element, environment key path, or request field produced,
and a seam test in `tests/llm-scripting-kit/test_completion_capabilities.py`
asserts every one of them. No fake seam can establish that a target HONORS a
control, so nothing here claims it. Two corollaries that were each got wrong
once and are now pinned by tests:

- **Suppressing a flag is not a control.** codex emits nothing at all for
  `network=False`; the absence of a flag is not a deny, and no such control is
  advertised.
- **A value menu is advertised only where the request-building code validates
  it.** `CodexCliBackend` calls the shared argv builder directly and bypasses
  `CodexAdapter`'s effort validation, so codex advertises no effort `values`.

When an adapter changes what it emits, its record changes in the same commit.
The capability facts live in adapter code and are hand-authored per adapter --
codex's argv is built in `bootstrap_lib.codex`, a different plugin, so deriving
the map mechanically would mean reaching across that boundary. The seam tests
are what keep the hand-authored record honest; a param applied but unadvertised
is what they exist to catch. Never keep a second copy of these facts in YAML or
a docs table: that copy is free to disagree with the adapter, which is the drift
the advertisement replaces.

**The advertisement has one matching language, and this package owns it.**
`completion/requirements.py::match_capabilities(capabilities, requirements)`
answers whether one adapter's `Capabilities` (or its `to_json()` mapping)
satisfies a requirement, so a consumer selecting among endpoints (job-kit's
`select.py` is one) does not keep a capability vocabulary of its own.
`requirements` is `None` or `{}` to match anything, a list as shorthand for
`{"params": [...]}`, or a mapping whose named convenience keys read the
advertisement's public shape: `params` (aliases `required_params`, `honors`; a
list of required param names, or a per-param mapping where `False` means the
param must be absent), `execution_controls` (alias `controls`; required control
ids), `dropped_params` (required dropped-param names), `structured_output`
(alias `structured`; a mode string, a result string, or a mapping) and
`system_prompt` (alias `system_prompt_mode`; a mode string or a mapping). Any
other key is a dotted path over `Capabilities.to_json()`, e.g.
`structured_output.mode`. The function walks the JSON shape and nothing else,
so adding a capability to an adapter needs no change here; pinned by
`tests/llm-scripting-kit/test_completion_requirements.py`.

**What the advertisement is BEFORE the call, the response is AFTER it.**
`LLMResponse` carries what actually happened on one call: `dropped_params` (the
params this caller requested that the adapter does not read), `forwarded_params`
(params sent downstream without the adapter validating them),
`execution_controls_applied` (the ids of advertised controls the request
actually emitted), `structured` (schema-backed output, parsed), `started_at` /
`ended_at`, and `status` / `error`. The two halves are one system, not two
lists: `completion/results.py` DERIVES the per-call report from the adapter's
own record -- `derive_dropped_params` intersects the advertised dropped set with
what the caller actually set, and `check_applied_controls` refuses an id the
advertisement does not carry. A second hand-maintained list is exactly the drift
the SSOT rule forbids, and here it would be a drift between two claims about the
same call.

**`extras` is reported per KEY, and dropped is not the only verdict.** The
`extras` map is where the seam's inequality is sharpest: codex reads a named
set, advertised as `extras.<key>` params in `completion/adapter_capabilities.py`
and pinned against the argv builder's own list by
`test_codex_extra_keys_match_the_advertisement`; openrouter copies EVERY key
into the request as a top-level parameter without validating it; and claude-cli
and opencode-cli read none. So the report answers per key,
via `derive_extras_report`, and the bare field name `extras` never survives into
a response -- on codex it would have no single answer, and on the others it
would not say WHICH key went nowhere.

The third verdict is the one that is easy to get wrong. An unadvertised
openrouter extra is FORWARDED, not dropped: reporting it as dropped would be a
fresh overclaim in place of the old silence, since the key really is sent and
the adapter's only claim is that it did not check it. `dropped_params` and
`forwarded_params` therefore stay separate fields, because they carry opposite
advice -- remove this param, versus this param reached the provider and may be
doing its job. The advertisement is what distinguishes them: a generic `extras`
param with `handling: passthrough` means forwarded, a per-key
`extras.<name>` entry means honored, and neither means dropped.

**Tool denial exists on exactly one adapter, and the record says so.**
claude-cli's `--disallowedTools` is the only real DENY channel across the four:
`allowed_tools` is an ALLOW-list and cannot express denying an arbitrary tool
without a complete tool universe, which no adapter has. codex constrains
RESOURCES (sandbox, network) rather than tools, opencode injects fixed scalar
permission denials of its own, and openrouter has no tool surface at all. So
the honest answer differs per adapter, which is why there is no flat
`tool_deny` enum -- one word would be false for at least two of them.

The deny control is the adapter's one CONDITIONAL control, and the asymmetry
with `allowed-tools` beside it is deliberate. `--allowedTools` is emitted on
every call, passing `""` when the caller named no tools, because an empty
ALLOW-list is a real restriction (allow nothing) and is therefore reported.
An empty DENY-list restricts nothing, so an unset `disallowed_tools` emits no
flag and reports no control -- suppressing a flag is not a control.

**`system_prompt_mode` is a real mode choice, not two spellings.**
`--system-prompt` makes the caller's text the WHOLE system prompt;
`--append-system-prompt` adds it to the CLI's own default prompt. The model
ends up with different text, so the advertisement carries both under
`system_prompt.modes` with a per-mode `emits_by_mode`, and `replace` remains
the default so an existing caller's request is unchanged. The param advertises
a `values` menu because the adapter REJECTS an unknown mode before dispatch --
forwarding one would silently hand the caller `replace` while they believed
they had asked for an append, and would make the advertised menu a claim no
code backs. `_CLAUDE_SYSTEM_PROMPT_FLAGS` is the single map behind both the
validation and the advertised menu, so a mode cannot reach the argv without
appearing in the record.

As everywhere here, both are claims about the argv this adapter BUILDS. Nothing
establishes that the CLI honors a deny or that the appended text survives --
no seam test can, and claiming it would be the overclaim the advertisement
exists to prevent.

Reporting the applied controls is per-adapter work, and deliberately so: only
the code that builds the request knows what it emitted. `source` does not settle
it -- codex's `sandbox-mode` is `source=REQUEST` yet emitted on every call
because it has a default -- so `CodexCliBackend` reads its controls off the
BUILT ARGV (the argv is the emission, and codex's argv is built in another
plugin), `OpencodeCliBackend` reads its advertised `FIXED` set because all of
its controls are unconditional, and `ClaudeCliBackend` names its three
unconditional ones. `--allowedTools` goes out even when the caller named no
tools: an empty allow-list is an emitted allow-list, not a suppression, so it is
reported.

**Structured output is parsed only under a caller schema.** codex advertises
`result: parsed` and the adapter parses its `-o` file into `structured` when --
and only when -- `extras.output_schema` was sent. Valid JSON a model produced
unbidden is not schema-backed output, and presenting it as such would be the
overclaim the advertisement exists to prevent; an unparseable result under a
schema leaves `structured` None rather than failing the call, because `text`
still carries the answer verbatim.

**Error-as-data lives at the CLI surface only.** The package API keeps RAISING
typed exceptions -- every existing consumer branches on them, and returning a
failure there would make it read as a SUCCESS at call sites that never asked for
this contract. The `complete` verb emits a failure in the same envelope shape as
a success (`status` of `completed` / `timeout` / `error`, plus an `error` object
carrying the halt classification as its `code`), on stdout, with exit codes
unchanged as the shell-level signal.

**The `complete` verb speaks a versioned protocol in BOTH directions.** The
result envelope carries `protocol` (`request_protocol.PROTOCOL_VERSION`)
alongside the unchanged `endpoint` / `kind` / `backend` / `response` keys, and
`--request-file <path|->` accepts a versioned JSON request instead of flags.
`request-schema` prints the accepted shape, derived from `BackendOptions` rather
than hand-written.

The request half exists because flags ran out, not for symmetry: `extras` is an
open JSON map whose per-key handling is observable in the response, and no flag
spelling expresses it. `allowed_tools`, `disallowed_tools` and
`system_prompt_mode` had no CLI path at all. A request and the call-describing
flags are REFUSED together rather than merged -- a precedence rule is invisible
at the call site, so a flag silently overriding a request field would be the
kind of lie this contract removes.

**`request_protocol.py` is where declared types become enforced ones.**
`capabilities.py` says `ParamCapability.type` is a declared expectation and that
"validation belongs at the CLI request boundary"; this is that boundary. Note
the one place it deliberately does NOT follow the seam's drop-and-report rule:
an unknown `options` key is a protocol ERROR, not a dropped param. The seam
drops what an ADAPTER does not read -- a fact about the adapter, reported
truthfully -- whereas a key that is no `BackendOptions` field at all is a fact
about the caller, which nothing advertises and no adapter could read, so there
is no honest per-call report to make.

**A protocol error is not an endpoint error, and the exit codes say so.**
`EXIT_PROTOCOL` (4) means the request could not be understood and no call was
attempted; it writes its envelope to STDERR, leaving stdout as the result
channel a consumer parses for one shape only. That is distinct from
`EXIT_USAGE` (2), which is argparse's territory -- a person typing a command
wrong -- and from `EXIT_FAILURE` (1), which means a call ran and failed and may
succeed on retry. Retrying a malformed request cannot help; the codes exist so a
consumer can tell those apart without parsing prose.

**An empty answer is CLASSIFIED, not raised -- except on an exhausted budget.**
A reasoning model can end its turn having emitted no content at all:
`finish_reason` is `stop`, not `length`, the token budget is nowhere near spent,
and `message.content` is the empty string. `OpenRouterBackend` returns that as an
ordinary response carrying `reasoning` and `finish_reason`, and leaves the halt
decision to the caller, per this layer's altitude. Only `finish_reason ==
"length"` raises `EmptyCompletionError` (a `RuntimeError`, so existing
`except RuntimeError` sites keep working) -- the trigger it always had.

Raising on EVERY empty answer was tried and reverted, and the reason generalizes:
**the provider bills for an empty answer.** content-pipeline-kit prices a call
only after its retry loop breaks on success, and `classify_halt` does not
classify an `EmptyCompletionError`, so raising would move a paid call into an
uncharged path and then retry it `retries` more times -- multiplying untracked
spend on the exact failure being diagnosed. A seam that halts on a caller's
behalf cannot see that; the caller can.

`finish_reason` is on `LLMResponse` because it is what separates the two causes,
and no consumer could previously tell them apart. Inferring exhaustion from
empty-text-plus-nonzero-tokens alone -- as content-pipeline-kit's
`likely_reasoning_exhausted` does -- gives the right answer for `length` and the
WRONG remedy ("consider raising max_tokens") for `stop`. Measured on Qwen3.8-27B
through NInfer, 2026-09-01: 4 of 11 requests at high effort returned empty, every
one `finish_reason="stop"`, one having spent 29k of a 60k budget.

`reasoning` surfaces the thinking block on both paths (OpenAI-compatible
transport only; the CLI transports leave it `""`). It is DIAGNOSTIC, not a
recoverable answer -- in the measured case the block ended in a repetition loop
and contained no answer to salvage. It is what makes an empty response
explicable at all; without it, a repetition loop and a model with nothing to
report are indistinguishable.
