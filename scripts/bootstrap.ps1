#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Deja axion-wizard listo para usar en un solo comando (Windows).

.DESCRIPTION
    Busca un Python >= 3.11, crea el entorno virtual, instala las
    dependencias y arranca el wizard. Es idempotente: volver a ejecutarlo
    sobre un entorno ya montado solo actualiza lo que haga falta.

    Usa `uv` si está disponible (respeta uv.lock, así que el entorno es
    reproducible) y cae a `venv` + `pip` si no lo está. No instala uv por su
    cuenta: meter una herramienta global en la máquina de alguien sin
    pedirlo no es cosa de un script de arranque.

.PARAMETER Check
    Instala además las herramientas de desarrollo y corre lint, tipos y tests
    antes de arrancar.

.PARAMETER NoRun
    Solo prepara el entorno; no arranca el wizard.

.PARAMETER WizardArgs
    Lo que sobre se le pasa tal cual al wizard.

.EXAMPLE
    .\scripts\bootstrap.ps1
    Instala todo y abre el wizard.

.EXAMPLE
    .\scripts\bootstrap.ps1 -Check doctor
    Instala, verifica el repo y corre `axion-wizard doctor`.
#>
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$NoRun,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$WizardArgs
)

# Nada de `$ErrorActionPreference = "Stop"` alrededor de comandos nativos: en
# PowerShell 5.1 un exe que escribe a stderr (pip y uv lo hacen para su log de
# progreso, no solo para errores) puede tratarse como error terminante bajo
# ciertas configuraciones y abortar el arranque con código 0. Se comprueba
# $LASTEXITCODE explícitamente después de cada uno.

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$MinPythonMinor = 11

function Write-Step { param([string]$Message) Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Message) Write-Host "    $Message" -ForegroundColor Green }
function Write-Warn { param([string]$Message) Write-Host "    $Message" -ForegroundColor Yellow }

function Stop-WithHelp {
    <#
    Un fallo de arranque tiene que decir qué pasó y qué hacer, no solo
    reventar: quien corre esto está montando el entorno por primera vez y no
    tiene contexto para interpretar un stack trace.
    #>
    param([string]$What, [string]$Why, [string[]]$Steps)
    Write-Host ""
    Write-Host "ERROR: $What" -ForegroundColor Red
    Write-Host "$Why" -ForegroundColor Red
    if ($Steps) {
        Write-Host ""
        Write-Host "Qué hacer:"
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
        Stop-WithHelp -What "$Description falló (código $LASTEXITCODE)" `
            -Why "El entorno ha quedado a medias; el wizard no arrancaría correctamente." `
            -Steps $Steps
    }
}

