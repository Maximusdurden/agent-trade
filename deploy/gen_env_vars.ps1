# filename: deploy/gen_env_vars.ps1
# Generates deploy/env_vars.yaml from .env for `gcloud run jobs update --env-vars-file`.
# Uses YAML (not --set-env-vars) because --set-env-vars splits on commas, which
# breaks values that themselves contain commas (e.g. STRATEGIST_AB_MODELS="a/b,c/d").
$ErrorActionPreference = "Stop"

$EnvPath = "Z:\python\projects\agent-trade\.env"
$OutPath = "Z:\python\projects\agent-trade\deploy\env_vars.yaml"

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

$AllowedRuntimeKeys = @(
    "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_PAPER", "LLM_PROVIDER", "GEMINI_API_KEY", "GEMINI_MODEL",
    "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL",
    "MODEL_HEAVYWEIGHT", "MODEL_DAILY_DRIVER", "MODEL_UTILITY",
    "ACTIVE_MODEL_TIER", "BRAIN_MODEL_TIER", "STRATEGIST_MODEL_TIER",
    "STRATEGIST_AB_MODELS", "STRATEGIST_AB_LABEL",
    "BRAIN_AB_MODELS", "BRAIN_AB_LABEL",
    "BRAIN_MAX_OUTPUT_TOKENS",
    "LLM_MAX_TOTAL_SECONDS",
    "TRADING_INTERVAL_MINUTES", "JIRA_URL", "JIRA_PROJECT_KEY", "JIRA_EMAIL", "JIRA_API_TOKEN",
    "OPTIONS_ENABLED", "OPTIONS_DTE_MIN", "OPTIONS_DTE_MAX",
    "OPTIONS_DTE_HARD_MIN", "OPTIONS_DTE_HARD_MAX", "OPTIONS_DTE_FALLBACK_MAX",
    "OPTIONS_WATCH_COOLDOWN_MINUTES",
    "OPTIONS_MAX_ALLOCATION_PCT", "OPTIONS_MAX_CONTRACTS_PER_TICKER",
    "OPTIONS_MAX_TOTAL_EXPOSURE_PCT",
    "OPTIONS_CONVICTION_THRESHOLD", "OPTIONS_AUTO_CLOSE_DTE",
    "OPTIONS_EVENT_GATE_ENABLED", "OPTIONS_EVENT_GATE_INCLUDE_FOMC",
    "OPTIONS_VEGA_CAP_MV_PCT", "OPTIONS_DELTA_CAP_PCT", "OPTIONS_EOD_FLAT",
    "OPTIONS_OTM_PERCENT_MIN", "OPTIONS_OTM_PERCENT_MAX",
    "MAX_CLUSTER_ALLOCATION_PCT",
    "MAX_CONSECUTIVE_LOSSES", "MAX_WHIPSAW_RATIO", "MIN_WHIPSAW_TRADES", "CIRCUIT_BREAKER_LOOKBACK_DAYS",
    "MIN_LOW_WIN_RATE_TRADES", "MAX_LOW_WIN_RATE", "STRICT_UNIVERSE_ENABLED", "ANTI_SCALE_IN_TOLERANCE_PCT",
    "STRATEGY_STALE_HOURS",
    "CRYPTO_BRACKET_ENABLED", "CRYPTO_TAKE_PROFIT_PCT", "CRYPTO_STOP_LOSS_PCT",
    "MIN_CRYPTO_ORDER_NOTIONAL", "MIN_SELL_VALUE",
    "VOL_SIZING_ENABLED", "VOL_SIZING_BASELINE_ATR_PCT", "VOL_SIZING_MIN_ALLOCATION_PCT",
    "INTRADAY_LOSS_LIMIT_PCT", "INTRADAY_BREAKER_ENABLED",
    "MAX_TRADES_PER_CYCLE",
    "EQUITY_RSI_ENTRY_MAX", "EQUITY_OPEN_BUY_CAP_PER_DAY", "EQUITY_DRAWDOWN_EXIT_PCT"
)

$Lines = @(
    "GOOGLE_CLOUD_PROJECT: $GcpProject",
    "GCS_BUCKET_NAME: $GcsBucket",
    # Quote booleans/numbers so YAML keeps them as strings (gcloud requires
    # env var values to be strings, not bool/int).
    "BYPASS_MARKET_WINDOW: `"True`""
)

Get-Content $EnvPath | ForEach-Object {
    $Line = $_.Trim()
    if ($Line -and -not $Line.StartsWith("#") -and $Line.Contains("=")) {
        $Parts = $Line.Split("=", 2)
        $Key = $Parts[0].Trim()
        $Val = $Parts[1].Trim().Trim("'`"")
        if ($AllowedRuntimeKeys -contains $Key -and $Val -and -not $Val.StartsWith("your_")) {
            # Quote values that YAML would otherwise coerce to a non-string
            # (booleans like True/False, numbers) OR that contain YAML-special
            # characters (commas, colons, #, braces, quotes, backslashes,
            # leading/trailing spaces). gcloud requires env values be strings.
            $LooksBool = $Val -match '^(true|false|yes|no|on|off)$'
            $LooksNum = $Val -match '^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$'
            if ($LooksBool -or $LooksNum -or $Val -match '[:,:#{}\[\]&*!|>%@`"\\]' -or $Val -match '^\s|\s$') {
                $Escaped = $Val.Replace("\", "\\").Replace('"', '\"')
                $Lines += "${Key}: `"${Escaped}`""
            } else {
                $Lines += "${Key}: $Val"
            }
        }
    }
}

$Content = [string]::Join("`n", $Lines)
[System.IO.File]::WriteAllText($OutPath, $Content, [System.Text.Encoding]::UTF8)
Write-Host "Wrote $OutPath"