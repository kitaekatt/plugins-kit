# Codex dispatch mechanics

The exact flags, command shape, and traps for launching a unit on the Codex
CLI backend. Load this when you have already CHOSEN Codex and are composing
the launch -- the rendered policy carries the summary; this carries the detail.

Rendered policy: `scripts/orchestration_guidance.py`. Backend record and the
one-line command: `defaults/orchestration.yaml`, `backends[id: codex]`.

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

**Truncation mid-tool-call poisons the session.** A turn that hits the token
limit while emitting tool-call arguments produces unparseable JSON, and every
later request carrying that history then fails outright -- not just the turn
that truncated. It is unrecoverable within that session and invisible until it
happens. The mitigation is generous token budgets: do not squeeze a unit on
this backend to save capacity.

## Absolute paths, always

Every path you hand codex -- `-C`, `--add-dir`, `-o` -- is ABSOLUTE. This is a
hard rule, not a style preference. A relative `-C` combined with `--add-dir`
voids the entire writable-root set: every write fails, including writes inside
the root itself. The measured 2x2:

  relative -C, no --add-dir      writes inside the root succeed
  relative -C, --add-dir TEMP    ALL writes fail, root included
  absolute -C, --add-dir any     works, including the add-dir target

And pass `-C` explicitly rather than relying on the process cwd: a `cd` inside a
backgrounded Bash call does not persist.

## The one invocation

There is one sanctioned way to launch a unit. Use it as written.

  codex exec -s workspace-write \
    -c 'windows.sandbox="unelevated"' \
    -c 'sandbox_workspace_write.network_access=true' \
    -C <ABSOLUTE root> \
    --add-dir <ABSOLUTE session scratchpad> \
    --skip-git-repo-check --color never \
    -o <ABSOLUTE result file> - < <ABSOLUTE brief file> \
    > <ABSOLUTE log file> 2>&1

Every element is present because a probe showed its absence fails silently:

  `codex exec`   The NON-INTERACTIVE subcommand. Bare `codex "<prompt>"`
                 launches the TUI, needs a terminal, and dies instantly under
                 run_in_background with `Error: stdin is not a terminal`.
  `-s workspace-write`
                 The sandbox is the ONLY control in exec mode (see below).
  `windows.sandbox="unelevated"`
                 Without it, on Windows, `-s workspace-write` silently degrades
                 to read-only: every write is declined and the unit still exits
                 0 with a failure narrative that reads as model incompetence.
                 Verified as the single deciding variable across two otherwise
                 identical runs. Do not assume the user's `~/.codex/config.toml`
                 sets it.
  `sandbox_workspace_write.network_access=true`
                 Without it, egress fails with no error text at all -- `curl`
                 returns HTTP 000 and exit 1.
  `--add-dir <session scratchpad>`
                 `%TEMP%` is not writable under workspace-write, and the
                 session scratchpad lives there. Naming that one directory is
                 preferred over flipping `exclude_tmpdir_env_var`, which would
                 grant all of TEMP.
  `-o <FILE>`    Writes ONLY the agent's final message. This is the return
                 value; without it you are grepping a transcript that routinely
                 runs thousands of lines.
                 Codex writes its final assistant message to the `-o` path
                 WHEN THE TURN ENDS, overwriting whatever the unit itself wrote
                 to that path during the run. Observed 2026-09-01 on
                 codex-cli 0.150.1: a unit that wrote a 2,000-word report to
                 the `-o` path had it replaced by a one-line "Done" summary.
                 A brief that wants a deliverable file must name a SIBLING
                 path for it and reserve `-o` for the sign-off; judge the run
                 by the deliverable path, and treat `-o` as the summary only.
  `--skip-git-repo-check`  Allows running outside a git repo.
  `--color never`          Keeps ANSI escapes out of the captured log.
  `-` and stdin  The brief comes from a FILE on stdin, never a shell argument.
                 Real briefs are long and full of apostrophes, quotes and
                 backticks; embedding one in an argument reliably produces
                 `unexpected EOF while looking for matching quote` and the
                 dispatch never happens.

Launch each unit as its own Bash tool call with run_in_background: true.

