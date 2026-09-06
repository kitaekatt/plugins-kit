---
_schema_version: 1
name: openrouter-account
author: christina
skill-type: technique-skill
description: Use when checking, setting, or rotating the OpenRouter API key, or diagnosing 401/402 errors. Do NOT use for general LLM/translation work.
---

# OpenRouter Account

Manage the shared OpenRouter API key that other plugins and project scripts depend on. The key is stored in a user-scoped `.env` file at `~/.claude/plugins/data/plugins-kit/llm-scripting-kit/.env` and consulted by anything that imports `llm_scripting_kit` (today: loc-ops; future: any plugin that calls OpenRouter).

## Technique

The load-bearing contract; the markdown below is reference detail for the CLI and the common scenarios.

```yaml
technique_skill:
  _schema_version: "1"
  identity: Manage the shared OpenRouter API credential other plugins depend on -- verify, set, rotate, and diagnose 401/402 -- via the llm-scripting-kit CLI.
  scope:
    covers:
      - preflighting the credential on behalf of a capability that is about to call OpenRouter
      - verifying whether the OpenRouter key is set and valid
      - setting or rotating the user-scoped key
      - diagnosing HTTP 401 (rejected key) vs 402 (no account balance)
      - resolving which .env file wins the precedence order
    excludes:
      - choosing models, setting temperature, or shaping OpenRouter requests
      - managing Anthropic / OpenAI / other providers' credentials
      - inspecting or modifying the bootstrap engine
  techniques:
    - id: preflight-openrouter-credential
      name: Preflight the credential at the point of need
      keywords: [preflight, deferred requirement, no key yet, about to call openrouter, ask for key, point of need, first API call]
      goal: Get the key in place at the moment a capability actually needs it -- bootstrap deliberately does not ask at session start.
      steps:
        - n: 1
          action: Run `llm-scripting-kit status` BEFORE the work that calls OpenRouter. Exit 0 means proceed; do not ask the user anything.
        - n: 2
          action: "On non-zero, read the recorded ask from `~/.claude/plugins/data/plugins-kit/llm-scripting-kit/deferred_requirements.json` and present the entry's `agent_msg` VERBATIM. It is the authored copy of the ask; do not paraphrase it."
          on_failure: If the file is absent (bootstrap not installed, or never ran), fall back to the two options in the Common scenarios section below -- same content, same order.
        - n: 3
          action: Set the key per the user's choice (they run `! llm-scripting-kit set-key`, or you run `llm-scripting-kit set-key --key <KEY>` if they pasted it), then retry the original action. No restart is needed.
      gotchas:
        - Preflight inside the action that needs the key, never at skill load -- every other action in a consuming skill must keep working without a key.
        - A declined key is not settled for the rest of the session. Re-ask on the next genuine need.
        - Do not tell the user to run `fix-all` for this. Bootstrap records the credential as a DEFERRED REQUIREMENT and raises no fix-all entry for it by design; see the bootstrap skill's deferred-requirements reference.
    - id: manage-openrouter-key
      name: Verify, set, or rotate the OpenRouter credential
      keywords: [openrouter key, api key, set-key, rotate credential, 401 402, status check]
      goal: Bring the shared OpenRouter key to a validated state and diagnose any auth/credit failure a consumer hit.
      steps:
        - n: 1
          action: Run `llm-scripting-kit status` to resolve the key (env var > project .env > user .env) and validate it against GET /auth/key.
          expected: Exit 0 prints account label, usage, limit, and free-tier flag. Non-zero means the key is missing or rejected.
        - n: 2
          action: If the source is ambiguous, run `llm-scripting-kit which` to see which file the resolver reads and rule out a shadowing project .env.
        - n: 3
          action: To set or rotate, run `llm-scripting-kit set-key` (interactive hidden prompt -- the user runs it, prefix with `!`) or `llm-scripting-kit set-key --key sk-or-v1-...` (non-interactive; Claude may run it only when the user already shared the key in chat). The key validates against /auth/key before it is written.
          on_failure: A typo is rejected at validation and never lands on disk; re-run with the corrected key.
        - n: 4
          action: Diagnose the failure class -- HTTP 401 means the key was revoked or rotated server-side (generate a new one at openrouter.ai/keys and re-run set-key); HTTP 402 means the key is valid but the account has no balance (top up at openrouter.ai/credits).
        - n: 5
          action: Re-run `llm-scripting-kit status` to confirm OK.
          expected: status reports OK with the key's label; bootstrap auto-clears last_validated.sha256 on the next successful /auth/key call, so no manual cache reset is needed.
      gotchas:
        - "`set-key` without `--key` requires an interactive hidden prompt Claude cannot supply; the user must run it (prefix with `!`)."
        - Precedence is env var > project .env > user .env. A project `.env` silently shadows the user-scoped file; use `which` to confirm the active source.
        - HTTP 402 is not a key problem -- the key is valid and the account is out of credit. Do not rotate the key in response to 402.
```

## When to invoke

- The user wants to check whether their OpenRouter key is set up and working.
- The user wants to set or rotate the key.
- A capability is about to call OpenRouter and needs the key preflighted (the usual trigger -- bootstrap does not ask at session start).
- A consumer (loc-ops, etc.) failed with HTTP 401 / 402 and the user needs to know whether the key or the account balance is the problem.

Do NOT use for translating strings, choosing models, debugging chunk failures, or any other LLM work that happens *after* the key is verified -- that is the consumer's responsibility.

## The CLI

The plugin ships a single CLI script at `${CLAUDE_PLUGIN_ROOT}/scripts/llm_scripting_kit_cli.py` with three subcommands.

