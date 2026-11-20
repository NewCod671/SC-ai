[CmdletBinding(SupportsShouldProcess)]
param()

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$demo = Join-Path $root '.runtime/vercel-demo'
$servicesFile = Join-Path $demo 'services.json'
$tunnelFile = Join-Path $demo 'tunnel.pid'
$targets = @()

if (Test-Path -LiteralPath $servicesFile) {
    $services = Get-Content -LiteralPath $servicesFile -Raw | ConvertFrom-Json
    $targets += @{ Id = [int]$services.web; Name = 'Demo web'; Image = 'node.exe'; Command = '\bserver\.js\s*"?$'; Port = 5182 }
    $python = Join-Path $root 'platform/.venv/Scripts/python.exe'
    $targets += @{ Id = [int]$services.backend; Name = 'Demo API'; Image = 'python.exe'; Command = [regex]::Escape($python) + '"?\s+-u -m uvicorn app\.main:app .*--port 8002\s*$'; Tree = $true }
}
if (Test-Path -LiteralPath $tunnelFile) {
    $targets += @{ Id = [int](Get-Content -LiteralPath $tunnelFile -Raw); Name = 'Demo tunnel'; Image = 'cloudflared.exe'; Command = 'tunnel --url http://127\.0\.0\.1:5182\s' }
}

$found = $false
foreach ($target in $targets) {
    if ($target.Id -le 0) { continue }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($target.Id)"
    if (-not $process) { continue }
    $found = $true
    $ownsPort = -not $target.Port -or @(
        Get-NetTCPConnection -LocalPort $target.Port -State Listen -ErrorAction SilentlyContinue |
            Where-Object OwningProcess -eq $target.Id
    ).Count -gt 0
    if ($process.Name -ne $target.Image -or $process.CommandLine -notmatch $target.Command -or -not $ownsPort) {
        Write-Warning "Skipped $($target.Name). PID $($target.Id) no longer matches the recorded Demo service."
        continue
    }
    if ($PSCmdlet.ShouldProcess("$($target.Name) (PID $($target.Id))", 'Stop')) {
        if ($target.Tree) {
            taskkill.exe /PID $target.Id /T /F
            if ($LASTEXITCODE -ne 0) { throw "Could not stop $($target.Name)." }
        } else {
            Stop-Process -Id $target.Id
        }
        Write-Output "Stopped $($target.Name)."
    }
}

if (-not $found) { Write-Output 'No recorded Vercel Demo services are running.' }
