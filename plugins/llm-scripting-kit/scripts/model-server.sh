#!/usr/bin/env bash
# Canonical local-model server launcher used by Claude and shell entry points.

set -euo pipefail

readonly profile="${1:-}"
if [[ -z "$profile" ]]; then
    printf 'Usage: model-server.sh <qwen36|qwen38> [--help|--print-command] [SERVER_ARGS...]\n' >&2
    exit 2
fi
shift

find_ninfer_root() {
    local candidate
    if [[ -n "${NINFER_ROOT:-}" ]]; then
        printf '%s\n' "$NINFER_ROOT"
        return
    fi
    for candidate in /d/dev/ninfer "$HOME/Dev/ninfer" "$HOME/dev/ninfer"; do
        if [[ -d "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return
        fi
    done
    printf '%s\n' /d/dev/ninfer
}

show_help() {
    printf '%s\n' \
        "Usage: ${profile}-server [--help|--print-command] [SERVER_ARGS...]" \
        '' \
        "Starts the ${profile} OpenAI-compatible local model server." \
        'Extra arguments are appended after the measured defaults.'
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    show_help
    exit 0
fi

declare -a command
case "$profile" in
    qwen36)
        readonly ninfer_root="$(find_ninfer_root)"
        readonly artifact="${QWEN36_ARTIFACT:-$ninfer_root/models/qwen3_6_35b_a3b.ninfer}"
        readonly server="${NINFER_SERVE:-$ninfer_root/build/apps/ninfer-serve}"
        [[ -x "$server" ]] || { printf 'qwen36-server: missing executable: %s\n' "$server" >&2; exit 1; }
        [[ -f "$artifact" ]] || { printf 'qwen36-server: missing artifact: %s\n' "$artifact" >&2; exit 1; }
        command=(
            "$server" "$artifact"
            --host "${QWEN36_HOST:-127.0.0.1}"
            --port "${QWEN36_PORT:-8080}"
            --model-id qwen3.6-35b-a3b
            --max-context "${QWEN36_CTX:-262144}"
            --kv-capacity auto
            --max-concurrency 1
            --prefill-chunk 1024
            --kv-dtype int8
            --spec mtp
            --draft-tokens "${QWEN36_MTP_DRAFT_TOKENS:-3}"
            --lm-head-draft
            --preserve-thinking
        )
        ;;
    qwen38)
        readonly llama_prefix="${LLAMA_CPP_PREFIX:-$HOME/.local/opt/llama.cpp}"
        readonly model="${QWEN38_GGUF:-$HOME/hf/models/Qwen3.8-27B-GGUF/Qwen3.8-27B-UD-Q4_K_XL.gguf}"
        readonly server="${LLAMA_SERVER:-$llama_prefix/bin/llama-server}"
        [[ -x "$server" ]] || { printf 'qwen38-server: missing executable: %s\n' "$server" >&2; exit 1; }
        [[ -f "$model" ]] || { printf 'qwen38-server: missing model: %s\n' "$model" >&2; exit 1; }
        export LD_LIBRARY_PATH="$llama_prefix/lib:${LD_LIBRARY_PATH:-}"
        command=(
            "$server"
            --model "$model"
            -ngl 99
            -c "${QWEN38_CTX:-262144}"
            --cache-type-k q8_0
            --cache-type-v q8_0
            --flash-attn on
            --host "${QWEN38_HOST:-127.0.0.1}"
            --port "${QWEN38_PORT:-8080}"
            --alias qwen3.8-27b
            --jinja
            --temp 1.0
            --top-p 0.95
            --top-k 20
            --min-p 0.0
        )
        ;;
    *)
        printf 'model-server.sh: unknown profile: %s\n' "$profile" >&2
        exit 2
        ;;
esac

if [[ "${1:-}" == "--print-command" ]]; then
    printf '%q ' "${command[@]}" "${@:2}"
    printf '\n'
    exit 0
fi

exec "${command[@]}" "$@"
