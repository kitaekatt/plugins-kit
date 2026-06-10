@echo off
:: UE Python Script Runner -- wrapper that runs ue_runner.py under the
:: bootstrap-provisioned plugin venv (the one interpreter policy: that venv
:: carries upyrc + pyyaml; `uv run --with ...` built a throwaway overlay that
:: ignored it). The venv path is stable across plugin versions.
set "_UEK_PY=%USERPROFILE%\.claude\plugins\data\plugins-kit\unreal-kit\.venv\Scripts\python.exe"
if not exist "%_UEK_PY%" (
    echo [ue-runner] unreal-kit plugin venv not found: %_UEK_PY% 1>&2
    echo [ue-runner] Install/enable the plugins-kit:bootstrap plugin and start a new session so it can provision unreal-kit, then retry. 1>&2
    exit /b 3
)
"%_UEK_PY%" "%~dp0ue_runner.py" %*