| Command | What it does |
|---------|--------------|
| `status` | Resolves the key from env / project / user .env (in that order), calls `GET /auth/key`, prints account label, usage, limit, free-tier flag, rate limit. Exit 0 = OK; non-zero = missing or rejected. |
| `set-key [--key VALUE] [--no-validate]` | Writes a new key to the user-scoped .env. With no `--key`, prompts via `getpass` (input hidden). Validates the key against `/auth/key` before writing unless `--no-validate` is passed. |
| `which` | Prints the source path of the resolved key (`env`, `project: <path>`, `user: <path>`, or `missing`). Useful when the user is confused about which file is being read. |

### Invocation

The plugin ships shims at `bin/llm-scripting-kit` (Unix) and `bin/llm-scripting-kit.cmd` (Windows). Claude Code adds each plugin's `bin/` directory to PATH, so the short form works from any cwd:

```bash
llm-scripting-kit status
llm-scripting-kit set-key            # interactive (hidden prompt)
llm-scripting-kit set-key --key sk-or-v1-...   # non-interactive; key lands in transcript
llm-scripting-kit which
```

`set-key` without `--key` requires an interactive hidden prompt -- Claude cannot supply that itself, so the user must run it (prefix with `!` to execute in the current prompt). `--key` is the non-interactive path Claude can run on the user's behalf when the user has already shared the key in chat.

The shims pick the interpreter: the plugin venv bootstrap provisions (which has
PyYAML, needed to read the layered `config.yaml`), falling back to the standalone
Python and then to PATH. On a fallback interpreter the CLI still works -- it warns
on stderr and uses the shipped model baseline instead of the config layers, so key
management never depends on the config being readable.

## Common scenarios

**No key is set yet** -- run `set-key` and paste the key from <https://openrouter.ai/keys> at the prompt. The script validates against `/auth/key` before writing, so a typo never silently lands on disk. After it returns, `status` should show `OK` with the key's label.

Bootstrap will NOT have prompted for this at session start -- an OpenRouter key is a deferred requirement, asked for only when something needs it. The two ways to set it, in preference order:

> 1. (preferred -- key stays out of the transcript) The user types this in the prompt with the leading `!`:
>      `! llm-scripting-kit set-key`
>    It prompts with hidden input. Paste from <https://openrouter.ai/keys> (starts with `sk-or-v1-`).
> 2. If they would rather paste the key in chat, Claude runs `llm-scripting-kit set-key --key <THE_KEY>`. WARNING: the key is then visible in the transcript.

**Key was rejected (HTTP 401)** -- the key was revoked or rotated on the OpenRouter side. Generate a new one at <https://openrouter.ai/keys> and re-run `set-key`. Old key value is overwritten.

**Account out of credit (HTTP 402)** -- the key is valid but the account has no balance. Top up at <https://openrouter.ai/credits>. The next bootstrap session-start automatically clears the cached `last_validated.sha256` once a successful `/auth/key` call happens, so no manual cache reset is needed.

**Key loaded from the wrong place** -- run `which` to see which file Wins the precedence resolution (env var > project `.env` > user `.env` > the endpoint's configured `key_file`, source `key_file`). If the user wants the user-scoped file to win but a project file is shadowing it, delete `<project>/.local-data/plugins-kit/llm-scripting-kit/.env` (and `<project>/.local-data/llm-scripting-kit/.env`, the superseded location, if it exists -- `which` names whichever one actually won).

**Bootstrap plugin not installed** -- llm-scripting-kit declares a dependency on `plugins-kit:bootstrap`. If bootstrap isn't installed/enabled, the session-start credential check never runs, so `deferred_requirements.json` is absent and the preflight has no recorded statement to present. Nothing breaks: the CLI still works (it self-heals to system Python), so run `llm-scripting-kit status` and fall back to the two options above. Installing/enabling bootstrap restores the recorded diagnosis on the next session.

## What lives where

| Path | Purpose |
|------|---------|
| `~/.claude/plugins/data/plugins-kit/llm-scripting-kit/.env` | Canonical user-scoped credential file. 0600 perms on Unix. |
| `<project>/.local-data/plugins-kit/llm-scripting-kit/.env` | Canonical per-project override. Wins over the user file when present. Same `<marketplace>/<plugin>` namespace as the project `config.yaml`. |
| `<project>/.local-data/llm-scripting-kit/.env` | Superseded per-project location (no marketplace segment). Still read, below the canonical project path, so existing files keep working; a key resolved from it prints a one-time "move the file" notice. |
| `OPENROUTER_API_KEY` env var | Highest priority. Useful for CI / one-shot overrides. |
| the endpoint's configured `key_file` | Lowest priority. A bare-value credential file (its whole, stripped content is the key), consulted only when every layer above misses. The escape hatch for a credential already materialized as a file (e.g. by secrets-kit) so it need not also be copied into a `.env`. Reported as source `key_file` by `which`. |
| `~/.claude/plugins/data/plugins-kit/llm-scripting-kit/deferred_requirements.json` | Bootstrap's recorded diagnosis when the key is missing/rejected, including the verbatim ask the preflight presents. Written each session-start pass and removed once the key validates. |
| `~/.claude/plugins/data/plugins-kit/llm-scripting-kit/last_validated.sha256` | Cache marker. Contains the SHA-256 of the last key that successfully validated, so subsequent sessions skip the network call when nothing changed. Safe to delete -- the next bootstrap re-validates. |

## What this skill does NOT do

- Choose models, set temperature, or shape requests. That is the consumer's job (loc-ops's `translate_*` functions, etc.).
- Manage Anthropic, OpenAI, or any other provider's credentials. Those would live in their own `<provider>-kit` plugin.
- Inspect or modify the bootstrap engine itself -- see `/bootstrap` for that.
