@echo off
:: UE Python Script Runner -- wrapper that runs ue_runner.py under the
:: bootstrap-provisioned plugin venv (the one interpreter policy: that venv
:: carries upyrc + pyyaml; `uv run --with ...` built a throwaway overlay that
:: ignored it). The venv path is stable across plugin versions.
setlocal enabledelayedexpansion
set "_UEK_PY=%USERPROFILE%\.claude\plugins\data\plugins-kit\unreal-kit\.venv\Scripts\python.exe"
:: BOOTSTRAP_PYTHON is accepted only as a fallback, and only when it names an
:: existing file under the standalone install directory -- ue_runner.py itself
:: re-execs into the plugin venv, so this only needs a working launcher when
:: the venv path above is not yet provisioned.
set "_STANDALONE_DIR=%USERPROFILE%\.local\share\python-standalone\"
if not exist "%_UEK_PY%" if defined BOOTSTRAP_PYTHON if exist "%BOOTSTRAP_PYTHON%" (
    rem Either slash direction, since the persisted value uses "/"; any case;
    rem and no ".." anywhere after the directory prefix.
    set "_BP=!BOOTSTRAP_PYTHON:/=\!"
    set "_TRIMMED=!_BP:*%_STANDALONE_DIR%=!"
    if /I "!_BP!"=="!_STANDALONE_DIR!!_TRIMMED!" if "!_TRIMMED:..=!"=="!_TRIMMED!" set "_UEK_PY=%BOOTSTRAP_PYTHON%"
)
if not exist "%_UEK_PY%" (
    echo [ue-runner] unreal-kit plugin venv not found: %_UEK_PY% 1>&2
    echo [ue-runner] Install/enable the plugins-kit:bootstrap plugin and start a new session so it can provision unreal-kit, then retry. 1>&2
    exit /b 3
)
"%_UEK_PY%" "%~dp0ue_runner.py" %*
