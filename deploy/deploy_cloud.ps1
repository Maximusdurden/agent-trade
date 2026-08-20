# filename: deploy_cloud.ps1
# Automated Deployment Script for Option 1: Ephemeral Cloud Run Job & Cloud Scheduler

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

if (-not $GcpProject) {
    Write-Error "GOOGLE_CLOUD_PROJECT is not defined in .env"
}
if (-not $GcsBucket) {
    Write-Error "GCS_BUCKET_NAME is not defined in .env"
}

$Region = "us-central1"
$BuildId = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
$ImageTag = "gcr.io/$GcpProject/agent-trade:$BuildId"
$JobName = "agent-trade-job"
$SchedulerName = "agent-trade-scheduler"

Write-Host "GCP Project: $GcpProject"
Write-Host "GCS Bucket: $GcsBucket"
Write-Host "Deployment Region: $Region"
Write-Host "Artifact Image: $ImageTag"

# 2. Check for gcloud installation and enable Google Cloud services
if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    Write-Error "gcloud CLI is not installed or not in PATH. Please install Google Cloud SDK."
}
Write-Host "Enabling necessary Google Cloud APIs (Cloud Scheduler, Cloud Run, Cloud Build)..."
gcloud config set project $GcpProject
gcloud services enable cloudscheduler.googleapis.com run.googleapis.com cloudbuild.googleapis.com

# 3. Create Clean Build Context Staging Directory
$StagingDir = "Z:\python\projects\agent-trade\deploy\temp_staging"
if (Test-Path $StagingDir) {
    Write-Host "Cleaning up old staging directory..."
    Remove-Item $StagingDir -Recurse -Force
}
New-Item -ItemType Directory -Path $StagingDir | Out-Null

Write-Host "Copying agent-trade files to staging..."
# Copy agent-trade contents except ignored directories (venv, .git, temp_staging)
Copy-Item "Z:\python\projects\agent-trade\*" -Destination $StagingDir -Recurse -Force -Exclude "venv", ".venv", ".git", "deploy", ".env", "trading_agent.db", "test_trading_agent.db", "trading.log", "__pycache__"

Write-Host "Copying sibling dependencies to staging..."
Copy-Item "Z:\python\projects\agent-jira-client" -Destination (Join-Path $StagingDir "agent-jira-client") -Recurse -Force -Exclude "venv", ".git"

# 4. Write Custom Production Dockerfile
$DockerProdPath = Join-Path $StagingDir "Dockerfile"
$DockerfileContent = @"
FROM python:3.11-slim

WORKDIR /app

# Install git
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

# Copy local dependencies into image container
COPY agent-jira-client /src/agent-jira-client

# Copy requirements and remove relative editable flags
COPY requirements.txt .
RUN python -c "lines = [l for l in open('requirements.txt') if '-e ' not in l]; open('requirements.txt', 'w').write(''.join(lines))"

# Install packages
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir /src/agent-jira-client

# Copy main codebase
COPY . .

# Set environment variables defaults
ENV BYPASS_MARKET_WINDOW=True
ENV PYTHONPATH="/app"

# Default command
ENTRYPOINT ["python", "runner.py"]
"@

$DockerfileContent | Out-File -FilePath $DockerProdPath -Encoding utf8

# 5. Create Cloud Build trigger
Write-Host "`n--- Triggering Google Cloud Build in the cloud ---"
gcloud config set project $GcpProject
gcloud builds submit $StagingDir --tag $ImageTag

# 6. Deploy Cloud Run Job
Write-Host "`n--- Deploying Cloud Run Job: $JobName ---"
# Check if Job already exists to choose deploy or update using LastExitCode safely
$OldPreference = $ErrorActionPreference
$ErrorActionPreference = "SilentlyContinue"
& gcloud run jobs describe $JobName --region $Region --format="value(name)" > $null 2>&1
$JobExists = ($LastExitCode -eq 0)
$ErrorActionPreference = $OldPreference

# Fetch env variables to inject at runtime
$EnvVariablesList = @(
    "GOOGLE_CLOUD_PROJECT=$GcpProject",
    "GCS_BUCKET_NAME=$GcsBucket",
    "BYPASS_MARKET_WINDOW=True"
)

