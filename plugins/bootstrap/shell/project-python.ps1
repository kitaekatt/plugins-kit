# project-python.ps1 -- BOOTSTRAP_PROJECT_PYTHON for the current PowerShell location.
#
# PowerShell twin of project-python.sh (interface-v3, section 1 "Normative
# resolution rule", steps 1, 2, 4, 5). Windows PowerShell 5.1 and PowerShell 7.
#
#   Resolve-BootstrapProjectPython [-Path <dir>]
#       Returns ONE absolute path (forward slashes, upper-case drive letter,
#       e.g. C:/Users/you/proj/.venv/Scripts/python.exe) -- or an EMPTY
#       string for an opted-out tree.
#
#       The walk: from <dir> upward (stop after checking the home directory
#       itself, or at a root; depth cap 64), each directory is checked for
#       (a) an opt-out -- <d>/.claude/bootstrap.json or bootstrap.local.json
#       declaring "project_python": false; never at the home directory
#       itself, whose .claude/bootstrap.json is the USER layer -- and then
#       (b) a venv -- <d>/.venv, or <d>/$env:UV_PROJECT_ENVIRONMENT when that
#       is relative, holding pyvenv.cfg and an interpreter. The first
#       directory with either stops the walk.
#
#       Order: an opt-out found by the walk (no project value) >
#       $env:VIRTUAL_ENV (when its bin/python or Scripts/python.exe exists) >
#       an absolute $env:UV_PROJECT_ENVIRONMENT holding pyvenv.cfg and an
#       interpreter > the venv found by the walk > $env:BOOTSTRAP_PYTHON > the
#       deterministic standalone interpreter. BOOTSTRAP_PYTHON is never
#       affected by an opt-out.
#
#   Update-BootstrapProjectPython
#       Sets $env:BOOTSTRAP_PROJECT_PYTHON for the current location. Cached on
#       $env:_BOOTSTRAP_PP_DIR (location, VIRTUAL_ENV, UV_PROJECT_ENVIRONMENT,
#       whether the location's venv has pyvenv.cfg, whether the last value still
#       exists). A value the user set (one that differs from
#       $env:_BOOTSTRAP_PP_LAST) is never overwritten or removed; in an
#       opted-out tree the update removes only the value it set itself. An
#       edit to a bootstrap.json takes effect at the next key change.
#
# Dot-sourced from an EXISTING profile.ps1 by the line bootstrap appends
# (bootstrap_lib/shell_hook.py). Unless $env:BOOTSTRAP_PP_NO_REGISTER is set,
# dot-sourcing also sets $env:BOOTSTRAP_PYTHON when it is unset and the
# standalone interpreter exists, wraps the prompt function once (the wrapper
# keeps $LASTEXITCODE and $? for the original prompt), and runs the update
# once. The home directory is $env:HOME when it names an existing directory,
# else $HOME (the rule path_check._home applies). The literal
# .local/share/python-standalone mirrors interpreter_env.STANDALONE_DIR_REL.

function ConvertTo-BootstrapPathForm {
    param([string]$Path)
    $onWindows = ($env:OS -eq 'Windows_NT')
    $p = $Path
    if (-not $p) { $p = '' }
    if ($onWindows) {
        $p = $p -replace '\\', '/'
        if ($p -match '^/cygdrive/([A-Za-z])(/.*)?$') { $p = '/' + $Matches[1] + $Matches[2] }
        if ($p -match '^/([A-Za-z])(/.*)?$') { $p = $Matches[1] + ':' + $Matches[2] }
        if ($p -match '^[a-z]:') { $p = $p.Substring(0, 1).ToUpperInvariant() + $p.Substring(1) }
    }
    $rooted = $p.StartsWith('/') -or ($onWindows -and ($p -match '^[A-Za-z]:'))
    if (-not $rooted) {
        $here = (Get-Location -PSProvider FileSystem).ProviderPath
        if ($onWindows) { $here = $here -replace '\\', '/' }
        if ($p) { $p = $here.TrimEnd('/') + '/' + $p } else { $p = $here }
    }
    while ($p.Length -gt 1 -and $p.EndsWith('/')) { $p = $p.Substring(0, $p.Length - 1) }
    if ($onWindows -and ($p -match '^[A-Za-z]:$')) { $p = $p + '/' }
    return $p
}

