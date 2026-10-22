<#
.SYNOPSIS
    Windows verification of relaunch-kit: the clauder launcher in every shell.

.DESCRIPTION
    Run it with ONE command, from any directory, in a Windows Terminal tab or a
    console window (not from a pipe, an IDE terminal that redirects output, or
    mintty):

        powershell -NoProfile -ExecutionPolicy Bypass -File "<checkout>\scripts\relaunch_verify_windows.ps1"

    The script needs no admin rights and makes no model calls: the chains use
    only the /relaunch-check command, which runs three hops with no model call.
    It resolves every path from its own location (or -RepoRoot), prints a
    PASS / FAIL / COULD-NOT-RUN table, saves the report to
    %TEMP%\relaunch-verify-<timestamp>.txt, and exits 1 on any FAIL (or when
    nothing could be verified at all). Scratch files and check logs are removed
    in a finally block.

    Phase A  the working copy: bin\clauder.cmd (bin\clauder for Git Bash) with
             --plugin-dir, one 3-hop /relaunch-check chain per shell present.
    Phase B  the installed command: plain "clauder" by name, in fresh child
             shells whose Path is rebuilt from the registry (only when
             relaunch-kit is installed).
    Phase C  two console windows running a chain at the same time.
    Phase D  argument forwarding and exit codes, as implemented: the cmd.exe
             shim boundary, the .cmd target refusals, the not-a-terminal
             pass-through. Phase D runs without a terminal.

    The first run of claude in the repo root may ask to trust the folder or to
    log in. Answer it once and run the script again; a chain that is blocked
    that way is reported as COULD-NOT-RUN, not FAIL.

.PARAMETER RepoRoot
    The plugins-kit checkout. Default: the parent of this script's directory.

.PARAMETER TimeoutSec
    Seconds one interactive chain may take before it is killed. Default 120.
#>
[CmdletBinding()]
param(
    [string]$RepoRoot,
    [int]$TimeoutSec = 120
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:Results = New-Object System.Collections.ArrayList
$script:Notes = New-Object System.Collections.ArrayList
$script:Live = New-Object System.Collections.ArrayList
$script:LogsToRemove = New-Object System.Collections.ArrayList
$script:SavedEnv = @{}
$script:Work = $null
$script:Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'

# --- reporting ---------------------------------------------------------------

function Add-Result {
    param([string]$Phase, [string]$Shell, [string]$Check, [string]$Status, [string]$Detail = '')
    [void]$script:Results.Add([pscustomobject]@{
        Phase = $Phase; Shell = $Shell; Check = $Check; Status = $Status; Detail = $Detail
    })
    $color = 'Yellow'
    if ($Status -eq 'PASS') { $color = 'Green' }
    if ($Status -eq 'FAIL') { $color = 'Red' }
    Write-Host ('  [{0}] {1} / {2} / {3}' -f $Status, $Phase, $Shell, $Check) -ForegroundColor $color
}

function Add-Note {
    param([string]$Text)
    [void]$script:Notes.Add($Text)
    Write-Host $Text
}

# --- small helpers -----------------------------------------------------------

function Test-FileSafe {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
    try { return (Test-Path -LiteralPath $Path -PathType Leaf) } catch { return $false }
}

function Test-DirSafe {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
    try { return (Test-Path -LiteralPath $Path -PathType Container) } catch { return $false }
}

function Get-Prop {
    param($Object, [string]$Name)
    $p = $Object.PSObject.Properties[$Name]
    if ($null -eq $p) { return $null }
    return $p.Value
}

function Write-TextFile {
    param([string]$Path, [string]$Text, [bool]$Bom)
    $enc = New-Object System.Text.UTF8Encoding $Bom
    [System.IO.File]::WriteAllText($Path, $Text, $enc)
}

function ConvertTo-PsLiteral {
    param([string]$Text)
    return "'" + $Text.Replace("'", "''") + "'"
}

function ConvertTo-BashLiteral {
    param([string]$Text)
    return "'" + $Text.Replace("'", "'\''") + "'"
}

function ConvertTo-CmdArg {
    param([string]$Text)
    if ($Text -match '[^A-Za-z0-9_.:\\/=,-]') { return '"' + $Text + '"' }
    return $Text
}

function New-RunId {
    param([string]$Tag)
    return 'rkv' + [Guid]::NewGuid().ToString('N').Substring(0, 8) + '-' + $Tag
}

function Find-App {
    param([string]$Name)
    $c = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $c) { return $null }
    return $c.Source
}

function Find-GitBash {
    $cands = New-Object System.Collections.ArrayList
    $git = Find-App 'git.exe'
    if ($git) {
        $dir = Split-Path -Parent $git
        for ($i = 0; $i -lt 3; $i++) {
            [void]$cands.Add((Join-Path $dir 'bin\bash.exe'))
            [void]$cands.Add((Join-Path $dir 'usr\bin\bash.exe'))
            $parent = Split-Path -Parent $dir
            if ([string]::IsNullOrEmpty($parent)) { break }
            $dir = $parent
        }
    }
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
        if ($base) {
            [void]$cands.Add((Join-Path $base 'Git\bin\bash.exe'))
            [void]$cands.Add((Join-Path $base 'Programs\Git\bin\bash.exe'))
        }
    }
    foreach ($c in $cands) { if (Test-FileSafe $c) { return $c } }
    return $null
}

