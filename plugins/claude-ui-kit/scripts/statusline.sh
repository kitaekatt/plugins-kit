#!/usr/bin/env bash
set -euo pipefail

DATA=$(cat)

# Prefer the absolute path bootstrap recorded; fall back to PATH lookup.
# See docs/planning/bootstrap/tool-resolution-redesign.md.
JQ="${BOOTSTRAP_BIN_JQ:-jq}"

# Fallback: with set -euo pipefail, malformed stdin (or a missing jq) used to
# kill the script and render a blank statusline indistinguishable from "not
# installed". Emit a minimal line instead.
if ! printf '%s' "$DATA" | "$JQ" -e . >/dev/null 2>&1; then
    printf '\xf0\x9f\x93\x81 %s\n' "$(basename "${PWD:-?}")"
    exit 0
fi

# Extract fields via single jq call. Delimit with \x1f (unit separator), not
# @tsv: tab is IFS whitespace, so bash `read` collapses consecutive tabs and
# empty fields (e.g. an absent rate-limit window) shift later values left.
# Non-whitespace IFS chars delimit one field each, preserving empties.
#
# .cwd is normalized to forward slashes before splitting: on Windows it arrives
# as D:\dev\env-config, which split("/") leaves whole, rendering the full path
# instead of the basename.
IFS=$'\x1f' read -r MODEL MODEL_ID DIR PCT SESS WEEK SESS_RESET WEEK_RESET EFFORT < <(
    echo "$DATA" | "$JQ" -r '[
        (.model.display_name // "Claude"),
        (try (.model.id // "unknown") catch "unknown"),
        (.cwd // "~" | gsub("\\\\"; "/") | split("/") | map(select(. != "")) | last // "~"),
        (try (
    if (.context_window.remaining_percentage // null) != null then
      .context_window.remaining_percentage | floor
    elif (.context_window.context_window_size // 0) > 0 then
      100 - ((((.context_window.current_usage.input_tokens // 0) +
        (.context_window.current_usage.cache_creation_input_tokens // 0) +
        (.context_window.current_usage.cache_read_input_tokens // 0)) * 100 /
       .context_window.context_window_size) | floor)
    else 100 end
  ) catch 100),
        ((.rate_limits.five_hour.used_percentage // null) | if . == null then "" else ((100 - .) | floor | tostring) end),
        ((.rate_limits.seven_day.used_percentage // null) | if . == null then "" else ((100 - .) | floor | tostring) end),
        ((.rate_limits.five_hour.resets_at // null) | if . == null then "" else (. | floor | tostring) end),
        ((.rate_limits.seven_day.resets_at // null) | if . == null then "" else (. | floor | tostring) end),
        (.effort.level // "")
    ] | map(tostring) | join("\u001f")' | tr -d '\r'
)

# Rate-limit snapshot. The statusline hook payload is the ONLY place Claude Code
# surfaces .rate_limits, and nothing but a statusline ever receives it -- so drop
# a copy where other tools can read it. Consumed by awesome-kit's orchestrate
# skill to report remaining capacity. This is a FILE contract, not an import
# edge: consumers treat the file as optional and neither plugin depends on the
# other. Disable with STATUSLINE_RATE_LIMIT_SNAPSHOT=0.
#
# Under `set -euo pipefail` a snapshot failure must never take the statusline
# with it, hence the trailing `|| true`; the write goes via a temp file so a
# concurrent reader never sees a partial object.
# `${HOME:-}` deliberately: under `set -u` a bare $HOME with HOME unset aborts
# the whole script, turning a skipped snapshot into a blank status line -- the
# exact failure this block promises never to cause.
if [ "${STATUSLINE_RATE_LIMIT_SNAPSHOT:-1}" = "1" ] && [ -n "${HOME:-}" ] &&
   printf '%s' "$DATA" | "$JQ" -e '.rate_limits != null' >/dev/null 2>&1; then
    SNAP_DIR="${HOME}/.claude/plugins/data/plugins-kit/claude-ui-kit"
    SNAP_TMP="$SNAP_DIR/rate-limits.json.$$.tmp"
    {
        mkdir -p "$SNAP_DIR" &&
        printf '%s' "$DATA" | "$JQ" -c --argjson now "$(date +%s)" \
            '{captured_at: $now, rate_limits: .rate_limits}' > "$SNAP_TMP" &&
        mv -f "$SNAP_TMP" "$SNAP_DIR/rate-limits.json"
    } 2>/dev/null || rm -f "$SNAP_TMP" 2>/dev/null || true
fi

# Model segment: display name with version tokens stripped ("Fable 5" -> "Fable",
# "Opus 4.8" -> "Opus"), prefixed with an effort meter glyph when the session
# reports a reasoning effort (absent for models without the effort parameter).
# On by default; hide with STATUSLINE_SHOW_MODEL=0 in settings.json env.
MODEL=$(printf '%s' "$MODEL" | sed -E 's/ [0-9][0-9.]*//g')
case "$EFFORT" in
    low)    EFFORT_GLYPH="▁" ;;
    medium) EFFORT_GLYPH="▃" ;;
    high)   EFFORT_GLYPH="▅" ;;
    xhigh)  EFFORT_GLYPH="▇" ;;
    max)    EFFORT_GLYPH="█" ;;
    *)      EFFORT_GLYPH="" ;;
esac

# System message: most recently modified file in <cwd>/.local-data/claude-ui-kit/
# matching systemmessage.*.txt. First line, capped at 20 chars.
CWD=$(echo "$DATA" | "$JQ" -r '.cwd // ""')
SYSMSG=""
if [ -n "$CWD" ] && [ -d "$CWD/.local-data/claude-ui-kit" ]; then
    LATEST=""
    LATEST_MTIME=0
    for f in "$CWD"/.local-data/claude-ui-kit/systemmessage.*.txt; do
        [ -e "$f" ] || continue
        MT=$(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f" 2>/dev/null || echo 0)
        if [ "$MT" -gt "$LATEST_MTIME" ]; then
            LATEST_MTIME=$MT
            LATEST=$f
        fi
    done
    if [ -n "$LATEST" ]; then
        SYSMSG=$(awk 'NR==1 {print substr($0,1,20); exit}' "$LATEST" 2>/dev/null | tr -d '\r')
    fi
fi

# Threshold colors (256-color: 196 = red, 208 = orange, 250 = default gray).
# All percentages now represent CAPACITY REMAINING (higher = better), so colors
# trigger when the value drops AT OR BELOW the threshold.
# Override defaults via env vars in settings.json:
#   STATUSLINE_CTX_ORANGE_AT, STATUSLINE_CTX_RED_AT,
#   STATUSLINE_SESS_ORANGE_AT, STATUSLINE_SESS_RED_AT,
#   STATUSLINE_WEEK_ORANGE_AT, STATUSLINE_WEEK_RED_AT
CTX_ORANGE_AT="${STATUSLINE_CTX_ORANGE_AT:-70}"
CTX_RED_AT="${STATUSLINE_CTX_RED_AT:-30}"
SESS_ORANGE_AT="${STATUSLINE_SESS_ORANGE_AT:-30}"
SESS_RED_AT="${STATUSLINE_SESS_RED_AT:-10}"
WEEK_ORANGE_AT="${STATUSLINE_WEEK_ORANGE_AT:-30}"
WEEK_RED_AT="${STATUSLINE_WEEK_RED_AT:-10}"

# Colors are real ESC bytes ($'...'), not "\033" strings: the line is emitted
# with printf '%s' rather than `echo -e`, because echo -e also interprets
# backslashes in the DATA -- a Windows cwd like D:\dev\env-config contains \e,
# which became a literal ESC and ate the following character (rendering
# "D:\devv-config"). Interpreting escapes at assignment keeps that
# interpretation off the interpolated values.
RED=$'\033[38;5;196m'
ORANGE=$'\033[38;5;208m'
GRAY=$'\033[38;5;250m'
RESET=$'\033[0m'
SEP=$'\033[2m\033[38;5;238m │ \033[0m'

if   [ "$PCT" -le "$CTX_RED_AT" ];    then CTX_CLR="$RED"
elif [ "$PCT" -le "$CTX_ORANGE_AT" ]; then CTX_CLR="$ORANGE"
else                                       CTX_CLR="$GRAY"
fi

SESS_CLR="$GRAY"
if [ -n "$SESS" ] && [ "$SESS" -eq "$SESS" ] 2>/dev/null; then
    if   [ "$SESS" -le "$SESS_RED_AT" ];    then SESS_CLR="$RED"
    elif [ "$SESS" -le "$SESS_ORANGE_AT" ]; then SESS_CLR="$ORANGE"
    fi
fi

WEEK_CLR="$GRAY"
if [ -n "$WEEK" ] && [ "$WEEK" -eq "$WEEK" ] 2>/dev/null; then
    if   [ "$WEEK" -le "$WEEK_RED_AT" ];    then WEEK_CLR="$RED"
    elif [ "$WEEK" -le "$WEEK_ORANGE_AT" ]; then WEEK_CLR="$ORANGE"
    fi
fi

# Format seconds-until-reset as: #d (>=1 day), Xh rounded to nearest hour
# (>=1 hour), or XXm (under 1 hour). Returns empty for missing timestamps.
fmt_reset() {
    local epoch="$1" now secs days hours mins
    [ -z "$epoch" ] && return
    now=$(date +%s)
    secs=$((epoch - now))
    [ "$secs" -le 0 ] && { printf "0m"; return; }
    if [ "$secs" -ge 86400 ]; then
        days=$((secs / 86400))
        printf "%dd" "$days"
    elif [ "$secs" -ge 3600 ]; then
        hours=$(((secs + 1800) / 3600))
        if [ "$hours" -ge 24 ]; then printf "1d"
        else                         printf "%dh" "$hours"
        fi
    else
        mins=$(((secs + 30) / 60))
        printf "%dm" "$mins"
    fi
}

SESS_RESET_STR=$(fmt_reset "$SESS_RESET")
WEEK_RESET_STR=$(fmt_reset "$WEEK_RESET")

OUT="${GRAY}📁 $DIR${RESET}"
if [ "${STATUSLINE_SHOW_MODEL:-1}" != "0" ] && [ -n "$MODEL" ]; then
    MODEL_SEG="$MODEL"
    [ -n "$EFFORT_GLYPH" ] && MODEL_SEG="$EFFORT_GLYPH $MODEL"
    OUT="$OUT$SEP${GRAY}$MODEL_SEG${RESET}"
fi
OUT="$OUT$SEP${CTX_CLR}🧠 $PCT%${RESET}"
if [ -n "$SESS" ]; then
    OUT="$OUT$SEP${SESS_CLR}🔋 $SESS%"
    [ -n "$SESS_RESET_STR" ] && OUT="$OUT ($SESS_RESET_STR)"
    OUT="$OUT${RESET}"
fi
if [ -n "$WEEK" ]; then
    OUT="$OUT$SEP${WEEK_CLR}📅 $WEEK%"
    [ -n "$WEEK_RESET_STR" ] && OUT="$OUT ($WEEK_RESET_STR)"
    OUT="$OUT${RESET}"
fi
if [ -n "$SYSMSG" ]; then
    OUT="$OUT$SEP${GRAY}💬 $SYSMSG${RESET}"
fi

# ---- Segment API: contributed cells --------------------------------------
# Other plugins add a cell by dropping ONE entry into the segments dir
# (sibling of scripts/ in this plugin's data dir; override with
# STATUSLINE_SEGMENTS_DIR). ui-kit owns COMPOSITION -- the separator and the
# ordering (lexical by filename; use NN- prefixes) -- contributors own
# CONTENT. Two entry kinds:
#   *.txt  first line shown while fresh (mtime within
#          STATUSLINE_SEGMENT_TXT_TTL seconds, default 300), capped 60 chars.
#   *.sh   run with the statusline stdin JSON on stdin, under a HARD
#          per-segment timeout (STATUSLINE_SEGMENT_TIMEOUT, default 2s,
#          via `timeout` or `gtimeout`). Emit your own ANSI; no leading
#          separator. Output is normalized like *.txt -- first line only,
#          capped 120 chars, CR stripped -- so a segment that ignores the
#          single-line contract degrades itself rather than the bar, and a
#          RESET is appended so unreset color cannot bleed onward. Empty
#          output, non-zero exit, timeout, or NO timeout binary on PATH all
#          render as an ABSENT cell -- a broken segment can lose only
#          itself, never blank the bar.
# Contract for *.sh entries: pure cache reader. Read pre-computed local
# state; never fetch, poll, or block on the network -- collect data in your
# own out-of-band process and write it somewhere cheap to read.
SEGMENTS_DIR="${STATUSLINE_SEGMENTS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)/segments}"
if [ -d "$SEGMENTS_DIR" ]; then
    TXT_TTL="${STATUSLINE_SEGMENT_TXT_TTL:-300}"
    SEG_TIMEOUT="${STATUSLINE_SEGMENT_TIMEOUT:-2}"
    NOW=$(date +%s)
    # Resolved ONCE rather than per segment. `command -v` in the loop was a
    # subprocess per *.sh segment per render, and the answer cannot change
    # mid-loop.
    # Same precedent as JQ above: prefer the absolute path bootstrap recorded,
    # fall back to a PATH lookup. Set-but-EMPTY is meaningful and distinct from
    # unset -- it declares "this machine has no timeout binary", which is what
    # bootstrap records on a stock macOS box and what the test below asserts
    # against. `${VAR+set}` is the POSIX set-vs-empty test and is safe under
    # bash 3.2, zsh, and `set -u`.
    SEG_TIMEOUT_BIN=""
    if [ "${BOOTSTRAP_BIN_TIMEOUT+set}" = set ]; then
        SEG_TIMEOUT_BIN="$BOOTSTRAP_BIN_TIMEOUT"
    elif command -v timeout >/dev/null 2>&1; then
        SEG_TIMEOUT_BIN="timeout"
    elif command -v gtimeout >/dev/null 2>&1; then
        SEG_TIMEOUT_BIN="gtimeout"
    fi
    SEG_SKIPPED=""

    for seg in "$SEGMENTS_DIR"/*; do
        [ -e "$seg" ] || continue
        SEGOUT=""
        case "$seg" in
            *.txt)
                MT=$(stat -c %Y "$seg" 2>/dev/null || stat -f %m "$seg" 2>/dev/null || echo 0)
                if [ $((NOW - MT)) -le "$TXT_TTL" ]; then
                    SEGOUT=$(awk 'NR==1 {print substr($0,1,60); exit}' "$seg" 2>/dev/null | tr -d '\r')
                fi
                ;;
            *.sh)
                # The timeout is the contract, not a nicety: without it one slow
                # segment stalls every prompt render. macOS ships no `timeout`
                # (coreutils installs it as `gtimeout`), so probing only for
                # `timeout` and falling through to an unbounded run would quietly
                # void the guarantee on exactly the platform that needs it. If
                # neither exists, skip *.sh segments rather than risk the bar.
                #
                # Skipping is right; skipping SILENTLY was not. A user on stock
                # macOS saw their segments simply absent from the bar, with
                # nothing saying why -- the failure looked identical to a
                # segment that had nothing to report. One marker is appended
                # below instead, once per render rather than once per segment.
                if [ -n "$SEG_TIMEOUT_BIN" ]; then
                    SEGOUT=$(printf '%s' "$DATA" | "$SEG_TIMEOUT_BIN" "$SEG_TIMEOUT" bash "$seg" 2>/dev/null || true)
                else
                    SEGOUT=""
                    SEG_SKIPPED=1
                fi
                ;;
        esac
        # Normalize contributed output the same way *.txt is: first line only,
        # length-capped, CR stripped. ui-kit owns composition, so a segment that
        # ignores the single-line contract degrades itself, never the bar.
        # Trailing RESET so an unreset color cannot bleed into what follows.
        if [ -n "$SEGOUT" ]; then
            SEGOUT=$(printf '%s' "$SEGOUT" | awk 'NR==1 {print substr($0,1,120); exit}' | tr -d '\r')
        fi
        [ -n "$SEGOUT" ] && OUT="$OUT$SEP$SEGOUT${RESET}"
    done

    # Say so, once, rather than letting the segments quietly not be there.
    # Absent output and "this machine cannot run segments" are different facts
    # and must not render identically.
    if [ -n "$SEG_SKIPPED" ]; then
        OUT="$OUT$SEP[segments off: no timeout(1)]${RESET}"
    fi
fi

printf '%s\n' "$OUT"
