# filename: deploy_flatbase.ps1
# Deploy the Flat Base Breakout sideload lane as Cloud Run Jobs + Cloud Schedulers.
# Modeled on deploy_swing.ps1.
#
# USAGE:
#   .\deploy\deploy_flatbase.ps1
#
# Pre-requisites: .env with GOOGLE_CLOUD_PROJECT, GCS_BUCKET_NAME, and (for
# error->Jira logging) JIRA_* credentials.
#
# Deploys TWO jobs from the SAME image (entrypoint differs):
#   - flatbase-runner : runner_flat_base.py --auto (self-selects scan vs monitor)
#   - flatbase-fill-audit : verify_flatbase_fills.py --weekly
#
# With THREE schedulers per the production cadence:
#   - flatbase-monitor-scheduler : hourly 10:00-15:00 ET Mon-Fri -> --monitor
#   - flatbase-scan-scheduler    : 3:45 PM ET Mon-Fri           -> --scan
#   - flatbase-fill-audit-scheduler : Fri 4:30 PM ET             -> --weekly audit
#
# NOTE: All schedulers pin --time-zone="America/New_York" so the execution
# window stays on US market hours year-round with NO DST drift. Schedules are
# ET-local (not UTC). The intraday monitor job no-ops outside 9:30-4:00 ET via
# the market-hours gate in runner_flat_base.py.

$ErrorActionPreference = "Continue"

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
$ImageTag = "gcr.io/$GcpProject/agent-trade-flatbase:$BuildId"
$JobName = "flatbase-runner"
$AuditJobName = "flatbase-fill-audit"

Write-Host "GCP Project:   $GcpProject"
Write-Host "GCS Bucket:    $GcsBucket"
Write-Host "Region:        $Region"
Write-Host "Image:         $ImageTag"

# Resolve gcloud.
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
$StagingDir = "Z:\python\projects\agent-trade\deploy\temp_staging_flatbase"
if (Test-Path $StagingDir) { Remove-Item $StagingDir -Recurse -Force }
New-Item -ItemType Directory -Path $StagingDir | Out-Null

Copy-Item "Z:\python\projects\agent-trade\*" -Destination $StagingDir -Recurse -Force `
    -Exclude "venv", ".venv", ".git", "deploy", ".env", "trading_agent.db", "trading.log", "__pycache__"

# Copy the sibling agent-jira-client dependency (needed for error->Jira logging).
# NOTE: must land at agent-jira-client (hyphen) to match the Dockerfile COPY,
# and must preserve the agent_jira/ package subdirectory.
$JiraClientSrc = "Z:\python\projects\agent-jira-client"
if (Test-Path $JiraClientSrc) {
    New-Item -ItemType Directory -Path "$StagingDir\agent-jira-client" -Force | Out-Null
    Copy-Item "$JiraClientSrc\agent_jira" -Destination "$StagingDir\agent-jira-client\" -Recurse -Force
    Copy-Item "$JiraClientSrc\pyproject.toml" -Destination "$StagingDir\agent-jira-client\" -Force
    Copy-Item "$JiraClientSrc\README.md" -Destination "$StagingDir\agent-jira-client\" -Force
}

# 3. Dockerfile (reuse the sideload Dockerfile pattern — installs jira-client).
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
# Entrypoint is overridden per-job via --command (runner_flat_base.py / verify_flatbase_fills.py).
"@
$DockerfileContent | Out-File -FilePath $DockerProdPath -Encoding utf8

# 4. Build image via Cloud Build (no local Docker daemon required).
Write-Host "`n--- Building flatbase image via Cloud Build ---"
& $GCloud builds submit $StagingDir --tag $ImageTag
if ($LASTEXITCODE -ne 0) {
    Write-Error "Cloud Build failed (exit $LASTEXITCODE)."
}

