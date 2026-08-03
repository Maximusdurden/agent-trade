# filename: deploy_dashboard.ps1
# Automated Deployment Script for the Live Dashboard Cloud Run Service

$ErrorActionPreference = "Stop"

# 1. Load Configurations from .env
$EnvPath = "Z:\python\projects\agent-trade\.env"
if (-not (Test-Path $EnvPath)) {
    Write-Error "Could not find .env file at $EnvPath"
}

Write-Host "--- Loading environment configurations from .env ---"
$GcpProject = ""
$AlpacaApiKey = ""
$AlpacaSecretKey = ""
$AlpacaPaper = ""
Get-Content $EnvPath | ForEach-Object {
    $Line = $_.Trim()
    if ($Line -and -not $Line.StartsWith("#") -and $Line.Contains("=")) {
        $Parts = $Line.Split("=", 2)
        $Key = $Parts[0].Trim()
        $Val = $Parts[1].Trim().Trim("'`"")
        if ($Key -eq "GOOGLE_CLOUD_PROJECT") { $GcpProject = $Val }
        if ($Key -eq "ALPACA_API_KEY") { $AlpacaApiKey = $Val }
        if ($Key -eq "ALPACA_SECRET_KEY") { $AlpacaSecretKey = $Val }
        if ($Key -eq "ALPACA_PAPER") { $AlpacaPaper = $Val }
    }
}

if (-not [string]::IsNullOrEmpty($env:DEPLOY_GCP_PROJECT)) {
    $GcpProject = $env:DEPLOY_GCP_PROJECT
}

if ([string]::IsNullOrEmpty($GcpProject)) {
    Write-Error "GOOGLE_CLOUD_PROJECT is not configured."
}

if ($GcpProject) {
    $env:CLOUDSDK_CORE_PROJECT = $GcpProject
}

$Region = "us-east1"

if (-not [string]::IsNullOrEmpty($env:DEPLOY_SERVICE_NAME)) {
    $ServiceName = $env:DEPLOY_SERVICE_NAME
} else {
    $ServiceName = "agenttrade-dashboard"
}

$BuildId = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")
$ImageTag = "us-east1-docker.pkg.dev/" + $GcpProject + "/cloud-run-source-deploy/" + $ServiceName + ":" + $BuildId

Write-Host "GCP Project: $GcpProject"
Write-Host "Deployment Region: $Region"
Write-Host "Dashboard Service: $ServiceName"
Write-Host "Artifact Image: $ImageTag"

# 2. Check for gcloud installation
if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    Write-Error "gcloud CLI is not installed or not in PATH. Please install Google Cloud SDK."
}

# 3. Create Clean Build Context Staging Directory
$StagingDir = "Z:\python\projects\agent-trade\deploy\temp_staging_dashboard"
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

# Expose server port 8080 (Cloud Run default)
EXPOSE 8080

# Default command for custom dashboard server
ENTRYPOINT ["python", "dashboard/dashboard.py"]
"@

Set-Content -Path $DockerProdPath -Value $DockerfileContent

# 5. Trigger Google Cloud Build
Write-Host "`n--- Triggering Google Cloud Build in region $Region ---"
gcloud config set project $GcpProject
gcloud builds submit $StagingDir --tag $ImageTag --region $Region

# 6. Deploy Cloud Run Service using the built Image
Write-Host "`n--- Deploying Cloud Run Service: $ServiceName ---"
gcloud run deploy $ServiceName `
    --image $ImageTag `
    --region $Region `
    --project $GcpProject `
    --set-env-vars "GCS_BUCKET_NAME=agenttrade-us-data-bucket,DATABASE_FILENAME=/tmp/trading_agent.db,ALPACA_API_KEY=$AlpacaApiKey,ALPACA_SECRET_KEY=$AlpacaSecretKey,ALPACA_PAPER=$AlpacaPaper" `
    --quiet

# 7. Clean Staging Directory
Write-Host "`nCleaning up local staging directory..."
Remove-Item $StagingDir -Recurse -Force

Write-Host "`n========================================================"
Write-Host "DASHBOARD DEPLOYMENT COMPLETED SUCCESSFULLY!"
Write-Host "- Cloud Run Service: $ServiceName has been updated."
Write-Host "- Immutable Image: $ImageTag"
Write-Host "- Query the deployed service URL with: gcloud run services describe $ServiceName --region $Region --project $GcpProject --format=value(status.url)"
Write-Host "========================================================"