function Find-Python {
    $cands = New-Object System.Collections.ArrayList
    [void]$cands.Add((Join-Path $env:USERPROFILE '.local\share\python-standalone\python\python.exe'))
    if ($env:BOOTSTRAP_PYTHON) { [void]$cands.Add($env:BOOTSTRAP_PYTHON) }
    foreach ($p in @(Get-Command python.exe -All -CommandType Application -ErrorAction SilentlyContinue)) {
        if ($p.Source -notmatch 'WindowsApps') { [void]$cands.Add($p.Source) }
    }
    foreach ($c in $cands) { if (Test-FileSafe $c) { return $c } }
    return $null
}

function Get-PathDirs {
    param([string]$PathValue)
    $dirs = New-Object System.Collections.ArrayList
    foreach ($d in ($PathValue -split ';')) {
        $t = $d.Trim().Trim('"')
        if ($t -ne '') { [void]$dirs.Add($t) }
    }
    return @($dirs)
}

function Get-ClauderDirs {
    param([string]$PathValue)
    $found = New-Object System.Collections.ArrayList
    foreach ($d in (Get-PathDirs $PathValue)) {
        if (Test-FileSafe (Join-Path $d 'clauder.cmd')) { [void]$found.Add($d) }
    }
    return @($found)
}

# First "claude" on a Path value in PATHEXT order, skipping directories that hold clauder.cmd.
function Resolve-Claude {
    param([string]$PathValue, [bool]$SkipClauderDirs)
    $pathext = $env:PATHEXT
    if ([string]::IsNullOrEmpty($pathext)) { $pathext = '.COM;.EXE;.BAT;.CMD' }
    $exts = @($pathext -split ';' | Where-Object { $_ -ne '' })
    foreach ($d in (Get-PathDirs $PathValue)) {
        if ($SkipClauderDirs -and (Test-FileSafe (Join-Path $d 'clauder.cmd'))) { continue }
        foreach ($e in $exts) {
            $candidate = Join-Path $d ('claude' + $e)
            if (Test-FileSafe $candidate) { return $candidate }
        }
    }
    return $null
}

function Get-PersistentPath {
    $parts = New-Object System.Collections.ArrayList
    foreach ($scope in @('Machine', 'User')) {
        $v = [Environment]::GetEnvironmentVariable('Path', $scope)
        if (-not [string]::IsNullOrEmpty($v)) { [void]$parts.Add([Environment]::ExpandEnvironmentVariables($v)) }
    }
    return ($parts -join ';')
}

function Get-RendezvousDir {
    $root = $env:CLAUDE_BOOTSTRAP_DATA_ROOT
    if ([string]::IsNullOrEmpty($root)) { $root = Join-Path $env:USERPROFILE '.claude\plugins\data' }
    return [System.IO.Path]::GetFullPath((Join-Path $root 'plugins-kit\relaunch-kit\relaunch'))
}

# --- running child processes -------------------------------------------------

function Stop-Tree {
    param([System.Diagnostics.Process]$Process)
    try {
        if (-not $Process.HasExited) {
            $null = & cmd.exe /d /c ('taskkill /PID {0} /T /F >nul 2>&1' -f $Process.Id)
        }
        [void]$Process.WaitForExit(5000)
    } catch {
        Write-Warning ('could not stop process {0}: {1}' -f $Process.Id, $_.Exception.Message)
    }
}

function Start-Child {
    param(
        [string]$File, [string]$Arguments, [string]$WorkDir,
        [hashtable]$EnvSet = @{}, [switch]$Capture, [switch]$ShellExecute, [int]$Timeout = 120
    )
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $File
    $psi.Arguments = $Arguments
    $psi.WorkingDirectory = $WorkDir
    if ($ShellExecute) {
        $psi.UseShellExecute = $true
    } else {
        $psi.UseShellExecute = $false
        foreach ($k in @($EnvSet.Keys)) { $psi.EnvironmentVariables[[string]$k] = [string]$EnvSet[$k] }
        if ($Capture) {
            $psi.RedirectStandardInput = $true
            $psi.RedirectStandardOutput = $true
            $psi.RedirectStandardError = $true
        }
    }
    $p = New-Object System.Diagnostics.Process
    $p.StartInfo = $psi
    [void]$p.Start()
    [void]$script:Live.Add($p)
    $so = $null
    $se = $null
    if ($Capture -and -not $ShellExecute) {
        $p.StandardInput.Close()
        $so = $p.StandardOutput.ReadToEndAsync()
        $se = $p.StandardError.ReadToEndAsync()
    }
    return @{ Process = $p; Out = $so; Err = $se; Deadline = [DateTime]::UtcNow.AddSeconds($Timeout) }
}

function Wait-Child {
    param([hashtable]$Handle)
    $p = $Handle.Process
    $remaining = [int][Math]::Max(0, ($Handle.Deadline - [DateTime]::UtcNow).TotalMilliseconds)
    $timedOut = $false
    if (-not $p.WaitForExit($remaining)) {
        $timedOut = $true
        Stop-Tree $p
    } else {
        $p.WaitForExit()
    }
    $out = ''
    $err = ''
    if ($null -ne $Handle.Out) {
        if ($Handle.Out.Wait(5000)) { $out = $Handle.Out.Result }
        if ($Handle.Err.Wait(5000)) { $err = $Handle.Err.Result }
    }
    $code = -1
    if (-not $timedOut) { $code = $p.ExitCode }
    return [pscustomobject]@{ ExitCode = $code; TimedOut = $timedOut; Out = $out; Err = $err }
}

