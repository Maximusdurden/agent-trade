$TaskName = "AgentTradeRunner"
$Description = "Runs the agent-trade autonomous trading cycle every 15 minutes between 09:00 and 16:30 on weekdays."

# 1. Action
$PythonPath = "Z:\python\projects\agent-trade\venv\Scripts\python.exe"
$ScriptPath = "Z:\python\projects\agent-trade\runner.py"
$WorkingDirectory = "Z:\python\projects\agent-trade"

$Action = New-ScheduledTaskAction -Execute $PythonPath -Argument "$ScriptPath --once" -WorkingDirectory $WorkingDirectory

# 2. Trigger
$OnceTrigger = New-ScheduledTaskTrigger -Once -At "9:00 AM" -RepetitionInterval (New-TimeSpan -Minutes 15) -RepetitionDuration (New-TimeSpan -Hours 7 -Minutes 30)
$WeeklyTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "9:00 AM"
$WeeklyTrigger.Repetition = $OnceTrigger.Repetition

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
