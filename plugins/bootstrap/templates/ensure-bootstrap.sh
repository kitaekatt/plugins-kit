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
#      install bootstrap (missing) or update it (too old).
#   4. Repair on failure only: when the claude CLI is missing or one of its
#      steps fails, reinstall the CLI with Anthropic's native installer and
#      retry once. Only a missing CLI or the native install in ~/.local/bin is
#      replaced. A CLI version this hook already reinstalled is not
#      reinstalled again, so a failure with another cause costs nothing more.
#   5. Repair the marketplace, only on failure: when `marketplace update` fails
#      and the plugins-kit entry has an empty installLocation (a corrupted
#      known_marketplaces.json entry), remove and re-add the marketplace and
#      update it once more. Removing a marketplace also uninstalls its plugins,
#      so bootstrap is then installed again at user scope. The project's
#      .claude/settings.json and settings.local.json, which the removal
#      rewrites, are restored byte for byte with their read-only state. No
#      other marketplace state is repaired, and any other update failure is
#      reported unchanged.
#
# The settings entry runs this with "async" and "asyncRewake", so it never
# delays a session. The healthy path exits 0 with no output. Every outcome the
# user must hear about -- installed, updated, or failed -- goes to stderr with
# exit 2, which wakes Claude with the message.
#
# Stdlib-free by design: this runs on a machine bootstrap has not provisioned,
# so it may rely only on bash and the `claude` CLI -- no python, jq, or node.

set -u

MIN_VERSION="@MIN_VERSION@"
PLUGIN_NAME="bootstrap"
MARKETPLACE="plugins-kit"
MARKETPLACE_SOURCE="https://github.com/kitaekatt/plugins-kit.git"
PLUGIN_REF="${PLUGIN_NAME}@${MARKETPLACE}"
# The native installer puts the CLI here on every platform.
CLI_DIR="$HOME/.local/bin"
# Holds the CLI version this hook last reinstalled. User-scoped data, in
# bootstrap's own data directory.
CLI_REPAIR_STAMP="$HOME/.claude/plugins/data/$MARKETPLACE/$PLUGIN_NAME/ensure-bootstrap-cli-repair"

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$PWD}"

[ -f "$PROJECT_DIR/.claude/bootstrap.json" ] || exit 0

# Tell the user one thing, through Claude: asyncRewake delivers stderr to
# Claude when the hook exits 2.
report() {
    printf 'ensure-bootstrap: %s\n' "$1" >&2
    exit 2
}

# Prefer the native install, the copy repair_cli writes, over any other claude
# on PATH. Otherwise an older copy earlier on PATH would hide the repaired one
# and every session would reinstall it.
[ -d "$CLI_DIR" ] && PATH="$CLI_DIR:$PATH"

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

LOG=""
MARKETPLACE_NOTE=""
run_step() {
    local out
    if out="$("$@" </dev/null 2>&1)"; then
        return 0
    fi
    LOG="'$*' failed: $(printf '%s' "$out" | tail -n 5)"
    return 1
}

# Success when `marketplace list --json` (pretty-printed, one flat object per
# marketplace) shows the plugins-kit entry with an empty installLocation.
marketplace_location_empty() {
    local line name="" empty=0
    while IFS= read -r line; do
        line="${line%$'\r'}"
        line="${line#"${line%%[![:space:]]*}"}"
        case "$line" in
            '{'*) name=""; empty=0 ;;
            '"name": "'*) name="${line#\"name\": \"}"; name="${name%%\"*}" ;;
            '"installLocation": ""'*) empty=1 ;;
            '}'*) [ "$name" = "$MARKETPLACE" ] && [ "$empty" -eq 1 ] && return 0 ;;
        esac
    done <<EOF
$1
EOF
    return 1
}

# Called after `marketplace update` failed. When the plugins-kit entry's
# installLocation is empty, remove and re-add the marketplace and update it
# again. Returns 1 with LOG set when the entry is not in that state (LOG keeps
# the update failure) or a repair step fails. Removing the marketplace
# uninstalls its plugins, so the caller re-resolves what is installed.
#
# `marketplace remove` also strips the marketplace's plugins from the project's
# enabledPlugins in .claude/settings.json and .claude/settings.local.json, which
# are tracked or read-only in some projects. Both files are saved first and put
# back, content and read-only state, on every path out of the repair. (A
# read-only settings.json is skipped by the CLI, so it only needs the check.)
repair_marketplace() {
    local update_log="$LOG" saved f rc
    if ! marketplace_location_empty "$(claude plugin marketplace list --json </dev/null 2>/dev/null)"; then
        LOG="$update_log"
        return 1
    fi
    saved="$(mktemp -d)" || { LOG="could not create a temporary directory."; return 1; }
    for f in settings.json settings.local.json; do
        [ -f ".claude/$f" ] || continue
        cp ".claude/$f" "$saved/$f"
        [ -w ".claude/$f" ] || : > "$saved/$f.readonly"
    done
    run_marketplace_repair
    rc=$?
    for f in settings.json settings.local.json; do
        [ -f "$saved/$f" ] || continue
        if ! cmp -s "$saved/$f" ".claude/$f"; then
            chmod u+w ".claude/$f"
            cat "$saved/$f" > ".claude/$f"
        fi
        [ -f "$saved/$f.readonly" ] && chmod a-w ".claude/$f"
    done
    rm -rf "$saved"
    return $rc
}

