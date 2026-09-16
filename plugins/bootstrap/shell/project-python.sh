# project-python.sh -- resolve BOOTSTRAP_PROJECT_PYTHON for a directory.
#
# Contract (interface-v3, section 1 "Normative resolution rule", steps 1, 2, 4, 5):
#
#   bootstrap_resolve_project_python <dir>
#       Prints ONE absolute path (forward slashes; drive letter on Windows,
#       e.g. C:/Users/you/proj/.venv/Scripts/python.exe) and returns 0 --
#       or, for an OPTED-OUT tree, prints an empty line and returns 1.
#
#       The walk: from <dir> upward (stop after checking $HOME itself, or at a
#       drive/filesystem root; depth cap 64), each directory is checked for
#       (a) an opt-out -- <d>/.claude/bootstrap.json or bootstrap.local.json
#       declaring "project_python": false; never at $HOME itself, whose
#       .claude/bootstrap.json is the USER layer -- and then (b) a venv --
#       <d>/.venv, or <d>/$UV_PROJECT_ENVIRONMENT when that is relative,
#       holding pyvenv.cfg and an executable interpreter. The first directory
#       with either stops the walk.
#
#       Order: an opt-out found by the walk (no project value) >
#       $VIRTUAL_ENV (when <VIRTUAL_ENV>/bin/python or
#       <VIRTUAL_ENV>/Scripts/python.exe is executable) > an absolute
#       $UV_PROJECT_ENVIRONMENT holding pyvenv.cfg and an interpreter > the
#       venv found by the walk > $BOOTSTRAP_PYTHON > the deterministic
#       standalone interpreter. BOOTSTRAP_PYTHON is never affected by an
#       opt-out.
#
# Sourced by TWO callers: the SessionStart hook prelude (in a subshell, with
# BOOTSTRAP_PP_NO_REGISTER=1, before $OS is set -- $OSTYPE is used here, never
# $OS) and interactive terminals from the data-dir copy through the rc line
# bootstrap writes (bootstrap_lib/shell_hook.py). Terminals additionally get a
# directory-change hook: bash PROMPT_COMMAND, zsh chpwd + precmd. Registration
# NEVER happens when BOOTSTRAP_PP_NO_REGISTER is set or the shell is not
# interactive; in that case sourcing defines functions and has no other effect.
#
# The hook (_bootstrap_project_python_update) keeps BOOTSTRAP_PROJECT_PYTHON in
# step with the current directory. It re-resolves only when its cache key
# changes: $PWD, $VIRTUAL_ENV, $UV_PROJECT_ENVIRONMENT, whether the current
# directory's venv has a pyvenv.cfg (an in-place `uv venv` refreshes), and
# whether the last exported interpreter still exists. The key is exported as
# _BOOTSTRAP_PP_DIR, the value it set as _BOOTSTRAP_PP_LAST. A value the user
# exported (one that differs from _BOOTSTRAP_PP_LAST) is never overwritten or
# unset. In an opted-out tree the hook unsets only the value it set itself.
# An edit to a bootstrap.json takes effect at the next key change. The last
# exit status ($?) is preserved.
#
# Constraints: bash 3.2 and zsh; no arrays; no `read -p`; no `${x^^}`; no forks
# on the resolution path (results travel through the _BPP_R variable, never
# through command substitution); no cygpath. zsh-only syntax is reached through
# eval so bash never parses it.
#
# Path form, applied to BOTH sides of every comparison: on Windows
# ($OSTYPE msys*, cygwin*, win32*) every backslash becomes "/", an MSYS mount
# (/c/..., /tmp/...; read once from /proc/mounts) becomes its native C:/...
# spelling, a lower-case drive letter is upper-cased, "X:" and "X:/" are roots,
# and $HOME is compared case-insensitively. The literal
# .local/share/python-standalone mirrors interpreter_env.STANDALONE_DIR_REL.

# _bpp_is_windows -- 0 when this shell runs on Windows (Git Bash, MSYS2, Cygwin).
_bpp_is_windows() {
    case "${OSTYPE:-}" in
        msys*|cygwin*|win32*) return 0 ;;
    esac
    return 1
}

# _bpp_mounts_load -- cache the MSYS/Cygwin mount table in _BPP_MOUNTS as
# "mountpoint|native" lines (native spellings only, \040 decoded). Once per shell.
_bpp_mounts_load() {
    local _src="" _mnt="" _rest="" _nl='
'
    _BPP_MOUNTS=""
    _BPP_MOUNTS_LOADED=1
    [ -r /proc/mounts ] || return 0
    while IFS=' ' read -r _src _mnt _rest; do
        case "$_src" in
            [A-Za-z]:*) ;;
            *) continue ;;
        esac
        _src="${_src//\\040/ }"
        _mnt="${_mnt//\\040/ }"
        _BPP_MOUNTS="${_BPP_MOUNTS}${_mnt}|${_src}${_nl}"
    done < /proc/mounts
    return 0
}