function Invoke-Child {
    param(
        [string]$File, [string]$Arguments, [string]$WorkDir,
        [hashtable]$EnvSet = @{}, [switch]$Capture, [int]$Timeout = 120
    )
    $h = Start-Child -File $File -Arguments $Arguments -WorkDir $WorkDir -EnvSet $EnvSet -Capture:$Capture -Timeout $Timeout
    return (Wait-Child $h)
}

# --- shell invocations -------------------------------------------------------

# Build how one shell runs the shim with the given argument values. cmd runs the
# line directly (what typing it at a prompt does); PowerShell and Git Bash run a
# small driver file so no command-line quoting layer sits between the script and
# the shim.
function New-Invocation {
    param([hashtable]$Sh, [bool]$ByName, [string[]]$ArgValues, [string]$CmdArgs, [string]$Tag)
    switch ($Sh.Kind) {
        'cmd' {
            $shim = 'clauder'
            if (-not $ByName) { $shim = '"' + $script:ShimCmd + '"' }
            if (-not $CmdArgs) {
                $CmdArgs = (@($ArgValues | ForEach-Object { ConvertTo-CmdArg $_ })) -join ' '
            }
            return @{ File = $Sh.Exe; Arguments = ('/d /s /c "{0} {1}"' -f $shim, $CmdArgs) }
        }
        'ps' {
            $callee = '& clauder'
            if (-not $ByName) { $callee = '& ' + (ConvertTo-PsLiteral $script:ShimCmd) }
            $argText = (@($ArgValues | ForEach-Object { ConvertTo-PsLiteral $_ })) -join ' '
            $lines = @(
                '$ErrorActionPreference = ''Continue''',
                ($callee + ' ' + $argText),
                '$rk = $LASTEXITCODE',
                'if ($null -eq $rk) { $rk = 99 }',
                'exit $rk'
            )
            $drv = Join-Path $script:Work ($Tag + '.ps1')
            Write-TextFile $drv ($lines -join "`r`n") $true
            return @{ File = $Sh.Exe; Arguments = ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $drv) }
        }
        'bash' {
            $shim = 'clauder'
            if (-not $ByName) { $shim = ConvertTo-BashLiteral ($script:ShimBash -replace '\\', '/') }
            $argText = (@($ArgValues | ForEach-Object { ConvertTo-BashLiteral $_ })) -join ' '
            $lines = @(
                'export MSYS2_ARG_CONV_EXCL=''*''',
                'export PATH="/usr/bin:$PATH"',
                ($shim + ' ' + $argText),
                'exit $?'
            )
            $drv = Join-Path $script:Work ($Tag + '.sh')
            Write-TextFile $drv (($lines -join "`n") + "`n") $false
            return @{ File = $Sh.Exe; Arguments = ('--noprofile --norc "{0}"' -f ($drv -replace '\\', '/')) }
        }
    }
    throw ('unknown shell kind ' + $Sh.Kind)
}

function Get-ChainArgs {
    param([hashtable]$Sh, [bool]$ByName, [string]$Run)
    $prompt = '/relaunch-check 2 ' + $Run
    if ($ByName) { return @($prompt) }
    $pd = $script:PluginDir
    if ($Sh.Kind -eq 'bash') { $pd = $pd -replace '\\', '/' }
    return @('--plugin-dir', $pd, $prompt)
}

# --- the chain check ---------------------------------------------------------

function Test-ChainLog {
    param([string]$Run, [string]$Log)
    $why = New-Object System.Collections.ArrayList
    $rows = New-Object System.Collections.ArrayList
    if (-not (Test-FileSafe $Log)) {
        [void]$why.Add('no check log at ' + $Log)
        return @{ Ok = $false; Why = @($why); Rows = @(); Text = ''; Session2 = ''; Tokens = @() }
    }
    $text = [System.IO.File]::ReadAllText($Log)
    foreach ($line in ($text -split "`r?`n")) {
        if ($line.Trim() -eq '') { continue }
        try { [void]$rows.Add(($line | ConvertFrom-Json)) }
        catch { [void]$why.Add('unparseable log line: ' + $line) }
    }
    $byHop = @{}
    $hopCount = 0
    $tokens = New-Object System.Collections.ArrayList
    foreach ($r in $rows) {
        if ((Get-Prop $r 'run') -ne $Run) { [void]$why.Add('log holds a foreign run id ' + (Get-Prop $r 'run')) }
        $hop = Get-Prop $r 'hop'
        if ($null -ne $hop) {
            $hopCount++
            $key = [string]$hop
            if (-not $byHop.ContainsKey($key)) { $byHop[$key] = $r }
            $tok = Get-Prop $r 'token'
            if ($tok) { [void]$tokens.Add([string]$tok) }
        }
    }
    $session2 = ''
    foreach ($need in @('2', '1', '0')) {
        if (-not $byHop.ContainsKey($need)) { [void]$why.Add('hop ' + $need + ' is missing') }
    }
    if ($hopCount -ne 3) { [void]$why.Add(('expected 3 hop lines, found {0}' -f $hopCount)) }
    if ($byHop.ContainsKey('2') -and $byHop.ContainsKey('1') -and $byHop.ContainsKey('0')) {
        $s2 = [string](Get-Prop $byHop['2'] 'session_id')
        $s1 = [string](Get-Prop $byHop['1'] 'session_id')
        $s0 = [string](Get-Prop $byHop['0'] 'session_id')
        $session2 = $s2
        if ($s2 -eq '' -or $s1 -eq '' -or $s0 -eq '') { [void]$why.Add('a hop logged an empty session id') }
        if ($s1 -ne $s2) { [void]$why.Add('hop 1 did not resume hop 2 session id') }
        if ($s0 -eq $s2) { [void]$why.Add('hop 0 did not get a new session id') }
        $distinct = @($tokens | Sort-Object -Unique)
        if ($distinct.Count -ne 3) { [void]$why.Add('tokens are not three distinct values') }
    }
    $verdicts = @($rows | Where-Object { $null -ne (Get-Prop $_ 'verdict') })
    if ($verdicts.Count -eq 0) {
        [void]$why.Add('no verdict line (hop 0 never ran)')
    } elseif ((Get-Prop $verdicts[0] 'verdict') -ne 'PASS') {
        [void]$why.Add('the mod reported FAIL: ' + ((@(Get-Prop $verdicts[0] 'reasons')) -join '; '))
    }
    $dir = Split-Path -Parent $Log
    foreach ($t in $tokens) {
        foreach ($suffix in @('.json', '.rejected.json')) {
            if (Test-FileSafe (Join-Path $dir ($t + $suffix))) {
                [void]$why.Add('token file left behind: ' + $t + $suffix)
            }
        }
    }
    return @{ Ok = ($why.Count -eq 0); Why = @($why); Rows = @($rows); Text = $text; Session2 = $session2; Tokens = @($tokens) }
}