function Get-BootstrapVenvPython {
    param([string]$VenvDir)
    foreach ($rel in @('bin/python', 'Scripts/python.exe')) {
        $candidate = $VenvDir.TrimEnd('/') + '/' + $rel
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    return $null
}

function Get-BootstrapHome {
    if ($env:HOME) {
        $h = ConvertTo-BootstrapPathForm $env:HOME
        if (Test-Path -LiteralPath $h -PathType Container) { return $h }
    }
    return (ConvertTo-BootstrapPathForm $HOME)
}

function Get-BootstrapStandalonePython {
    $h = (Get-BootstrapHome).TrimEnd('/')
    if ($env:OS -eq 'Windows_NT') {
        return $h + '/.local/share/python-standalone/python/python.exe'
    }
    return $h + '/.local/bin/python3'
}

function Test-BootstrapProjectPythonOptOut {
    param([string]$Dir)
    foreach ($leaf in @('bootstrap.json', 'bootstrap.local.json')) {
        $file = $Dir.TrimEnd('/') + '/.claude/' + $leaf
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { continue }
        try { $body = [System.IO.File]::ReadAllText($file) } catch { continue }
        if ($body -match '"project_python"\s*:\s*false(?![A-Za-z0-9_])') { return $true }
    }
    return $false
}

function Resolve-BootstrapProjectPython {
    param([string]$Path)
    $onWindows = ($env:OS -eq 'Windows_NT')
    if (-not $Path) { $Path = (Get-Location -PSProvider FileSystem).ProviderPath }
    $name = '.venv'
    $abs = $null
    if ($env:UV_PROJECT_ENVIRONMENT) {
        $name = $env:UV_PROJECT_ENVIRONMENT
        if ($onWindows) { $name = $name -replace '\\', '/' }
        if ($name.StartsWith('/') -or ($onWindows -and ($name -match '^[A-Za-z]:'))) {
            $abs = $name
            $name = '.venv'
        }
        while ($name.EndsWith('/')) { $name = $name.Substring(0, $name.Length - 1) }
        while ($name.StartsWith('./')) { $name = $name.Substring(2) }
        if (-not $name) { $name = '.venv' }
    }
    $homeDir = $null
    if ($env:HOME -or $HOME) { $homeDir = Get-BootstrapHome }
    $d = ConvertTo-BootstrapPathForm $Path
    $walkFound = $null
    # The walk: the nearest directory that opts out or holds a venv decides.
    for ($n = 0; $n -lt 64; $n++) {
        $atHome = $false
        if ($homeDir) {
            if ($onWindows) { $atHome = ($d -ieq $homeDir) } else { $atHome = ($d -ceq $homeDir) }
        }
        # Step 1: an opted-out project has no project interpreter. The home
        # directory's .claude/ holds the USER layer, which never opts out.
        if ((-not $atHome) -and (Test-BootstrapProjectPythonOptOut $d)) { return '' }
        if ($d.EndsWith('/')) { $venv = $d + $name } else { $venv = $d + '/' + $name }
        if (Test-Path -LiteralPath ($venv + '/pyvenv.cfg') -PathType Leaf) {
            $walkFound = Get-BootstrapVenvPython $venv
            if ($walkFound) { break }
        }
        if ($atHome) { break }
        if ($d -eq '/' -or ($d -match '^[A-Za-z]:/$')) { break }
        $cut = $d.LastIndexOf('/')
        if ($cut -le 0) { $d = '/' } else { $d = $d.Substring(0, $cut) }
        if ($d -match '^[A-Za-z]:$') { $d = $d + '/' }
    }
    # Step 2: an activated venv is the user's explicit choice.
    if ($env:VIRTUAL_ENV) {
        $found = Get-BootstrapVenvPython (ConvertTo-BootstrapPathForm $env:VIRTUAL_ENV)
        if ($found) { return $found }
    }
    # Step 4: an absolute UV_PROJECT_ENVIRONMENT first, then the walk's venv.
    if ($abs) {
        $venv = ConvertTo-BootstrapPathForm $abs
        if (Test-Path -LiteralPath ($venv + '/pyvenv.cfg') -PathType Leaf) {
            $found = Get-BootstrapVenvPython $venv
            if ($found) { return $found }
        }
    }
    if ($walkFound) { return $walkFound }
    # Step 5: the bootstrap interpreter.
    if ($env:BOOTSTRAP_PYTHON) { return (ConvertTo-BootstrapPathForm $env:BOOTSTRAP_PYTHON) }
    return (Get-BootstrapStandalonePython)
}

function Get-BootstrapProjectPythonKey {
    param([string]$Here)
    $uv = $env:UV_PROJECT_ENVIRONMENT
    if (-not $uv) {
        $venv = Join-Path $Here '.venv'
    } elseif ([System.IO.Path]::IsPathRooted($uv)) {
        $venv = $uv
    } else {
        $venv = Join-Path $Here $uv
    }
    $key = $Here + '|' + $env:VIRTUAL_ENV + '|' + $uv + '|'
    if (Test-Path -LiteralPath (Join-Path $venv 'pyvenv.cfg') -PathType Leaf) { $key += 'v' }
    $last = $env:_BOOTSTRAP_PP_LAST
    if ($last -and (Test-Path -LiteralPath $last -PathType Leaf)) { $key += 'x' }
    return $key
}

function Update-BootstrapProjectPython {
    $current = $env:BOOTSTRAP_PROJECT_PYTHON
    if ($current -and ($current -cne $env:_BOOTSTRAP_PP_LAST)) { return }
    $location = Get-Location
    if ($location.Provider.Name -ne 'FileSystem') { return }
    $here = $location.ProviderPath
    if ((Get-BootstrapProjectPythonKey $here) -ceq $env:_BOOTSTRAP_PP_DIR) { return }
    $new = Resolve-BootstrapProjectPython $here
    if ($new -and (Test-Path -LiteralPath $new -PathType Leaf)) {
        $env:BOOTSTRAP_PROJECT_PYTHON = $new
        $env:_BOOTSTRAP_PP_LAST = $new
    } else {
        # Opted out, or nothing runnable: drop the value this update set. A
        # user-set value never reaches here (the check above returned).
        Remove-Item -LiteralPath 'Env:BOOTSTRAP_PROJECT_PYTHON' -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath 'Env:_BOOTSTRAP_PP_LAST' -ErrorAction SilentlyContinue
    }
    $env:_BOOTSTRAP_PP_DIR = Get-BootstrapProjectPythonKey $here
}

if (-not $env:BOOTSTRAP_PP_NO_REGISTER) {
    if (-not $env:BOOTSTRAP_PYTHON) {
        $bootstrapStandalone = Get-BootstrapStandalonePython
        if (Test-Path -LiteralPath $bootstrapStandalone -PathType Leaf) {
            $env:BOOTSTRAP_PYTHON = $bootstrapStandalone
        }
        Remove-Variable -Name bootstrapStandalone -ErrorAction SilentlyContinue
    }
    if (-not $global:BootstrapProjectPythonPromptInstalled) {
        $global:BootstrapProjectPythonPromptInstalled = $true
        $global:BootstrapProjectPythonOriginalPrompt = $function:prompt
        function global:prompt {
            $bootstrapOk = $?
            $bootstrapCode = $global:LASTEXITCODE
            try { Update-BootstrapProjectPython } catch { }
            $global:LASTEXITCODE = $bootstrapCode
            if ($global:BootstrapProjectPythonOriginalPrompt) {
                if (-not $bootstrapOk) { Write-Error -Message 'restore status' -ErrorAction Ignore }
                & $global:BootstrapProjectPythonOriginalPrompt
            } else {
                'PS ' + $executionContext.SessionState.Path.CurrentLocation + ('>' * ($nestedPromptLevel + 1)) + ' '
            }
        }
    }
    try { Update-BootstrapProjectPython } catch { }
}
