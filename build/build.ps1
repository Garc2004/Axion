#!/usr/bin/env pwsh
# Packages axion-wizard.exe on Windows (§7). There is no cross-compilation:
# this script only runs in a `windows-latest` job.
#
# Note: `$ErrorActionPreference = "Stop"` is not used around the native commands
# (`uv`, `pyinstaller`) — in PowerShell 5.1 a native exe that writes to stderr
# (uv does, for its progress log, not just for errors) can be treated as a
# terminating error under certain configurations (output redirection in CI, for
# instance), aborting the build even though the command exited 0. Instead,
# `$LASTEXITCODE` is checked explicitly after each one.

Set-Location (Join-Path $PSScriptRoot "..")

function Invoke-Checked {
    param([string]$Description, [scriptblock]$Command)
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

# The environment is always built by `bootstrap.ps1`, which knows how to do it
# with or without uv. This script used to call `uv sync` directly and died with
# a "term not recognized" on any machine without uv installed — including that
# of someone who only wants to package the binary once.
Invoke-Checked "bootstrap" { & (Join-Path $PSScriptRoot "..\scripts\bootstrap.ps1") -NoRun }

$VenvPython = Join-Path (Get-Location) ".venv\Scripts\python.exe"

# PyInstaller is a development dependency: `uv sync --group dev` already brings
# it, but on the pip path it has to be asked for separately.
& $VenvPython -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Invoke-Checked "pip install pyinstaller" {
        & $VenvPython -m pip install --quiet --disable-pip-version-check pyinstaller
    }
}

Invoke-Checked "pyinstaller" {
    & $VenvPython -m PyInstaller build/axion-wizard.spec `
        --distpath dist --workpath build/work --noconfirm
}

$exePath = "dist/axion-wizard.exe"
if (-not (Test-Path $exePath)) {
    throw "Build failed: $exePath was not produced"
}

$hash = (Get-FileHash -Algorithm SHA256 $exePath).Hash.ToLower()

# This is written with .NET rather than Out-File for two reasons, and both break
# verification with `sha256sum -c` on Linux if left unattended:
#
#   - Line ending: Out-File emits CRLF, and `sha256sum -c` takes the \r as part
#     of the filename ("axion-wizard.exe\r: No such file").
#   - BOM: `-Encoding utf8` on Windows PowerShell 5.1 prepends one, which
#     dirties the first hash in the list.
#
# And only THIS binary's line is replaced, keeping the rest: with `-Append`,
# every build left one more line for the same file and `sha256sum -c` ended up
# validating against hashes from earlier builds, failing on all but the last;
# but overwriting the whole file erased the Linux binary's hash, so having both
# at once was impossible.
$checksumsPath = Join-Path (Get-Location) "dist\checksums.txt"
$lines = @()
if (Test-Path $checksumsPath) {
    # `[\s*]`, not `\s`: `sha256sum` in binary mode — the default under Git
    # Bash on Windows — writes `<hash> *name`, so the character before the name
    # is an asterisk and a `\s`-anchored filter walks straight past its own
    # earlier line. That is how a stale entry for this same file survived a
    # rebuild and made `sha256sum -c` fail on a binary that was perfectly fine.
    $lines = @(Get-Content $checksumsPath | Where-Object { $_ -and ($_ -notmatch '[\s*]axion-wizard\.exe$') })
}
$lines += "$hash  axion-wizard.exe"
$noBomAscii = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($checksumsPath, (($lines | Sort-Object) -join "`n") + "`n", $noBomAscii)

Write-Host "Build complete: $exePath"
Write-Host ""
Write-Host "Elevation: by default the binary starts unelevated and asks for UAC only for"
Write-Host "the subcommands that need it (see privileges.ensure_elevated)."
Write-Host "For a binary that ALWAYS asks for UAC on launch, build with:"
Write-Host '    $env:AXION_UAC_ADMIN = "1"; .\build\build.ps1'
Write-Host "Careful: that variant will not start from an unelevated console and it breaks"
Write-Host "--unattended."
Write-Host ""
Write-Host "Mandatory post-build verification (section 7.2): run this binary on a machine"
Write-Host "with no Python installed and execute 'axion-wizard doctor'."