Per-unit knobs on top of that shape:

  -m, --model <MODEL>      The provider model value. Routing uses an entry id;
                           the rendered target carries the harness and entry id.
  -c model_reasoning_effort=<low|medium|high|xhigh|max>
                           The effort dial, and a `-c` CONFIG KEY rather than a
                           flag -- `codex exec --help` does not list it, so
                           finding nothing there is expected. Verify a spelling
                           with `--strict-config`, which rejects an unknown key
                           at launch instead of ignoring it.
                           `xhigh` is real but undocumented by the CLI: this
                           list omitted it until a live run on codex-cli 0.149.1
                           echoed `reasoning effort: xhigh` in its session
                           header. Since the CLI documents neither the key nor
                           its values, absence from `--help` is not evidence a
                           level does not exist -- confirm with a run, not with
                           the help text.
  --output-schema <FILE>   JSON Schema for the final response, when you want
                           the return value machine-parseable.
  --json                   Emit events as JSONL, for progress tracking.

## Exit 0 is not success

`codex exec` has no approval channel -- it rejects `-a/--ask-for-approval`
outright. An action the sandbox forbids is AUTO-DENIED, never escalated to
anyone. The denial comes back to the model as a tool error, the model narrates
around it, and the turn completes normally with exit code 0. So `$?` carries no
information about whether the work happened. Read the `-o` file, then verify
against the actual diff: a session's report describes what it intended, not
necessarily what it did.

The same holds for the Bash tool's run_in_background notification -- it tells
you the process EXITED, not that the work succeeded. A session that died on a
bad flag notifies exactly like one that finished.

One corollary is worth stating because it inverts the usual intuition:
"permission spam" is never a reason to relax the sandbox, because there is no
prompting either way. The cost of the sandbox is silent capability loss, not
interruption.

## What the sandbox does and does not bound

It is an INTEGRITY (write) and EXFILTRATION (network) boundary. It is NOT a
confidentiality boundary: there is no read restriction at any level. Under
`-s read-only`, the most restrictive mode, codex read `~/.codex/config.toml`,
far outside its `-C` root. Assume a codex unit can read anything the invoking
user can -- SSH keys, .env files, any repo on any drive. Brief accordingly, and
do not treat `-C` as containment for secrets.

Never brief a unit to read anything under `~/.claude/projects`. Reads are
unrestricted and outside the `-C` root by design, so a unit told (or left) to
go looking there can reach it -- and a unit was observed grepping the
orchestrating session's own transcript for its brief text, wasting minutes and
exposing other sessions' content to the unit. Give the unit the brief text
directly; never point it at the transcript that already contains it.

Escape hatch: `-s danger-full-access`, for the case where the set of writable
roots genuinely cannot be enumerated up front. It requires the user's explicit
sign-off for that unit. `--dangerously-bypass-approvals-and-sandbox` produced
identical results in testing and is not a separate capability; prefer
`-s danger-full-access`, whose intent is legible in the command line.

Network on Windows: with egress open, HTTP and TLS both work, with one
exception -- clients using the native schannel TLS stack, notably `curl.exe`,
fail with `SEC_E_NO_CREDENTIALS`, because the sandbox's restricted token cannot
reach the credential store. Any OpenSSL-based client is fine, so tell the unit
to use node, python, or MSYS `curl` for HTTPS. Plain `curl.exe` over HTTP is
also fine. (Measured on codex-cli 0.146.0, Windows: HTTP 200, node HTTPS 200,
curl.exe HTTPS SEC_E_NO_CREDENTIALS.)

## Monitoring

Tail each log ONCE after launch. Use Monitor to stream output if you need live
progress. Do not poll with sleep loops.

## Parallel isolation

`git worktree add -b wt/<unit> ../<repo>-<unit>-wt master` per parallel writer
(the -b is required -- master is already checked out in the main copy), then
point that unit's `-C` at the worktree's ABSOLUTE path. A fresh worktree
contains only TRACKED files, so gitignored paths (a repo venv, a staged tmp/
directory) will not be there: either give that unit the main tree, or tell it in
the brief where those resources live.

## Collecting

Read the `-o` last-message file -- that is the conclusion, and usually the only
thing that belongs in the orchestrating context. Leave the full transcript on
disk; consult it only to verify a specific claim. The gap is large: a routine
unit produced a 1-line result file against a 2248-line transcript.

