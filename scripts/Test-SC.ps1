param([string]$PythonExe)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
if (-not $PythonExe) { $PythonExe = Join-Path $root 'platform/.venv/Scripts/python.exe' }

$failed = @()
Push-Location $root
try {
    & $PythonExe -m pytest platform/backend/tests -q
    if ($LASTEXITCODE -ne 0) { $failed += 'backend tests' }

    $tests = Get-ChildItem 'SC-Project-main/tests' -Filter '*.test.mjs' | ForEach-Object FullName
    & node --test @tests
    if ($LASTEXITCODE -ne 0) { $failed += 'frontend tests' }

    & npm.cmd --prefix SC-Project-main run build
    if ($LASTEXITCODE -ne 0) { $failed += 'frontend build' }
} finally {
    Pop-Location
}
if ($failed.Count) { throw "Verification failed: $($failed -join ', ')" }
