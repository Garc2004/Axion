#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Gets axion-wizard ready to use in a single command (Windows).

.DESCRIPTION
    Looks for a Python >= 3.11, creates the virtual environment, installs the
    dependencies and starts the wizard. It is idempotent: running it again over
    an environment that is already set up only updates what needs updating.

    Uses `uv` if available (it honours uv.lock, so the environment is
    reproducible) and falls back to `venv` + `pip` if not. It does not install
    uv on its own: putting a global tool on someone's machine unasked is not a
    bootstrap script's business.

.PARAMETER Check
    Also installs the development tools and runs lint, types and tests before
    starting.

.PARAMETER NoRun
    Only prepares the environment; does not start the wizard.

.PARAMETER WizardArgs
    Anything left over is passed straight through to the wizard.

.EXAMPLE
    .\scripts\bootstrap.ps1
    Installs everything and opens the wizard.

.EXAMPLE
    .\scripts\bootstrap.ps1 -Check doctor
    Installs, verifies the repo and runs `axion-wizard doctor`.
#>
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$NoRun,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$WizardArgs
)

# No `$ErrorActionPreference = "Stop"` around native commands: in PowerShell 5.1
# an exe that writes to stderr (pip and uv do, for their progress logs, not just
# for errors) can be treated as a terminating error under certain configurations
# and abort the bootstrap with exit code 0. $LASTEXITCODE is checked explicitly
# after each one.

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$MinPythonMinor = 11

function Write-Step { param([string]$Message) Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Message) Write-Host "    $Message" -ForegroundColor Green }
function Write-Warn { param([string]$Message) Write-Host "    $Message" -ForegroundColor Yellow }

function Stop-WithHelp {
    <#
    A bootstrap failure has to say what happened and what to do, not just blow
    up: whoever runs this is setting the environment up for the first time and
    has no context with which to read a stack trace.
    #>
    param([string]$What, [string]$Why, [string[]]$Steps)
    Write-Host ""
    Write-Host "ERROR: $What" -ForegroundColor Red
    Write-Host "$Why" -ForegroundColor Red
    if ($Steps) {
        Write-Host ""
        Write-Host "What to do:"
        $i = 1
        foreach ($step in $Steps) {
            Write-Host "  $i. $step"
            $i++
        }
    }
    Write-Host ""
    exit 1
}

function Invoke-Checked {
    param([string]$Description, [scriptblock]$Command, [string[]]$Steps)
    & $Command
    if ($LASTEXITCODE -ne 0) {
        Stop-WithHelp -What "$Description failed (exit code $LASTEXITCODE)" `
            -Why "The environment is half-built; the wizard would not start correctly." `
            -Steps $Steps
    }
}

function Get-PythonVersion {
    <# Returns [int[]]@(major, minor), or $null if the executable is no good. #>
    param([string]$Exe, [string[]]$PreArgs = @())
    try {
        $output = & $Exe @PreArgs -c "import sys; print(f'{sys.version_info[0]} {sys.version_info[1]}')" 2>$null
    } catch {
        return $null
    }
    if ($LASTEXITCODE -ne 0 -or -not $output) { return $null }
    $parts = ($output | Select-Object -First 1).Trim() -split '\s+'
    if ($parts.Count -lt 2) { return $null }
    return @([int]$parts[0], [int]$parts[1])
}

function Find-Python {
    <#
    The `py` launcher comes first: on Windows it is what actually knows which
    versions are installed. A bare `python` may be the Microsoft Store stub,
    which exists on PATH but only opens the store.
    #>
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in @('-3.14', '-3.13', '-3.12', '-3.11', '-3')) {
            $candidates += , @('py', @($v))
        }
    }
    foreach ($name in @('python3', 'python')) {
        if (Get-Command $name -ErrorAction SilentlyContinue) {
            $candidates += , @($name, @())
        }
    }

    foreach ($candidate in $candidates) {
        $version = Get-PythonVersion -Exe $candidate[0] -PreArgs $candidate[1]
        if ($version -and ($version[0] -gt 3 -or ($version[0] -eq 3 -and $version[1] -ge $MinPythonMinor))) {
            return [pscustomobject]@{
                Exe     = $candidate[0]
                PreArgs = $candidate[1]
                Version = "$($version[0]).$($version[1])"
            }
        }
    }
    return $null
}

# --- 1. Interpreter ------------------------------------------------------------

Write-Step "Looking for Python >= 3.$MinPythonMinor"
$python = Find-Python
if (-not $python) {
    Stop-WithHelp -What "No Python 3.$MinPythonMinor or newer was found" `
        -Why "axion-wizard uses syntax and libraries that do not exist in earlier versions." `
        -Steps @(
            "Install Python from https://www.python.org/downloads/ (tick 'Add to PATH').",
            "Or install it with winget: winget install Python.Python.3.12",
            "Close and reopen the terminal so PATH is refreshed, then retry."
        )
}
Write-Ok "Python $($python.Version) — $($python.Exe) $($python.PreArgs -join ' ')"

