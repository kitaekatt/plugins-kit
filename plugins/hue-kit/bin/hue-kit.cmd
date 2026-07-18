@echo off
rem hue-kit -- Windows shim launching the bundled CLI. The CLI re-execs itself
rem under the plugin's bootstrap-provisioned venv, so this only needs any Python.
setlocal
set "SCRIPT_DIR=%~dp0"
set "PLUGIN_ROOT=%SCRIPT_DIR%.."
set "CLI=%PLUGIN_ROOT%\scripts\hue_kit_cli.py"
rem Prefer the standalone Python bootstrap installs (avoids the WindowsApps
rem Store-alias stub that `where python.exe` otherwise resolves to). The CLI
rem re-execs into the plugin venv itself, so this only needs a working launcher.
set "PY=%USERPROFILE%\.local\share\python-standalone\python\python.exe"

if not exist "%PY%" (
    where python.exe >nul 2>&1
    if errorlevel 1 (
        echo hue-kit: no Python interpreter found 1>&2
        exit /b 1
    )
    set "PY=python.exe"
)

"%PY%" "%CLI%" %*
exit /b %ERRORLEVEL%
