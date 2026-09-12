# filename: deploy_blog.ps1
# Deploy the Dexter Blog Update as a Cloud Run Job + Cloud Scheduler.
# Modeled on deploy_cloud.ps1 (agent-trade strategy job). Deploys the SAME
# image (which includes tools/blog_update.py, core/*, requirements), but runs
# `run_blog.py` and schedules it AFTER the strategy job has synced its DB to GCS.
#
# USAGE (after cutover ready):
#   .\deploy\deploy_blog.ps1
#
# Pre-requisites: .env with GOOGLE_CLOUD_PROJECT, GCS_BUCKET_NAME, WP_* secrets.

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
$ImageTag = "gcr.io/$GcpProject/agent-trade-blog:$BuildId"
$JobName = "dexter-blog-update"
$SchedulerName = "dexter-blog-scheduler"

Write-Host "GCP Project:   $GcpProject"
Write-Host "GCS Bucket:    $GcsBucket"
Write-Host "Region:        $Region"
Write-Host "Image:         $ImageTag"

# Resolve gcloud. Plain `gcloud` is sometimes missing from PATH in a PS
# subprocess (e.g. when this script is invoked from a python/automation context),
# so fall back to the standard Cloud SDK install path.
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
$StagingDir = "Z:\python\projects\agent-trade\deploy\temp_staging_blog"
if (Test-Path $StagingDir) { Remove-Item $StagingDir -Recurse -Force }
New-Item -ItemType Directory -Path $StagingDir | Out-Null

Copy-Item "Z:\python\projects\agent-trade\*" -Destination $StagingDir -Recurse -Force `
    -Exclude "venv", ".venv", ".git", "deploy", ".env", "trading_agent.db", "trading.log", "__pycache__"

# 3. Blog Dockerfile (entrypoint = run_blog.py)
$DockerProdPath = Join-Path $StagingDir "Dockerfile"
$DockerfileContent = @"
FROM python:3.11-slim
WORKDIR /app
ENV PYTHONPATH="/app"
COPY requirements.txt .
RUN python -c "lines = [l for l in open('requirements.txt') if '-e ' not in l]; open('requirements.txt', 'w').write(''.join(lines))"
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# The blog job pulls DB from GCS (abort if missing), builds mirror, grades,
# publishes, updates sidebar/calendar, and notifies Discord. It must NOT trade.
ENTRYPOINT ["python", "run_blog.py"]
"@
$DockerfileContent | Out-File -FilePath $DockerProdPath -Encoding utf8

# 4. Build image
Write-Host "`n--- Building blog image ---"
& $GCloud builds submit $StagingDir --tag $ImageTag

# 5. Deploy Cloud Run Job
Write-Host "`n--- Deploying Cloud Run Job: $JobName ---"
$OldPreference = $ErrorActionPreference
$ErrorActionPreference = "SilentlyContinue"
& $GCloud run jobs describe $JobName --region $Region --format="value(name)" > $null 2>&1
$JobExists = ($LastExitCode -eq 0)
$ErrorActionPreference = $OldPreference

