#Requires -Version 5.1
<#
.SYNOPSIS
    Native dev run ("Method 2"): the agent runs on THIS machine via uvicorn,
    with only Qdrant in Docker. The all-in-Docker stack is `docker compose up`.

.DESCRIPTION
    Checks the venv and .env, brings up the Qdrant container, points the agent
    at it, and starts uvicorn with reload. Reload watches only the source
    folders - mem/ holds the chat-memory sqlite file, which is written on every
    turn and would otherwise restart the server mid-conversation.

.EXAMPLE
    .\dev-run.ps1                 # Qdrant + agent on http://127.0.0.1:8001
    .\dev-run.ps1 -Port 8080
    .\dev-run.ps1 -NoQdrant       # tools-only, no vector store
    .\dev-run.ps1 -Install        # (re)install requirements first
    .\dev-run.ps1 -Test           # run the test suite instead of serving
#>
[CmdletBinding()]
param(
    [int]$Port = 8001,
    [string]$BindHost = '127.0.0.1',
    [switch]$NoQdrant,
    [switch]$NoReload,
    [switch]$Install,
    [switch]$Test
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

function Write-Step ($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Warn ($msg) { Write-Host "  ! $msg" -ForegroundColor Yellow }
function Write-Ok   ($msg) { Write-Host "  + $msg" -ForegroundColor Green }

# Windows PowerShell 5.1 turns anything an .exe writes to stderr into an
# ErrorRecord, so under ErrorActionPreference='Stop' a docker warning or a pip
# notice would abort the script. Native calls run with that relaxed and are
# judged on their EXIT CODE instead, which is the only reliable signal.
function Invoke-Native {
    param([Parameter(Mandatory = $true)][scriptblock]$Command)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Command } finally { $ErrorActionPreference = $previous }
}

# ── venv ────────────────────────────────────────────────────────────────────
$venvPython = Join-Path $PSScriptRoot 'venv\Scripts\python.exe'

if (-not (Test-Path $venvPython)) {
    Write-Step 'Creating virtual environment (venv\)'
    $launcher = (Get-Command py -ErrorAction SilentlyContinue)
    if ($null -ne $launcher) { Invoke-Native { & py -3 -m venv venv } }
    else { Invoke-Native { & python -m venv venv } }
    if (-not (Test-Path $venvPython)) { throw "venv creation failed - no $venvPython" }
    $Install = $true
}

if ($Install) {
    Write-Step 'Installing requirements'
    Invoke-Native { & $venvPython -m pip install --upgrade pip --quiet }
    Invoke-Native { & $venvPython -m pip install -r requirements.txt }
    if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }
}

# ── .env ────────────────────────────────────────────────────────────────────
# api/main.py loads it itself; this only catches the missing-file case early,
# with a clearer message than a stack trace about a blank API token.
if (-not (Test-Path (Join-Path $PSScriptRoot '.env'))) {
    Write-Warn 'No .env found. Create one from the template and fill it in:'
    Write-Host '      Copy-Item .env.example .env' -ForegroundColor Gray
    throw '.env is required'
}

# ── tests ───────────────────────────────────────────────────────────────────
if ($Test) {
    Write-Step 'Running tests'
    $env:PYTHONPATH = $PSScriptRoot
    Invoke-Native { & $venvPython -m unittest discover -s tests -p "test_*.py" }
    exit $LASTEXITCODE
}

# ── Qdrant (RAG vector store) ───────────────────────────────────────────────
# Without QDRANT_URL the agent starts tools-only: no knowledge search, every
# other feature unchanged. That is a valid dev mode, not an error.
$qdrantUrl = 'http://127.0.0.1:6333'

if ($NoQdrant) {
    Write-Warn 'Qdrant skipped (-NoQdrant): knowledge search is OFF'
}
else {
    Write-Step 'Starting Qdrant'
    if ($null -eq (Get-Command docker -ErrorAction SilentlyContinue)) {
        Write-Warn 'docker not found - continuing without knowledge search'
        $NoQdrant = $true
    }
    else {
        # compose v2 (docker compose) with a fallback to the v1 binary
        Invoke-Native { docker compose up -d qdrant }
        if ($LASTEXITCODE -ne 0 -and (Get-Command docker-compose -ErrorAction SilentlyContinue)) {
            Invoke-Native { docker-compose up -d qdrant }
        }

        $ready = $false
        foreach ($attempt in 1..20) {
            try {
                Invoke-RestMethod -Uri "$qdrantUrl/readyz" -TimeoutSec 2 | Out-Null
                $ready = $true
                break
            }
            catch { Start-Sleep -Milliseconds 500 }
        }
        if ($ready) {
            Write-Ok "Qdrant ready at $qdrantUrl"
        }
        else {
            Write-Warn "Qdrant did not answer at $qdrantUrl - continuing without knowledge search"
            $NoQdrant = $true
        }
    }
}

if ($NoQdrant) {
    Remove-Item Env:\QDRANT_URL -ErrorAction SilentlyContinue
}
else {
    # QDRANT_URL is deliberately absent from .env (the compose stack sets its
    # own), so this process value is what core/manager.py reads
    $env:QDRANT_URL = $qdrantUrl
}

# ── serve ───────────────────────────────────────────────────────────────────
$env:PYTHONPATH = $PSScriptRoot

$uvicornArgs = @(
    '-m', 'uvicorn', 'api.main:app',
    '--host', $BindHost,
    '--port', $Port
)

if (-not $NoReload) {
    # watch source only - mem/ (chat-memory sqlite) and logs/ are written
    # during normal operation and would trigger a restart on every message
    $uvicornArgs += @(
        '--reload',
        '--reload-dir', 'api',
        '--reload-dir', 'core',
        '--reload-dir', 'infra',
        '--reload-dir', 'services',
        '--reload-dir', 'tools'
    )
}

$base = "http://$BindHost`:$Port"
Write-Host ''
Write-Step "Agent starting on $base"
Write-Host "     health   $base/health"          -ForegroundColor Gray
Write-Host "     docs     $base/docs"            -ForegroundColor Gray
Write-Host "     webchat  POST $base/webhooks/webchat/{channel_id}/session[?institution_id=N]" -ForegroundColor Gray
if ($NoQdrant) { Write-Host '     knowledge search OFF (no Qdrant)' -ForegroundColor Gray }
Write-Host '     Ctrl+C to stop (Qdrant keeps running: docker compose down)' -ForegroundColor DarkGray
Write-Host ''

Invoke-Native { & $venvPython @uvicornArgs }
exit $LASTEXITCODE