## Custom providers: driving other OpenAI-compatible servers

Codex is a HARNESS, not a client for one vendor: the same agent loop, tools,
sandbox and `-o` contract can be pointed at another server with `-c` config
pairs. This section covers the custom-provider mechanics and compatibility
requirements.

### The wire API, and the inference to avoid

Codex speaks the **Responses** API only. `wire_api = "chat"` was REMOVED from
the enum (2026-02-05, upstream discussion #7782); passing it is a hard config
error with a fix-it message, not a fallback. The built-in `ollama` and
`lmstudio` providers moved to Responses too.

The trap is the inference, not the fact: reading "wire_api chat was removed"
and concluding "codex cannot drive a chat-completions server" is WRONG. A
server-side conversion layer satisfies codex without the removed client mode --
llama.cpp ships one, and a live run against it produces proper Responses SSE
(`response.created`, `response.output_item.added`,
`response.reasoning_text.delta`) with `function_call` output items carrying
`call_id`. Anyone re-deriving this from issue trackers alone will reach the
wrong answer. Test the server; do not infer from the wire enum.

### The predicate: necessary but not sufficient

Serving `/v1/responses` is NECESSARY BUT NOT SUFFICIENT. The endpoint must also
accept codex's TOOL SCHEMA. Observed on a vendor endpoint that serves
`/v1/responses` correctly (HTTP 200 on a real Responses completion): every
codex request is rejected with 422 Unprocessable Entity, because codex emits a
core tool of `type: "namespace"` and the server's tool enum has no such
variant (`unknown variant 'namespace', expected one of function, web_search,
x_search, image_generation, collections_search, file_search, code_execution,
code_interpreter, mcp, shell`). Auth and provider selection both succeed; the
run exits 1 and writes no `-o` file. No user-side configuration removes that
tool -- disabling MCP servers and feature flags only moves its index.

So the checkable predicate for "codex can drive endpoint X" is a LIVE
`codex exec` against X. Not "OpenAI-compatible", and not "serves
/v1/responses" either. llama.cpp passes the full predicate, including
`function_call` round-trips; the endpoint above does not.

### Keyless is native

`ModelProviderInfo.env_key` is `Option<String>` and `requires_openai_auth`
defaults false, so a provider block with no `env_key` sends no `Authorization`
header at all -- that is how the built-in OSS providers are built
(`create_oss_provider_with_base_url` sets `env_key: None`). Do not fabricate a
dummy key for a keyless server.

### The invocation shape

    codex exec \
      -c 'model_providers.<id>={name="<id>",base_url="<BASE_URL>",wire_api="responses"}' \
      -c 'model_provider="<id>"' \
      -c 'model="<MODEL_ID>"' \
      -c 'model_context_window=<N>' \
      -s workspace-write --skip-git-repo-check \
      -C <ABSOLUTE root> -o <ABSOLUTE result file> -

Single quotes around each `-c` pair, double quotes inside the TOML value --
the same shape as `windows.sandbox="unelevated"` above. A keyed provider adds
`env_key="<VAR>"` inside the provider block; the variable must be set in the
launching environment.

`--oss` and `--local-provider` accept only `lmstudio` and `ollama`, so the
explicit `-c` path is the sanctioned route to any other server.

### Operational notes

  Two stderr lines per run against a llama.cpp server. `failed to refresh
  available models: ... missing field 'slug'` is codex parsing that server's
  `/v1/models` against a richer schema, and `Model metadata for '<model>' not
  found` is codex having no metadata for a model it does not ship. Both occur
  at exit 0 and neither affects the run. The second one still prints with
  `-c model_context_window=<N>` set -- what that flag buys is the real limit
  instead of the guessed fallback the warning announces. Judge the run by the
  exit code and the `-o` file, as always.

  Fixed overhead. About 11,000 input tokens of system prompt and tool
  definitions precede any work, measured on a trivial prompt. That is a real
  budget line on a small local server.

  Windows edits can land with a UTF-8 BOM and CRLF line endings, which fights a
  repo enforcing `text eol=lf`.

  MCP servers are reported broken against llama.cpp (codex#36942, codex#26977)
  and are unverified. Codex's built-in shell, file-edit and apply_patch tools
  work against it.
