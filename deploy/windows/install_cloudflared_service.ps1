$cfgDir = "$env:USERPROFILE\.cloudflared"
$configPath = "$cfgDir\config.yml"
$serviceName = 'hearthkeeper-cloudflared'
$logsDir = 'C:\Users\cempo\Projects\AutoRestock\logs'

# Find the REAL cloudflared.exe (not the choco shim, which NSSM tracks poorly)
$candidates = @(
    'C:\ProgramData\chocolatey\lib\cloudflared\tools\cloudflared.exe',
    'C:\Program Files\cloudflared\cloudflared.exe',
    'C:\Program Files (x86)\cloudflared\cloudflared.exe',
    'C:\ProgramData\chocolatey\bin\cloudflared.exe'
)
$cloudflared = $null
foreach ($p in $candidates) {
    if (Test-Path $p) { $cloudflared = $p; break }
}
if (-not $cloudflared) {
    Write-Host 'cloudflared.exe NOT FOUND' -ForegroundColor Red
    exit 1
}
Write-Host "cloudflared at: $cloudflared"
& $cloudflared --version

# Normalize config.yml path: CemPo -> cempo to match this machine
$cfgContent = Get-Content $configPath -Raw
$cfgContent = $cfgContent -replace 'C:\\Users\\CemPo\\', 'C:\Users\cempo\'
Set-Content -Path $configPath -Value $cfgContent -NoNewline
Write-Host ''
Write-Host '=== Active config.yml ===' -ForegroundColor Cyan
Get-Content $configPath
Write-Host ''

New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

# Idempotent remove
$existing = Get-Service $serviceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host 'Removing existing service for clean install...'
    & nssm stop $serviceName confirm 2>$null
    Start-Sleep -Seconds 2
    & nssm remove $serviceName confirm
    Start-Sleep -Seconds 2
}

Write-Host "Installing NSSM service: $serviceName"
& nssm install $serviceName $cloudflared
& nssm set $serviceName AppParameters "--config `"$configPath`" --no-autoupdate tunnel run"
& nssm set $serviceName AppDirectory $cfgDir
& nssm set $serviceName AppStdout "$logsDir\service-cloudflared-stdout.log"
& nssm set $serviceName AppStderr "$logsDir\service-cloudflared-stderr.log"
& nssm set $serviceName Start SERVICE_AUTO_START
& nssm set $serviceName AppRestartDelay 5000
& nssm set $serviceName DisplayName 'Hearthkeeper Cloudflare Tunnel'
& nssm set $serviceName Description 'cloudflared tunnel for hearthkeeper.cc -> http://localhost:8000'
& nssm set $serviceName AppRotateFiles 1
& nssm set $serviceName AppRotateOnline 1
& nssm set $serviceName AppRotateBytes 10485760

& nssm start $serviceName
Start-Sleep -Seconds 12

Write-Host ''
Write-Host '=== Service status ===' -ForegroundColor Cyan
Get-Service $serviceName | Format-Table Name, Status, StartType

Write-Host '=== cloudflared stderr tail ===' -ForegroundColor Cyan
Get-Content "$logsDir\service-cloudflared-stderr.log" -Tail 40 -ErrorAction SilentlyContinue
Write-Host '=== cloudflared stdout tail ===' -ForegroundColor Cyan
Get-Content "$logsDir\service-cloudflared-stdout.log" -Tail 20 -ErrorAction SilentlyContinue

Write-Host ''
Write-Host '=== Testing https://hearthkeeper.cc/ ===' -ForegroundColor Cyan
Start-Sleep -Seconds 5
try {
    $r = Invoke-WebRequest -Uri 'https://hearthkeeper.cc/' -UseBasicParsing -TimeoutSec 20
    Write-Host "HTTP $($r.StatusCode), Length=$($r.Content.Length)" -ForegroundColor Green
    if ($r.Content -match '<title>([^<]+)</title>') {
        Write-Host "Page title: $($matches[1])"
    }
} catch {
    Write-Host "Test failed: $($_.Exception.Message)" -ForegroundColor Yellow
    Write-Host '(May be normal for a minute or two while tunnel connects and DNS propagates)'
}
