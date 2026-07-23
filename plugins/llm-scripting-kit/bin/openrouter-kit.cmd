@echo off
rem openrouter-kit -- Windows shim invoking the bundled CLI via standalone Python.
setlocal
set "SCRIPT_DIR=%~dp0"
set "PLUGIN_ROOT=%SCRIPT_DIR%.."
set "CLI=%PLUGIN_ROOT%\scripts\openrouter_kit_cli.py"
set "PY=%USERPROFILE%\.local\share\python-standalone\python\python.exe"

if not exist "%PY%" (
    where python.exe >nul 2>&1
    if errorlevel 1 (
        echo openrouter-kit: no Python interpreter found 1>&2
        exit /b 1
    )
    set "PY=python.exe"
)

"%PY%" "%CLI%" %*
exit /b %ERRORLEVEL%
