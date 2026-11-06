# ======================================================================
#  pypimgr.ps1  -  Domshon Setup / Run / Build / Update Manager  (Windows)
#  Requirements: PowerShell 5.1+, Python 3.10+ (git optional, docker optional)
#  Usage: powershell -ExecutionPolicy Bypass -File scripts\pypimgr\pypimgr.ps1 [command]
#         commands: setup | init | serve | gui | build | update | docker | service | passwd | pypi
#
#  Everything installs into <project>\.venv - nothing touches system Python.
# ======================================================================

#Requires -Version 5.1
param([string]$Command = '', [string]$Arg = '')
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'   # native stderr (e.g. failed import probes) must not abort the script

$SCRIPT_DIR  = Split-Path -Parent $MyInvocation.MyCommand.Path
$PROJECT_DIR = if ($env:PROJECT_DIR) { $env:PROJECT_DIR } else { (Resolve-Path (Join-Path $SCRIPT_DIR '..\..')).Path }
$VENV_DIR    = Join-Path $PROJECT_DIR '.venv'
$VPY         = Join-Path $VENV_DIR 'Scripts\python.exe'

# ------ UI helpers ---------------------------------------------------------------
function clr($t, $c) { Write-Host $t -ForegroundColor $c -NoNewline }
function Write-Hr    { Write-Host ('-' * 72) -ForegroundColor DarkGray }
function Write-Ok($m)   { Write-Host '  ' -NoNewline; clr '[OK]' 'Green';  Write-Host "  $m" }
function Write-Warn($m) { Write-Host '  ' -NoNewline; clr '[!]'  'Yellow'; Write-Host "  $m" }
function Write-Err($m)  { Write-Host '  ' -NoNewline; clr '[X]'  'Red';    Write-Host "  $m" }
function Write-Info($m) { Write-Host '  ' -NoNewline; clr '[*]'  'Blue';   Write-Host "  $m" }
function Wait-AnyKey { Write-Host ''; Write-Host '  Press any key to continue...' -ForegroundColor DarkGray -NoNewline; [void][Console]::ReadKey($true); Write-Host '' }
function Confirm-Yes($q) { $r = Read-Host "  ? $q [y/N]"; return ($r -match '^(y|yes)$') }
function Write-Banner($t) { Write-Host ''; Write-Hr; Write-Host "  $t" -ForegroundColor Cyan; Write-Hr }
function Ask($q, $def) { $v = Read-Host ("  {0} [{1}]" -f $q, $def); if ([string]::IsNullOrWhiteSpace($v)) { $def } else { $v } }

# Runs a native command; returns $true on exit code 0 (never throws on non-zero).
function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments)
    & $Exe @Arguments | Out-Host
    return ($LASTEXITCODE -eq 0)
}

function Find-Python {
    $cands = @()
    if ($env:PYTHON) { $cands += , @($env:PYTHON) }
    $cands += , @('py', '-3'); $cands += , @('python'); $cands += , @('python3')
    foreach ($c in $cands) {
        if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
        $rest = @(); if ($c.Count -gt 1) { $rest = $c[1..($c.Count - 1)] }
        & $c[0] @rest -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { return , $c }
    }
    return $null
}

function Test-Venv { Test-Path $VPY }
function Need-Venv { if (-not (Test-Venv)) { Write-Warn 'Not set up yet - running setup first.'; return (Do-Setup) } return $true }

function Set-EnvVar($key, $value) {
    $f = Join-Path $PROJECT_DIR '.env'
    $lines = @(); if (Test-Path $f) { $lines = @(Get-Content $f) }
    $found = $false
    $lines = @($lines | ForEach-Object { if ($_ -match "^$key=") { $found = $true; "$key=$value" } else { $_ } })
    if (-not $found) { $lines += "$key=$value" }
    Set-Content -Path $f -Value $lines -Encoding UTF8
}

# ------ Steps --------------------------------------------------------------------
function Do-Setup {
    Write-Banner 'Setup: fetch dependencies'
    $py = Find-Python
    if (-not $py) { Write-Err 'Python 3.10+ not found (install it or set PYTHON)'; return $false }
    if (-not (Test-Venv)) {
        Write-Info 'Creating virtual environment (.venv) ...'
        $rest = @(); if ($py.Count -gt 1) { $rest = $py[1..($py.Count - 1)] }
        if (-not (Invoke-Native $py[0] ($rest + @('-m', 'venv', $VENV_DIR)))) { Write-Err 'venv creation failed'; return $false }
    }
    [void](Invoke-Native $VPY @('-m', 'pip', 'install', '--quiet', '--upgrade', 'pip'))
    $extras = @()
    if ((Ask 'Install the desktop app too ([gui], PySide6)? y/n' 'y') -match '^y') { $extras += 'gui' }
    if ((Ask 'Install development tools ([dev,build] - only for working on Domshon)? y/n' 'n') -match '^y') { $extras += 'dev'; $extras += 'build' }
    $target = $PROJECT_DIR; if ($extras.Count) { $target = "$PROJECT_DIR[$($extras -join ',')]" }
    Write-Info "Installing Domshon (editable) ..."
    if (-not (Invoke-Native $VPY @('-m', 'pip', 'install', '-e', $target))) { Write-Err 'pip install failed'; return $false }
    Write-Ok "Dependencies installed in $VENV_DIR"
    return $true
}

