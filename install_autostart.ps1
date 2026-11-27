# Register a logon task that keeps the X monitor running (no admin needed).
# Run from anywhere: paths are resolved relative to this script.
$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
$taskName = 'X-Monitor'

$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $pythonw)) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($cmd) { $pythonw = $cmd.Source }
    else { throw "pythonw.exe not found. Create a venv (.venv) or install Python with PATH." }
}

$scriptPath = Join-Path $root 'x_monitor.py'
if (-not (Test-Path $scriptPath)) { throw "x_monitor.py not found in $root" }

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$scriptPath`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description 'X timeline monitor (CDP capture)' -Force | Out-Null

Write-Output "registered task: $taskName"
Write-Output "  python : $pythonw"
Write-Output "  script : $scriptPath"
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 4
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State | Format-Table -AutoSize