# 5. Deploy the Cloud Run jobs.
$FlagsFile = "$StagingDir\flags.yaml"
$EnvVariablesList = @(
    "GOOGLE_CLOUD_PROJECT=$GcpProject",
    "GCS_BUCKET_NAME=$GcsBucket",
    "DATABASE_FILENAME=/tmp/trading_agent.db"
)
# Jira credentials (for error->Jira logging) from .env.
$JiraKeys = @("JIRA_URL", "JIRA_PROJECT_KEY", "JIRA_EMAIL", "JIRA_API_TOKEN")
# Alpaca credentials the flat-base lane needs to run on REAL market data instead
# of falling back to the mock client.
$ConfigKeys = @(
    "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_PAPER"
)
Get-Content $EnvPath | ForEach-Object {
    $Line = $_.Trim()
    if ($Line -and -not $Line.StartsWith("#") -and $Line.Contains("=")) {
        $Parts = $Line.Split("=", 2)
        $Key = $Parts[0].Trim()
        $Val = $Parts[1].Trim().Trim("'`"")
        if (($JiraKeys -contains $Key -or $ConfigKeys -contains $Key) -and $Val -and -not $Val.StartsWith("your_")) {
            $EnvVariablesList += "$Key=$Val"
        }
    }
}
$SecretReferences = @(
    "DISCORD_WEBHOOK_URL=DISCORD_WEBHOOK_URL:latest"
)
$FlagLines = @("--set-env-vars:")
foreach ($Entry in $EnvVariablesList) {
    $Parts = $Entry.Split("=", 2)
    $K = $Parts[0]
    $V = $Parts[1]
    $Escaped = $V.Replace("\", "\\").Replace('"', '\"')
    $FlagLines += "  ${K}: `"$Escaped`""
}
[System.IO.File]::WriteAllText($FlagsFile, [string]::Join("`n", $FlagLines), [System.Text.Encoding]::UTF8)

# --- flatbase-runner job (entrypoint runner_flat_base.py --auto) ---
$JobExists = (& $GCloud run jobs list --region $Region --format="value(name)" 2>$null | Select-String "^$JobName$")
if (-not $JobExists) {
    & $GCloud run jobs create $JobName --image $ImageTag --region $Region `
        --command "python" --args="sideload/runner_flat_base.py,--auto" `
        --flags-file $FlagsFile `
        --set-secrets ($SecretReferences -join ",")
} else {
    & $GCloud run jobs update $JobName --image $ImageTag --region $Region `
        --command "python" --args="sideload/runner_flat_base.py,--auto" `
        --flags-file $FlagsFile `
        --set-secrets ($SecretReferences -join ",")
}
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to deploy job $JobName (exit $LASTEXITCODE)."
}

# --- flatbase-fill-audit job (entrypoint verify_flatbase_fills.py --weekly) ---
$AuditJobExists = (& $GCloud run jobs list --region $Region --format="value(name)" 2>$null | Select-String "^$AuditJobName$")
if (-not $AuditJobExists) {
    & $GCloud run jobs create $AuditJobName --image $ImageTag --region $Region `
        --command "python" --args="sideload/verify_flatbase_fills.py,--weekly" `
        --flags-file $FlagsFile
} else {
    & $GCloud run jobs update $AuditJobName --image $ImageTag --region $Region `
        --command "python" --args="sideload/verify_flatbase_fills.py,--weekly" `
        --flags-file $FlagsFile
}
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to deploy job $AuditJobName (exit $LASTEXITCODE)."
}

# 6. Cloud Schedulers
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
        [string]$Schedule,
        [string]$TimeZone = "America/New_York"
    )
    Write-Host "`n--- Registering Cloud Scheduler: $SchedulerName ($Schedule, tz=$TimeZone) ---"
    $OldPreference = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    & $GCloud scheduler jobs delete $SchedulerName --location $Region --quiet 2>$null
    $ErrorActionPreference = $OldPreference
    & $GCloud scheduler jobs create http $SchedulerName --schedule=$Schedule `
        --time-zone="$TimeZone" `
        --location $Region `
        --uri="https://$Region-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$GcpProject/jobs/${JobName}:run" `
        --http-method=POST `
        --oauth-service-account-email=$DefaultComputeSa `
        --oauth-token-scope="https://www.googleapis.com/auth/cloud-platform"
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Scheduler creation failed for $SchedulerName (exit $LASTEXITCODE)."
    }
}

# Production cadence, pinned to America/New_York so the window stays on US
# market hours year-round (no DST drift). Schedules are ET-local.
# Intraday monitor: hourly 10:00-15:00 ET Mon-Fri (job no-ops outside
# 9:30-4:00 ET via the market-hours gate).
Register-Scheduler -SchedulerName "flatbase-monitor-scheduler" -JobName $JobName `
    -Schedule "0 10-15 * * 1-5" -TimeZone "America/New_York"

# MOC scan: 3:45 PM ET Mon-Fri.
Register-Scheduler -SchedulerName "flatbase-scan-scheduler" -JobName $JobName `
    -Schedule "45 15 * * 1-5" -TimeZone "America/New_York"

# Weekly fill audit: every Friday 4:30 PM ET (cron day-of-week 5 = Friday).
Register-Scheduler -SchedulerName "flatbase-fill-audit-scheduler" -JobName $AuditJobName `
    -Schedule "30 16 * * 5" -TimeZone "America/New_York"

Write-Host "`nDone: flatbase jobs deployed."
Write-Host "  Monitor    : flatbase-monitor-scheduler    (hourly 10-15 ET Mon-Fri)"
Write-Host "  MOC scan   : flatbase-scan-scheduler       (3:45 PM ET Mon-Fri)"
Write-Host "  Fill audit : flatbase-fill-audit-scheduler (Fri 4:30 PM ET)"
Write-Host "Verify: & $GCloud run jobs list --region $Region"