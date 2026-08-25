# Model-endpoints dispatch

The registry of OpenAI-compatible endpoints this machine can drive, how a unit
is pointed at one, and the behaviors of the locally hosted server class. Load
this when the user has asked for a registered model endpoint -- the rendered
policy carries the summary; this carries the detail.

Rendered policy: `scripts/orchestration_guidance.py`. Backend record and the
one-line command: `defaults/orchestration.yaml`, `backends[id: model-endpoints]`.
The harness mechanics -- the wire API, the custom-provider `-c` shape, keyless
providers, the stderr noise -- are in
[codex-dispatch.md](codex-dispatch.md), "Custom providers"; this file does not
repeat them.

## The registry

Machine-specific values are not shipped in this plugin. They live in a file the
user owns:

    ~/.claude/config/model-endpoints.yaml

or, when `MODEL_ENDPOINTS_REGISTRY` is set, the file that variable names. Read
it at dispatch time and take the chosen entry's values from it. When neither
exists, this backend does not render at all, so a reader here has one.

    version: 1
    default: <entry id>          # used when a caller names no entry
    models:
      <entry id>:
        name: <human label>            # optional
        base_url: http://<host>:<port>/v1
        model: <model id the server serves>
        context_window: <tokens>       # optional
        reasoning_effort: <level>      # optional, this entry's default
        key_env: <VAR>                 # optional; absent means keyless

`base_url` and `model` are the only required fields. Unknown keys are ignored,
so the file may carry more than this; `version:` exists for a true schema
break.

**Choosing an entry.** The Detected roster in the rendered policy lists which
entries answered a `GET {base_url}/models` probe at render time, with each
entry's id and model id. When the user names an entry, a model, or a model
family, that names the dispatch. When the user names none, use the registry's
`default` entry if the roster shows it up, otherwise the one that is up. If
several are up and nothing distinguishes them, ask rather than pick.

**Filling the command.** `base_url`, `model` and `context_window` go into the
`-c` pairs of the record's command; a missing `context_window` gets a
conservative floor rather than an optimistic guess, because a model-name-derived
guess is how a run overruns a context it does not have. `reasoning_effort` is a
request parameter, so pass the entry's default -- or the level the user asked
for -- as `-c model_reasoning_effort=<level>`.

## When a harness is warranted at all

A harness is not a transport wrapper. A raw endpoint returns text; codex
supplies the agent loop, the shell / file / apply_patch tools, instruction-file
ingestion, a sandbox, a working directory, and a machine-parseable result. That
is worth roughly 11,000 tokens of fixed prompt overhead per run and turns a
seconds-long call into a minutes-long session.

So the rule is: **a harness is warranted exactly when the information the unit
needs is not knowable when the prompt is written** -- it must discover what to
read, verify its own output, iterate, edit files in place, or honour
instruction files it was not handed. Pure transformation of a fully supplied
context -- summarize, classify, translate, rewrite, extract, score -- is a
completions call and must stay one. Wrapping such a unit in a harness buys
nothing and pays the whole overhead.

## The local server class

These behaviors are properties of a locally hosted llama.cpp-class server, not
of the registry. Verify them against any other runtime before assuming they
carry over.

**The result contract holds on the failure path.** Exit 0 with the `-o` file
written, or nonzero and NO file when the server is unreachable. A down server
fails at launch rather than hanging, so a dispatch never needs a watchdog of
its own -- but it does mean an unavailable entry looks exactly like a bad
launch until you read the error.

**Servers are down by design.** A LAN model server is typically started by
hand, with no daemon and no restart-on-boot. The roster is a render-time
snapshot; a long session can outlive it. Re-render, or ask, rather than
retrying a failed dispatch blind.

**Truncation mid-tool-call poisons the session.** A turn that hits the token
limit while emitting tool-call arguments produces unparseable JSON, and every
later request carrying that history then fails outright -- not just the turn
that truncated. It is unrecoverable within that session and invisible until it
happens. The mitigation is generous token budgets: do not squeeze a unit on
this backend to save capacity.

**Reasoning effort is data, never a hardcoded level.** On this server class the
top-level request parameter accepts the full menu including `none`, while the
`chat_template_kwargs` form raises a server error on `none` and `minimal`. A
level that works through one path can fail through the other, and the accepted
menu differs per model. This is why effort is a per-entry registry field rather
than a constant in any shipped file: carry whatever the entry declares, and
pass a user-named level through unchanged.

**A large tool roster can suppress tool calls.** Qwen3-Coder-class templates
are documented to sometimes fail to emit a tool call when given many tools and
a long system prompt, because the lazy grammar never triggers
(llama.cpp#26530); `tool_choice: "required"` is the documented mitigation for a
direct API caller. It did not reproduce under a codex run with twelve tools at
both an 11k and a 118k prompt, so treat it as a known failure mode to recognize
rather than one to pre-empt.

**Concurrency shares one context pool.** A default-configured server serves each
slot the full context rather than dividing it, but the pool is shared, so
several long concurrent sessions contend for it. Prefer sequencing units on one
entry over fanning out against it.
