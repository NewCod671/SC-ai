$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$demo = Join-Path $root '.runtime/vercel-demo'
$webRoot = Join-Path $root 'SC-Project-main'
$platform = Join-Path $root 'platform'
$python = (Resolve-Path (Join-Path $platform '.venv/Scripts/python.exe')).Path
$node = (Get-Command node.exe).Source
$cloudflared = (Get-Command cloudflared.exe -ErrorAction SilentlyContinue).Source
if (-not $cloudflared) { $cloudflared = 'C:\Program Files (x86)\cloudflared\cloudflared.exe' }
foreach ($file in @($cloudflared, (Join-Path $demo 'compliance.db'), (Join-Path $demo 'web-data/sc-control-center.sqlite'), (Join-Path $webRoot 'dist/index.html'))) {
    if (-not (Test-Path -LiteralPath $file)) { throw "Required demo file not found: $file" }
}
foreach ($port in @(5182, 8002)) {
    $socket = [System.Net.Sockets.TcpClient]::new()
    try { $socket.Connect('127.0.0.1', $port); throw "Port $port is already in use." }
    catch [System.Net.Sockets.SocketException] {}
    finally { $socket.Dispose() }
}
$names = @('DATABASE_URL','APP_API_KEY','COMPLIANCE_API_KEY','COMPLIANCE_API_URL','SC_DATA_DIR','SC_WEB_HOST','PORT','NODE_ENV')
$old = @{}
foreach ($name in $names) { $old[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
$started = @()
try {
    $env:DATABASE_URL = 'sqlite:///' + (Join-Path $demo 'compliance.db').Replace('\','/')
    $env:APP_API_KEY = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
    $backend = Start-Process $python -ArgumentList @('-u','-m','uvicorn','app.main:app','--app-dir','backend','--host','127.0.0.1','--port','8002') -WorkingDirectory $platform -WindowStyle Hidden -RedirectStandardOutput (Join-Path $demo 'api.log') -RedirectStandardError (Join-Path $demo 'api-error.log') -PassThru
    $started += $backend
    $env:COMPLIANCE_API_KEY = $env:APP_API_KEY
    $env:COMPLIANCE_API_URL = 'http://127.0.0.1:8002'
    $env:SC_DATA_DIR = Join-Path $demo 'web-data'
    $env:SC_WEB_HOST = '127.0.0.1'
    $env:PORT = '5182'
    $env:NODE_ENV = 'production'
    $web = Start-Process $node -ArgumentList 'server.js' -WorkingDirectory $webRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $demo 'web.log') -RedirectStandardError (Join-Path $demo 'web-error.log') -PassThru
    $started += $web
    $health = $null
    for ($attempt = 0; $attempt -lt 20 -and -not $health; $attempt++) {
        try {
            $null = Invoke-RestMethod 'http://127.0.0.1:8002/api/health' -Headers @{ Authorization = "Bearer $env:APP_API_KEY" } -TimeoutSec 2
            $health = Invoke-RestMethod 'http://127.0.0.1:5182/api/health' -TimeoutSec 2
        } catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $health.ok) { throw 'Demo web or API did not start. Check the logs.' }
    $tunnelLog = Join-Path $demo 'tunnel-error.log'
    $tunnel = Start-Process $cloudflared -ArgumentList @('tunnel','--url','http://127.0.0.1:5182','--no-autoupdate') -WindowStyle Hidden -RedirectStandardOutput (Join-Path $demo 'tunnel.log') -RedirectStandardError $tunnelLog -PassThru
    $started += $tunnel
    $origin = $null
    for ($attempt = 0; $attempt -lt 30 -and -not $origin; $attempt++) {
        $logText = Get-Content -LiteralPath $tunnelLog -Raw
        if ($null -eq $logText) { $logText = '' }
        $match = [regex]::Match($logText, 'https://[a-z0-9-]+\.trycloudflare\.com')
        if ($match.Success) { $origin = $match.Value }
        elseif ($tunnel.HasExited) { break }
        else { Start-Sleep -Milliseconds 500 }
    }
    if (-not $origin) { throw 'Cloudflare did not return a tunnel URL. Check tunnel-error.log.' }
    & $node (Join-Path $webRoot 'scripts/configure-vercel.mjs') $origin
    if ($LASTEXITCODE -ne 0) { throw 'Could not update vercel.json' }
    @{ backend = $backend.Id; web = $web.Id } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $demo 'services.json')
    $tunnel.Id | Set-Content -LiteralPath (Join-Path $demo 'tunnel.pid')
    $origin | Set-Content -LiteralPath (Join-Path $demo 'origin.txt')
    Write-Output 'Demo is running. Deploy SC-Project-main to Vercel again to use the new tunnel URL.'
} catch {
    foreach ($process in $started) {
        if (-not $process.HasExited) { & taskkill.exe /PID $process.Id /T /F | Out-Null }
    }
    throw
} finally {
    foreach ($name in $names) { [Environment]::SetEnvironmentVariable($name, $old[$name], 'Process') }
}