# --- 2. Environment and dependencies -------------------------------------------

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$HasUv = [bool](Get-Command uv -ErrorAction SilentlyContinue)

if ($HasUv) {
    Write-Step "Installing dependencies with uv (honours uv.lock)"
    Invoke-Checked "uv sync" { uv sync --group dev } -Steps @(
        "Check the internet connection (uv downloads from PyPI).",
        "Retry with more detail: uv sync --group dev --verbose"
    )
} else {
    Write-Warn "uv is not installed; using venv + pip (slower and without a lockfile)."
    Write-Warn "For reproducible builds: winget install astral-sh.uv"

    if (-not (Test-Path $VenvPython)) {
        Write-Step "Creating the virtual environment in .venv"
        Invoke-Checked "python -m venv" { & $python.Exe @($python.PreArgs) -m venv .venv } -Steps @(
            "Check there is space and write permission on $ProjectRoot.",
            "Delete .venv if it was left over from an earlier attempt, then retry."
        )
    } else {
        Write-Ok "The virtual environment already exists."
    }

    # A .venv created by uv has no pip: uv installs packages itself and spares
    # itself the trouble. If this script falls back to the pip path over such an
    # environment (uv installed earlier, no longer), `python -m pip` fails with
    # "No module named pip" and the bootstrap dies for no visible reason.
    # `ensurepip` puts it back.
    & $VenvPython -m pip --version *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Step "The environment has no pip (uv created it); restoring it with ensurepip"
        Invoke-Checked "ensurepip" { & $VenvPython -m ensurepip --default-pip --upgrade } -Steps @(
            "Delete the .venv folder and run this script again to create it clean."
        )
    }

    Write-Step "Installing axion-wizard and its dependencies"
    Invoke-Checked "pip install -e ." { & $VenvPython -m pip install --quiet --disable-pip-version-check -e . } -Steps @(
        "Check the internet connection (pip downloads from PyPI).",
        "Retry without --quiet to see the full error: .venv\Scripts\python -m pip install -e ."
    )

    if ($Check) {
        # The development tools live in [dependency-groups] (PEP 735), which pip
        # only understands from 25.1 onwards — they are installed by name so as
        # not to depend on whatever pip version the system ships.
        # `fastapi` and `python-multipart` are not dependencies of the wizard:
        # the bridge only runs inside the container. But its tests do need them
        # and, without them, they are skipped silently — which is worse than
        # failing, because `-Check` would say "all green" without having tested
        # the code that gets shipped to the user.
        Write-Step "Installing development tools"
        Invoke-Checked "pip install (dev)" {
            & $VenvPython -m pip install --quiet --disable-pip-version-check pytest pytest-cov pytest-mock ruff mypy fastapi python-multipart
        } -Steps @("Check the internet connection.")
    }
}

if (-not (Test-Path $VenvPython)) {
    Stop-WithHelp -What "The virtual environment was not created in .venv" `
        -Why "Without it there is nowhere to run the wizard." `
        -Steps @("Delete the .venv folder and run this script again.")
}
Write-Ok "Environment ready: $VenvPython"

# --- 3. Optional verification ---------------------------------------------------

if ($Check) {
    Write-Step "Lint (ruff)"
    Invoke-Checked "ruff" { & $VenvPython -m ruff check . } -Steps @(
        "Fix automatically whatever can be fixed: .venv\Scripts\python -m ruff check --fix ."
    )
    Write-Step "Types (mypy)"
    Invoke-Checked "mypy" { & $VenvPython -m mypy src } -Steps @(
        "Review the type errors listed above."
    )
    Write-Step "Tests (pytest)"
    Invoke-Checked "pytest" { & $VenvPython -m pytest -q } -Steps @(
        "Reproduce one specific failure: .venv\Scripts\python -m pytest -k <name> -vv"
    )
    Write-Ok "Lint, types and tests all green."
}

# --- 4. Start ------------------------------------------------------------------

if ($NoRun) {
    Write-Host ""
    Write-Ok "Environment prepared. Start it with:"
    Write-Host "    .venv\Scripts\python.exe -m axion_wizard --help"
    exit 0
}

$runArgs = @()
if ($WizardArgs) { $runArgs = $WizardArgs }
if ($runArgs.Count -eq 0) { $runArgs = @('--help') }

Write-Step "Starting axion-wizard $($runArgs -join ' ')"
Write-Host ""
& $VenvPython -m axion_wizard @runArgs
exit $LASTEXITCODE
