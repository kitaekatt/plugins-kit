#!/usr/bin/env bash
# Replace and inspect the resident local Qwen server.

set -euo pipefail

readonly script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly launcher="$script_dir/model-server.sh"
readonly data_dir="${HOME}/.claude/plugins/data/plugins-kit/llm-scripting-kit"
readonly default_timeout="${QWEN_SWITCH_TIMEOUT:-180}"

usage() {
    printf '%s\n' \
        'Usage: qwen-switch start <qwen36|qwen38|qwen38l> [SERVER_ARGS...]' \
        '       qwen-switch status' \
        '' \
        'start stops the identified server on the requested profile port, starts' \
        'the existing profile launcher detached, and waits for its model id.' \
        'status reports the identified server on each configured local port.'
}

profile_port() {
    case "$1" in
        qwen36) printf '%s\n' "${QWEN36_PORT:-8080}" ;;
        qwen38|qwen38l) printf '%s\n' "${QWEN38_PORT:-8080}" ;;
        *) return 1 ;;
    esac
}

profile_host() {
    case "$1" in
        qwen36) printf '%s\n' "${QWEN36_HOST:-127.0.0.1}" ;;
        qwen38|qwen38l) printf '%s\n' "${QWEN38_HOST:-127.0.0.1}" ;;
        *) return 1 ;;
    esac
}

expected_model() {
    case "$1" in
        qwen36) printf '%s\n' 'qwen3.6-35b-a3b' ;;
        qwen38|qwen38l) printf '%s\n' 'qwen3.8-27b' ;;
        *) return 1 ;;
    esac
}

listener_pids() {
    local port="$1"
    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true
    elif command -v fuser >/dev/null 2>&1; then
        fuser -n tcp "$port" 2>/dev/null | awk '{ for (i = 1; i <= NF; i++) if ($i ~ /^[0-9]+$/) print $i }'
    else
        printf 'qwen-switch: lsof or fuser is required to identify port %s\n' "$port" >&2
        return 1
    fi
}

process_name() {
    ps -p "$1" -o comm= 2>/dev/null | awk 'NR == 1 { print $1 }'
}

process_args() {
    ps -p "$1" -o args= 2>/dev/null | sed -n '1p'
}

is_server_pid() {
    case "$(process_name "$1")" in
        ninfer-serve|llama-server) return 0 ;;
        *) return 1 ;;
    esac
}

profile_for_pid() {
    local pid="$1"
    local name
    local args
    name="$(process_name "$pid")"
    args="$(process_args "$pid")"
    case "$name:$args" in
        ninfer-serve:*'--model-id qwen3.6-35b-a3b'*) printf '%s\n' qwen36 ;;
        ninfer-serve:*'--model-id qwen3.8-27b'*) printf '%s\n' qwen38 ;;
        llama-server:*) printf '%s\n' qwen38l ;;
        *) return 1 ;;
    esac
}

stop_pid() {
    local pid="$1"
    local name
    name="$(process_name "$pid")"
    case "$name" in
        ninfer-serve|llama-server) ;;
        *)
            printf 'qwen-switch: refusing to terminate pid %s (%s is not a managed server)\n' "$pid" "${name:-unknown}" >&2
            return 1
            ;;
    esac
    kill -TERM "$pid" 2>/dev/null || true
    local remaining=20
    while kill -0 "$pid" 2>/dev/null; do
        if [[ "$remaining" -le 0 ]]; then
            kill -KILL "$pid" 2>/dev/null || true
            break
        fi
        sleep 1
        remaining=$((remaining - 1))
    done
}

stop_listeners() {
    local port="$1"
    local pid
    local found=0
    while IFS= read -r pid; do
        [[ -n "$pid" ]] || continue
        found=1
        if ! is_server_pid "$pid"; then
            printf 'qwen-switch: refusing to replace port %s; pid %s is not ninfer-serve or llama-server\n' "$port" "$pid" >&2
            return 1
        fi
        stop_pid "$pid"
    done <<EOF
$(listener_pids "$port")
EOF
    return 0
}