run_marketplace_repair() {
    run_step claude plugin marketplace remove "$MARKETPLACE" || return 1
    run_step claude plugin marketplace add "$MARKETPLACE_SOURCE" || return 1
    run_step claude plugin marketplace update "$MARKETPLACE" || return 1
    MARKETPLACE_NOTE="Repaired the $MARKETPLACE marketplace entry (empty installLocation) by removing and re-adding it, which uninstalled its plugins; bootstrap reinstalls this project's plugins after the restart."
    return 0
}

# Bring bootstrap to MIN_VERSION. Returns 0 when it is there, with ACTION set
# to "installed" or "updated" when this call did the work, or 1 with LOG set
# to the step that failed.
ensure_bootstrap() {
    local listing
    ACTION=""
    if ! command -v claude >/dev/null 2>&1; then
        LOG="the claude CLI is not on PATH."
        return 1
    fi
    if ! resolve_effective; then
        LOG="'claude plugin list --json' failed."
        return 1
    fi
    if [ -n "$EFFECTIVE_VERSION" ] && version_ge "$EFFECTIVE_VERSION" "$MIN_VERSION"; then
        return 0
    fi

    listing="$(claude plugin marketplace list --json </dev/null 2>/dev/null)"
    if ! printf '%s' "$listing" | grep -q "\"name\": \"$MARKETPLACE\""; then
        run_step claude plugin marketplace add "$MARKETPLACE_SOURCE" || return 1
    fi
    if ! run_step claude plugin marketplace update "$MARKETPLACE"; then
        repair_marketplace || return 1
        if ! resolve_effective; then
            LOG="'claude plugin list --json' failed after the marketplace repair."
            return 1
        fi
    fi

    if [ -z "$EFFECTIVE_VERSION" ]; then
        ACTION="installed"
        run_step claude plugin install "$PLUGIN_REF" --scope user || return 1
    else
        ACTION="updated"
        run_step claude plugin update "$PLUGIN_REF" --scope "$EFFECTIVE_SCOPE" || return 1
    fi

    resolve_effective
    if [ -z "$EFFECTIVE_VERSION" ] || ! version_ge "$EFFECTIVE_VERSION" "$MIN_VERSION"; then
        LOG="after the $ACTION step the installed version is '${EFFECTIVE_VERSION:-none}'."
        return 1
    fi
    return 0
}

cli_version() {
    if command -v claude >/dev/null 2>&1; then
        claude --version </dev/null 2>/dev/null | cut -d' ' -f1
    else
        echo "missing"
    fi
}

# Reinstall the claude CLI with the native installer, which installs the
# current release over a missing, outdated, or crashing copy. Only a missing CLI
# or the native install is replaced; a copy another tool installed is left to
# that tool. Skipped when this hook already reinstalled the version now
# present: that version failed after a reinstall, so the CLI is not the cause.
# Returns 0 when it reinstalled.
repair_cli() {
    local current before
    current="$(command -v claude 2>/dev/null)"
    case "$current" in
        ""|"$CLI_DIR"/*) ;;
        *)
            REPAIR_NOTE="The claude CLI at $current was not installed by the native installer, so this hook did not reinstall it; update it with the tool that installed it."
            return 1
            ;;
    esac
    before="$(cli_version)"
    if [ -f "$CLI_REPAIR_STAMP" ] && [ "$(cat "$CLI_REPAIR_STAMP")" = "$before" ]; then
        REPAIR_NOTE="The claude CLI ($before) was already reinstalled by this hook, so it was not reinstalled again."
        return 1
    fi
    case "$(uname -s)" in
        MINGW*|MSYS*|CYGWIN*)
            run_step powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://claude.ai/install.ps1 | iex"
            ;;
        *)
            run_step bash -c "curl -fsSL https://claude.ai/install.sh | bash"
            ;;
    esac || { REPAIR_NOTE="Reinstalling the claude CLI also failed: $LOG"; return 1; }
    PATH="$CLI_DIR:$PATH"
    hash -r
    mkdir -p "$(dirname "$CLI_REPAIR_STAMP")"
    cli_version > "$CLI_REPAIR_STAMP"
    REPAIR_NOTE="Reinstalled the claude CLI ($before -> $(cat "$CLI_REPAIR_STAMP")) first."
    return 0
}

REPAIR_NOTE=""
if ! ensure_bootstrap; then
    FIRST_LOG="$LOG"
    if ! repair_cli; then
        report "could not bring $PLUGIN_REF to $MIN_VERSION or later. $FIRST_LOG $REPAIR_NOTE${MARKETPLACE_NOTE:+ $MARKETPLACE_NOTE} Tell the user this, so bootstrap and the project's plugins can be installed."
    fi
    if ! ensure_bootstrap; then
        report "could not bring $PLUGIN_REF to $MIN_VERSION or later. $FIRST_LOG $REPAIR_NOTE${MARKETPLACE_NOTE:+ $MARKETPLACE_NOTE} It still failed afterwards: $LOG Tell the user this, so bootstrap and the project's plugins can be installed."
    fi
fi

[ -n "$ACTION" ] || exit 0
report "$ACTION $PLUGIN_REF $EFFECTIVE_VERSION (minimum $MIN_VERSION). ${REPAIR_NOTE:+$REPAIR_NOTE }${MARKETPLACE_NOTE:+$MARKETPLACE_NOTE }Tell the user to restart Claude Code to load it; bootstrap then provisions this project's other plugins."
