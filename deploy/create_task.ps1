$TaskName = "AgentTradeRunner"
$Description = "Runs the agent-trade autonomous trading cycle every 15 minutes. Trades stocks + crypto during weekday market hours, and dynamically filters to crypto-only on weekends and off-hours."

# 1. Action
$PythonPath = "Z:\python\projects\agent-trade\venv\Scripts\python.exe"
$ScriptPath = "Z:\python\projects\agent-trade\runner.py"
$WorkingDirectory = "Z:\python\projects\agent-trade"

# We use cmd.exe /c to set the BYPASS_MARKET_WINDOW=True environment variable so the script runs on weekends/off-hours.
# (Note: we use 'set VAR=Val&&' without spaces around '&&' to prevent trailing spaces in the environment variable value in cmd).
$Action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"set BYPASS_MARKET_WINDOW=True&& `"$PythonPath`" `"$ScriptPath`" --once`"" -WorkingDirectory $WorkingDirectory

# 2. Trigger (Run every 15 minutes indefinitely, 7 days a week starting at 12:00 AM)
$OnceTrigger = New-ScheduledTaskTrigger -Once -At "12:00 AM" -RepetitionInterval (New-TimeSpan -Minutes 15)
$WeeklyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday,Saturday,Sunday -At "12:00 AM"
$WeeklyTrigger.Repetition = $OnceTrigger.Repetition
$WeeklyTrigger.Repetition.StopAtDurationEnd = $false

# 3. Settings
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

# 4. Register Scheduled Task
Write-Host "Registering scheduled task '$TaskName'..."
try {
    # Delete the task if it already exists to avoid conflicts
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Write-Host "Task '$TaskName' already exists. Unregistering first..."
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
    
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $WeeklyTrigger -Settings $Settings -Description $Description
    Write-Host "Successfully registered scheduled task '$TaskName'!"
} catch {
    Write-Error "Failed to register scheduled task: $_"
}
