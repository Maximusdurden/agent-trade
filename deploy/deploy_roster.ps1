# filename: deploy_roster.ps1
# Deploy the Ticker Roster jobs (daily recommendations + weekly pool edits) as
# Cloud Run Jobs + Cloud Schedulers. Modeled on deploy_blog.ps1.
#
# USAGE:
#   .\deploy\deploy_roster.ps1
#
# Pre-requisites: .env with GOOGLE_CLOUD_PROJECT, GCS_BUCKET_NAME, and (for the
# weekly Jira ticket) JIRA_* credentials.
#
# Deploys TWO jobs from the SAME image (entrypoint differs):
#   - ticker-roster-daily   : recommendations + learning report + Discord
#   - ticker-roster-weekly  : applies pool edits + uploads pool to GCS + Jira

$ErrorActionPreference = "Stop"

# 1. Load Configurations from .env
$EnvPath = "Z:\python\projects\agent-trade\.env"
if (-not (Test-Path $EnvPath)) {
    Write-Error "Could not find .env file at $EnvPath"
}

Write-Host "--- Loading environment configurations from .env ---"
$GcpProject = ""
$GcsBucket = ""
Get-Content $EnvPath | ForEach-Object {
    $Line = $_.Trim()
    if ($Line -and -not $Line.StartsWith("#") -and $Line.Contains("=")) {
        $Parts = $Line.Split("=", 2)
        $Key = $Parts[0].Trim()
        $Val = $Parts[1].Trim().Trim("'`"")
        if ($Key -eq "GOOGLE_CLOUD_PROJECT") { $GcpProject = $Val }
        if ($Key -eq "GCS_BUCKET_NAME") { $GcsBucket = $Val }
    }
}
if (-not $GcpProject) { Write-Error "GOOGLE_CLOUD_PROJECT is not defined in .env" }
if (-not $GcsBucket) { Write-Error "GCS_BUCKET_NAME is not defined in .env" }

$env:CLOUDSDK_CORE_PROJECT = $GcpProject
$Region = "us-central1"
$BuildId = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
$ImageTag = "gcr.io/$GcpProject/agent-trade-roster:$BuildId"
$DailyJobName = "ticker-roster-daily"
$WeeklyJobName = "ticker-roster-weekly"
$DailyScheduler = "ticker-roster-daily-scheduler"
$WeeklyScheduler = "ticker-roster-weekly-scheduler"

Write-Host "GCP Project:   $GcpProject"
Write-Host "GCS Bucket:    $GcsBucket"
Write-Host "Region:        $Region"
Write-Host "Image:         $ImageTag"

# Resolve gcloud (same fallback as deploy_blog.ps1).
$GCloud = ""
if (Get-Command gcloud -ErrorAction SilentlyContinue) {
    $GCloud = "gcloud"
} elseif (Test-Path "$env:LOCALAPPDATA\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd") {
    $GCloud = "$env:LOCALAPPDATA\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
} elseif (Test-Path "C:\Users\$env:USERNAME\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd") {
    $GCloud = "C:\Users\$env:USERNAME\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
}
if (-not $GCloud) {
    Write-Error "gcloud CLI is not installed or not in PATH. Please install Google Cloud SDK."
}
& $GCloud config set project $GcpProject
& $GCloud services enable cloudscheduler.googleapis.com run.googleapis.com cloudbuild.googleapis.com

# 2. Staging dir
$StagingDir = "Z:\python\projects\agent-trade\deploy\temp_staging_roster"
if (Test-Path $StagingDir) { Remove-Item $StagingDir -Recurse -Force }
New-Item -ItemType Directory -Path $StagingDir | Out-Null

Copy-Item "Z:\python\projects\agent-trade\*" -Destination $StagingDir -Recurse -Force `
    -Exclude "venv", ".venv", ".git", "deploy", ".env", "trading_agent.db", "trading.log", "__pycache__"

# Copy the sibling agent-jira-client dependency (needed for the weekly Jira ticket).
Copy-Item "Z:\python\projects\agent-jira-client" -Destination (Join-Path $StagingDir "agent-jira-client") -Recurse -Force -Exclude "venv", ".git"

# 3. Roster Dockerfile (entrypoint set per-job at deploy time via --command).
$DockerProdPath = Join-Path $StagingDir "Dockerfile"
$DockerfileContent = @"
FROM python:3.11-slim
WORKDIR /app
ENV PYTHONPATH="/app"
COPY agent-jira-client /src/agent-jira-client
COPY requirements.txt .
RUN python -c "lines = [l for l in open('requirements.txt') if '-e ' not in l]; open('requirements.txt', 'w').write(''.join(lines))"
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir /src/agent-jira-client
COPY . .
# Entrypoint is overridden per-job via --command (run_roster_daily.py / run_roster_weekly.py).
"@
$DockerfileContent | Out-File -FilePath $DockerProdPath -Encoding utf8

# 4. Build image
Write-Host "`n--- Building roster image ---"
& $GCloud builds submit $StagingDir --tag $ImageTag

# 5. Deploy Cloud Run Jobs
$EnvVariablesList = @(
    "GOOGLE_CLOUD_PROJECT=$GcpProject",
    "GCS_BUCKET_NAME=$GcsBucket",
    "DATABASE_FILENAME=/tmp/trading_agent.db"
)
# Jira credentials (for error->Jira logging + the weekly audit ticket) from .env.
# NOTE: config.py reads JIRA_URL (not JIRA_SITE), so we must pass JIRA_URL.
$JiraKeys = @("JIRA_URL", "JIRA_PROJECT_KEY", "JIRA_EMAIL", "JIRA_API_TOKEN")
Get-Content $EnvPath | ForEach-Object {
    $Line = $_.Trim()
    if ($Line -and -not $Line.StartsWith("#") -and $Line.Contains("=")) {
        $Parts = $Line.Split("=", 2)
        $Key = $Parts[0].Trim()
        $Val = $Parts[1].Trim().Trim("'`"")
        if ($JiraKeys -contains $Key -and $Val -and -not $Val.StartsWith("your_")) {
            $EnvVariablesList += "$Key=$Val"
        }
    }
}
$SecretReferences = @(
    "DISCORD_WEBHOOK_URL=DISCORD_WEBHOOK_URL:latest"
)

function Deploy-Job {
    param(
        [string]$JobName,
        [string]$Entrypoint
    )
    Write-Host "`n--- Deploying Cloud Run Job: $JobName ---"
    $OldPreference = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    & $GCloud run jobs describe $JobName --region $Region --format="value(name)" > $null 2>&1
    $JobExists = ($LastExitCode -eq 0)
    $ErrorActionPreference = $OldPreference

    if (-not $JobExists) {
        & $GCloud run jobs create $JobName --image $ImageTag --region $Region `
            --command "python" --args $Entrypoint `
            --set-env-vars ($EnvVariablesList -join ",") `
            --set-secrets ($SecretReferences -join ",")
    } else {
        & $GCloud run jobs update $JobName --image $ImageTag --region $Region `
            --command "python" --args $Entrypoint `
            --set-env-vars ($EnvVariablesList -join ",") `
            --set-secrets ($SecretReferences -join ",")
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to deploy job $JobName (exit $LASTEXITCODE)."
    }
}

Deploy-Job -JobName $DailyJobName -Entrypoint "run_roster_daily.py"
Deploy-Job -JobName $WeeklyJobName -Entrypoint "run_roster_weekly.py"

# 6. Cloud Schedulers
# Resolve the default compute SA (same approach as deploy_blog.ps1).
$DefaultComputeSa = (& $GCloud iam service-accounts list --format="value(email)" 2>$null |
    Where-Object { $_ -like "*-compute@developer.gserviceaccount.com" } | Select-Object -First 1)
if (-not $DefaultComputeSa) {
    $DefaultComputeSa = "$GcpProject-number-compute@developer.gserviceaccount.com"
}
Write-Host "Scheduler service account: $DefaultComputeSa"

function Register-Scheduler {
    param(
        [string]$SchedulerName,
        [string]$JobName,
        [string]$Schedule
    )
    Write-Host "`n--- Registering Cloud Scheduler: $SchedulerName ($Schedule) ---"
    # Delete any existing scheduler first, but tolerate NOT_FOUND (first deploy).
    $OldPreference = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    & $GCloud scheduler jobs delete $SchedulerName --location $Region --quiet 2>$null
    $ErrorActionPreference = $OldPreference
    & $GCloud scheduler jobs create http $SchedulerName --schedule=$Schedule `
        --location $Region `
        --uri="https://$Region-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$GcpProject/jobs/$JobName:run" `
        --http-method=POST `
        --oauth-service-account-email=$DefaultComputeSa `
        --oauth-token-scope="https://www.googleapis.com/auth/cloud-platform"
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Scheduler creation failed for $SchedulerName (exit $LASTEXITCODE)."
    }
}

# Daily: 9:00pm NY. UTC cron depends on DST:
#   - 21:00 EDT (Mar-Nov) == 01:00 UTC next day -> "0 1 * * *"
#   - 21:00 EST (Nov-Mar) == 02:00 UTC next day -> "0 2 * * *"
# Runs AFTER the blog job (8:30pm) so the DB has synced to GCS.
Register-Scheduler -SchedulerName $DailyScheduler -JobName $DailyJobName -Schedule "0 1 * * *"

# Weekly: Saturday 9:00pm NY (weekend, agent idle-ish so pool edits don't race).
#   - 21:00 EDT Sat == 01:00 UTC Sun -> "0 1 * * 6"
#   - 21:00 EST Sat == 02:00 UTC Sun -> "0 2 * * 6"
Register-Scheduler -SchedulerName $WeeklyScheduler -JobName $WeeklyJobName -Schedule "0 1 * * 6"

Write-Host "`nDone: roster jobs deployed."
Write-Host "  Daily  : $DailyJobName  ($DailyScheduler)  ~9:00pm NY"
Write-Host "  Weekly : $WeeklyJobName  ($WeeklyScheduler)  Sat ~9:00pm NY"
Write-Host "Verify: & $GCloud run jobs list --region $Region"