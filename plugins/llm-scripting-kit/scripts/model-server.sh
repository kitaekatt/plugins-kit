#!/usr/bin/env bash
# Canonical local-model server launcher used by Claude and shell entry points.

set -euo pipefail

readonly profile="${1:-}"
if [[ -z "$profile" ]]; then
    printf 'Usage: model-server.sh <qwen36|qwen38|qwen38l> [--help|--print-command] [SERVER_ARGS...]\n' >&2
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
        "Usage: ${profile}-server [--serial] [--help|--print-command] [SERVER_ARGS...]" \
        '' \
        "Starts the ${profile} OpenAI-compatible local model server." \
        'Extra arguments are appended after the measured defaults.' \
        '' \
        '  --serial   Serve ONE request at a time, giving it the whole KV pool.' \
        '             For a single very large prompt. The default serves several' \
        '             at once, which is faster for many independent requests but' \
        '             splits the pool between them.' \
        '' \
        'Concurrency can also be set outright with QWEN38_MAX_CONCURRENCY /' \
        'QWEN36_MAX_CONCURRENCY; --serial is shorthand for setting it to 1.'
}

# --serial is consumed HERE rather than passed through: it is not a server flag,
# it selects one of this launcher's two measured configurations. Parsed before
# the profile blocks so it can set the env default they read.
if [[ "${1:-}" == "--serial" ]]; then
    QWEN36_MAX_CONCURRENCY=1
    QWEN38_MAX_CONCURRENCY=1
    export QWEN36_MAX_CONCURRENCY QWEN38_MAX_CONCURRENCY
    shift
fi

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
            # Left at 1 DELIBERATELY, unlike qwen38 below: the concurrency
            # arithmetic was measured on qwen38 only and does not transfer. This
            # is a 35B MoE with int8 KV and an `auto` KV pool, so its weights
            # footprint, its workspace-per-slot cost and even the pool's SIZE
            # are all different numbers. Raise it once someone has measured
            # this profile the way qwen38 was measured.
            --max-concurrency "${QWEN36_MAX_CONCURRENCY:-1}"
            --prefill-chunk 1024
            --kv-dtype int8
            --spec mtp
            --draft-tokens "${QWEN36_MTP_DRAFT_TOKENS:-3}"
            --lm-head-draft
            --preserve-thinking
        )
        ;;
    # QWEN38_HOST and QWEN38_PORT are deliberately SHARED by both Qwen3.8
    # profiles: they describe the endpoint, which is the same contract whichever
    # backend is behind it, and the fleet exports QWEN38_HOST globally on that
    # basis. The CONTEXT is not shared, because the two backends do not have the
    # same ceiling -- NInfer's NVFP4 artifact fits 240,000 tokens on an RTX 5090
    # (upstream's own evaluation had to drop to 252,928 "to fit the RTX 5090
    # after weights"), while the llama.cpp GGUF takes the model's full 262,144.
    # One name meaning both would let a QWEN38_CTX exported for llama.cpp feed
    # an out-of-range context to NInfer, so llama.cpp uses QWEN38L_CTX.
    qwen38)
        readonly ninfer_root_38="$(find_ninfer_root)"
        readonly artifact_38="${QWEN38_ARTIFACT:-$ninfer_root_38/models/qwen3_8_27b_nvfp4.ninfer}"
        readonly server_38="${NINFER_SERVE:-$ninfer_root_38/build/apps/ninfer-serve}"
        [[ -x "$server_38" ]] || { printf 'qwen38-server: missing executable: %s\n' "$server_38" >&2; exit 1; }
        [[ -f "$artifact_38" ]] || { printf 'qwen38-server: missing artifact: %s\n' "$artifact_38" >&2; exit 1; }
        command=(
            "$server_38" "$artifact_38"
            --host "${QWEN38_HOST:-127.0.0.1}"
            --port "${QWEN38_PORT:-8080}"
            --model-id qwen3.8-27b
            --max-context "${QWEN38_CTX:-240000}"
            --kv-capacity "${QWEN38_KV_CAPACITY:-${QWEN38_CTX:-240000}}"
            # 4 is the BATCH default; export QWEN38_MAX_CONCURRENCY=1 for a
            # single huge prompt. Measured on an RTX 5090 (2026-09-01):
            #   - This flag pre-allocates workspace whether or not requests
            #     arrive: 9.53 GiB at 4, and the server REFUSES to start at 8
            #     (wants 11.85 GiB, 11.24 available). ~0.58 GiB per slot, so 5
            #     just fits the 1.02 GiB of slack at 4 and 6+ needs the KV pool
            #     cut to pay for it. Do not raise this past 5 at a 240k pool.
            #   - It is only a CAP. Actual parallelism is the KV pool divided by
            #     each request's reservation, and a request reserves
            #     (prompt + max_tokens) up front rather than what it consumes --
            #     so a 30k prompt with a 60k budget reserves 90k and only THREE
            #     run at once here. A client wanting more parallelism should
            #     lower ITS max_tokens; raising this flag will not do it.
            #   - Over-admission is backpressure, not failure: the extra request
            #     waits and is 503'd after ~30s ("inference request expired while
            #     waiting for admission"). Clients must retry a 503 or they
            #     silently drop work.
            --max-concurrency "${QWEN38_MAX_CONCURRENCY:-4}"
            --prefill-chunk 1024
            --kv-dtype fp8
            --spec mtp
            --draft-tokens "${QWEN38_MTP_DRAFT_TOKENS:-3}"
            --lm-head-draft
            --preserve-thinking
        )
        ;;
    qwen38l)
        readonly llama_prefix="${LLAMA_CPP_PREFIX:-$HOME/.local/opt/llama.cpp}"
        readonly model="${QWEN38_GGUF:-$HOME/hf/models/Qwen3.8-27B-GGUF/Qwen3.8-27B-UD-Q4_K_XL.gguf}"
        readonly server="${LLAMA_SERVER:-$llama_prefix/bin/llama-server}"
        [[ -x "$server" ]] || { printf 'qwen38l-server: missing executable: %s\n' "$server" >&2; exit 1; }
        [[ -f "$model" ]] || { printf 'qwen38l-server: missing model: %s\n' "$model" >&2; exit 1; }
        export LD_LIBRARY_PATH="$llama_prefix/lib:${LD_LIBRARY_PATH:-}"
        command=(
            "$server"
            --model "$model"
            -ngl 99
            -c "${QWEN38L_CTX:-262144}"
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
