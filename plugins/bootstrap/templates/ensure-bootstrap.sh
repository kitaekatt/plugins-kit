#!/usr/bin/env bash
# ensure-bootstrap.sh -- project SessionStart hook that installs or updates the
# bootstrap plugin, so bootstrap can then provision everything else.
#
# Written into the project by `bootstrap install-hook`, which overwrites this
# file on every run. To change the minimum version, re-run that command in the
# project root rather than editing this file.
#
# Why it exists: from Claude Code 2.1.144 a plugin listed only in a project's
# .claude/settings.json `enabledPlugins` is not installed automatically,
# so a fresh clone never gets bootstrap. See the bootstrap skill reference
# fleet-management.md.
#
# Contract:
#   1. Opt-out: no <project>/.claude/bootstrap.json -> exit silently.
#   2. Detect: bootstrap@plugins-kit installed for this project at or above
#      MIN_VERSION -> exit silently.
#   3. Remediate: add the plugins-kit marketplace if missing, update it, then
#      install bootstrap (missing) or update it (too old). Report the outcome
#      to the user as a systemMessage.
#
# Stdlib-free by design: this runs on a machine bootstrap has not provisioned,
# so it may rely only on bash and the `claude` CLI -- no python, jq, or node.
# It always exits 0 so a remediation failure never blocks a session.

set -u

MIN_VERSION="@MIN_VERSION@"
PLUGIN_NAME="bootstrap"
MARKETPLACE="plugins-kit"
MARKETPLACE_SOURCE="https://github.com/kitaekatt/plugins-kit.git"
PLUGIN_REF="${PLUGIN_NAME}@${MARKETPLACE}"

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"

[ -f "$PROJECT_DIR/.claude/bootstrap.json" ] || exit 0

# Emit one systemMessage for the user. JSON-escapes backslash, quote, tab, CR,
# and newline -- the only characters the messages below can contain.
emit() {
    local msg="$1"
    msg="${msg//\\/\\\\}"
    msg="${msg//\"/\\\"}"
    msg="${msg//$'\t'/ }"
    msg="${msg//$'\r'/}"
    msg="${msg//$'\n'/\\n}"
    printf '{"systemMessage": "%s"}\n' "$msg"
}

if ! command -v claude >/dev/null 2>&1; then
    emit "ensure-bootstrap: the claude CLI is not on PATH, so $PLUGIN_REF could not be checked or installed."
    exit 0
fi

# A nested claude refuses to run while CLAUDECODE is set by the parent session.
unset CLAUDECODE
cd "$PROJECT_DIR" 2>/dev/null || exit 0

