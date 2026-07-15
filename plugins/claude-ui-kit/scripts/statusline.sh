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
printf '%s\n' "$OUT"
