# ======================================================
# Sync a production frontend build into this repository (Windows / PowerShell).
#
# PowerShell counterpart of scripts/build-frontend.sh. Same behavior:
# 1. npm install (legacy peer deps — required by the Vue 2 + ant-design-vue stack)
# 2. npm run build
# 3. mirror QuantDinger-Vue-src/dist -> frontend/dist
#
# Usage from the repo root:
#   powershell -ExecutionPolicy Bypass -File scripts/build-frontend.ps1
#
# Or point to a Vue source tree elsewhere on disk:
#   $env:QUANTDINGER_VUE_SRC = "D:\work\QuantDinger-Vue"
#   powershell -ExecutionPolicy Bypass -File scripts/build-frontend.ps1
# ======================================================

$ErrorActionPreference = 'Stop'

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir '..')
$DistTarget  = Join-Path $ProjectRoot 'frontend\dist'

# Default to the in-repo Vue source folder if QUANTDINGER_VUE_SRC isn't set.
$VueSrc = $env:QUANTDINGER_VUE_SRC
if ([string]::IsNullOrWhiteSpace($VueSrc)) {
    $VueSrc = Join-Path $ProjectRoot 'QuantDinger-Vue-src'
}
if (-not (Test-Path $VueSrc)) {
    Write-Host "ERROR: Vue source not found: $VueSrc" -ForegroundColor Red
    Write-Host "       Set `$env:QUANTDINGER_VUE_SRC to the root of your Vue repo." -ForegroundColor Red
    exit 1
}

Write-Host '============================================' -ForegroundColor Cyan
Write-Host '  QuantDinger - sync frontend dist'           -ForegroundColor Cyan
Write-Host '============================================' -ForegroundColor Cyan
Write-Host "Vue repo: $VueSrc"
Write-Host "Target:   $DistTarget"
Write-Host ''

Push-Location $VueSrc
try {
    Write-Host '[1/3] Installing dependencies...' -ForegroundColor Yellow
    & npm install --legacy-peer-deps
    if ($LASTEXITCODE -ne 0) { throw "npm install failed (exit $LASTEXITCODE)" }

    Write-Host ''
    Write-Host '[2/3] Building production bundle...' -ForegroundColor Yellow
    & npm run build
    if ($LASTEXITCODE -ne 0) { throw "npm run build failed (exit $LASTEXITCODE)" }

    Write-Host ''
    Write-Host '[3/3] Syncing dist -> frontend/dist/...' -ForegroundColor Yellow
    if (-not (Test-Path $DistTarget)) {
        New-Item -ItemType Directory -Path $DistTarget | Out-Null
    } else {
        Get-ChildItem -Path $DistTarget -Force | Remove-Item -Recurse -Force
    }
    $SourceDist = Join-Path $VueSrc 'dist'
    Copy-Item -Path (Join-Path $SourceDist '*') -Destination $DistTarget -Recurse -Force

    $FileCount = (Get-ChildItem -Path $DistTarget -Recurse -File | Measure-Object).Count
    $TotalSize = (Get-ChildItem -Path $DistTarget -Recurse -File | Measure-Object -Property Length -Sum).Sum
    $SizeMB    = [math]::Round($TotalSize / 1MB, 2)

    Write-Host ''
    Write-Host '============================================' -ForegroundColor Green
    Write-Host '  Done. Output: frontend/dist/'              -ForegroundColor Green
    Write-Host "  Files: $FileCount"                          -ForegroundColor Green
    Write-Host "  Size:  $SizeMB MB"                          -ForegroundColor Green
    Write-Host '============================================' -ForegroundColor Green
}
finally {
    Pop-Location
}