function Do-Init {
    Write-Banner 'Initialize'
    if (-not (Need-Venv)) { return }
    Push-Location $PROJECT_DIR
    try {
        if (-not (Test-Path '.env')) { Copy-Item '.env.example' '.env'; Write-Ok 'Created .env from .env.example' }
        else { Write-Info '.env already exists - keeping it' }
        $domain = Ask 'Public domain (blank = none yet)' ''
        $port   = Ask 'Server port' '8787'
        Set-EnvVar 'DS_DOMAIN' $domain; Set-EnvVar 'DS_PORT' $port
        if ($domain) { Set-EnvVar 'DS_TRUST_PROXY' 'true' }
        Write-Ok 'Settings written to .env (the running server reloads it live, except host/port/proxy)'
        if (Confirm-Yes 'Set the admin password now?') { Do-Passwd }
        & $VPY -m domshon server show
    } finally { Pop-Location }
}

function Do-Passwd { if (-not (Need-Venv)) { return }; Push-Location $PROJECT_DIR; try { & $VPY -m domshon server passwd } finally { Pop-Location } }
function Do-Serve  { Write-Banner 'Server (Ctrl+C to stop)'; if (-not (Need-Venv)) { return }; Push-Location $PROJECT_DIR; try { & $VPY -m domshon server serve } finally { Pop-Location } }
function Do-Gui {
    if (-not (Need-Venv)) { return }
    & $VPY -c 'import PySide6' 2>$null
    if ($LASTEXITCODE -ne 0) { Write-Info 'Installing desktop support...'; if (-not (Invoke-Native $VPY @('-m', 'pip', 'install', '--quiet', '-e', "$PROJECT_DIR[gui]"))) { return } }
    & $VPY -m domshon gui
}

function Do-Build {
    Write-Banner 'Build applications'
    if (-not (Need-Venv)) { return }
    & $VPY -c 'import PyInstaller, PySide6' 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Info 'Installing build dependencies...'
        if (-not (Invoke-Native $VPY @('-m', 'pip', 'install', '--quiet', '-e', "$PROJECT_DIR[gui,build]"))) { Write-Err 'install failed'; return }
    }
    $srv = Ask 'Default server address to bake in' "$env:DS_BUILD_SERVER"
    $a = @((Join-Path $PROJECT_DIR 'scripts\build.py'), '--package'); if ($srv) { $a += @('--server', $srv) }
    if (Invoke-Native $VPY $a) { Write-Ok "Built. See $PROJECT_DIR\dist" } else { Write-Err 'Build failed' }
}

function Do-Update {
    Write-Banner 'Update'
    Push-Location $PROJECT_DIR
    try {
        if ((Test-Path '.git') -and (Get-Command git -ErrorAction SilentlyContinue)) {
            Write-Info 'Pulling latest source...'
            if (-not (Invoke-Native 'git' @('pull', '--ff-only'))) { Write-Err 'git pull failed (local changes?)'; return }
        } else { Write-Warn 'Not a git checkout - skipping source update.' }
        if (-not (Test-Venv)) { Write-Warn 'No .venv - run setup.'; return }
        [void](Invoke-Native $VPY @('-m', 'pip', 'install', '--quiet', '--upgrade', 'pip'))
        if (Invoke-Native $VPY @('-m', 'pip', 'install', '--upgrade', '-e', $PROJECT_DIR)) { Write-Ok 'Updated.' } else { Write-Err 'pip install failed' }
    } finally { Pop-Location }
}

function Do-Docker([string]$c) {
    Write-Banner 'Docker (testing / containerised server)'
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Write-Err 'docker not found'; return }
    Push-Location $PROJECT_DIR
    try {
        if (-not $c) {
            Write-Host '   1) build + start   2) stop   3) logs   4) start with nginx HTTPS (needs DS_DOMAIN + certificate, see README)   0) back'
            switch (Read-Host '  >') { '1' { $c = 'up' } '2' { $c = 'down' } '3' { $c = 'logs' } '4' { $c = 'https' } default { return } }
        }
        if (-not (Test-Path '.env')) { Copy-Item '.env.example' '.env'; Write-Ok 'Created .env from .env.example' }
        switch ($c) {
            'up'    { [void](Invoke-Native 'docker' @('compose', 'up', '-d', '--build')) }
            'down'  { [void](Invoke-Native 'docker' @('compose', 'down')) }
            'logs'  { [void](Invoke-Native 'docker' @('compose', 'logs', '-f', '--tail=100')) }
            'https' { [void](Invoke-Native 'docker' @('compose', '--profile', 'https', 'up', '-d', '--build')) }
            default { Write-Err "unknown docker command '$c' (up|down|logs|https)" }
        }
    } finally { Pop-Location }
}

