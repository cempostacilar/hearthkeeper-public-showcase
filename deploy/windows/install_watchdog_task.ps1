$projectDir = 'C:\Users\cempo\Projects\AutoRestock'
$pyExe = "$projectDir\venv\Scripts\python.exe"
$taskName = 'hearthkeeper-watchdog'
$logsDir = "$projectDir\logs"

if (-not (Test-Path $pyExe)) {
    Write-Host "ERROR: $pyExe not found" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path "$projectDir\watchdog.py")) {
    Write-Host "ERROR: $projectDir\watchdog.py not found" -ForegroundColor Red
    exit 1
}

New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

# Remove any existing task for idempotency.
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction `
    -Execute $pyExe `
    -Argument 'watchdog.py' `
    -WorkingDirectory $projectDir

# Every 5 minutes, indefinitely. The Once+RepetitionInterval pattern is the
# only way PowerShell's New-ScheduledTaskTrigger exposes a fixed-interval
# repeating trigger; RepetitionDuration unset means "indefinitely" in Win11.
$trigger = New-ScheduledTaskTrigger `
    -Once -At ((Get-Date).AddSeconds(30)) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 4) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId 'SYSTEM' `
    -LogonType ServiceAccount `
    -RunLevel Highest

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'Hearthkeeper app health watchdog. Every 5 minutes, checks /api/health on localhost:8000. If unhealthy, restarts the hearthkeeper-app NSSM service.' | Out-Null

Write-Host '=== Task registered ===' -ForegroundColor Cyan
Get-ScheduledTask -TaskName $taskName | Format-Table TaskName, State

# Trigger once now as a sanity test
Write-Host '=== Triggering once for sanity test ===' -ForegroundColor Cyan
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 8

Write-Host '=== watchdog.log tail ===' -ForegroundColor Cyan
Get-Content "$logsDir\watchdog.log" -Tail 15 -ErrorAction SilentlyContinue
