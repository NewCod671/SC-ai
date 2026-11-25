param([string]$PythonExe)
$ErrorActionPreference = 'Stop'
$webRoot = Join-Path $PSScriptRoot 'SC-Project-main'
$platformRoot = Join-Path $PSScriptRoot 'platform'
$logRoot = Join-Path $PSScriptRoot '.runtime'
if (-not $PythonExe) {
    $PythonExe = Join-Path $platformRoot '.venv\Scripts\python.exe'
}
if (-not (Test-Path -LiteralPath $PythonExe)) { throw 'Install platform Python requirements or pass -PythonExe with the Python executable.' }
foreach ($configFile in @((Join-Path $webRoot '.env'), (Join-Path $platformRoot '.env'))) {
    if (-not (Test-Path -LiteralPath $configFile)) { throw "Missing local configuration: $configFile" }
}
if (-not (Test-Path -LiteralPath (Join-Path $webRoot 'node_modules'))) { throw 'Run npm install in SC-Project-main first.' }
foreach ($port in @(8001,5180)) {
    $socket = [System.Net.Sockets.TcpClient]::new()
    try { $socket.Connect('127.0.0.1', $port); throw "Port $port is already in use. Stop its service before starting this workspace." }
    catch [System.Net.Sockets.SocketException] { }
    finally { $socket.Dispose() }
}
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$backend = Start-Process -FilePath $PythonExe -ArgumentList @('-m','uvicorn','app.main:app','--app-dir','backend','--host','127.0.0.1','--port','8001') -WorkingDirectory $platformRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logRoot 'backend.log') -RedirectStandardError (Join-Path $logRoot 'backend-error.log') -PassThru
try {
    $node = (Get-Command node).Source
    $key = & $node "--env-file=$(Join-Path $webRoot '.env')" -p 'process.env.COMPLIANCE_API_KEY || String()'
    if ($LASTEXITCODE -ne 0) { throw 'Could not read the web configuration.' }
    $headers = @{}
    if ($key) { $headers.Authorization = "Bearer $key" }
    $web = Start-Process -FilePath $node -ArgumentList 'server.js' -WorkingDirectory $webRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logRoot 'web.log') -RedirectStandardError (Join-Path $logRoot 'web-error.log') -PassThru
    $ready = $false
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while (-not $ready -and [DateTime]::UtcNow -lt $deadline) {
        if ($backend.HasExited -or $web.HasExited) { break }
        try {
            $apiHealth = Invoke-RestMethod 'http://127.0.0.1:8001/api/health' -Headers $headers -TimeoutSec 2
            $webHealth = Invoke-RestMethod 'http://127.0.0.1:5180/api/health' -TimeoutSec 2
            $ready = $apiHealth.status -eq 'ok' -and $webHealth.ok -eq $true
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $ready) { throw 'The SC web or assessment API did not pass its health check.' }
    @{ backend = $backend.Id; web = $web.Id } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $logRoot 'services.json')
    Write-Output 'SC web: http://127.0.0.1:5180'
    Write-Output "Logs: $logRoot"
} catch {
    if (-not $backend.HasExited) { & taskkill.exe /PID $backend.Id /T /F | Out-Null }
    if ($web -and -not $web.HasExited) { Stop-Process -Id $web.Id }
    foreach ($log in @('backend-error.log', 'web-error.log')) {
        Write-Host $log
        Get-Content -LiteralPath (Join-Path $logRoot $log) -Tail 10 -ErrorAction SilentlyContinue
    }
    throw
}
