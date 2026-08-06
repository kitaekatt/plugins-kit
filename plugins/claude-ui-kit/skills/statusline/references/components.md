# Components Reference

Fields available from the JSON piped to a statusline script on stdin. Load this only when the user asks what they can display, or names a component you need to know how to read.

## Reading the input

```bash
DATA=$(cat)
```

Then extract via `jq`. Parsing every field in a single jq call (with `@tsv`) is fastest — see `scripts/statusline.sh` for the pattern.

## Available fields

| Field | jq path | Notes |
|-------|---------|-------|
| Model display name | `.model.display_name` | e.g. "Claude Opus 4.7" |
| Model id | `.model.id` | e.g. "claude-opus-4-7" |
| Reasoning effort | `.effort.level` | `low`/`medium`/`high`/`xhigh`/`max`; live session value (tracks `/effort`); ultracode reports as `xhigh`. Absent when the model has no effort parameter. Default statusline renders it as a meter glyph `▁▃▅▇█`. |
| Working directory | `.cwd` | Absolute path. Use `\| split("/") \| last` for basename. |
| Session id | `.session_id` | UUID for the current session |
| Context window size | `.context_window.context_window_size` | Total tokens |
| Context remaining % | `.context_window.remaining_percentage` | 0-100 (CAPACITY remaining; use `100 - x` for "used") |
| Input tokens used | `.context_window.current_usage.input_tokens` | |
| Cache creation tokens | `.context_window.current_usage.cache_creation_input_tokens` | |
| Cache read tokens | `.context_window.current_usage.cache_read_input_tokens` | |
| 5-hour rate-limit % used | `.rate_limits.five_hour.used_percentage` | 0-100, may be null. Default statusline shows `100 - x` as remaining capacity. |
| 7-day rate-limit % used | `.rate_limits.seven_day.used_percentage` | 0-100, may be null. Default statusline shows `100 - x` as remaining capacity. |
| 5-hour reset time | `.rate_limits.five_hour.resets_at` | Unix epoch seconds when the 5-hour window resets. May be null. |
| 7-day reset time | `.rate_limits.seven_day.resets_at` | Unix epoch seconds when the 7-day window resets. May be null. |

The default statusline normalizes all three percentages to **capacity remaining** so the user-facing numbers share one mental model: higher = healthier, lower = warning.

Always guard nulls with `// 0` or `// ""`. Use `try ... catch` around computations that depend on multiple fields.

### The rate-limit snapshot

A statusline is the only thing Claude Code ever hands `.rate_limits` to, so tools that
want it have no way to ask. `statusline.sh` therefore writes a snapshot on every render:

```
~/.claude/plugins/data/plugins-kit/claude-ui-kit/rate-limits.json
{"captured_at": <epoch>, "rate_limits": {...}}     # verbatim, plus the capture time
```

Written via a temp file and `mv`, so a reader never sees a partial object; skipped when
the payload carries no `.rate_limits`; disabled with `STATUSLINE_RATE_LIMIT_SNAPSHOT=0`.
A write failure is swallowed -- the snapshot must never cost the user their status line.

This is a **file contract, not a dependency**: consumers (awesome-kit's `orchestrate`
skill reports remaining capacity from it) treat the file as optional and degrade to
"unknown" when it is absent, and neither plugin declares the other. The windows are
account-wide, with no per-model breakdown -- nothing downstream can infer that a
specific model's usage is spent.

## Out-of-scope (would require external commands)

- Git branch / status — `git branch --show-current`, `git status --porcelain`
- Cost — needs a usage-tracker; not in stdin JSON
- Time of day — `date +%H:%M`

These can be added; they cost a fork+exec per render. Keep the script fast; users notice slowness.
