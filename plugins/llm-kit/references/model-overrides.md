# Model usage overrides

The user-scoped model usage policy lives at `~/.config/llm-kit/model-overrides.yaml`.
When `XDG_CONFIG_HOME` is set to an absolute path, llm-kit uses
`$XDG_CONFIG_HOME/llm-kit/model-overrides.yaml` instead. Set
`LLM_KIT_MODEL_OVERRIDES_FILE` to select an explicit file.

The file uses schema version 1. Harness settings apply to every entry on that
harness; an entry's model setting replaces the harness setting field by field.
`disabled` takes precedence over `reset_mode` on the same entry.

```yaml
schema_version: 1
harnesses:
  codex:
    reset_mode: true
models:
  luna:
    reset_mode: false
  opus:
    disabled: true
```

- `disabled: true` excludes an entry from routing and consult seats. llm-kit
  reports it as disabled and never selects it.
- `reset_mode: true` bypasses measured quota verdicts and moves a usable entry
  to the front of its declaration. llm-kit keeps and reports the actual usage
  reading. An observed dispatch halt still blocks the entry.

Use `llm-kit models overrides` to inspect the loaded file, its configured
switches, and effective per-entry settings. `llm-kit models describe` and
`llm-kit models seats` apply the policy on each call. Malformed files fail with
an error naming the file and invalid field.
