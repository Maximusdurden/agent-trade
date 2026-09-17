# filename: create_sideload_task.ps1
# Register a Windows Scheduled Task that starts the AMD sideload trading lane
# at market open (9:30 AM ET) on weekdays and runs it continuously.
#
# The lane runs `runner_sideload.py --loop` which trades AMD on the validated
# RSI<=50 daily setup, writing to the same DB (dashboard + blog pick it up).
#
# USAGE:
#   .\deploy\create_sideload_task.ps1

$TaskName = "AgentTradeSideloadAMD"
$Description = "Runs the AMD sideload trading lane continuously during weekday market hours. Trades AMD on the backtest-validated RSI<=50 daily setup, writing to the same DB."

# 1. Action
$PythonPath = "Z:\python\projects\agent-trade\.venv\Scripts\python.exe"
$ScriptPath = "Z:\python\projects\agent-trade\sideload\runner_sideload.py"
$WorkingDirectory = "Z:\python\projects\agent-trade"

# Run the AMD lane in loop mode (continuous trading during market hours).
$Action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$PythonPath`" `"$ScriptPath`" --loop`"" -WorkingDirectory $WorkingDirectory

# 2. Trigger: Weekdays at 9:30 AM (market open). The loop keeps running until
# the task's execution time limit stops it.
$WeeklyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "9:30 AM"

# 3. Settings: allow on battery, start if missed, and cap the run at 7 hours
# (9:30 AM - 4:30 PM ET covers the full market session).
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 7)

# 4. Register Scheduled Task
Write-Host "Registering scheduled task '$TaskName'..."
try {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Write-Host "Task '$TaskName' already exists. Unregistering first..."
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $WeeklyTrigger -Settings $Settings -Description $Description
    Write-Host "Successfully registered scheduled task '$TaskName'!"
} catch {
    Write-Error "Failed to register scheduled task: $_"
}