# _bpp_norm <path> -- set _BPP_R to the normalized absolute form of <path>
# (an empty or relative <path> is taken relative to $PWD).
_bpp_norm() {
    local _p="${1:-}" _rest="" _line="" _mnt="" _src="" _best="" _bestn=0
    local _l="" _lower="abcdefghijklmnopqrstuvwxyz" _upper="ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    local _nl='
'
    if _bpp_is_windows; then
        _p="${_p//\\//}"
    fi
    case "$_p" in
        /*) ;;
        [A-Za-z]:*) _bpp_is_windows || _p="$PWD/$_p" ;;
        "") _p="$PWD" ;;
        *) _p="$PWD/$_p" ;;
    esac
    if _bpp_is_windows; then
        case "$_p" in
            /*)
                [ -n "${_BPP_MOUNTS_LOADED:-}" ] || _bpp_mounts_load
                _rest="${_BPP_MOUNTS:-}"
                while [ -n "$_rest" ]; do
                    _line="${_rest%%"$_nl"*}"
                    _rest="${_rest#*"$_nl"}"
                    _mnt="${_line%%|*}"
                    if [ "$_mnt" = / ]; then
                        # A one-letter top directory is a drive, never a
                        # directory under the MSYS root.
                        case "$_p" in
                            /[A-Za-z]|/[A-Za-z]/*) continue ;;
                        esac
                    else
                        case "$_p" in
                            "$_mnt"|"$_mnt"/*) ;;
                            *) continue ;;
                        esac
                    fi
                    if [ "${#_mnt}" -gt "$_bestn" ]; then
                        _bestn="${#_mnt}"
                        _best="$_line"
                    fi
                done
                if [ -n "$_best" ]; then
                    _mnt="${_best%%|*}"
                    _src="${_best#*|}"
                    if [ "$_mnt" = / ]; then
                        _p="${_src}${_p}"
                    else
                        _p="${_src}${_p#"$_mnt"}"
                    fi
                else
                    case "$_p" in
                        /cygdrive/[A-Za-z]|/cygdrive/[A-Za-z]/*) _p="${_p#/cygdrive}" ;;
                    esac
                    case "$_p" in
                        /[A-Za-z]|/[A-Za-z]/*) _p="${_p:1:1}:${_p:2}" ;;
                    esac
                fi
                ;;
        esac
        case "$_p" in
            [a-z]:*)
                _l="${_p%"${_p#?}"}"
                _lower="${_lower%%"$_l"*}"
                _p="${_upper:${#_lower}:1}${_p#?}"
                ;;
        esac
    fi
    while :; do
        case "$_p" in
            /) break ;;
            */) _p="${_p%/}" ;;
            *) break ;;
        esac
    done
    case "$_p" in
        "") _p=/ ;;
        [A-Za-z]:) _bpp_is_windows && _p="$_p/" ;;
    esac
    _BPP_R="$_p"
    return 0
}

# _bpp_same <a> <b> -- 0 when two normalized paths name the same directory
# (case-insensitive on Windows).
_bpp_same() {
    local _had=1 _r=1
    [ "$1" = "$2" ] && return 0
    _bpp_is_windows || return 1
    if [ -n "${ZSH_VERSION:-}" ]; then
        eval '[ "${(L)1}" = "${(L)2}" ]'
        return $?
    fi
    shopt -q nocasematch && _had=0
    shopt -s nocasematch
    [[ "$1" == "$2" ]] && _r=0
    [ "$_had" -eq 0 ] || shopt -u nocasematch
    return "$_r"
}

# _bpp_venv_python <venv-dir> -- 0 and _BPP_R set when the venv holds an
# executable interpreter (bin/python first, then Scripts/python.exe -- the
# order of venv_check.venv_python).
_bpp_venv_python() {
    if [ -f "$1/bin/python" ] && [ -x "$1/bin/python" ]; then
        _BPP_R="$1/bin/python"
        return 0
    fi
    if [ -f "$1/Scripts/python.exe" ] && [ -x "$1/Scripts/python.exe" ]; then
        _BPP_R="$1/Scripts/python.exe"
        return 0
    fi
    return 1
}

