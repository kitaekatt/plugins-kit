# lever-cmd-shim.sh -- cmd.exe and PowerShell shims for the ~/.local/bin levers.
#
# Sourced by session-bootstrap.sh on Windows (Git Bash / MSYS). It defines
# functions and runs nothing.
#
# The problem. On Windows the levers (bootstrap, bootstrap-reset-cooldown,
# env-reset-cooldown) are extensionless bash scripts copied into ~/.local/bin.
# Git Bash runs them by name. cmd.exe never matches an extensionless file, so
# there the command is not found. Windows PowerShell 5.1 resolves `bootstrap`
# to that file as an Application and does not run it as a bash script: the
# command returns at once, prints nothing, and does nothing.
#
# The fix is a `<lever>.cmd` twin in the same directory. When both files are
# in one directory, cmd.exe and Windows PowerShell 5.1 run the .cmd (measured
# 2026-09-21: PowerShell's Get-Command lists foo.cmd before foo). There is NO
# .ps1 twin: PowerShell prefers a .ps1 to a .cmd, and under the default
# Restricted execution policy it then refuses the .ps1 instead of falling back
# to the .cmd. That would break the command again.
#
# Which bash. The shim embeds an ABSOLUTE bash path, resolved here, inside the
# hook, with `command -v bash`. That is the shell form of the engine's single
# Windows bash lookup, bootstrap_lib.tool_check.resolve_bash()
# (shutil.which("bash")), and it finds the Git for Windows bash that Claude
# Code runs this hook under. The path is embedded at render time for the same
# reason fix_queue embeds one in the elevation queue: a bare `bash` lookup from
# cmd.exe or PowerShell can resolve WSL's C:\Windows\System32\bash.exe.
#
# The shim also puts that bash's own directory first on PATH, as
# fix_runner._child_env does for the elevated runner. A bash started from
# cmd.exe or PowerShell is not a login shell, so without it the lever finds no
# uname, sed, or dirname, and `sort` resolves to the Windows sort.exe. With it,
# the Python the lever starts also resolves Git's bash through resolve_bash().

# bootstrap_lever_bash_win -- print the Windows path of the bash the shims run.
# Returns 1, printing nothing, when no usable Git for Windows bash resolves:
# no bash on PATH, no cygpath, a relative answer, or WSL's System32 launcher
# or a WindowsApps stub.
bootstrap_lever_bash_win() {
    local posix win lower
    posix="$(command -v bash 2>/dev/null)" || return 1
    case "$posix" in
        /*) ;;
        *) return 1 ;;
    esac
    win="$(cygpath -w "$posix" 2>/dev/null)" || return 1
    case "$win" in
        [A-Za-z]:\\*) ;;
        *) return 1 ;;
    esac
    lower="$(printf '%s' "$win" | tr '[:upper:]' '[:lower:]')"
    case "$lower" in
        *\\system32\\*|*\\windowsapps\\*) return 1 ;;
    esac
    printf '%s\n' "$win"
}

# bootstrap_lever_cmd_shim <lever> <bash_win> -- print the .cmd shim for
# <lever>, with CRLF line endings, running the lever beside it under
# <bash_win>. The shim forwards every argument and returns bash's exit code.
# If <bash_win> is missing when the shim runs, it prints why and exits 127.
bootstrap_lever_cmd_shim() {
    local lever="$1" bash_win="$2" bs='\' bash_dir
    # cmd.exe expands %NAME% even inside quotes, so a literal % is doubled.
    bash_win="${bash_win//%/%%}"
    bash_dir="${bash_win%"$bs"*}"
    printf '%s\r\n' \
        '@echo off' \
        "rem $lever -- cmd.exe and PowerShell entry point for the extensionless" \
        'rem bash lever of the same name in this directory, which neither shell' \
        'rem can run. The bootstrap SessionStart hook writes this file and' \
        'rem rewrites it when its content changes; do not edit it.' \
        'rem The bash path is absolute on purpose: a bare bash lookup from cmd.exe' \
        'rem or PowerShell can resolve WSL bash in C:\Windows\System32.' \
        'setlocal' \
        "set \"_BOOTSTRAP_BASH=$bash_win\"" \
        'if exist "%_BOOTSTRAP_BASH%" goto run' \
        ">&2 echo $lever: Git for Windows bash not found at \"%_BOOTSTRAP_BASH%\"." \
        ">&2 echo Start a Claude Code session so bootstrap rewrites this file, or run $lever from Git Bash." \
        'exit /b 127' \
        ':run' \
        "set \"PATH=$bash_dir;%PATH%\"" \
        "set \"_BOOTSTRAP_LEVER=%~dp0$lever\"" \
        '"%_BOOTSTRAP_BASH%" "%_BOOTSTRAP_LEVER:\=/%" %*' \
        'exit /b %ERRORLEVEL%'
}

# bootstrap_install_lever_cmd_shim <bin_dir> <lever> <bash_win> -- write
# <bin_dir>/<lever>.cmd when its content differs from the rendered shim. This
# is the same compare-then-replace refresh the extensionless lever copy gets,
# so a shim from an older bootstrap, or one that embeds a bash path that has
# since moved, is replaced on the next session start. Prints "wrote" after a
# write and nothing when the file is already current. Returns 1 if the file
# cannot be written.
bootstrap_install_lever_cmd_shim() {
    local bin_dir="$1" lever="$2" bash_win="$3"
    local dst="$bin_dir/$lever.cmd"
    local tmp="$bin_dir/.$lever.cmd.$$"
    if ! bootstrap_lever_cmd_shim "$lever" "$bash_win" > "$tmp" 2>/dev/null; then
        rm -f "$tmp" 2>/dev/null
        return 1
    fi
    if cmp -s "$tmp" "$dst" 2>/dev/null; then
        rm -f "$tmp" 2>/dev/null
        return 0
    fi
    if ! mv -f "$tmp" "$dst" 2>/dev/null; then
        rm -f "$tmp" 2>/dev/null
        return 1
    fi
    printf 'wrote\n'
}
