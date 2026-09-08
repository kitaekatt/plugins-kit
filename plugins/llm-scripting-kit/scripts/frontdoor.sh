#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DATA_DIR="${HOME}/.claude/plugins/data/plugins-kit/llm-scripting-kit"

if [[ "${1:-}" == "--print-command" ]]; then
    shift
    PRINT_ONLY=1
else
    PRINT_ONLY=0
fi

OS="$(uname -s 2>/dev/null || printf '%s' Unknown)"
if [[ "$OS" == MINGW* || "$OS" == MSYS* || "$OS" == CYGWIN* ]]; then
    DEFAULT_PY="$DATA_DIR/.venv/Scripts/python.exe"
else
    DEFAULT_PY="$DATA_DIR/.venv/bin/python"
fi
PYTHON="${LLM_SCRIPTING_KIT_PYTHON:-$DEFAULT_PY}"
if [[ ! -x "$PYTHON" ]]; then
    printf 'frontdoor: Python interpreter not found: %s\n' "$PYTHON" >&2
    exit 1
fi

if [[ "$PRINT_ONLY" == 1 ]]; then
    printf '%q ' "$PYTHON" -m llm_scripting_kit.frontdoor "$@"
    printf '\n'
    exit 0
fi
export PYTHONPATH="$PLUGIN_ROOT/lib${PYTHONPATH:+:$PYTHONPATH}"
exec "$PYTHON" -m llm_scripting_kit.frontdoor "$@"