# _bpp_opted_out <dir> [<at-home>] -- 0 when <dir>/.claude/bootstrap.json or
# <dir>/.claude/bootstrap.local.json declares "project_python": false (the two
# project layers the engine honours). A non-empty <at-home> means <dir> is
# $HOME, whose .claude/ holds the USER layer: never an opt-out.
_bpp_opted_out() {
    [ -z "${2:-}" ] || return 1
    _bpp_opted_out_file "${1%/}/.claude/bootstrap.json" && return 0
    _bpp_opted_out_file "${1%/}/.claude/bootstrap.local.json"
}

# _bpp_opted_out_file <file> -- 0 when <file> declares "project_python": false
# (any occurrence; read without a fork).
_bpp_opted_out_file() {
    local _f="$1" _key='"project_python"'
    local _body="" _rest="" _ws=""
    [ -f "$_f" ] || return 1
    IFS= read -r -d '' _body < "$_f" || :
    while :; do
        case "$_body" in
            *"$_key"*) ;;
            *) return 1 ;;
        esac
        _body="${_body#*"$_key"}"
        _ws="${_body%%[![:space:]]*}"
        _rest="${_body#"$_ws"}"
        case "$_rest" in
            :*) _rest="${_rest#:}" ;;
            *) continue ;;
        esac
        _ws="${_rest%%[![:space:]]*}"
        _rest="${_rest#"$_ws"}"
        case "$_rest" in
            false|false[!A-Za-z0-9_]*) return 0 ;;
        esac
    done
}

# _bpp_standalone -- set _BPP_R to the deterministic bootstrap interpreter
# (session-bootstrap.sh WANT_PYTHON; interpreter_env.standalone_python).
_bpp_standalone() {
    _bpp_norm "${HOME:-/}"
    if _bpp_is_windows; then
        _BPP_R="${_BPP_R%/}/.local/share/python-standalone/python/python.exe"
    else
        _BPP_R="${_BPP_R%/}/.local/bin/python3"
    fi
    return 0
}

# _bpp_resolve <dir> -- the resolution rule. Returns 0 with _BPP_R set to the
# interpreter, or 1 with _BPP_R empty for an opted-out tree.
_bpp_resolve() {
    local _start="${1:-}" _name=.venv _abs="" _home="" _d="" _c="" _n=0
    local _found="" _at_home=""
    [ -z "${ZSH_VERSION:-}" ] || emulate -L zsh
    _BPP_R=""
    if [ -n "${UV_PROJECT_ENVIRONMENT:-}" ]; then
        _name="$UV_PROJECT_ENVIRONMENT"
        if _bpp_is_windows; then
            _name="${_name//\\//}"
        fi
        case "$_name" in
            /*) _abs="$_name"; _name=.venv ;;
            [A-Za-z]:*) if _bpp_is_windows; then _abs="$_name"; _name=.venv; fi ;;
        esac
        while :; do
            case "$_name" in
                */) _name="${_name%/}" ;;
                ./*) _name="${_name#./}" ;;
                *) break ;;
            esac
        done
        [ -n "$_name" ] || _name=.venv
    fi
    if [ -n "${HOME:-}" ]; then
        _bpp_norm "$HOME"
        _home="$_BPP_R"
    fi
    _bpp_norm "$_start"
    _d="$_BPP_R"
    # The walk: the nearest directory that opts out or holds a venv decides.
    while [ "$_n" -lt 64 ]; do
        _n=$((_n + 1))
        _at_home=""
        if [ -n "$_home" ] && _bpp_same "$_d" "$_home"; then
            _at_home=1
        fi
        # Step 1: an opted-out project has no project interpreter. The home
        # directory's .claude/ holds the USER layer, which never opts out.
        if _bpp_opted_out "$_d" "$_at_home"; then
            _BPP_R=""
            return 1
        fi
        case "$_d" in
            */) _c="${_d}${_name}" ;;
            *) _c="${_d}/${_name}" ;;
        esac
        if [ -f "$_c/pyvenv.cfg" ] && _bpp_venv_python "$_c"; then
            _found="$_BPP_R"
            break
        fi
        if [ -n "$_at_home" ]; then
            break
        fi
        case "$_d" in
            /|[A-Za-z]:/) break ;;
        esac
        _d="${_d%/*}"
        case "$_d" in
            "") _d=/ ;;
            [A-Za-z]:) _d="$_d/" ;;
        esac
    done
    # Step 2: an activated venv is the user's explicit choice.
    if [ -n "${VIRTUAL_ENV:-}" ]; then
        _bpp_norm "$VIRTUAL_ENV"
        _bpp_venv_python "$_BPP_R" && return 0
    fi
    # Step 4: an absolute UV_PROJECT_ENVIRONMENT first, then the walk's venv.
    if [ -n "$_abs" ]; then
        _bpp_norm "$_abs"
        _c="$_BPP_R"
        if [ -f "$_c/pyvenv.cfg" ] && _bpp_venv_python "$_c"; then
            return 0
        fi
    fi
    if [ -n "$_found" ]; then
        _BPP_R="$_found"
        return 0
    fi
    # Step 5: the bootstrap interpreter.
    if [ -n "${BOOTSTRAP_PYTHON:-}" ]; then
        _bpp_norm "$BOOTSTRAP_PYTHON"
        return 0
    fi
    _bpp_standalone
    return 0
}

