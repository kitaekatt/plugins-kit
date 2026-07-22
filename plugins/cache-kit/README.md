# cache-kit

See exactly how much of every Claude Code request came from cache.

## What it does

Claude Code already writes a full transcript of every session to
`~/.claude/projects/*/<session>.jsonl`, including the API usage block for
each request. cache-kit parses those files and reports:

- token totals per session (direct input, cache writes, cache reads,
  output)
- the cache write vs read split, including 1h vs 5m TTL buckets when
  present
- overall cache hit rate, and per-request hit rates with `--detailed`
- a per-session rollup across the whole project with `--all`

It is read-only, local-only, and stdlib-only. Nothing leaves your
machine; no API calls are made.

## Sample output

```
## Cache Usage Report

Session:   0f3a...-....
Period:    2026-07-21 14:02:11 -> 2026-07-21 15:40:03
Requests:  42

### Token Summary
Metric                                       Tokens
---------------------------------------------------
Total input (all sources)                 5,812,344
  Direct input tokens                        14,210
  Cache write tokens                        391,102
    1h TTL                                  322,880
    5m TTL                                   68,222
  Cache read tokens (hits)                5,407,032
Output tokens                                88,451

### Cache Performance
Hit rate:         93.0%
```

`--detailed` appends a per-request table (model, input, write, read,
output, hit percent per row).

## How it relates to ccusage

ccusage does cost dashboards and daily/monthly rollups. cache-kit is the
cache-forensics cut ccusage does not focus on: hit rate per request, TTL
buckets, write-vs-read split. No dollar figures anywhere -- if you want
spend tracking, that is ccusage's job.

## Install

```
/plugin marketplace add kitaekatt/plugins-kit
/plugin install cache-kit@plugins-kit
```

## Try this first

```
/cache-report
```

Reports on the most recent session for the current project. Add
`--detailed` for the per-request breakdown, `--all` for every session,
or a session ID for a specific one.

## When not to use it

If you want spend-in-dollars tracking, use ccusage.
