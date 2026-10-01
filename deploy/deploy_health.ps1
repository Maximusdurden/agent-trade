# filename: deploy_health.ps1
# Deploy the Agent-Trade Health Check as a Cloud Run Job + Cloud Scheduler.
# Modeled on deploy_blog.ps1.
#
# USAGE:
#   .\deploy\deploy_health.ps1
#
# Deploys ONE job (run_health_check.py) with ONE scheduler:
#   - agent-trade-health-scheduler : daily 6:00 PM ET -> run_health_check.py
#
# The health check alerts via Discord when: GCS DB is stale (>48h), broker
# positions are orphaned (not in any lane state), the blog has 3+ quiet days,
# or an enabled scheduler has not fired in 48h.

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
$ImageTag = "gcr.io/$GcpProject/agent-trade-health:$BuildId"
$JobName = "agent-trade-health"
$SchedulerName = "agent-trade-health-scheduler"

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
$StagingDir = "Z:\python\projects\agent-trade\deploy\temp_staging_health"
if (Test-Path $StagingDir) { Remove-Item $StagingDir -Recurse -Force }
New-Item -ItemType Directory -Path $StagingDir | Out-Null

Copy-Item "Z:\python\projects\agent-trade\*" -Destination $StagingDir -Recurse -Force `
    -Exclude "venv", ".venv", ".git", "deploy", ".env", "trading_agent.db", "trading.log", "__pycache__"

# Copy the sibling agent-jira-client dependency.
$JiraClientSrc = "Z:\python\projects\agent-jira-client"
if (Test-Path $JiraClientSrc) {
    New-Item -ItemType Directory -Path "$StagingDir\agent-jira-client" -Force | Out-Null
    Copy-Item "$JiraClientSrc\agent_jira" -Destination "$StagingDir\agent-jira-client\" -Recurse -Force
    Copy-Item "$JiraClientSrc\pyproject.toml" -Destination "$StagingDir\agent-jira-client\" -Force
    Copy-Item "$JiraClientSrc\README.md" -Destination "$StagingDir\agent-jira-client\" -Force
}

# 3. Dockerfile
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
# Entrypoint is overridden per-job via --command (run_health_check.py).
"@
$DockerfileContent | Out-File -FilePath $DockerProdPath -Encoding utf8

# 4. Build image via Cloud Build
Write-Host "`n--- Building health image via Cloud Build ---"
& $GCloud builds submit $StagingDir --tag $ImageTag
if ($LASTEXITCODE -ne 0) {
    Write-Error "Cloud Build failed (exit $LASTEXITCODE)."
}

# 5. Deploy the Cloud Run job.
$FlagsFile = "$StagingDir\flags.yaml"
$EnvVariablesList = @(
    "GOOGLE_CLOUD_PROJECT=$GcpProject",
    "GCS_BUCKET_NAME=$GcsBucket",
    "DATABASE_FILENAME=/tmp/trading_agent.db"
)
# Jira credentials (for error->Jira logging).
$JiraKeys = @("JIRA_URL", "JIRA_PROJECT_KEY", "JIRA_EMAIL", "JIRA_API_TOKEN")
# Alpaca credentials (to check broker positions for orphans).
$ConfigKeys = @("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_PAPER")
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

$JobExists = (& $GCloud run jobs list --region $Region --format="value(name)" 2>$null | Select-String "^$JobName$")
if (-not $JobExists) {
    & $GCloud run jobs create $JobName --image $ImageTag --region $Region `
        --command "python" --args="run_health_check.py" `
        --flags-file $FlagsFile `
        --set-secrets ($SecretReferences -join ",")
} else {
    & $GCloud run jobs update $JobName --image $ImageTag --region $Region `
        --command "python" --args="run_health_check.py" `
        --flags-file $FlagsFile `
        --set-secrets ($SecretReferences -join ",")
}
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to deploy job $JobName (exit $LASTEXITCODE)."
}

# 6. Cloud Scheduler (daily 6:00 PM ET).
$DefaultComputeSa = (& $GCloud iam service-accounts list --format="value(email)" 2>$null |
    Where-Object { $_ -like "*-compute@developer.gserviceaccount.com" } | Select-Object -First 1)
if (-not $DefaultComputeSa) {
    $DefaultComputeSa = "$GcpProject-number-compute@developer.gserviceaccount.com"
}
Write-Host "Scheduler service account: $DefaultComputeSa"

Write-Host "`n--- Registering Cloud Scheduler: $SchedulerName (0 18 * * 1-5, tz=America/New_York) ---"
$OldPreference = $ErrorActionPreference
$ErrorActionPreference = "SilentlyContinue"
& $GCloud scheduler jobs delete $SchedulerName --location $Region --quiet 2>$null
$ErrorActionPreference = $OldPreference
& $GCloud scheduler jobs create http $SchedulerName --schedule="0 18 * * 1-5" `
    --time-zone="America/New_York" `
    --location $Region `
    --uri="https://$Region-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$GcpProject/jobs/${JobName}:run" `
    --http-method=POST `
    --oauth-service-account-email=$DefaultComputeSa `
    --oauth-token-scope="https://www.googleapis.com/auth/cloud-platform"
if ($LASTEXITCODE -ne 0) {
    Write-Error "Scheduler creation failed for $SchedulerName (exit $LASTEXITCODE)."
}

Write-Host "`nDone: health check job deployed."
Write-Host "  Health check : agent-trade-health-scheduler (daily 6:00 PM ET Mon-Fri)"
Write-Host "Verify: & $GCloud run jobs list --region $Region"