readiness_body() {
    local host="$1"
    local port="$2"
    local url_host="$host"
    [[ "$url_host" == '0.0.0.0' ]] && url_host=127.0.0.1
    curl -fsS --max-time 2 "http://$url_host:$port/v1/models" 2>/dev/null
}

model_id_from_body() {
    sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | sed -n '1p'
}

wait_ready() {
    local profile="$1"
    local host="$2"
    local port="$3"
    local expected="$4"
    local server_pid="$5"
    local body
    local model_id
    local elapsed=0
    while [[ "$elapsed" -lt "$default_timeout" ]]; do
        body="$(readiness_body "$host" "$port" || true)"
        model_id="$(printf '%s' "$body" | model_id_from_body)"
        if [[ "$model_id" == "$expected" ]] &&
            [[ " $(listener_pids "$port") " == *" $server_pid "* ]] &&
            is_server_pid "$server_pid"; then
            printf 'qwen-switch: %s is ready on port %s (pid %s)\n' "$profile" "$port" "$(listener_pids "$port" | sed -n '1p')"
            return 0
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
    printf 'qwen-switch: %s did not become ready on port %s with model id %s within %ss\n' "$profile" "$port" "$expected" "$default_timeout" >&2
    return 1
}

start_profile() {
    local profile="$1"
    shift
    local port
    local host
    local expected
    local log_file
    profile_port "$profile" >/dev/null || {
        printf 'qwen-switch: unknown profile: %s\n' "$profile" >&2
        return 2
    }
    port="$(profile_port "$profile")"
    host="$(profile_host "$profile")"
    expected="$(expected_model "$profile")"
    case "$port" in
        ''|*[!0-9]*) printf 'qwen-switch: invalid port for %s: %s\n' "$profile" "$port" >&2; return 2 ;;
    esac
    stop_listeners "$port"
    mkdir -p "$data_dir"
    log_file="$data_dir/qwen-switch-$profile.log"
    if command -v setsid >/dev/null 2>&1; then
        setsid nohup "$launcher" "$profile" "$@" >"$log_file" 2>&1 < /dev/null &
    else
        nohup "$launcher" "$profile" "$@" >"$log_file" 2>&1 < /dev/null &
    fi
    local server_pid=$!
    wait_ready "$profile" "$host" "$port" "$expected" "$server_pid" || {
        printf 'qwen-switch: server log: %s\n' "$log_file" >&2
        return 1
    }
}

status() {
    local profile
    local port
    local pid
    local seen_ports=' '
    local reported=0
    for profile in qwen36 qwen38 qwen38l; do
        port="$(profile_port "$profile")"
        case "$seen_ports" in
            *" $port "*) continue ;;
        esac
        seen_ports="$seen_ports$port "
        while IFS= read -r pid; do
            [[ -n "$pid" ]] || continue
            reported=1
            if ! is_server_pid "$pid"; then
                printf 'qwen-switch: port %s has an unrecognized listener (pid %s)\n' "$port" "$pid"
            elif profile_for_pid "$pid" >/dev/null 2>&1; then
                printf '%s running on port %s (pid %s)\n' "$(profile_for_pid "$pid")" "$port" "$pid"
            else
                printf 'qwen-switch: managed server on port %s has an unrecognized model (pid %s)\n' "$port" "$pid"
            fi
        done <<EOF
$(listener_pids "$port")
EOF
    done
    [[ "$reported" == '1' ]] || printf 'no managed Qwen server is listening on the configured ports\n'
}

command_name="${1:-}"
case "$command_name" in
    start)
        [[ $# -ge 2 ]] || { usage >&2; exit 2; }
        shift
        start_profile "$@"
        ;;
    status)
        [[ $# -eq 1 ]] || { usage >&2; exit 2; }
        status
        ;;
    --help|-h|'')
        usage
        [[ -n "$command_name" ]] || exit 2
        ;;
    *)
        printf 'qwen-switch: unknown command: %s\n' "$command_name" >&2
        usage >&2
        exit 2
        ;;
esac
