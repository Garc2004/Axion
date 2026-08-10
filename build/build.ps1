#!/usr/bin/env pwsh
# Empaqueta axion-wizard.exe en Windows (§7). No hay cross-compilation: este
# script solo se corre en un job `windows-latest`.
#
# Nota: no usamos `$ErrorActionPreference = "Stop"` alrededor de los
# comandos nativos (`uv`, `pyinstaller`) — en PowerShell 5.1, un exe nativo
# que escribe a stderr (uv lo hace para su log de progreso, no solo para
# errores) puede tratarse como error terminante bajo ciertas
# configuraciones (p.ej. redirección de salida en CI), abortando el build
# aunque el comando haya terminado con código 0. Comprobamos
# `$LASTEXITCODE` explícitamente después de cada uno en su lugar.

Set-Location (Join-Path $PSScriptRoot "..")

function Invoke-Checked {
    param([string]$Description, [scriptblock]$Command)
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Description falló con código de salida $LASTEXITCODE"
    }
}

# El entorno lo monta siempre `bootstrap.ps1`, que sabe hacerlo con uv o sin
# él. Antes este script llamaba a `uv sync` directamente y moría con un
# "término no reconocido" en cualquier máquina sin uv instalado — incluida la
# de quien solo quiere empaquetar el binario una vez.
Invoke-Checked "bootstrap" { & (Join-Path $PSScriptRoot "..\scripts\bootstrap.ps1") -NoRun }

$VenvPython = Join-Path (Get-Location) ".venv\Scripts\python.exe"

# PyInstaller es dependencia de desarrollo: con `uv sync --group dev` ya está,
# pero por el camino de pip hay que pedirlo aparte.
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
    throw "Build falló: no se generó $exePath"
}

$hash = (Get-FileHash -Algorithm SHA256 $exePath).Hash.ToLower()

# Se escribe con .NET y no con Out-File por dos motivos, y ambos rompen la
# verificación con `sha256sum -c` en Linux si se descuidan:
#
#   - Salto de línea: Out-File emite CRLF, y `sha256sum -c` se lleva el \r
#     dentro del nombre del archivo ("axion-wizard.exe\r: No such file").
#   - BOM: `-Encoding utf8` en Windows PowerShell 5.1 antepone uno, que
#     ensucia el primer hash de la lista.
#
# Y se reemplaza la línea de ESTE binario conservando las demás: con
# `-Append`, cada build dejaba una línea más para el mismo archivo y
# `sha256sum -c` acababa validando contra hashes de builds anteriores,
# fallando en todos menos el último; pero sobrescribir el archivo entero
# borraba el hash del binario de Linux, así que tener los dos a la vez era
# imposible.
$checksumsPath = Join-Path (Get-Location) "dist\checksums.txt"
$lines = @()
if (Test-Path $checksumsPath) {
    $lines = @(Get-Content $checksumsPath | Where-Object { $_ -and ($_ -notmatch '\saxion-wizard\.exe$') })
}
$lines += "$hash  axion-wizard.exe"
$noBomAscii = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($checksumsPath, (($lines | Sort-Object) -join "`n") + "`n", $noBomAscii)

Write-Host "Build completo: $exePath"
Write-Host ""
Write-Host "Elevacion: por defecto el binario arranca sin elevar y pide UAC solo para"
Write-Host "los subcomandos que lo necesitan (ver privileges.ensure_elevated)."
Write-Host "Para un binario que SIEMPRE pida UAC al abrirse, construir con:"
Write-Host '    $env:AXION_UAC_ADMIN = "1"; .\build\build.ps1'
Write-Host "Ojo: esa variante no arranca desde una consola sin elevar y rompe --unattended."
Write-Host ""
Write-Host "Verificacion post-build obligatoria (parrafo 7.2): ejecutar este binario en"
Write-Host "una maquina sin Python instalado y correr 'axion-wizard doctor'."
