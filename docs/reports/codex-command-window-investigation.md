# Transient Command Prompt windows: investigation

Date: 2026-09-28

Scope: Read-only diagnosis; no hooks, settings, tasks, scripts, or registry entries changed.

## Finding

The Codex-hook hypothesis is strongly supported as a configured launch path. The enabled global Claude Plugins Kit Codex hook runs on Codex `startup` and `resume`, and its Windows command explicitly starts `cmd.exe`. This makes it the leading candidate for a brief console window. Evidence proves the command is configured and enabled; it does not prove that Windows displayed the console during the user's sightings. No process-creation trace or popup timestamp was available, so confidence in it being the observed window is moderate.

- `C:\Users\truff\.codex\config.toml:296-298` records the global `session_start` hook as `enabled = true`.
- `C:\Users\truff\.codex\plugins\cache\codex-kit\claude-plugins-kit\0.1.6\hooks\hooks.json:3-10` matches `startup|resume` and sets `commandWindows` to `cmd.exe /d /c ...\scripts\launch.cmd`.
- The called `scripts\launch.cmd:1-4` runs `powershell.exe` without `-WindowStyle Hidden`; `scripts\launch.ps1:19` then runs the bridge Python process. The explicit `cmd.exe` wrapper is the clearest source for a Command Prompt flash.

There is also a project-local Codex hook at `D:\dev\plugins-kit\.codex\hooks.json:3-12`: on `startup|resume`, it invokes `C:\Users\truff\.local\bin\bootstrap.cmd codex-hook`. That wrapper (`C:\Users\truff\.local\bin\bootstrap.cmd:1-18`) launches Git Bash. The matching project hook state at `C:\Users\truff\.codex\config.toml:300-301` contains a trusted hash but no `enabled = true`, so the inspected state does not establish that Codex currently runs it.

## Other launch paths considered

- Codex also has a non-hook `notify` command in `C:\Users\truff\.codex\config.toml:10`, triggered on `turn-ended`. The executable exists. If flashes happen after turns rather than at startup/resume, this is another Codex-side candidate.
- `C:\Users\truff\.claude\settings.json:136-161` has an empty user `SessionStart` list, a Bash `PreToolUse` hook, and a Bash `Stop` hook. Enabled Claude plugins add Bash hooks: cached bootstrap `0.132.0` uses Bash for `SessionStart` and `UserPromptSubmit` (`C:\Users\truff\.claude\plugins\cache\plugins-kit\bootstrap\0.132.0\hooks\hooks.json:3-20`); its session script invokes PowerShell at `D:\dev\plugins-kit\plugins\bootstrap\hooks\sessionstart\session-bootstrap.sh:703-720`. These can start shell processes, but the inspected definitions do not explicitly use `start`, `Start-Process`, or a visible-window option.
- One custom scheduled task, `Ethernet Wedge Diagnostic Monitor`, is `Running` with `Hidden = False`; its last run was 2026-09-23 17:37. It uses PowerShell in S4U logon mode. Its script (`D:\Dev\env-config\scripts\perf\monitor-ethernet-wedge.ps1:200-256`) is a polling loop, and the scoped search found no `cmd.exe`, `Start-Process`, or child-shell launch. This is a weaker explanation for a window on the interactive desktop. Task Scheduler event-log querying was unavailable, so task timing could not be correlated.
- The Claude bootstrap log's last write was 2026-09-27 08:36 and does not provide Codex hook invocation timestamps. No relevant live process-creation history was available.

## Next diagnostic

If the window recurs, capture one occurrence with Process Monitor filtered to process creation for `cmd.exe`, `powershell.exe`, and `bash.exe`; record timestamp, parent process, and command line. Compare it with Codex startup/resume and turn-end times. That will identify the launcher without changing configuration.

## Verification

Referenced files and paths were rechecked while reading. No secrets were quoted. This unit changed only this report; no infrastructure settings were changed.
