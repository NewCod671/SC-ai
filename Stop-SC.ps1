$ErrorActionPreference = 'Stop'
$stateFile = Join-Path $PSScriptRoot '.runtime\services.json'
if (-not (Test-Path -LiteralPath $stateFile)) { Write-Output 'No recorded SC services.'; return }
$serviceIds = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
foreach ($serviceName in @('web','backend')) {
    $serviceId = $serviceIds.$serviceName
    if (-not $serviceId) { continue }
    $serviceProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$serviceId"
    if (-not $serviceProcess) { continue }
    $expected = if ($serviceName -eq 'backend') {
        $serviceProcess.CommandLine -match 'uvicorn app.main:app' -and $serviceProcess.CommandLine -match '--port 8001'
    } else { $serviceProcess.Name -eq 'node.exe' -and $serviceProcess.CommandLine -match 'server.js' }
    if (-not $expected) { throw "Recorded $serviceName process no longer matches the SC service; refusing to stop it." }
    # Stop the verified launcher and its children, including Windows venv Python wrappers.
    & taskkill.exe /PID $serviceId /T /F | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not stop $serviceName." }
}
Remove-Item -LiteralPath $stateFile
Write-Output 'SC services stopped.'