# Blog needs only the WP/DB/LLM envs. Keys are injected from Secret Manager
# references (<name>:latest) so plaintext is not baked in.
$EnvVariablesList = @(
    "GOOGLE_CLOUD_PROJECT=$GcpProject",
    "GCS_BUCKET_NAME=$GcsBucket",
    "DATABASE_FILENAME=/tmp/trading_agent.db",
    "BLOG_PERSONA=dexter"
)
# Alpaca keys are REQUIRED for the per-ticker candlestick charts. Without them
# get_client_instance() falls into mock mode and generates fake ~$400 bars,
# forcing a secondary y-axis and breaking the chart (the 9/10-9/11 regression).
# Inject them as plain env vars from .env, matching the strategy job's pattern.
$AlpacaKeys = @("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_PAPER")
Get-Content $EnvPath | ForEach-Object {
    $Line = $_.Trim()
    if ($Line -and -not $Line.StartsWith("#") -and $Line.Contains("=")) {
        $Parts = $Line.Split("=", 2)
        $Key = $Parts[0].Trim()
        $Val = $Parts[1].Trim().Trim("'`"")
        if ($AlpacaKeys -contains $Key -and $Val -and -not $Val.StartsWith("your_")) {
            $EnvVariablesList += "$Key=$Val"
        }
    }
}
# Jira credentials so the blog job's error->Jira logging (run_blog.py ->
# logger_setup.setup_logging) can file bug tickets. config.py reads JIRA_URL.
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
    "WP_USER=WP_USER:latest",
    "WP_APP_PASSWORD=WP_APP_PASSWORD:latest",
    "GEMINI_API_KEY=GEMINI_API_KEY:latest",
    "OPENROUTER_API_KEY=OPENROUTER_API_KEY:latest",
    "DISCORD_WEBHOOK_URL=DISCORD_WEBHOOK_URL:latest"
)
if (-not $JobExists) {
    & $GCloud run jobs create $JobName --image $ImageTag --region $Region `
        --set-env-vars ($EnvVariablesList -join ",") `
        --set-secrets ($SecretReferences -join ",")
} else {
    & $GCloud run jobs update $JobName --image $ImageTag --region $Region `
        --set-env-vars ($EnvVariablesList -join ",") `
        --set-secrets ($SecretReferences -join ",")
}

# 6. Cloud Scheduler after the strategy job's DB sync each trading day.
# The cron is expressed in UTC. 16:30 ET:
#   - 16:30 EDT (Mar-Nov, daylight) == 20:30 UTC  -> schedule "30 20 * * 1-5"
#   - 16:30 EST (Nov-Mar, standard) == 21:30 UTC   -> schedule "30 21 * * 1-5"
# Adjust the scheduled time for your timezone / DST below. Day-of-week 1-5 =
# weekdays (Mon-Fri) market days; 16:30 ET runs shortly after the 16:00 close so
# the full day's round-trips are captured and the DB has synced to GCS.
#
# NOTE (2026-09-10): Use the DEFAULT compute service account, NOT a custom
# `run-invoker` SA. The `run-invoker@<project>.iam.gserviceaccount.com` SA does
# not exist in this project, so referencing it made scheduler creation fail
# silently during a redeploy and dropped `dexter-blog-scheduler` entirely (no
# daily posts). The working `agent-trade-scheduler` uses the default compute SA
# (812795138269-compute@developer.gserviceaccount.com). Resolve it dynamically
# so this stays correct across projects.
$DefaultComputeSa = (& $GCloud iam service-accounts list --format="value(email)" 2>$null |
    Where-Object { $_ -like "*-compute@developer.gserviceaccount.com" } | Select-Object -First 1)
if (-not $DefaultComputeSa) {
    # Fallback: the standard default compute SA email for the project.
    $DefaultComputeSa = "$GcpProject-number-compute@developer.gserviceaccount.com"
}
$SchedulerSa = $DefaultComputeSa
Write-Host "Scheduler service account: $SchedulerSa"
& $GCloud scheduler jobs delete $SchedulerName --location $Region --quiet 2>$null
& $GCloud scheduler jobs create http $SchedulerName --schedule="30 20 * * 1-5" `
    --location $Region `
    --uri="https://$Region-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$GcpProject/jobs/$JobName:run" `
    --http-method=POST `
    --oauth-service-account-email=$SchedulerSa `
    --oauth-token-scope="https://www.googleapis.com/auth/cloud-platform"
if ($LASTEXITCODE -ne 0) {
    Write-Error "Scheduler creation failed (exit $LASTEXITCODE). The blog job will not run daily. Fix and re-run."
}

Write-Host "`nDone: blog job $JobName deployed; scheduler $SchedulerName registered."
Write-Host "Schedule runs 16:30 ET (Mon-Fri) via UTC cron '30 20 * * 1-5' (EDT)."
Write-Host "If DST changes, flip to '30 21 * * 1-5' (EST)."
Write-Host "Verify: & $GCloud run jobs describe $JobName --region $Region"