$projectDir = 'C:\Users\cempo\Projects\AutoRestock'
$serviceDir = "$projectDir\whatsapp-service"
$logsDir = "$projectDir\logs"
$serviceName = 'hearthkeeper-whatsapp'

# Find node.exe
$nodeExe = $null
$cmd = Get-Command node -ErrorAction SilentlyContinue
if ($cmd) {
    $nodeExe = $cmd.Source
} elseif (Test-Path 'C:\Program Files\nodejs\node.exe') {
    $nodeExe = 'C:\Program Files\nodejs\node.exe'
}
if (-not $nodeExe) {
    Write-Host 'node.exe NOT FOUND' -ForegroundColor Red
    exit 1
}
Write-Host "node.exe at: $nodeExe"
& $nodeExe --version

if (-not (Test-Path "$serviceDir\index.js")) {
    Write-Host "ERROR: $serviceDir\index.js not found" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path "$serviceDir\node_modules")) {
    Write-Host "WARNING: $serviceDir\node_modules missing -- npm install may be needed" -ForegroundColor Yellow
}

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
& nssm install $serviceName $nodeExe 'index.js'
& nssm set $serviceName AppDirectory $serviceDir
& nssm set $serviceName AppStdout "$logsDir\service-whatsapp-stdout.log"
& nssm set $serviceName AppStderr "$logsDir\service-whatsapp-stderr.log"
& nssm set $serviceName Start SERVICE_AUTO_START
& nssm set $serviceName AppRestartDelay 5000
& nssm set $serviceName AppStopMethodConsole 5000
& nssm set $serviceName AppStopMethodWindow 5000
& nssm set $serviceName AppStopMethodThreads 5000
& nssm set $serviceName DisplayName 'Hearthkeeper WhatsApp Sidecar'
& nssm set $serviceName Description 'Node.js sidecar wrapping whatsapp-web.js. Listens on :3001, forwards inbound WhatsApp messages to localhost:8000/webhook/whatsapp.'
& nssm set $serviceName AppRotateFiles 1
& nssm set $serviceName AppRotateOnline 1
& nssm set $serviceName AppRotateBytes 10485760

# Puppeteer's bundled Chromium lives in the installing user's profile
# (~/.cache/puppeteer) and a LocalSystem service can't reach it. Point
# Puppeteer at the system-wide Google Chrome installation instead --
# whatsapp-web.js works fine against current stable Chrome.
$chromeExe = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
if (-not (Test-Path $chromeExe)) {
    Write-Host "WARNING: $chromeExe not found -- install Google Chrome first." -ForegroundColor Yellow
}
& nssm set $serviceName AppEnvironmentExtra "PUPPETEER_EXECUTABLE_PATH=$chromeExe"

Write-Host 'Starting service...'
& nssm start $serviceName
Start-Sleep -Seconds 15

Write-Host ''
Write-Host '=== Service status ===' -ForegroundColor Cyan
Get-Service $serviceName | Format-Table Name, Status, StartType

Write-Host '=== sidecar status endpoint ===' -ForegroundColor Cyan
try {
    $r = Invoke-WebRequest -Uri 'http://localhost:3001/status' -UseBasicParsing -TimeoutSec 8
    Write-Host "HTTP $($r.StatusCode):"
    $r.Content
} catch {
    Write-Host "/status check failed: $($_.Exception.Message)" -ForegroundColor Yellow
    Write-Host '(May be normal during startup -- check the logs below)'
}

Write-Host ''
Write-Host '=== whatsapp-service stderr tail ===' -ForegroundColor Cyan
Get-Content "$logsDir\service-whatsapp-stderr.log" -Tail 40 -ErrorAction SilentlyContinue
Write-Host ''
Write-Host '=== whatsapp-service stdout tail ===' -ForegroundColor Cyan
Get-Content "$logsDir\service-whatsapp-stdout.log" -Tail 40 -ErrorAction SilentlyContinue
