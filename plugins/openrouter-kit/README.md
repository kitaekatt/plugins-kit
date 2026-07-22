# openrouter-kit

One validated OpenRouter key, shared by every plugin that needs it.

Plugins in this marketplace that call OpenRouter (loc-ops, workflow-kit, ...)
all read the same credential and the same model registry from this plugin,
so the key is set up once and consumed everywhere.

## What it does

- **Key setup that validates before writing.** `openrouter-kit set-key`
  checks the key against the OpenRouter API (`GET /auth/key`) before anything
  lands on disk, so a typo is rejected instead of stored. It also
  distinguishes the two failure classes consumers hit: HTTP 401 (key revoked
  or rotated -- get a new one) vs HTTP 402 (key is fine, account is out of
  credit -- do not rotate).
- **Resolution with source attribution.** The key resolves in order:
  `OPENROUTER_API_KEY` env var > project `.env`
  (`<project>/.local-data/openrouter-kit/.env`) > user `.env`
  (`~/.claude/plugins/data/plugins-kit/openrouter-kit/.env`).
  `openrouter-kit which` tells you which source won, so a shadowing project
  file is diagnosable instead of mysterious.
- **Shared model registry.** A layered `config.yaml` maps aliases (plus
  `default` / `defaultCheap` selectors) to concrete OpenRouter slugs. Any
  consuming plugin resolves models through it, so one project override
  changes the model for all of them at once.

## Key handling

Interactive `set-key` uses a hidden prompt (`getpass`), the `.env` file is
created with mode 0600 on Unix (at creation time, not post-hoc chmod), and
writes are atomic (temp file + rename).

## Install and first move

```
/plugin marketplace add kitaekatt/plugins-kit
/plugin install openrouter-kit
```

Then run `openrouter-kit status` (the plugin's `bin/` shim is on PATH). If no
key is set, run `openrouter-kit set-key` yourself -- the hidden prompt is
interactive, so an agent cannot drive it. The `openrouter-account` skill
covers verify / rotate / diagnose flows.

## When not to use

If you just export `OPENROUTER_API_KEY` yourself and have a single consumer,
you do not need this plugin. It earns its keep when several plugins share one
credential and you want validation, source attribution, and a common model
registry.
