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

if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    Write-Error "gcloud CLI is not installed or not in PATH."
}
gcloud config set project $GcpProject
gcloud services enable cloudscheduler.googleapis.com run.googleapis.com cloudbuild.googleapis.com

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
gcloud builds submit $StagingDir --tag $ImageTag

# 5. Deploy Cloud Run Job
Write-Host "`n--- Deploying Cloud Run Job: $JobName ---"
$OldPreference = $ErrorActionPreference
$ErrorActionPreference = "SilentlyContinue"
& gcloud run jobs describe $JobName --region $Region --format="value(name)" > $null 2>&1
$JobExists = ($LastExitCode -eq 0)
$ErrorActionPreference = $OldPreference

# Blog needs only the WP/DB/LLM envs. Keys are injected from Secret Manager
# references (<name>:latest) so plaintext is not baked in.
$EnvVariablesList = @(
    "GOOGLE_CLOUD_PROJECT=$GcpProject",
    "GCS_BUCKET_NAME=$GcsBucket",
    "BLOG_PERSONA=dexter"
)
$SecretReferences = @(
    "WP_USER=projects/$GcpProject/secrets/WP_USER:latest",
    "WP_APP_PASSWORD=projects/$GcpProject/secrets/WP_APP_PASSWORD:latest",
    "GEMINI_API_KEY=projects/$GcpProject/secrets/GEMINI_API_KEY:latest",
    "OPENROUTER_API_KEY=projects/$GcpProject/secrets/OPENROUTER_API_KEY:latest"
)
if (-not $JobExists) {
    gcloud run jobs create $JobName --image $ImageTag --region $Region `
        --set-env-vars ($EnvVariablesList -join ",") `
        --set-secrets ($SecretReferences -join ",")
} else {
    gcloud run jobs update $JobName --image $ImageTag --region $Region `
        --set-env-vars ($EnvVariablesList -join ",") `
        --set-secrets ($SecretReferences -join ",")
}

# 6. Cloud Scheduler after the strategy job's EOD sync. The cron is in UTC.
# 19:05 ET (EDT) == 23:05 UTC in summer; adjust for your timezone. The schedule
# below ("30 19") is a placeholder — set it to run a few minutes AFTER the
# strategy job uploads its DB to GCS each trading day.
$SchedulerSa = "run-invoker@$GcpProject.iam.gserviceaccount.com"
& gcloud scheduler jobs delete $SchedulerName --location $Region --quiet 2>$null
gcloud scheduler jobs create http $SchedulerName --schedule="30 19 * * 1-5" `
    --location $Region `
    --uri="https://$Region-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$GcpProject/jobs/$JobName:run" `
    --http-method=POST `
    --oauth-service-account-email=$SchedulerSa `
    --oauth-token-scope="https://www.googleapis.com/auth/cloud-platform"

Write-Host "`nDone: blog job $JobName deployed; scheduler $SchedulerName registered."
Write-Host "NOTE: adjust --schedule so the blog runs AFTER the strategy job pushes its DB to GCS."
Write-Host "Verify: gcloud run jobs describe $JobName --region $Region"