function Get-ChainVerdict {
    param([string]$Run, [string]$Log, $Result, [string]$Where)
    $check = Test-ChainLog -Run $Run -Log $Log
    if ($Result.TimedOut) {
        if (@($check.Rows).Count -eq 0) {
            return @('COULD-NOT-RUN', ('blocked: trust dialog or login? claude was killed after {0} s and wrote no check log. Answer the prompt once in {1} and run the script again.' -f $TimeoutSec, $Where), $check)
        }
        return @('FAIL', ('stalled: killed after {0} s. {1}  Log: {2}' -f $TimeoutSec, ((@($check.Why)) -join '; '), $check.Text), $check)
    }
    $reasons = New-Object System.Collections.ArrayList
    if ($Result.ExitCode -ne 0) { [void]$reasons.Add(('the shell saw exit code {0}, expected 0' -f $Result.ExitCode)) }
    foreach ($w in @($check.Why)) { [void]$reasons.Add($w) }
    if ($reasons.Count -eq 0) { return @('PASS', 'three hops; hop 1 resumed hop 2; hop 0 is a new session; tokens distinct; no token file left; exit code 0', $check) }
    return @('FAIL', ((@($reasons)) -join '; ') + '  Log: ' + $check.Text, $check)
}

function Invoke-Chain {
    param([string]$Phase, [hashtable]$Sh, [bool]$ByName, [hashtable]$EnvSet = @{})
    $run = New-RunId ($Phase.ToLower() + '-' + $Sh.Tag)
    $log = Join-Path $script:RdvDir ('check-' + $run + '.log')
    [void]$script:LogsToRemove.Add($log)
    $inv = New-Invocation -Sh $Sh -ByName $ByName -ArgValues (Get-ChainArgs $Sh $ByName $run) -CmdArgs '' -Tag ('chain-' + $run)
    Write-Host ('  running {0} chain in {1} (up to {2} s; the claude screen appears and restarts twice)...' -f $Phase, $Sh.Name, $TimeoutSec)
    $r = Invoke-Child -File $inv.File -Arguments $inv.Arguments -WorkDir $script:Repo -EnvSet $EnvSet -Timeout $TimeoutSec
    $v = Get-ChainVerdict -Run $run -Log $log -Result $r -Where $Sh.Name
    Add-Result $Phase $Sh.Name '3-hop /relaunch-check chain' $v[0] $v[1]
}

# --- main --------------------------------------------------------------------

$scrubVars = @('RELAUNCH_KIT_TOKEN', 'RELAUNCH_KIT_DIR', 'RELAUNCH_KIT_SHIM', 'RELAUNCH_KIT_CLAUDE', 'CLAUDECODE')