function Do-Service {
    Write-Banner 'Install server to start at boot (Task Scheduler)'
    if (-not (Need-Venv)) { return }
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin) { Write-Err 'Run this from an elevated (Administrator) PowerShell.'; return }
    Push-Location $PROJECT_DIR
    try { & $VPY -m domshon autostart enable --server } finally { Pop-Location }
}


function Do-Pypi([string]$c) {
    Write-Banner 'PyPI: build / check / upload'
    if (-not (Need-Venv)) { return }
    Push-Location $PROJECT_DIR
    try {
        & $VPY -c 'import build, twine' 2>$null
        if ($LASTEXITCODE -ne 0) {
            Write-Info 'Installing build + twine into .venv ...'
            if (-not (Invoke-Native $VPY @('-m', 'pip', 'install', '--quiet', 'build', 'twine'))) { Write-Err 'install failed'; return }
        }
        if (-not $c) {
            Write-Host '   1) build + check   2) upload to TestPyPI   3) upload to PyPI   4) clean dist\   0) back'
            switch (Read-Host '  >') { '1' { $c = 'build' } '2' { $c = 'test' } '3' { $c = 'release' } '4' { $c = 'clean' } default { return } }
        }
        if ($c -eq 'clean') { Remove-Item -Recurse -Force dist, build -ErrorAction SilentlyContinue; Write-Ok 'Cleaned'; return }
        if ($c -notin 'build', 'test', 'release') { Write-Err "unknown pypi command '$c' (build|test|release|clean)"; return }
        Remove-Item -Recurse -Force dist -ErrorAction SilentlyContinue
        if (-not (Invoke-Native $VPY @('-m', 'build'))) { Write-Err 'build failed'; return }
        $files = @(Get-ChildItem dist | ForEach-Object { $_.FullName })
        if (-not (Invoke-Native $VPY (@('-m', 'twine', 'check') + $files))) { Write-Err 'twine check failed'; return }
        Write-Ok 'Built and checked:'; Get-ChildItem dist | ForEach-Object { Write-Host "       $($_.Name)" }
        if ($c -eq 'build') { return }
        $repo = if ($c -eq 'release') { 'pypi' } else { 'testpypi' }
        if (-not (Confirm-Yes "Upload to $repo now?")) { return }
        Write-Info 'Username is __token__; the password is your API token (or set TWINE_USERNAME / TWINE_PASSWORD).'
        if (Invoke-Native $VPY (@('-m', 'twine', 'upload', '--repository', $repo) + $files)) { Write-Ok "Uploaded to $repo" } else { Write-Err 'Upload failed' }
    } finally { Pop-Location }
}

# ------ Entry --------------------------------------------------------------------
if ($Command) {
    switch ($Command) {
        'setup' { [void](Do-Setup) } 'init' { Do-Init } 'serve' { Do-Serve } 'gui' { Do-Gui } 'build' { Do-Build }
        'update' { Do-Update } 'docker' { Do-Docker $Arg } 'service' { Do-Service } 'passwd' { Do-Passwd } 'pypi' { Do-Pypi $Arg }
        default { Write-Err "Unknown command: $Command"; Write-Host 'Use: setup init serve gui build update docker service passwd pypi'; exit 2 }
    }
    exit 0
}

while ($true) {
    Clear-Host
    Write-Banner 'Domshon Manager'
    Write-Host '   1) Setup      - create .venv, fetch dependencies'
    Write-Host '   2) Initialize - create .env, set domain/port/admin password'
    Write-Host '   3) Run server'
    Write-Host '   4) Run desktop app'
    Write-Host '   5) Build applications (dist\)'
    Write-Host '   6) Update     - pull source, refresh dependencies'
    Write-Host '   7) Docker     - build / start / stop / logs'
    Write-Host '   8) Install server to start at boot (Administrator)'
    Write-Host '   9) PyPI       - build, check, upload (TestPyPI / PyPI)'
    Write-Host '   0) Exit'
    Write-Host ''
    $choice = Read-Host '  >'
    try {
        switch ($choice) {
            '1' { [void](Do-Setup); Wait-AnyKey } '2' { Do-Init; Wait-AnyKey } '3' { Do-Serve; Wait-AnyKey }
            '4' { Do-Gui; Wait-AnyKey } '5' { Do-Build; Wait-AnyKey } '6' { Do-Update; Wait-AnyKey }
            '7' { Do-Docker ''; Wait-AnyKey } '8' { Do-Service; Wait-AnyKey } '9' { Do-Pypi ''; Wait-AnyKey }
            { $_ -in '0', 'q', 'Q' } { exit 0 }
        }
    } catch { Write-Err $_.Exception.Message; Wait-AnyKey }
}
