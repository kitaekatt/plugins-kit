@echo off
rem secrets-kit -- Windows shim invoking the bundled CLI via standalone Python.
rem The passphrase verbs (unlock/init/rotate-identity) prompt on the console via
rem age itself; do not pipe or capture this command's stdio.
setlocal enabledelayedexpansion
set "SCRIPT_DIR=%~dp0"
set "PLUGIN_ROOT=%SCRIPT_DIR%.."
set "CLI=%PLUGIN_ROOT%\scripts\secrets_kit_cli.py"
set "PY=%USERPROFILE%\.local\share\python-standalone\python\python.exe"

rem BOOTSTRAP_PYTHON is accepted only as a fallback, and only when it names an
rem existing file under the standalone install directory -- never a bare PATH
rem lookup, which is how the Windows Store stub gets picked up.
set "_STANDALONE_DIR=%USERPROFILE%\.local\share\python-standalone\"
if not exist "%PY%" if defined BOOTSTRAP_PYTHON if exist "%BOOTSTRAP_PYTHON%" (
    rem Either slash direction, since the persisted value uses "/"; any case;
    rem and no ".." anywhere after the directory prefix.
    set "_BP=!BOOTSTRAP_PYTHON:/=\!"
    set "_TRIMMED=!_BP:*%_STANDALONE_DIR%=!"
    if /I "!_BP!"=="!_STANDALONE_DIR!!_TRIMMED!" if "!_TRIMMED:..=!"=="!_TRIMMED!" set "PY=%BOOTSTRAP_PYTHON%"
)

if not exist "%PY%" (
    where python.exe >nul 2>&1
    if errorlevel 1 (
        echo secrets-kit: no Python interpreter found 1>&2
        exit /b 1
    )
    set "PY=python.exe"
)

"%PY%" "%CLI%" %*
exit /b %ERRORLEVEL%
