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
          action: Invoke "${CACHE_KIT_ROOT:?requires a bootstrap engine pass; run bootstrap run}/scripts/cache_report.py" with $ARGUMENTS.
          tool: '"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}"'
          expected: stdout containing the cache hit-rate, token usage, and (if --detailed) per-request breakdown.
          on_failure: If the script is missing at the path "${CACHE_KIT_ROOT:?requires a bootstrap engine pass; run bootstrap run}/scripts/cache_report.py" resolves to, surface the error to the user verbatim. Do not improvise the script path.
        - n: 2
          action: Display the script's stdout verbatim in the user's chat. Do not summarize, paraphrase, or omit any lines.
      output_template: |
        Display the script's stdout verbatim. Do not summarize, paraphrase, or omit any lines.
      gotchas:
        - ${CLAUDE_PLUGIN_ROOT} is expanded by Claude Code only in the values the harness reads before executing them, such as a hook's `command:` field (worked example -- plugins/bootstrap/hooks/hooks.json). It is NOT present in the Bash tool's environment, so a command carrying it resolves the variable to the empty string and runs against a bare /scripts/cache_report.py. Anchor a command you run on "${CACHE_KIT_ROOT:?requires a bootstrap engine pass; run bootstrap run}" instead, and if the script is missing at the resulting path, surface the error to the user verbatim rather than improvising a path.
```

## Instructions

Run `scripts/cache_report.py` as the contract above specifies, then display its
output verbatim. Do not summarize, paraphrase, or omit any lines. Show the
complete report exactly as produced by the script.

---

To see all sessions: run `"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CACHE_KIT_ROOT:?requires a bootstrap engine pass; run bootstrap run}/scripts/cache_report.py" --all`

To see a specific session: run `"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CACHE_KIT_ROOT:?requires a bootstrap engine pass; run bootstrap run}/scripts/cache_report.py" SESSION_ID`

To include per-request breakdown: run `"${BOOTSTRAP_PYTHON:?requires bootstrap >= 0.120.0}" "${CACHE_KIT_ROOT:?requires a bootstrap engine pass; run bootstrap run}/scripts/cache_report.py" --detailed`
