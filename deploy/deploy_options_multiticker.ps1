# filename: deploy_options_multiticker.ps1
# Deploy the multi-ticker Options Model A runner as a Cloud Run Job
# + Cloud Scheduler. Modeled on deploy_options_sentiment_sr.ps1.
#
# USAGE:
#   .\deploy\deploy_options_multiticker.ps1
#
# Pre-requisites: .env with GOOGLE_CLOUD_PROJECT, GCS_BUCKET_NAME, ALPACA_*,
# and (for error->Jira logging) JIRA_* credentials.
#
# Deploys ONE job from the sideload image (entrypoint = runner_options_multiticker.py):
#   - options-multiticker-runner : TSLA/META Model A options runner (live)
#
# Schedule: 09:29 AM ET Mon-Fri (cron "29 9 * * 1-5").
#   - 09:29 EDT (Mar-Nov) == 13:29 UTC -> "29 13 * * 1-5"
#   - 09:29 EST (Nov-Mar) == 14:29 UTC -> "29 14 * * 1-5"

# Accept -Auto (non-interactive) flag. The script is already non-interactive;
# this parameter is accepted for compatibility with automated invocation.
param(
    [switch]$Auto
)

# Use "Continue" (not "Stop") because the gcloud.ps1 PowerShell wrapper converts
# benign stderr output into a NativeCommandError that would abort the deploy.
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
$ImageTag = "gcr.io/$GcpProject/agent-trade-sideload:$BuildId"
$JobName = "options-multiticker-runner"
$SchedulerName = "options-multiticker-scheduler"

# Resource specs (per directive).
$Cpu = "1"
$Memory = "1Gi"
$Timeout = "3600s"
$MaxRetries = 0

Write-Host "GCP Project:   $GcpProject"
Write-Host "GCS Bucket:    $GcsBucket"
Write-Host "Region:        $Region"
Write-Host "Image:         $ImageTag"
Write-Host "Job:           $JobName"
Write-Host "Scheduler:     $SchedulerName"

# Resolve gcloud (same fallback as deploy_sideload.ps1).
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
$StagingDir = "Z:\python\projects\agent-trade\deploy\temp_staging_options_multiticker"
if (Test-Path $StagingDir) { Remove-Item $StagingDir -Recurse -Force }
New-Item -ItemType Directory -Path $StagingDir | Out-Null

Copy-Item "Z:\python\projects\agent-trade\*" -Destination $StagingDir -Recurse -Force `
    -Exclude "venv", ".venv", ".git", "deploy", ".env", "trading_agent.db", "trading.log", "__pycache__"

# Copy the sibling agent-jira-client dependency (needed for error->Jira logging).
Copy-Item "Z:\python\projects\agent-jira-client" -Destination (Join-Path $StagingDir "agent-jira-client") -Recurse -Force -Exclude "venv", ".git"

# 3. Sideload Dockerfile (entrypoint set per-job at deploy time via --command).
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
# Entrypoint is overridden per-job via --command (runner_options_multiticker.py).
"@
$DockerfileContent | Out-File -FilePath $DockerProdPath -Encoding utf8

# 4. Build image
Write-Host "`n--- Building sideload image ---"
& $GCloud builds submit $StagingDir --tag $ImageTag