try {
    if ([string]::IsNullOrWhiteSpace($RepoRoot)) { $RepoRoot = Split-Path -Parent $PSScriptRoot }
    $script:Repo = (Resolve-Path -LiteralPath $RepoRoot).ProviderPath
    $script:PluginDir = Join-Path $script:Repo 'plugins\relaunch-kit'
    $script:ShimCmd = Join-Path $script:PluginDir 'bin\clauder.cmd'
    $script:ShimBash = Join-Path $script:PluginDir 'bin\clauder'
    $script:RdvDir = Get-RendezvousDir
    $script:Work = Join-Path ([System.IO.Path]::GetTempPath()) ('relaunch-verify-work-' + $script:Stamp)
    [void](New-Item -ItemType Directory -Path $script:Work -Force)

    foreach ($v in $scrubVars) {
        $script:SavedEnv[$v] = [Environment]::GetEnvironmentVariable($v, 'Process')
        [Environment]::SetEnvironmentVariable($v, $null, 'Process')
    }

    Write-Host ('relaunch-kit Windows verification ' + $script:Stamp)
    Add-Note ('repo root        : ' + $script:Repo)

    # ---- shells ----
    $psExe5 = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $cmdExe = $env:ComSpec
    if (-not (Test-FileSafe $cmdExe)) { $cmdExe = Join-Path $env:SystemRoot 'System32\cmd.exe' }
    $pwshExe = Find-App 'pwsh.exe'
    $bashExe = Find-GitBash
    $shells = @(
        @{ Name = 'pwsh 7';      Tag = 'ps7';  Kind = 'ps';   Exe = $pwshExe; Present = [bool]$pwshExe },
        @{ Name = 'powershell 5.1'; Tag = 'ps51'; Kind = 'ps'; Exe = $psExe5; Present = (Test-FileSafe $psExe5) },
        @{ Name = 'cmd';         Tag = 'cmd';  Kind = 'cmd';  Exe = $cmdExe; Present = (Test-FileSafe $cmdExe) },
        @{ Name = 'Git Bash';    Tag = 'bash'; Kind = 'bash'; Exe = $bashExe; Present = [bool]$bashExe }
    )
    foreach ($s in $shells) {
        $where = 'not found'
        if ($s.Present) { $where = $s.Exe }
        Add-Note ('shell            : {0,-15} {1}' -f $s.Name, $where)
    }

    # ---- claude, python, repo ----
    $claudeTarget = Resolve-Claude -PathValue $env:Path -SkipClauderDirs $true
    $claudeKind = ''
    if ($claudeTarget) {
        $claudeKind = 'native (.exe/.com)'
        $ext = [System.IO.Path]::GetExtension($claudeTarget).ToLower()
        if ($ext -eq '.cmd' -or $ext -eq '.bat') { $claudeKind = 'batch (.cmd/.bat)' }
        Add-Note ('claude target    : {0}  [{1}]' -f $claudeTarget, $claudeKind)
        if ($claudeKind -like 'batch*') {
            $vr = Invoke-Child -File $cmdExe -Arguments ('/d /s /c ""{0}" --version"' -f $claudeTarget) -WorkDir $script:Work -Capture -Timeout 60
        } else {
            $vr = Invoke-Child -File $claudeTarget -Arguments '--version' -WorkDir $script:Work -Capture -Timeout 60
        }
        Add-Note ('claude --version : {0}' -f ((($vr.Out + $vr.Err).Trim()) -replace '\s+', ' '))
    } else {
        Add-Note 'claude target    : NOT FOUND on this console Path'
    }
    $python = Find-Python
    $pyText = 'NOT FOUND'
    if ($python) { $pyText = $python }
    Add-Note ('interpreter      : ' + $pyText)
    $git = Find-App 'git.exe'
    $head = 'unknown'
    if ($git) {
        try {
            $h = & $git -C $script:Repo rev-parse --short HEAD
            if ($LASTEXITCODE -eq 0 -and $h) { $head = [string]$h }
        } catch {
            Write-Warning ('git rev-parse failed: ' + $_.Exception.Message)
        }
    }
    Add-Note ('repo HEAD        : ' + $head)

    # ---- installed relaunch-kit, persistent Path ----
    $cacheRoot = Join-Path $env:USERPROFILE '.claude\plugins\cache'
    $installedDir = $null
    if (Test-DirSafe $cacheRoot) {
        $installedDir = Get-ChildItem -LiteralPath $cacheRoot -Directory |
            ForEach-Object { Join-Path $_.FullName 'relaunch-kit' } |
            Where-Object { Test-DirSafe $_ } | Select-Object -First 1
    }
    $installedVersion = ''
    if ($installedDir) {
        $latest = Get-ChildItem -LiteralPath $installedDir -Directory | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($latest) { $installedVersion = $latest.Name }
        Add-Note ('relaunch-kit     : installed, version {0} ({1})' -f $installedVersion, $installedDir)
    } else {
        Add-Note 'relaunch-kit     : not installed (no plugin cache directory)'
    }
    $regPath = Get-PersistentPath
    $regDirs = @(Get-ClauderDirs $regPath)
    $conDirs = @(Get-ClauderDirs $env:Path)
    $regText = 'no clauder.cmd entry'
    if ($regDirs.Count -gt 0) { $regText = $regDirs -join '; ' }
    Add-Note ('registry Path    : ' + $regText)
    if ($conDirs.Count -gt 0) {
        Add-Note 'console Path     : holds clauder.cmd'
    } elseif ($regDirs.Count -gt 0) {
        Add-Note 'console Path     : no clauder.cmd -- the registry has it, so open a NEW terminal tab to see it'
    } else {
        Add-Note 'console Path     : no clauder.cmd'
    }
    Add-Note ('rendezvous dir   : ' + $script:RdvDir)

    # ---- preflight verdicts ----
    $cannot = ''
    $interactive = -not ([Console]::IsInputRedirected -or [Console]::IsOutputRedirected)
    if (-not (Test-FileSafe $script:ShimCmd)) {
        Add-Result 'Preflight' '-' 'working copy shim exists' 'FAIL' ('missing ' + $script:ShimCmd + ' (is -RepoRoot a plugins-kit checkout?)')
        $cannot = 'the working copy shim is missing'
    } else {
        Add-Result 'Preflight' '-' 'working copy shim exists' 'PASS' $script:ShimCmd
    }
    if ($claudeTarget) {
        Add-Result 'Preflight' '-' 'real claude resolves' 'PASS' ($claudeTarget + ' [' + $claudeKind + ']')
    } else {
        Add-Result 'Preflight' '-' 'real claude resolves' 'FAIL' 'claude was not found on this console Path (clauder directories skipped)'
        if (-not $cannot) { $cannot = 'claude is not on Path' }
    }
    if ($python) {
        Add-Result 'Preflight' '-' 'python interpreter for the shim' 'PASS' $python
    } else {
        Add-Result 'Preflight' '-' 'python interpreter for the shim' 'FAIL' 'no standalone python, BOOTSTRAP_PYTHON or python.exe (outside WindowsApps) was found'
        if (-not $cannot) { $cannot = 'no Python interpreter' }
    }
    $interactiveBlock = $cannot
    if (-not $interactiveBlock -and -not $interactive) {
        $interactiveBlock = 'stdin or stdout is redirected (a pipe, an IDE terminal or mintty): the launcher passes through there. Run from a Windows Terminal tab or a console window.'
    }

    # ---- Phase A ----
    Write-Host ''
    Write-Host 'Phase A: the working copy'
    foreach ($s in $shells) {
        if (-not $s.Present) { Add-Result 'A' $s.Name '3-hop /relaunch-check chain' 'COULD-NOT-RUN' 'shell not installed'; continue }
        if ($interactiveBlock) { Add-Result 'A' $s.Name '3-hop /relaunch-check chain' 'COULD-NOT-RUN' $interactiveBlock; continue }
        Invoke-Chain -Phase 'A' -Sh $s -ByName $false
    }

    # ---- Phase B ----
    Write-Host ''
    Write-Host 'Phase B: the installed command'
    $bReason = $interactiveBlock
    if (-not $bReason -and -not $installedDir -and $regDirs.Count -eq 0) {
        $bReason = 'relaunch-kit is not installed (no plugin cache directory, no clauder.cmd on the registry Path)'
    }
    if ($bReason) {
        foreach ($s in $shells) { Add-Result 'B' $s.Name 'plain clauder chain' 'COULD-NOT-RUN' $bReason }
    } elseif ($regDirs.Count -eq 0) {
        foreach ($s in $shells) {
            Add-Result 'B' $s.Name 'plain clauder chain' 'FAIL' 'relaunch-kit is installed but no registry Path entry holds clauder.cmd (bootstrap puts the data bin on the user Path)'
        }
    } else {
        $plainClaude = Resolve-Claude -PathValue $regPath -SkipClauderDirs $false
        if ($plainClaude) {
            Add-Result 'B' '-' 'plain claude resolves to the real target' 'PASS' $plainClaude
        } else {
            Add-Result 'B' '-' 'plain claude resolves to the real target' 'FAIL' 'claude is not on the registry Path'
        }
        $bEnv = @{ Path = $regPath }
        foreach ($s in $shells) {
            if (-not $s.Present) { Add-Result 'B' $s.Name 'plain clauder chain' 'COULD-NOT-RUN' 'shell not installed'; continue }
            Invoke-Chain -Phase 'B' -Sh $s -ByName $true -EnvSet $bEnv
        }
    }

    # ---- Phase C ----
    Write-Host ''
    Write-Host 'Phase C: two terminals at once'
    $cShell = $null
    foreach ($s in $shells) { if ($s.Kind -eq 'ps' -and $s.Present) { $cShell = $s; break } }
    if ($interactiveBlock) {
        Add-Result 'C' '-' 'two windows, one directory' 'COULD-NOT-RUN' $interactiveBlock
    } elseif ($null -eq $cShell) {
        Add-Result 'C' '-' 'two windows, one directory' 'COULD-NOT-RUN' 'no PowerShell found'
    } else {
        $runs = @((New-RunId 'c-1'), (New-RunId 'c-2'))
        $handles = @()
        foreach ($run in $runs) {
            $log = Join-Path $script:RdvDir ('check-' + $run + '.log')
            [void]$script:LogsToRemove.Add($log)
            $inv = New-Invocation -Sh $cShell -ByName $false -ArgValues (Get-ChainArgs $cShell $false $run) -CmdArgs '' -Tag ('chain-' + $run)
            $handles += , (Start-Child -File $inv.File -Arguments $inv.Arguments -WorkDir $script:Repo -ShellExecute -Timeout $TimeoutSec)
        }
        Write-Host ('  two console windows opened in {0}; waiting up to {1} s...' -f $script:Repo, $TimeoutSec)
        $verdicts = @()
        for ($i = 0; $i -lt 2; $i++) {
            $r = Wait-Child $handles[$i]
            $log = Join-Path $script:RdvDir ('check-' + $runs[$i] + '.log')
            $verdicts += , (Get-ChainVerdict -Run $runs[$i] -Log $log -Result $r -Where 'a new window')
        }
        for ($i = 0; $i -lt 2; $i++) {
            Add-Result 'C' ('window ' + ($i + 1)) '3-hop chain with its own run id' $verdicts[$i][0] $verdicts[$i][1]
        }
        $c1 = $verdicts[0][2]
        $c2 = $verdicts[1][2]
        if ($verdicts[0][0] -eq 'PASS' -and $verdicts[1][0] -eq 'PASS') {
            $problems = New-Object System.Collections.ArrayList
            if ($c1.Session2 -eq $c2.Session2) { [void]$problems.Add('both windows ran in the same session') }
            $all = @($c1.Tokens) + @($c2.Tokens)
            if (@($all | Sort-Object -Unique).Count -ne 6) { [void]$problems.Add('the two chains shared a token') }
            if ($problems.Count -eq 0) {
                Add-Result 'C' '-' 'sessions and files stay separate' 'PASS' 'different sessions, six distinct tokens, each log holds only its own run id'
            } else {
                Add-Result 'C' '-' 'sessions and files stay separate' 'FAIL' ((@($problems)) -join '; ')
            }
        } else {
            Add-Result 'C' '-' 'sessions and files stay separate' 'COULD-NOT-RUN' 'a window did not complete its chain'
        }
    }

    # ---- Phase D ----
    Write-Host ''
    Write-Host 'Phase D: quoting and exit codes (no terminal, no model calls)'
    $dBlock = ''
    if (-not (Test-FileSafe $script:ShimCmd)) { $dBlock = 'the working copy shim is missing' }
    elseif (-not $python) { $dBlock = 'no Python interpreter' }
    if ($dBlock) {
        foreach ($s in $shells) { Add-Result 'D' $s.Name 'argument forwarding and exit codes' 'COULD-NOT-RUN' $dBlock }
    } else {
        $argsOut = Join-Path $script:Work 'argv.json'
        $fakeOut = Join-Path $script:Work 'fake-args.txt'
        $echoPy = Join-Path $script:Work 'echoargs.py'
        $fakeCmd = Join-Path $script:Work 'fake-claude.cmd'
        Write-TextFile $echoPy ("import json, os, sys`nwith open(os.environ['RK_ARGS_OUT'], 'w') as f:`n    f.write(json.dumps(sys.argv[1:]))`n") $false
        Write-TextFile $fakeCmd (("@echo off`r`n>""{0}"" echo %*`r`nexit /b 7`r`n") -f $fakeOut) $false
        $weird = 'a b&c(d)^e<f>g|h''i!j'

        foreach ($s in $shells) {
            if (-not $s.Present) { Add-Result 'D' $s.Name 'argument forwarding and exit codes' 'COULD-NOT-RUN' 'shell not installed'; continue }
            $echoArg = $echoPy
            if ($s.Kind -eq 'bash') { $echoArg = $echoPy -replace '\\', '/' }

            # D1: a native target, no terminal: the launcher passes through and the arguments arrive exactly.
            foreach ($f in @($argsOut)) { if (Test-FileSafe $f) { Remove-Item -LiteralPath $f -Force } }
            $inv = New-Invocation -Sh $s -ByName $false -ArgValues @($echoArg, $weird) -CmdArgs '' -Tag ('d1-' + $s.Tag)
            $r = Invoke-Child -File $inv.File -Arguments $inv.Arguments -WorkDir $script:Work -Capture -Timeout 60 `
                -EnvSet @{ RELAUNCH_KIT_CLAUDE = $python; RK_ARGS_OUT = $argsOut }
            $want = '["' + $weird + '"]'
            $got = ''
            if (Test-FileSafe $argsOut) { $got = ([System.IO.File]::ReadAllText($argsOut)).Trim() }
            $bad = New-Object System.Collections.ArrayList
            if ($r.TimedOut) { [void]$bad.Add('timed out') }
            if ($r.ExitCode -ne 0) { [void]$bad.Add(('exit code {0}, expected 0' -f $r.ExitCode)) }
            if ($got -ne $want) { [void]$bad.Add(('target received [{0}], expected [{1}]' -f $got, $want)) }
            if (($r.Out + $r.Err).Trim() -ne '') { [void]$bad.Add('printed output: ' + ($r.Out + $r.Err).Trim()) }
            if ($bad.Count -eq 0) {
                Add-Result 'D' $s.Name 'native target: & ( ) ^ < > | '' ! and a space arrive exactly' 'PASS' 'exit code 0, nothing printed'
            } else {
                Add-Result 'D' $s.Name 'native target: & ( ) ^ < > | '' ! and a space arrive exactly' 'FAIL' ((@($bad)) -join '; ')
            }

            # D2: a .cmd target: pass-through records the quoted line and its exit code reaches the shell.
            $d2Env = @{ RELAUNCH_KIT_CLAUDE = $fakeCmd }
            if (Test-FileSafe $fakeOut) { Remove-Item -LiteralPath $fakeOut -Force }
            $inv = New-Invocation -Sh $s -ByName $false -ArgValues @('-p', 'a b&c') -CmdArgs '' -Tag ('d2-' + $s.Tag)
            $r = Invoke-Child -File $inv.File -Arguments $inv.Arguments -WorkDir $script:Work -Capture -Timeout 60 -EnvSet $d2Env
            $got = ''
            if (Test-FileSafe $fakeOut) { $got = ([System.IO.File]::ReadAllText($fakeOut)).Trim() }
            $want = '"-p" "a b&c"'
            $bad = New-Object System.Collections.ArrayList
            if ($r.TimedOut) { [void]$bad.Add('timed out') }
            if ($r.ExitCode -ne 7) { [void]$bad.Add(('the shell saw exit code {0}, expected 7' -f $r.ExitCode)) }
            if ($got -ne $want) { [void]$bad.Add(('the .cmd target recorded [{0}], expected [{1}]' -f $got, $want)) }
            if (($r.Out + $r.Err).Trim() -ne '') { [void]$bad.Add('printed output: ' + ($r.Out + $r.Err).Trim()) }
            if ($bad.Count -eq 0) {
                Add-Result 'D' $s.Name '.cmd target: pass-through line and exit code 7' 'PASS' ('recorded ' + $want + '; nothing printed')
            } else {
                Add-Result 'D' $s.Name '.cmd target: pass-through line and exit code 7' 'FAIL' ((@($bad)) -join '; ')
            }

            # D3 and D4: a .cmd target refuses an argument it cannot carry, loudly and before running it.
            $refusals = @(
                @{ Tag = 'd3'; Label = '.cmd target: a percent sign is refused'; Value = 'a%b'; Match = 'percent'; CmdArgs = '' },
                @{ Tag = 'd4'; Label = '.cmd target: a double quote is refused'; Value = 'a"b'; Match = 'double quote'; CmdArgs = '-p a"b' }
            )
            foreach ($case in $refusals) {
                if (Test-FileSafe $fakeOut) { Remove-Item -LiteralPath $fakeOut -Force }
                $cmdArgs = ''
                if ($s.Kind -eq 'cmd') { $cmdArgs = $case.CmdArgs }
                $inv = New-Invocation -Sh $s -ByName $false -ArgValues @('-p', $case.Value) -CmdArgs $cmdArgs -Tag ($case.Tag + '-' + $s.Tag)
                $r = Invoke-Child -File $inv.File -Arguments $inv.Arguments -WorkDir $script:Work -Capture -Timeout 60 -EnvSet $d2Env
                $bad = New-Object System.Collections.ArrayList
                if ($r.TimedOut) { [void]$bad.Add('timed out') }
                if ($r.ExitCode -eq 0 -or $r.ExitCode -eq 7) { [void]$bad.Add(('exit code {0}, expected a non-zero refusal' -f $r.ExitCode)) }
                if ($r.Err -notmatch $case.Match) { [void]$bad.Add(('message does not name "{0}": [{1}]' -f $case.Match, $r.Err.Trim())) }
                if (Test-FileSafe $fakeOut) { [void]$bad.Add('the target ran although the argument should have been refused') }
                if ($bad.Count -eq 0) {
                    Add-Result 'D' $s.Name $case.Label 'PASS' ('exit code {0}: {1}' -f $r.ExitCode, ($r.Err.Trim() -replace '\s+', ' '))
                } else {
                    Add-Result 'D' $s.Name $case.Label 'FAIL' ((@($bad)) -join '; ')
                }
            }
        }
    }
}
catch {
    Add-Result 'Script' '-' 'unexpected error' 'FAIL' ($_.Exception.Message + '  at ' + $_.InvocationInfo.PositionMessage)
}
finally {
    foreach ($p in @($script:Live)) {
        try { if (-not $p.HasExited) { Stop-Tree $p } } catch { Write-Warning ('cleanup: ' + $_.Exception.Message) }
    }
    foreach ($log in @($script:LogsToRemove)) {
        try { if (Test-FileSafe $log) { Remove-Item -LiteralPath $log -Force } }
        catch { Write-Warning ('could not remove ' + $log + ': ' + $_.Exception.Message) }
    }
    if ($script:Work -and (Test-DirSafe $script:Work)) {
        try { Remove-Item -LiteralPath $script:Work -Recurse -Force }
        catch { Write-Warning ('could not remove ' + $script:Work + ': ' + $_.Exception.Message) }
    }
    foreach ($k in @($script:SavedEnv.Keys)) {
        [Environment]::SetEnvironmentVariable([string]$k, $script:SavedEnv[$k], 'Process')
    }
}

# --- report ------------------------------------------------------------------

$out = New-Object System.Collections.ArrayList
[void]$out.Add('relaunch-kit Windows verification ' + $script:Stamp)
[void]$out.Add('')
foreach ($n in $script:Notes) { [void]$out.Add($n) }
[void]$out.Add('')
[void]$out.Add(('{0,-14} {1,-5} {2,-15} {3}' -f 'STATUS', 'PHASE', 'SHELL', 'CHECK'))
[void]$out.Add(('{0,-14} {1,-5} {2,-15} {3}' -f '------', '-----', '-----', '-----'))
foreach ($r in $script:Results) {
    [void]$out.Add(('{0,-14} {1,-5} {2,-15} {3}' -f $r.Status, $r.Phase, $r.Shell, $r.Check))
}
$notPass = @($script:Results | Where-Object { $_.Status -ne 'PASS' })
if ($notPass.Count -gt 0) {
    [void]$out.Add('')
    [void]$out.Add('Details for everything that is not PASS:')
    foreach ($r in $notPass) {
        [void]$out.Add(('[{0}] {1} / {2} / {3}' -f $r.Status, $r.Phase, $r.Shell, $r.Check))
        [void]$out.Add('    ' + $r.Detail)
    }
}
$fails = @($script:Results | Where-Object { $_.Status -eq 'FAIL' }).Count
$passes = @($script:Results | Where-Object { $_.Status -eq 'PASS' }).Count
$cnr = @($script:Results | Where-Object { $_.Status -eq 'COULD-NOT-RUN' }).Count
$verdict = 'OK'
$code = 0
if ($fails -gt 0) { $verdict = 'FAILED'; $code = 1 }
elseif ($passes -eq 0) { $verdict = 'NOTHING VERIFIED'; $code = 1 }
[void]$out.Add('')
[void]$out.Add(('Result: {0} (PASS {1}, FAIL {2}, COULD-NOT-RUN {3}); exit code {4}' -f $verdict, $passes, $fails, $cnr, $code))

$reportPath = Join-Path ([System.IO.Path]::GetTempPath()) ('relaunch-verify-' + $script:Stamp + '.txt')
[System.IO.File]::WriteAllLines($reportPath, [string[]]@($out))
Write-Host ''
foreach ($line in $out) { Write-Host $line }
Write-Host ''
Write-Host ('Report saved to ' + $reportPath)
exit $code