# Extract explicitly allowed non-secret runtime settings. API keys for Gemini
# and OpenRouter are included so the brain can initialize in the cloud.
$AllowedRuntimeKeys = @(
    "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_PAPER", "LLM_PROVIDER", "GEMINI_API_KEY", "GEMINI_MODEL",
    "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL",
    "MODEL_HEAVYWEIGHT", "MODEL_DAILY_DRIVER", "MODEL_UTILITY",
    "ACTIVE_MODEL_TIER", "BRAIN_MODEL_TIER", "STRATEGIST_MODEL_TIER",
    "TRADING_INTERVAL_MINUTES", "JIRA_URL", "JIRA_PROJECT_KEY", "JIRA_EMAIL", "JIRA_API_TOKEN",
    "OPTIONS_ENABLED", "OPTIONS_DTE_MIN", "OPTIONS_DTE_MAX",
    "OPTIONS_DTE_HARD_MIN", "OPTIONS_DTE_HARD_MAX",
    "OPTIONS_MAX_ALLOCATION_PCT", "OPTIONS_MAX_CONTRACTS_PER_TICKER",
    "OPTIONS_CONVICTION_THRESHOLD", "OPTIONS_AUTO_CLOSE_DTE",
    "OPTIONS_OTM_PERCENT_MIN", "OPTIONS_OTM_PERCENT_MAX"
)
Get-Content $EnvPath | ForEach-Object {
    $Line = $_.Trim()
    if ($Line -and -not $Line.StartsWith("#") -and $Line.Contains("=")) {
        $Parts = $Line.Split("=", 2)
        $Key = $Parts[0].Trim()
        $Val = $Parts[1].Trim().Trim("'`"")
        if ($AllowedRuntimeKeys -contains $Key -and $Val -and -not $Val.StartsWith("your_")) {
            $EnvVariablesList += "$Key=$Val"
        }
    }
}

$EnvString = [string]::Join(",", $EnvVariablesList)

if ($JobExists) {
    Write-Host "Updating existing Cloud Run Job..."
    gcloud run jobs update $JobName `
        --image $ImageTag `
        --region $Region `
        --command python `
        --args "runner.py,--once" `
        --tasks 1 `
        --max-retries 1 `
        --task-timeout 10m `
        --set-env-vars $EnvString
} else {
    Write-Host "Creating new Cloud Run Job..."
    gcloud run jobs create $JobName `
        --image $ImageTag `
        --region $Region `
        --command python `
        --args "runner.py,--once" `
        --tasks 1 `
        --max-retries 1 `
        --task-timeout 10m `
        --set-env-vars $EnvString
}

# 7. Create/Update Cloud Scheduler Job (Run every 15 minutes weekdays)
Write-Host "`n--- Setting up Cloud Scheduler trigger: $SchedulerName ---"

# Retrieve project number for service account
$ProjectNumber = (gcloud projects describe $GcpProject --format="value(projectNumber)").Trim()
$ServiceAccount = "${ProjectNumber}-compute@developer.gserviceaccount.com"

# Delete existing Scheduler job to prevent naming conflicts using LastExitCode safely
$OldPreference = $ErrorActionPreference
$ErrorActionPreference = "SilentlyContinue"
& gcloud scheduler jobs describe $SchedulerName --location $Region --format="value(name)" > $null 2>&1
$SchedulerExists = ($LastExitCode -eq 0)
$ErrorActionPreference = $OldPreference

if ($SchedulerExists) {
    Write-Host "Scheduler job exists. Recreating trigger..."
    & gcloud scheduler jobs delete $SchedulerName --location $Region --quiet
}

# Schedule: Every 15 minutes, 24/7/365 (to handle crypto holdings during weekends and nights)
# We use New York time zone
gcloud scheduler jobs create http $SchedulerName `
    --location $Region `
    --schedule "*/15 * * * *" `
    --time-zone "America/New_York" `
    --uri "https://${Region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$GcpProject/jobs/${JobName}:run" `
    --http-method POST `
    --oauth-service-account-email $ServiceAccount

# 8. Clean Staging Directory
Write-Host "`nCleaning up local staging directory..."
Remove-Item $StagingDir -Recurse -Force

Write-Host "`n========================================================"
Write-Host "DEPLOIMENT COMPLETED SUCCESSFULLY!"
Write-Host "- Cloud Run Job: $JobName is deployed."
Write-Host "- Cloud Scheduler: $SchedulerName is registered."
Write-Host "- Immutable Image: $ImageTag"
Write-Host "- Trigger Interval: Every 15 minutes, 24/7, NY Time."
Write-Host "========================================================"