# 5. Deploy Cloud Run Job
$EnvVariablesList = @(
    "GOOGLE_CLOUD_PROJECT=$GcpProject",
    "GCS_BUCKET_NAME=$GcsBucket",
    "DATABASE_FILENAME=/tmp/trading_agent.db"
)
# Jira credentials (for error->Jira logging) from .env.
$JiraKeys = @("JIRA_URL", "JIRA_PROJECT_KEY", "JIRA_EMAIL", "JIRA_API_TOKEN")
# Alpaca + config keys the multiticker runner needs.
$ConfigKeys = @(
    "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_PAPER",
    "BYPASS_MARKET_WINDOW"
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

Write-Host "`n--- Deploying Cloud Run Job: $JobName ---"
$OldPreference = $ErrorActionPreference
$ErrorActionPreference = "SilentlyContinue"
& $GCloud run jobs describe $JobName --region $Region --format="value(name)" > $null 2>&1
$JobExists = ($LastExitCode -eq 0)
$ErrorActionPreference = $OldPreference

# Pass env vars via a YAML flags file (robust to commas/special chars).
$FlagsFile = Join-Path $StagingDir "deploy_flags_${JobName}.yaml"
$FlagLines = @("--set-env-vars:")
foreach ($Entry in $EnvVariablesList) {
    $Parts = $Entry.Split("=", 2)
    $K = $Parts[0]
    $V = $Parts[1]
    $Escaped = $V.Replace("\", "\\").Replace('"', '\"')
    $FlagLines += "  ${K}: `"$Escaped`""
}
[System.IO.File]::WriteAllText($FlagsFile, [string]::Join("`n", $FlagLines), [System.Text.Encoding]::UTF8)

if (-not $JobExists) {
    & $GCloud run jobs create $JobName --image $ImageTag --region $Region `
        --command "python" --args "sideload/runner_options_multiticker.py,--live" `
        --flags-file $FlagsFile `
        --set-secrets ($SecretReferences -join ",") `
        --cpu $Cpu --memory $Memory --task-timeout $Timeout --max-retries $MaxRetries
} else {
    & $GCloud run jobs update $JobName --image $ImageTag --region $Region `
        --command "python" --args "sideload/runner_options_multiticker.py,--live" `
        --flags-file $FlagsFile `
        --set-secrets ($SecretReferences -join ",") `
        --cpu $Cpu --memory $Memory --task-timeout $Timeout --max-retries $MaxRetries
}
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to deploy job $JobName (exit $LASTEXITCODE)."
}

# 6. Cloud Scheduler
$DefaultComputeSa = (& $GCloud iam service-accounts list --format="value(email)" 2>$null |
    Where-Object { $_ -like "*-compute@developer.gserviceaccount.com" } | Select-Object -First 1)
if (-not $DefaultComputeSa) {
    $DefaultComputeSa = "$GcpProject-number-compute@developer.gserviceaccount.com"
}
Write-Host "Scheduler service account: $DefaultComputeSa"

Write-Host "`n--- Registering Cloud Scheduler: $SchedulerName ---"
$OldPreference = $ErrorActionPreference
$ErrorActionPreference = "SilentlyContinue"
& $GCloud scheduler jobs delete $SchedulerName --location $Region --quiet 2>$null
$ErrorActionPreference = $OldPreference

# Run at 09:29 AM ET Mon-Fri. UTC cron depends on DST:
#   - 09:29 EDT (Mar-Nov) == 13:29 UTC -> "29 13 * * 1-5"
#   - 09:29 EST (Nov-Mar) == 14:29 UTC -> "29 14 * * 1-5"
& $GCloud scheduler jobs create http $SchedulerName --schedule="29 13 * * 1-5" `
    --location $Region `
    --uri="https://$Region-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$GcpProject/jobs/${JobName}:run" `
    --http-method=POST `
    --oauth-service-account-email=$DefaultComputeSa `
    --oauth-token-scope="https://www.googleapis.com/auth/cloud-platform"
if ($LASTEXITCODE -ne 0) {
    Write-Error "Scheduler creation failed for $SchedulerName (exit $LASTEXITCODE)."
}

Write-Host "`nDone: options-multiticker-runner job deployed."
Write-Host "  Job       : $JobName"
Write-Host "  Scheduler : $SchedulerName  ~09:29 AM ET Mon-Fri"
Write-Host "  Resources : $Cpu CPU, $Memory RAM, timeout $Timeout, max-retries $MaxRetries"
Write-Host "Verify: & $GCloud run jobs list --region $Region"
