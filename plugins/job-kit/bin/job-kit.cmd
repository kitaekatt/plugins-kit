@echo off
rem job-kit -- Windows shim invoking the bundled CLI.
rem
rem Interpreter preference:
rem   1. the plugin venv bootstrap provisions -- it has the shared-lib links
rem      (llm_scripting_kit) job-kit requires at import time.
rem   2. anything on PATH.
rem The target is lib\job_kit_entrypoint.py rather than job_kit.cli directly:
rem it re-execs itself under the plugin venv (a no-op once already there), so
rem even the PATH fallback below gets a correct, actionable diagnostic
rem instead of a raw ImportError when the shared libs are not linked into
rem that interpreter.
setlocal enabledelayedexpansion
set "SCRIPT_DIR=%~dp0"
set "PLUGIN_ROOT=%SCRIPT_DIR%.."
set "ENTRYPOINT=%PLUGIN_ROOT%\lib\job_kit_entrypoint.py"
set "DATA_DIR=%USERPROFILE%\.claude\plugins\data\plugins-kit\job-kit"
set "VENV_PY=%DATA_DIR%\.venv\Scripts\python.exe"

set "PY="
if exist "%VENV_PY%" set "PY=%VENV_PY%"
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
        echo job-kit: no Python interpreter found 1>&2
        exit /b 1
    )
    set "PY=python.exe"
)

"%PY%" "%ENTRYPOINT%" %*
exit /b %ERRORLEVEL%
