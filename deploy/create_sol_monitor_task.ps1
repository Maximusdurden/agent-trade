# filename: create_sol_monitor_task.ps1
# Register a Windows Scheduled Task that runs the SOL/USD performance monitor
# daily (off-hours) and sends a Discord health notification.
#
# The monitor (sideload/monitor_amd.py --symbol "SOL/USD") compares live SOL
# realized PnL against the locked backtest baseline and notifies on-track /
# deviating / kill-worthy.
#
# USAGE:
#   .\deploy\create_sol_monitor_task.ps1

$TaskName = "AgentTradeSOLMonitor"
$Description = "Runs the SOL/USD performance monitor daily, comparing live SOL PnL against the locked backtest baseline and notifying Discord."

# 1. Action
$PythonPath = "Z:\python\projects\agent-trade\.venv\Scripts\python.exe"
$ScriptPath = "Z:\python\projects\agent-trade\sideload\monitor_amd.py"
$WorkingDirectory = "Z:\python\projects\agent-trade"

# Run the monitor once (30-day lookback) for SOL/USD, notify Discord.
$Action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$PythonPath`" `"$ScriptPath`" --symbol `"SOL/USD`" --days 30`"" -WorkingDirectory $WorkingDirectory

# 2. Trigger: Daily at 9:05 PM (after market close, off-hours; offset from AMD monitor).
$DailyTrigger = New-ScheduledTaskTrigger -Daily -At "9:05 PM"

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