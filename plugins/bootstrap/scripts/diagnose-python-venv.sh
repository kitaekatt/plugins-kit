#!/usr/bin/env bash
# Diagnose Python venv issues — particularly the Windows junction/mount point problem.
# Run from any directory. No changes are made to the system.

set -euo pipefail

echo "=== Python venv diagnostics ==="
echo

# 1. Check uv installation
echo "--- uv ---"
if command -v uv &>/dev/null; then
    echo "uv: $(command -v uv)"
    echo "version: $(uv --version 2>&1)"
else
    echo "uv: NOT FOUND"
    echo "Install uv first: https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
fi
echo

# 2. List installed Python versions
echo "--- Installed Python versions ---"
uv python list --only-installed 2>&1
echo

# 3. Check uv python directory for junctions/symlinks
echo "--- uv python directory ---"
UV_PYTHON_DIR="${APPDATA:-$HOME/.local/share}/uv/python"
if [ -d "$UV_PYTHON_DIR" ]; then
    echo "Location: $UV_PYTHON_DIR"
    ls -la "$UV_PYTHON_DIR/" 2>&1 || true

    # On Windows, check for junction issues
    if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || -n "${WINDIR:-}" ]]; then
        echo
        echo "--- Windows junction check ---"
        for dir in "$UV_PYTHON_DIR"/cpython-3.1[3-9]-windows-*; do
            [ -d "$dir" ] || continue
            basename="$(basename "$dir")"
            echo
            echo "Checking: $basename"
            # Test if the directory is a junction
            win_path=$(cygpath -w "$dir" 2>/dev/null || echo "$dir")
            cmd.exe /c "dir /AL \"$(dirname "$win_path")\"" 2>&1 | grep -i "$basename" || echo "  (not a junction)"
            # Test if python.exe is accessible through this path
            python_exe="$dir/python.exe"
            if [ -f "$python_exe" ]; then
                echo "  python.exe exists: yes"
                # Try running it
                if "$python_exe" -c "print('ok')" 2>/dev/null; then
                    echo "  python.exe runs: yes"
                else
                    echo "  python.exe runs: FAILED"
                    # Try via cmd.exe to bypass bash junction issues
                    win_python=$(cygpath -w "$python_exe" 2>/dev/null || echo "$python_exe")
                    result=$(cmd.exe /c "\"$win_python\" -c \"print('ok')\"" 2>&1) || true
                    echo "  via cmd.exe: $result"
                fi
            else
                echo "  python.exe exists: no"
            fi
        done
    fi
else
    echo "Not found at: $UV_PYTHON_DIR"
fi
echo

# 4. Check bootstrap venv
echo "--- Bootstrap venv ---"
VENV_DIR="$HOME/.claude/plugins/data/plugins-kit/bootstrap/.venv"
if [ -d "$VENV_DIR" ]; then
    echo "Location: $VENV_DIR"
    # Check pyvenv.cfg
    if [ -f "$VENV_DIR/pyvenv.cfg" ]; then
        echo
        echo "pyvenv.cfg:"
        cat "$VENV_DIR/pyvenv.cfg"
    fi
    echo
    # Find and test python
    PYTHON_BIN=""
    if [ -f "$VENV_DIR/Scripts/python.exe" ]; then
        PYTHON_BIN="$VENV_DIR/Scripts/python.exe"
    elif [ -f "$VENV_DIR/bin/python" ]; then
        PYTHON_BIN="$VENV_DIR/bin/python"
    fi
    if [ -n "$PYTHON_BIN" ]; then
        echo "Python binary: $PYTHON_BIN"
        echo -n "  runs: "
        if "$PYTHON_BIN" -c "import sys; print(f'Python {sys.version}')" 2>&1; then
            echo -n "  import yaml: "
            "$PYTHON_BIN" -c "import yaml; print(yaml.__file__)" 2>&1 || echo "FAILED"
        else
            echo "FAILED"
        fi
    else
        echo "Python binary: NOT FOUND"
    fi
else
    echo "Not found at: $VENV_DIR"
fi
echo

