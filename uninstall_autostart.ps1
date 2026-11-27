# Remove the logon task registered by install_autostart.ps1
$ErrorActionPreference = 'Continue'
$taskName = 'X-Monitor'

Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue | Stop-ScheduledTask -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
Write-Output "removed task: $taskName (if it existed)"

Get-CimInstance Win32_Process |
    Where-Object { $_.Name -eq 'pythonw.exe' -and $_.CommandLine -match 'x_monitor\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Write-Output "stopped leftover monitor processes"