function Get-PythonVersion {
    <# Devuelve [int[]]@(major, minor) o $null si el ejecutable no sirve. #>
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
    El lanzador `py` va primero: en Windows es quien sabe qué versiones hay
    instaladas de verdad. `python` a secas puede ser el stub de Microsoft
    Store, que existe en PATH pero solo abre la tienda.
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

# --- 1. Intérprete -------------------------------------------------------------

Write-Step "Buscando Python >= 3.$MinPythonMinor"
$python = Find-Python
if (-not $python) {
    Stop-WithHelp -What "No se encontró un Python 3.$MinPythonMinor o superior" `
        -Why "axion-wizard usa sintaxis y librerías que no existen en versiones anteriores." `
        -Steps @(
            "Instalar Python desde https://www.python.org/downloads/ (marcar 'Add to PATH').",
            "O instalarlo con winget: winget install Python.Python.3.12",
            "Cerrar y reabrir la terminal para que el PATH se actualice, y reintentar."
        )
}
Write-Ok "Python $($python.Version) — $($python.Exe) $($python.PreArgs -join ' ')"

# --- 2. Entorno y dependencias --------------------------------------------------

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$HasUv = [bool](Get-Command uv -ErrorAction SilentlyContinue)

if ($HasUv) {
    Write-Step "Instalando dependencias con uv (respeta uv.lock)"
    Invoke-Checked "uv sync" { uv sync --group dev } -Steps @(
        "Comprobar la conexión a internet (uv descarga desde PyPI).",
        "Reintentar con más detalle: uv sync --group dev --verbose"
    )
} else {
    Write-Warn "uv no está instalado; usando venv + pip (más lento y sin lockfile)."
    Write-Warn "Para builds reproducibles: winget install astral-sh.uv"

    if (-not (Test-Path $VenvPython)) {
        Write-Step "Creando el entorno virtual en .venv"
        Invoke-Checked "python -m venv" { & $python.Exe @($python.PreArgs) -m venv .venv } -Steps @(
            "Comprobar que hay espacio y permisos de escritura en $ProjectRoot.",
            "Borrar .venv si quedó de un intento anterior y reintentar."
        )
    } else {
        Write-Ok "El entorno virtual ya existe."
    }

    # Un .venv creado por uv no trae pip: uv instala paquetes él mismo y se lo
    # ahorra. Si este script cae al camino de pip sobre un entorno así (uv
    # instalado antes, ya no), `python -m pip` falla con "No module named pip"
    # y el arranque muere sin motivo aparente. `ensurepip` lo repone.
    & $VenvPython -m pip --version *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Step "El entorno no tiene pip (lo creó uv); reponiéndolo con ensurepip"
        Invoke-Checked "ensurepip" { & $VenvPython -m ensurepip --default-pip --upgrade } -Steps @(
            "Borrar la carpeta .venv y volver a ejecutar este script para crearla limpia."
        )
    }

    Write-Step "Instalando axion-wizard y sus dependencias"
    Invoke-Checked "pip install -e ." { & $VenvPython -m pip install --quiet --disable-pip-version-check -e . } -Steps @(
        "Comprobar la conexión a internet (pip descarga desde PyPI).",
        "Reintentar sin --quiet para ver el error completo: .venv\Scripts\python -m pip install -e ."
    )

    if ($Check) {
        # Las herramientas de desarrollo viven en [dependency-groups] (PEP 735),
        # que pip solo entiende desde 25.1 — se instalan por nombre para no
        # depender de la versión de pip que traiga el sistema.
        # `fastapi` y `python-multipart` no son dependencias del wizard: el
        # puente solo corre dentro del contenedor. Pero sus tests sí las
        # necesitan y, sin ellas, se saltan en silencio — que es peor que
        # fallar, porque `-Check` diría "todo en verde" sin haber probado el
        # código que se envía al usuario.
        Write-Step "Instalando herramientas de desarrollo"
        Invoke-Checked "pip install (dev)" {
            & $VenvPython -m pip install --quiet --disable-pip-version-check pytest pytest-cov pytest-mock ruff mypy fastapi python-multipart
        } -Steps @("Comprobar la conexión a internet.")
    }
}

if (-not (Test-Path $VenvPython)) {
    Stop-WithHelp -What "El entorno virtual no quedó creado en .venv" `
        -Why "Sin él no hay dónde ejecutar el wizard." `
        -Steps @("Borrar la carpeta .venv y volver a ejecutar este script.")
}
Write-Ok "Entorno listo: $VenvPython"

# --- 3. Verificación opcional ----------------------------------------------------

if ($Check) {
    Write-Step "Lint (ruff)"
    Invoke-Checked "ruff" { & $VenvPython -m ruff check . } -Steps @(
        "Corregir automáticamente lo que se pueda: .venv\Scripts\python -m ruff check --fix ."
    )
    Write-Step "Tipos (mypy)"
    Invoke-Checked "mypy" { & $VenvPython -m mypy src } -Steps @(
        "Revisar los errores de tipos indicados arriba."
    )
    Write-Step "Tests (pytest)"
    Invoke-Checked "pytest" { & $VenvPython -m pytest -q } -Steps @(
        "Reproducir un fallo concreto: .venv\Scripts\python -m pytest -k <nombre> -vv"
    )
    Write-Ok "Lint, tipos y tests en verde."
}

# --- 4. Arranque -------------------------------------------------------------------

if ($NoRun) {
    Write-Host ""
    Write-Ok "Entorno preparado. Arrancar con:"
    Write-Host "    .venv\Scripts\python.exe -m axion_wizard --help"
    exit 0
}

$runArgs = @()
if ($WizardArgs) { $runArgs = $WizardArgs }
if ($runArgs.Count -eq 0) { $runArgs = @('--help') }

Write-Step "Arrancando axion-wizard $($runArgs -join ' ')"
Write-Host ""
& $VenvPython -m axion_wizard @runArgs
exit $LASTEXITCODE