# 5. Check unreal-kit venv
echo "--- Unreal-kit venv ---"
UE_VENV_DIR="$HOME/.claude/plugins/data/plugins-kit/unreal-kit/.venv"
if [ -d "$UE_VENV_DIR" ]; then
    echo "Location: $UE_VENV_DIR"
    if [ -f "$UE_VENV_DIR/pyvenv.cfg" ]; then
        echo
        echo "pyvenv.cfg:"
        cat "$UE_VENV_DIR/pyvenv.cfg"
    fi
    echo
    PYTHON_BIN=""
    if [ -f "$UE_VENV_DIR/Scripts/python.exe" ]; then
        PYTHON_BIN="$UE_VENV_DIR/Scripts/python.exe"
    elif [ -f "$UE_VENV_DIR/bin/python" ]; then
        PYTHON_BIN="$UE_VENV_DIR/bin/python"
    fi
    if [ -n "$PYTHON_BIN" ]; then
        echo "Python binary: $PYTHON_BIN"
        echo -n "  runs: "
        "$PYTHON_BIN" -c "import sys; print(f'Python {sys.version}')" 2>&1 || echo "FAILED"
    else
        echo "Python binary: NOT FOUND"
    fi
else
    echo "Not found at: $UE_VENV_DIR"
fi
echo

# 6. Check installed plugins and versions
echo "--- Installed plugins ---"
REGISTRY="$HOME/.claude/plugins/installed_plugins.json"
if [ -f "$REGISTRY" ]; then
    # Deterministic standalone path first; BOOTSTRAP_PYTHON accepted only as a
    # fallback whose realpath is inside the standalone install directory; then
    # whatever is on PATH. A plain interpreter invocation has no side effect on
    # any directory's own project venv, so this keeps line 3's promise without
    # needing `uv run` at all.
    _OS="$(uname -s 2>/dev/null || echo unknown)"
    if [[ "$_OS" == MINGW* ]] || [[ "$_OS" == MSYS* ]] || [[ "$_OS" == CYGWIN* ]]; then
        _DIAG_PY="${HOME}/.local/share/python-standalone/python/python.exe"
    else
        _DIAG_PY="${HOME}/.local/share/python-standalone/python/bin/python3"
    fi
    if [ ! -x "$_DIAG_PY" ] && [ -n "${BOOTSTRAP_PYTHON:-}" ] && [ -x "$BOOTSTRAP_PYTHON" ]; then
        case "$(cd "$(dirname "$BOOTSTRAP_PYTHON")" 2>/dev/null && pwd -P)" in
            "$(cd "${HOME}/.local/share/python-standalone" 2>/dev/null && pwd -P)"/*) _DIAG_PY="$BOOTSTRAP_PYTHON" ;;
        esac
    fi
    [ -x "$_DIAG_PY" ] || _DIAG_PY="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)"
    if [ -n "$_DIAG_PY" ]; then
        "$_DIAG_PY" -c "
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
for ref, entries in data.get('plugins', {}).items():
    for e in entries:
        scope = e.get('scope', '?')
        version = e.get('version', '?')
        path = e.get('installPath', '?')
        print(f'  {ref} scope={scope} version={version}')
        print(f'    path: {path}')
" "$REGISTRY" 2>&1 || echo "  (failed to parse registry)"
    else
        echo "  (no Python interpreter found to parse registry)"
    fi
else
    echo "  Registry not found at: $REGISTRY"
fi
echo

# 7. Check cached plugin versions
echo "--- Cached plugin versions ---"
CACHE_DIR="$HOME/.claude/plugins/cache"
if [ -d "$CACHE_DIR" ]; then
    for marketplace in "$CACHE_DIR"/*/; do
        [ -d "$marketplace" ] || continue
        mkt_name=$(basename "$marketplace")
        for plugin_dir in "$marketplace"*/; do
            [ -d "$plugin_dir" ] || continue
            plugin_name=$(basename "$plugin_dir")
            for version_dir in "$plugin_dir"*/; do
                [ -d "$version_dir" ] || continue
                ver=$(basename "$version_dir")
                pj="$version_dir/.claude-plugin/plugin.json"
                if [ -f "$pj" ]; then
                    echo "  $mkt_name:$plugin_name cached=$ver"
                fi
            done
        done
    done
else
    echo "  Cache not found at: $CACHE_DIR"
fi
echo

echo "=== Done ==="
