@echo off
rem llm-kit -- Windows shim invoking the bundled CLI.
rem
rem Interpreter preference:
rem   1. the plugin venv bootstrap provisions -- it carries every extra the CLI
rem      reaches (PyYAML, the OpenAI SDK, psutil, the front door's web stack)
rem      and the shared-lib link (bootstrap_lib).
rem   2. the standalone Python bootstrap installs, only through BOOTSTRAP_PYTHON
rem      and only when it resolves inside that install directory.
rem   3. anything on PATH.
rem The target is bin\llm_kit_entrypoint.py rather than llm_kit.cli directly. It
rem adds nothing to sys.path: the library that runs is the katsoft-llm-kit package
rem installed in the chosen interpreter's environment (the plugin venv). An
rem interpreter that has no katsoft-llm-kit installed fails at the import.
setlocal enabledelayedexpansion
set "SCRIPT_DIR=%~dp0"
set "PLUGIN_ROOT=%SCRIPT_DIR%.."

rem Under claudx (CLAUDE_PLUGIN_TEST) the working copy is the plugin under test, so a
rem shim running from the plugin cache would run the INSTALLED release. Refuse it.
rem Same scope as bootstrap_lib.env_var_check.cache_root_refusal: only the cache of
rem CLAUDE_PLUGIN_TEST_MARKETPLACE is refused (every cache path when it is unset).
for %%I in ("%SCRIPT_DIR%..") do set "_PR=%%~fI\"
set "_CACHE_NEEDLE=\plugins\cache\"
if defined CLAUDE_PLUGIN_TEST_MARKETPLACE set "_CACHE_NEEDLE=\plugins\cache\%CLAUDE_PLUGIN_TEST_MARKETPLACE%\"
if defined CLAUDE_PLUGIN_TEST if not "!_PR:%_CACHE_NEEDLE%=!"=="!_PR!" goto :cached_copy
set "ENTRYPOINT=%PLUGIN_ROOT%\bin\llm_kit_entrypoint.py"
set "DATA_DIR=%USERPROFILE%\.claude\plugins\data\plugins-kit\llm-kit"
set "VENV_PY=%DATA_DIR%\.venv\Scripts\python.exe"
rem CLAUDE_BOOTSTRAP_DATA_ROOT is set only for a session that redirects bootstrap's
rem data tree (scripts\claude_plugin_test.py). When it is set, this plugin's venv is
rem the one under that tree and nothing else: a missing redirected venv exits 2
rem rather than falling back to the production venv, which holds the published
rem release. When it is unset the chain below is unchanged.
if defined CLAUDE_BOOTSTRAP_DATA_ROOT set "VENV_PY=%CLAUDE_BOOTSTRAP_DATA_ROOT%\plugins-kit\llm-kit\.venv\Scripts\python.exe"
if defined CLAUDE_BOOTSTRAP_DATA_ROOT if not exist "%VENV_PY%" goto :redirect_missing

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
rem The no-interpreter refusal is a goto, not an "exit /b 1" inside the block: cmd reports
rem exit code 0 for an exit /b issued from a block nested in another block, which would
rem turn this refusal into a success.
if not defined PY (
    where python.exe >nul 2>&1
    if errorlevel 1 goto :no_python
    set "PY=python.exe"
)

"%PY%" "%ENTRYPOINT%" %*
exit /b %ERRORLEVEL%

:cached_copy
echo llm-kit: refusing to run the cached copy at %_PR%: CLAUDE_PLUGIN_TEST is set, so this session tests the working copy and the cached copy is the INSTALLED release. Run the working copy's bin\llm-kit.cmd. 1>&2
exit /b 2

:redirect_missing
echo llm-kit: CLAUDE_BOOTSTRAP_DATA_ROOT redirects bootstrap's data tree, but the redirected plugin interpreter is missing at %VENV_PY%; refusing to fall back to the production venv. 1>&2
exit /b 2

:no_python
echo llm-kit: no Python interpreter found 1>&2
exit /b 1
