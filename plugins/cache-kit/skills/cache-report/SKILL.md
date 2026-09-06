---
name: cache-report
author: christina
skill-type: technique-skill
description: Use when the user asks about cache hit rate, token usage, or prompt-caching stats. Do NOT use for runtime cache configuration.
disable-model-invocation: true
---

# Cache Report

Display the prompt-cache hit-rate and token-usage report for the current
or a specified session. The slash-command body is the technique; the
contract data below routes user phrasing to it.

```yaml
technique_skill:
  _schema_version: "1"
  trigger_model: user-only
  identity: Display the prompt-cache hit-rate report for the current or a specified session.
  scope:
    covers:
      - cache hit rate questions
      - token usage questions
      - prompt-caching stats requests
      - per-request token breakdown requests
    excludes:
      - runtime cache configuration changes
      - cache invalidation policy decisions
  techniques:
    - id: show_cache_report
      name: Show cache report
      keywords: [cache hit rate, cache report, token usage, prompt caching stats, session cache, cache breakdown, cache tokens, /cache-report]
      goal: Render the cache_report.py output verbatim, in the user's chat.
      arguments:
        - name: SESSION_ID
          required: false
          description: Specific session to report on; omit for current.
        - name: "--all"
          required: false
          description: Report across all sessions (one row per session, subagent transcripts folded in).
        - name: "--detailed"
          required: false
          description: Include per-request breakdown; cannot be combined with --all (the script exits with a parser error).
      steps:
        - n: 1
          action: Invoke ${CLAUDE_PLUGIN_ROOT}/scripts/cache_report.py with $ARGUMENTS.
          tool: uv run --no-project python
          expected: stdout containing the cache hit-rate, token usage, and (if --detailed) per-request breakdown.
          on_failure: If the script is missing at ${CLAUDE_PLUGIN_ROOT}/scripts/cache_report.py, surface the error to the user verbatim. Do not improvise the script path.
        - n: 2
          action: Display the script's stdout verbatim in the user's chat. Do not summarize, paraphrase, or omit any lines.
      output_template: |
        Display the script's stdout verbatim. Do not summarize, paraphrase, or omit any lines.
      gotchas:
        - The slash command resolves the script via ${CLAUDE_PLUGIN_ROOT}, which Claude Code expands to the plugin's install path at runtime. If the script is missing there, the invocation fails -- surface the error to the user, do not improvise the path.
```

## Instructions

Display the report output below verbatim. Do not summarize, paraphrase, or omit any lines. Show the complete report exactly as produced by the script.

## Cache Usage Report

!`uv run --no-project python "${CLAUDE_PLUGIN_ROOT}/scripts/cache_report.py" --session "${CLAUDE_SESSION_ID}" $ARGUMENTS`

---

To see all sessions: run `uv run --no-project python "${CLAUDE_PLUGIN_ROOT}/scripts/cache_report.py" --all`

To see a specific session: run `uv run --no-project python "${CLAUDE_PLUGIN_ROOT}/scripts/cache_report.py" SESSION_ID`

To include per-request breakdown: run `uv run --no-project python "${CLAUDE_PLUGIN_ROOT}/scripts/cache_report.py" --detailed`
