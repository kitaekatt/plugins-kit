@echo off
rem secrets-kit -- Windows shim invoking the bundled CLI via standalone Python.
rem The passphrase verbs (unlock/init/rotate-identity) prompt on the console via
rem age itself; do not pipe or capture this command's stdio.
setlocal
set "SCRIPT_DIR=%~dp0"
set "PLUGIN_ROOT=%SCRIPT_DIR%.."
set "CLI=%PLUGIN_ROOT%\scripts\secrets_kit_cli.py"
set "PY=%USERPROFILE%\.local\share\python-standalone\python\python.exe"

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
