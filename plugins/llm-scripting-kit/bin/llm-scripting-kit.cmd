@echo off
rem llm-scripting-kit -- Windows shim invoking the bundled CLI.
rem
rem Interpreter preference:
rem   1. the plugin venv bootstrap provisions -- it has PyYAML, which the CLI
rem      needs to read the layered config.yaml (bootstrap_lib.config_resolve).
rem   2. the standalone Python bootstrap installs -- no third-party packages, so
rem      the CLI degrades to the shipped model baseline (it warns and continues).
rem   3. anything on PATH.
setlocal enabledelayedexpansion
set "SCRIPT_DIR=%~dp0"
set "PLUGIN_ROOT=%SCRIPT_DIR%.."
set "CLI=%PLUGIN_ROOT%\scripts\llm_scripting_kit_cli.py"
set "DATA_DIR=%USERPROFILE%\.claude\plugins\data\plugins-kit\llm-scripting-kit"
set "VENV_PY=%DATA_DIR%\.venv\Scripts\python.exe"
set "STANDALONE_PY=%USERPROFILE%\.local\share\python-standalone\python\python.exe"

set "PY="
if exist "%VENV_PY%" set "PY=%VENV_PY%"
if not defined PY if exist "%STANDALONE_PY%" set "PY=%STANDALONE_PY%"
rem BOOTSTRAP_PYTHON is accepted only as a fallback, and only when it names an
rem existing file under the standalone install directory -- never a bare PATH
rem lookup, which is how the Windows Store stub gets picked up.
set "_STANDALONE_DIR=%USERPROFILE%\.local\share\python-standalone\"
if not defined PY if defined BOOTSTRAP_PYTHON if exist "%BOOTSTRAP_PYTHON%" (
    rem Either slash direction, since the persisted value uses "/"; any case;
    rem and no ".." anywhere after the directory prefix.
    set "_BP=!BOOTSTRAP_PYTHON:/=\!"
    set "_TRIMMED=!_BP:*%_STANDALONE_DIR%=!"
    if /I "!_BP!"=="!_STANDALONE_DIR!!_TRIMMED!" if "!_TRIMMED:..=!"=="!_TRIMMED!" set "PY=%BOOTSTRAP_PYTHON%"
)
if not defined PY (
    where python.exe >nul 2>&1
    if errorlevel 1 (
        echo llm-scripting-kit: no Python interpreter found 1>&2
        exit /b 1
    )
    set "PY=python.exe"
)

"%PY%" "%CLI%" %*
exit /b %ERRORLEVEL%