# version_ge A B -> success when dotted version A >= B (numeric per component).
version_ge() {
    local IFS=.
    local -a a b
    a=($1)
    b=($2)
    local i n x y
    n=${#a[@]}
    [ ${#b[@]} -gt "$n" ] && n=${#b[@]}
    for ((i = 0; i < n; i++)); do
        x="${a[i]:-0}"
        y="${b[i]:-0}"
        case "$x" in *[!0-9]*|'') x=0 ;; esac
        case "$y" in *[!0-9]*|'') y=0 ;; esac
        if ((10#$x > 10#$y)); then return 0; fi
        if ((10#$x < 10#$y)); then return 1; fi
    done
    return 0
}

# Normalize a path for comparison: backslashes to slashes, MSYS /d/ to D:/,
# an upper-case drive letter, no trailing slash. The rest of the path stays
# case-sensitive because Claude Code matches projectPath that way: a record
# for D:\Dev\x does not apply to a session in D:\dev\x.
norm_path() {
    local p drive
    p="$(printf '%s' "$1" | tr '\\' '/' | tr -s '/')"
    case "$p" in
        /[a-zA-Z]/*) p="${p:1:1}:${p:2}" ;;
        /[a-zA-Z]) p="${p:1:1}:" ;;
    esac
    case "$p" in
        [a-zA-Z]:*)
            drive="$(printf '%s' "${p:0:1}" | tr '[:lower:]' '[:upper:]')"
            p="$drive${p:1}"
            ;;
    esac
    p="${p%/}"
    printf '%s' "$p"
}

# Print "scope version" for every bootstrap record that applies to this
# project: user scope always; project and local scope only when their
# projectPath is this project. Parses the pretty-printed JSON of
# `claude plugin list --json` line by line (no JSON tool is guaranteed here),
# reading only top-level fields of each record.
applicable_records() {
    local listing="$1" project line depth=0 key value
    local id="" version="" scope="" path=""
    project="$(norm_path "$PROJECT_DIR")"
    while IFS= read -r line; do
        line="${line%$'\r'}"
        line="${line#"${line%%[![:space:]]*}"}"
        case "$line" in
            '{'*)
                depth=$((depth + 1))
                [ "$depth" -eq 1 ] && { id=""; version=""; scope=""; path=""; }
                continue
                ;;
            '}'*)
                if [ "$depth" -eq 1 ] && [ "$id" = "$PLUGIN_REF" ]; then
                    case "$scope" in
                        user) printf '%s %s\n' "$scope" "$version" ;;
                        project|local)
                            [ "$(norm_path "$path")" = "$project" ] &&
                                printf '%s %s\n' "$scope" "$version"
                            ;;
                    esac
                fi
                depth=$((depth - 1))
                continue
                ;;
        esac
        case "$line" in
            *'{') depth=$((depth + 1)); continue ;;
        esac
        [ "$depth" -eq 1 ] || continue
        if [[ "$line" =~ ^\"([A-Za-z]+)\":\ \"(.*)\",?$ ]]; then
            key="${BASH_REMATCH[1]}"
            value="${BASH_REMATCH[2]}"
            case "$key" in
                id) id="$value" ;;
                version) version="$value" ;;
                scope) scope="$value" ;;
                projectPath) path="$value" ;;
            esac
        fi
    done <<EOF
$listing
EOF
}

# Sets EFFECTIVE_SCOPE and EFFECTIVE_VERSION to the record Claude Code loads
# for this project -- local over project over user, highest version within a
# scope -- or leaves both empty when bootstrap is not installed here.
resolve_effective() {
    local listing records s v best_scope="" best_version="" rank best_rank=0
    EFFECTIVE_SCOPE=""
    EFFECTIVE_VERSION=""
    listing="$(claude plugin list --json </dev/null 2>/dev/null)" || return 1
    records="$(applicable_records "$listing")"
    while read -r s v; do
        [ -n "$s" ] || continue
        case "$s" in local) rank=3 ;; project) rank=2 ;; *) rank=1 ;; esac
        if [ "$rank" -gt "$best_rank" ] ||
           { [ "$rank" -eq "$best_rank" ] && ! version_ge "$best_version" "$v"; }; then
            best_rank=$rank
            best_scope="$s"
            best_version="$v"
        fi
    done <<EOF
$records
EOF
    EFFECTIVE_SCOPE="$best_scope"
    EFFECTIVE_VERSION="$best_version"
    return 0
}

if ! resolve_effective; then
    emit "ensure-bootstrap: 'claude plugin list --json' failed, so $PLUGIN_REF could not be checked."
    exit 0
fi

if [ -n "$EFFECTIVE_VERSION" ] && version_ge "$EFFECTIVE_VERSION" "$MIN_VERSION"; then
    exit 0
fi

# --- Remediation ---
LOG=""
run_step() {
    local out
    if out="$("$@" </dev/null 2>&1)"; then
        return 0
    fi
    LOG="'$*' failed: $(printf '%s' "$out" | tail -n 5)"
    return 1
}

fail() {
    emit "ensure-bootstrap: could not bring $PLUGIN_REF to $MIN_VERSION or later. $LOG"
    exit 0
}

listing="$(claude plugin marketplace list --json </dev/null 2>/dev/null)"
if ! printf '%s' "$listing" | grep -q "\"name\": \"$MARKETPLACE\""; then
    run_step claude plugin marketplace add "$MARKETPLACE_SOURCE" || fail
fi
run_step claude plugin marketplace update "$MARKETPLACE" || fail

if [ -z "$EFFECTIVE_VERSION" ]; then
    action="installed"
    run_step claude plugin install "$PLUGIN_REF" --scope user || fail
else
    action="updated"
    run_step claude plugin update "$PLUGIN_REF" --scope "$EFFECTIVE_SCOPE" || fail
fi

resolve_effective
if [ -z "$EFFECTIVE_VERSION" ] || ! version_ge "$EFFECTIVE_VERSION" "$MIN_VERSION"; then
    LOG="after the $action step the installed version is '${EFFECTIVE_VERSION:-none}'."
    fail
fi

emit "ensure-bootstrap: $action $PLUGIN_REF $EFFECTIVE_VERSION (minimum $MIN_VERSION). Restart Claude Code to load it; bootstrap then provisions this project's other plugins."
exit 0
