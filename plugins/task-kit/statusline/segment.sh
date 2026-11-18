#!/usr/bin/env bash
# task-kit statusline cell for claude-ui-kit: "<task folder> [<status>]" for the task this session works.
# Pure cache reader: stdin is the statusline JSON; the pointer is written by `task.py work`.
# Silent exit 0 (no cell) when jq, the pointer, or the task file is missing.
command -v jq >/dev/null 2>&1 || exit 0
sid=$(jq -r '.session_id // empty' 2>/dev/null) || exit 0
case "$sid" in ''|*/*|*..*) exit 0 ;; esac
root="${CLAUDE_BOOTSTRAP_DATA_ROOT:-$HOME/.claude/plugins/data}"
ptr="$root/plugins-kit/task-kit/statusline/$sid"
[ -f "$ptr" ] || exit 0
IFS= read -r dir < "$ptr" || [ -n "$dir" ]
[ -n "$dir" ] && [ -f "$dir/task.yaml" ] || exit 0
status=$(sed -n 's/^[[:space:]]*status:[[:space:]]*//p' "$dir/task.yaml" | head -n 1 | sed "s/^['\"]//; s/['\"][[:space:]]*\$//")
[ -n "$status" ] || exit 0
printf '%s [%s]\n' "${dir##*/}" "$status"
exit 0
