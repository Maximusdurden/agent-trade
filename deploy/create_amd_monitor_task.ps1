# filename: create_amd_monitor_task.ps1
# Register a Windows Scheduled Task that runs the AMD performance monitor
# daily (off-hours) and sends a Discord health notification.
#
# The monitor (sideload/monitor_amd.py) compares live AMD realized PnL against
# the locked backtest baseline and notifies on-track / deviating / kill-worthy.
#
# USAGE:
#   .\deploy\create_amd_monitor_task.ps1

$TaskName = "AgentTradeAMDMonitor"
$Description = "Runs the AMD performance monitor daily, comparing live AMD PnL against the locked backtest baseline and notifying Discord."

# 1. Action
$PythonPath = "Z:\python\projects\agent-trade\.venv\Scripts\python.exe"
$ScriptPath = "Z:\python\projects\agent-trade\sideload\monitor_amd.py"
$WorkingDirectory = "Z:\python\projects\agent-trade"

# Run the monitor once (30-day lookback), notify Discord.
$Action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$PythonPath`" `"$ScriptPath`" --days 30`"" -WorkingDirectory $WorkingDirectory

# 2. Trigger: Daily at 9:00 PM (after market close, off-hours).
$DailyTrigger = New-ScheduledTaskTrigger -Daily -At "9:00 PM"

# 3. Settings: allow on battery, start if missed, cap at 10 minutes.
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

# 4. Register Scheduled Task
Write-Host "Registering scheduled task '$TaskName'..."
try {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Write-Host "Task '$TaskName' already exists. Unregistering first..."
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $DailyTrigger -Settings $Settings -Description $Description
    Write-Host "Successfully registered scheduled task '$TaskName'!"
} catch {
    Write-Error "Failed to register scheduled task: $_"
}