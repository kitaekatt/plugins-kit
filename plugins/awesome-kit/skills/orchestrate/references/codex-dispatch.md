# Codex dispatch mechanics

The exact flags, command shape, and traps for launching a unit on the Codex
CLI backend. Load this when you have already CHOSEN Codex and are composing
the launch -- the rendered policy carries the summary; this carries the detail.

Rendered policy: `scripts/orchestration_guidance.py`. Backend record and the
one-line command: `defaults/orchestration.yaml`, `backends[id: codex]`.

## Mechanics

Use `codex exec` -- the NON-INTERACTIVE subcommand. Bare
`codex "<prompt>"` launches the interactive TUI, needs a terminal, and
dies instantly under run_in_background with
`Error: stdin is not a terminal`.

Pass the prompt on STDIN, not as an argument. Real briefs are long and
full of apostrophes, quotes and backticks; embedding one in a shell
argument reliably produces `unexpected EOF while looking for matching
quote` and the dispatch never happens. Write the brief to a file, then
feed it in -- `-` means "read the prompt from stdin":

  codex exec -s workspace-write \
    -c sandbox_workspace_write.network_access=true \
    --skip-git-repo-check --color never \
    -o tmp/codex-<unit>-result.txt - < tmp/prompt-<unit>.md \
    > tmp/codex-<unit>.log 2>&1

Launch each unit as its own Bash tool call with run_in_background: true.

Flags that matter:
  -m, --model <MODEL>
        The rung. Model ids are FULLY QUALIFIED -- `gpt-5.6-sol`, not
        `sol`. The bare codenames are not dispatchable and fail at
        launch.
  -c model_reasoning_effort=<low|medium|high|max>
        The effort dial, and it is a `-c` CONFIG KEY, not a flag --
        `codex exec --help` does not list it, so looking there and
        finding nothing is the expected outcome, not a sign it does not
        exist. Verify a spelling with `--strict-config`, which rejects
        an unknown key at launch (`unknown configuration field ... in
        -c/--config override`) instead of silently ignoring it.
  -s, --sandbox <read-only|workspace-write|danger-full-access>
        The ONLY safety control in exec mode. `--ask-for-approval` does
        NOT exist here: exec is non-interactive, so there is nobody to
        answer a prompt. Default to `workspace-write` for units that edit
        files, `read-only` for pure research. NEVER pass
        `--dangerously-bypass-approvals-and-sandbox` -- with no approval
        prompts to skip, all it buys is removing the sandbox.
  -c sandbox_workspace_write.network_access=true
        Opens network egress while keeping writes confined to the
        workspace. Shipped in the command above; see the network note.
  --add-dir <DIR>
        An extra writable root, for the occasional unit whose work
        legitimately spans two trees. Prefer this over widening -s.
  -o, --output-last-message <FILE>
        Writes ONLY the agent's final message to FILE. This is the return
        value. Without it you are grepping a transcript that routinely
        runs thousands of lines.
  -C, --cd <DIR>
        The working root. Use this rather than `cd <dir> && codex ...`: a
        directory change inside a backgrounded Bash call does not persist.
  --skip-git-repo-check   Allows running outside a git repo.
  --color never           Keeps ANSI escapes out of the captured log.
  --output-schema <FILE>  JSON Schema for the final response, when you
                          want the return value machine-parseable.
  --json                  Emit events as JSONL, for progress tracking.

Network: bare `-s workspace-write` blocks egress at a loopback proxy, so
the command above adds `-c sandbox_workspace_write.network_access=true`.
With it, HTTP and TLS both work. On WINDOWS one thing does not: clients
using the native schannel TLS stack -- notably `curl.exe` -- fail with
`SEC_E_NO_CREDENTIALS`, because the sandbox's restricted token cannot
reach the credential store. Any OpenSSL-based client is fine, so tell the
unit to use node, python, or MSYS `curl` for HTTPS. Plain `curl.exe` over
HTTP is also fine. (Measured on codex-cli 0.146.0, Windows: HTTP 200,
node HTTPS 200, curl.exe HTTPS SEC_E_NO_CREDENTIALS.)

What the sandbox is still for: exec mode has NO approval channel, so
unlike a background Claude agent -- which can hit the permission layer
and be denied mid-run -- a Codex unit cannot be interrupted. Workspace
confinement is the only bound that exists on it, which is why the default
keeps it even though network is open. Widen with `--add-dir` for a second
tree; reach for `danger-full-access` only with the user's explicit
sign-off for that unit.

Monitoring: the Bash tool's own run_in_background notification tells you
the process EXITED, not that the work succeeded -- a session that died on
a bad flag or a quoting error notifies exactly like one that finished. So
DO tail each log once after launch. Use Monitor to stream output if you
need live progress. Do not poll with sleep loops.

Parallel isolation: `git worktree add -b wt/<unit> ../<repo>-<unit>-wt master`
per parallel writer (the -b is required -- master is already checked out
in the main copy). A fresh worktree contains only TRACKED files, so
gitignored paths (a repo venv, a staged tmp/ directory) will not be
there: either give that unit the main tree, or tell it in the brief where
those resources live.

Collecting: read the `-o` last-message file -- that is the conclusion,
and usually the only thing that belongs in the orchestrating context.
Leave the full transcript on disk; consult it only to verify a specific
claim. The gap is large: a routine unit produced a 1-line result file
against a 2248-line transcript. Then verify: a Codex session's report
describes what it intended, not necessarily what it did -- check the
actual diff before reporting the work as done.