# Public: print the project interpreter for <dir> (default: $PWD); an
# opted-out tree prints an empty line and returns 1.
bootstrap_resolve_project_python() {
    if _bpp_resolve "${1:-$PWD}"; then
        printf '%s\n' "$_BPP_R"
        return 0
    fi
    printf '\n'
    return 1
}

# _bpp_cache_key -- set _BPP_R to the hook's cache key for the current state.
_bpp_cache_key() {
    local _v=""
    case "${UV_PROJECT_ENVIRONMENT:-}" in
        "") _v="$PWD/.venv" ;;
        /*|\\*|[A-Za-z]:*) _v="$UV_PROJECT_ENVIRONMENT" ;;
        *) _v="$PWD/$UV_PROJECT_ENVIRONMENT" ;;
    esac
    _BPP_R="$PWD|${VIRTUAL_ENV:-}|${UV_PROJECT_ENVIRONMENT:-}|"
    if [ -f "$_v/pyvenv.cfg" ]; then
        _BPP_R="${_BPP_R}v"
    fi
    if [ -n "${_BOOTSTRAP_PP_LAST:-}" ] && [ -x "$_BOOTSTRAP_PP_LAST" ]; then
        _BPP_R="${_BPP_R}x"
    fi
    return 0
}

# Prompt hook: keep BOOTSTRAP_PROJECT_PYTHON in step with $PWD.
_bootstrap_project_python_update() {
    local _rc=$?
    local _new=""
    [ -z "${ZSH_VERSION:-}" ] || emulate -L zsh
    if [ -n "${BOOTSTRAP_PROJECT_PYTHON:-}" ] \
        && [ "$BOOTSTRAP_PROJECT_PYTHON" != "${_BOOTSTRAP_PP_LAST:-}" ]; then
        return "$_rc"
    fi
    _bpp_cache_key
    if [ "$_BPP_R" = "${_BOOTSTRAP_PP_DIR:-}" ]; then
        return "$_rc"
    fi
    _new=""
    if _bpp_resolve "$PWD"; then
        _new="$_BPP_R"
    fi
    if [ -n "$_new" ] && [ -f "$_new" ] && [ -x "$_new" ]; then
        BOOTSTRAP_PROJECT_PYTHON="$_new"
        _BOOTSTRAP_PP_LAST="$_new"
        export BOOTSTRAP_PROJECT_PYTHON
    else
        # Opted out, or nothing runnable: drop the value this hook set. A
        # user-set value never reaches here (the check above returned).
        unset BOOTSTRAP_PROJECT_PYTHON
        _BOOTSTRAP_PP_LAST=""
    fi
    _bpp_cache_key
    _BOOTSTRAP_PP_DIR="$_BPP_R"
    export _BOOTSTRAP_PP_LAST _BOOTSTRAP_PP_DIR
    return "$_rc"
}

# Registration: interactive terminals only, never for the hook prelude.
if [ -z "${BOOTSTRAP_PP_NO_REGISTER:-}" ]; then
    case "$-" in
        *i*)
            if [ -z "${BOOTSTRAP_PYTHON:-}" ]; then
                _bpp_standalone
                if [ -f "$_BPP_R" ] && [ -x "$_BPP_R" ]; then
                    BOOTSTRAP_PYTHON="$_BPP_R"
                    export BOOTSTRAP_PYTHON
                fi
            fi
            if [ -n "${ZSH_VERSION:-}" ]; then
                if autoload -Uz add-zsh-hook 2>/dev/null; then
                    add-zsh-hook chpwd _bootstrap_project_python_update
                    add-zsh-hook precmd _bootstrap_project_python_update
                fi
            elif [ -n "${BASH_VERSION:-}" ]; then
                case "${PROMPT_COMMAND:-}" in
                    *_bootstrap_project_python_update*) ;;
                    *) PROMPT_COMMAND="${PROMPT_COMMAND:+$PROMPT_COMMAND$'\n'}_bootstrap_project_python_update" ;;
                esac
            fi
            _bootstrap_project_python_update
            ;;
    esac
fi
