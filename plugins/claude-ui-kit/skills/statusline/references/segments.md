# Segments Reference

How another plugin adds a cell to the claude-ui-kit status line, and what the
default script does with it. Load this when a user asks what a cell on the bar
is, why one is missing or cut off, or how to contribute one.

## Where segments live

A contributor drops ONE entry into the segments directory:
`~/.claude/plugins/data/<marketplace>/claude-ui-kit/segments/` (sibling of
`scripts/` in the plugin's data dir; `STATUSLINE_SEGMENTS_DIR` overrides it).
No change to `statusLine.command` is needed.

claude-ui-kit owns composition: the separator (U+2502, a box-drawing
vertical bar) and the order (lexical by filename, so use `NN-` prefixes such
as `50-build.sh`). Contributors own the cell content. Every segment is appended after the built-in cells.

## Entry kinds

- `*.txt` -- the first line is shown while the file is fresh (mtime within
  `STATUSLINE_SEGMENT_TXT_TTL` seconds, default 300), capped at 60 chars.
- `*.sh` -- run with the statusline stdin JSON on stdin, under a hard
  per-segment timeout (`STATUSLINE_SEGMENT_TIMEOUT`, default 2s, via
  `timeout` or `gtimeout`). Emit your own ANSI, one line, no leading
  separator.

## Normalization of `*.sh` output

Stdout is NOT appended verbatim. The script:

1. Keeps the first line only and strips CR.
2. Caps it at 120 visible characters. ANSI color codes are not counted and
   are never cut in half.
3. Appends a RESET, so an unreset color cannot bleed into later cells.

A segment that ignores the single-line contract degrades itself, not the bar.

## When a segment renders as no cell

Empty output, a non-zero exit, a timeout, or no `timeout(1)` binary on PATH
all render as an absent cell. The last case disables every `*.sh` segment,
and the bar shows one `[segments off: no timeout(1)]` marker instead.

## Contract for `*.sh` entries

- **Pure cache reader.** Read pre-computed local state; never fetch, poll, or
  block on the network. Collect data in your own out-of-band process (a
  daemon, a hook, a cron) and write it somewhere cheap to read.
- Uninstalling a contributor should remove its segment file.
- A segment whose backing plugin is gone should exit 0 silently, which
  renders as no cell.

## Diagnosing a cell

To find which segment drew a cell, list the segments directory and run each
`*.sh` with a small payload, for example
`printf '{"cwd":"%s"}' "$PWD" | bash <segment>.sh | cat -v`. The filename
prefix gives its position on the bar.
