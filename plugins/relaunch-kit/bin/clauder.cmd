@echo off
rem clauder -- Windows shim: run Claude Code, and run it again in this terminal
rem when the session asks to be restarted. The launcher is lib\relaunch.py.
rem
rem ARGUMENT FORWARDING BOUNDARY (exact; golden-tested in
rem tests/relaunch-kit/test_launcher.py):
rem   * Delayed expansion stays off for the whole file, so ! and ^ inside
rem     double quotes reach the launcher unaltered. No variable, shift or
rem     numbered parameter ever touches the arguments.
rem   * The arguments are forwarded ONCE, by the last command, as the
rem     all-arguments token (percent, star), verbatim. That token appears on no
rem     other line, comments included: cmd expands it even inside a comment.
rem   * cmd.exe parses the caller's line BEFORE this file runs, and a batch file
rem     cannot inspect the arguments without expanding them into a line cmd then parses. So
rem     this file refuses nothing itself. Characters & | < > ^ ( ) are literal
rem     only inside double quotes; a bare one is consumed by cmd (it splits or
rem     drops text) and no .cmd can see or refuse it. Quote such an argument.
rem   * After cmd, lib\relaunch.py re-reads the raw command line, because this
rem     file sets RELAUNCH_KIT_SHIM=cmd. It refuses, loudly and with the
rem     argument named, any argument that cmd and the program parser would read
rem     differently: a double quote inside an argument, an unterminated quote,
rem     or a backslash run before the closing quote. It also refuses when the
rem     arguments it received differ from the raw text.
rem   * Exact round trip for every character needs a native shim; this file is
rem     the best a batch file can do.
rem
rem Interpreter order (root CLAUDE.md "Scoped exception: Python CLI launcher
rem shims"): the standalone Python bootstrap installs; BOOTSTRAP_PYTHON only
rem when it names an existing file under the standalone directory; python.exe on
rem PATH. No labels or goto: the file is stored with LF line endings.
setlocal DisableDelayedExpansion
set "RK_BIN=%~dp0"
set "RK_LAUNCHER=%~dp0..\lib\relaunch.py"

rem Under claudx (CLAUDE_PLUGIN_TEST) the working copy is the plugin under test, so the
rem cached copy and the data-directory copy would run the INSTALLED release. Refuse both.
rem Same scope as bootstrap_lib.env_var_check.cache_root_refusal: only the cache of
rem CLAUDE_PLUGIN_TEST_MARKETPLACE is refused (every cache path when it is unset).
set "RK_BP="
set "RK_REST="
set "RK_NEEDLE=\plugins\cache\"
if defined CLAUDE_PLUGIN_TEST_MARKETPLACE set "RK_NEEDLE=\plugins\cache\%CLAUDE_PLUGIN_TEST_MARKETPLACE%\"
call set "RK_STRIPPED=%%RK_BIN:%RK_NEEDLE%=%%"
if defined CLAUDE_PLUGIN_TEST if not "%RK_STRIPPED%"=="%RK_BIN%" (echo clauder: refusing to run the cached copy: CLAUDE_PLUGIN_TEST is set, so this session tests the working copy and the cached copy is the INSTALLED release. Run the working copy's bin\clauder.cmd. 1>&2 & exit /b 2)
call set "RK_STRIPPED=%%RK_BIN:\plugins\data\=%%"
if defined CLAUDE_PLUGIN_TEST if not "%RK_STRIPPED%"=="%RK_BIN%" (echo clauder: refusing to run the cached copy: CLAUDE_PLUGIN_TEST is set, so this session tests the working copy and the data-directory copy is the INSTALLED release. Run the working copy's bin\clauder.cmd. 1>&2 & exit /b 2)

if not exist "%RK_LAUNCHER%" (echo clauder: launcher not found 1>&2 & exit /b 2)

set "RK_STANDALONE=%USERPROFILE%\.local\share\python-standalone\"
set "RK_PY=%RK_STANDALONE%python\python.exe"
if not exist "%RK_PY%" set "RK_PY="
rem BOOTSTRAP_PYTHON counts only as a file inside the standalone directory: %%~fI makes it
rem absolute with either slash direction and no "..", then the standalone prefix is
rem stripped (case-insensitively) and must reassemble to the same path.
if not defined RK_PY if defined BOOTSTRAP_PYTHON if exist "%BOOTSTRAP_PYTHON%" for %%I in ("%BOOTSTRAP_PYTHON%") do set "RK_BP=%%~fI"
if defined RK_BP call set "RK_REST=%%RK_BP:%RK_STANDALONE%=%%"
if defined RK_BP if /I "%RK_STANDALONE%%RK_REST%"=="%RK_BP%" set "RK_PY=%RK_BP%"
if not defined RK_PY where python.exe >nul 2>&1
if not defined RK_PY if not errorlevel 1 set "RK_PY=python.exe"
if not defined RK_PY (echo clauder: no Python interpreter found 1>&2 & exit /b 1)

set "RELAUNCH_KIT_SHIM=cmd"
"%RK_PY%" "%RK_LAUNCHER%" %*
exit /b %ERRORLEVEL%
