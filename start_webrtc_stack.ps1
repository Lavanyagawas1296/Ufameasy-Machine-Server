<#! Start MediaMTX in background, then run dashboard server in this window. #>
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$config = Join-Path $root "streaming.local.ps1"
if (-not (Test-Path -LiteralPath $config)) {
    throw "Create streaming.local.ps1 by copying streaming.local.ps1.example, then edit it once."
}
. $config
$cfg = $UFAMeasyMediaGateway
foreach ($field in "DeviceId", "EdgeIp", "MediaMtxExe", "MediaMtxConfig") {
    if (-not $cfg[$field]) { throw "$field is required in streaming.local.ps1." }
}
if (-not (Test-Path -LiteralPath $cfg.MediaMtxExe)) { throw "MediaMTX not found: $($cfg.MediaMtxExe)" }
if (-not (Test-Path -LiteralPath $cfg.MediaMtxConfig)) { throw "MediaMTX config not found: $($cfg.MediaMtxConfig)" }

$env:UFAMEASY_STREAM_PROVIDER = "webrtc"
$deviceJson = @{ $cfg.DeviceId = @{ playback_url = "http://$($cfg.EdgeIp):8889"; control_url = "http://127.0.0.1:9997"; path = "machines/$($cfg.DeviceId)/main" } } | ConvertTo-Json -Compress -Depth 4
$env:UFAMEASY_MEDIA_DEVICES_JSON = $deviceJson

function Test-MediaMtxApi {
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:9997/v3/paths/list" -TimeoutSec 2 -ErrorAction Stop | Out-Null
        return $true
    } catch {
        return $false
    }
}

if (-not (Test-MediaMtxApi)) {
    $mediaDir = Split-Path -Parent $cfg.MediaMtxConfig
    Start-Process -FilePath $cfg.MediaMtxExe -ArgumentList $cfg.MediaMtxConfig -WorkingDirectory $mediaDir -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $mediaDir "mediamtx.stdout.log") `
        -RedirectStandardError (Join-Path $mediaDir "mediamtx.stderr.log")
    $ready = $false
    foreach ($attempt in 1..10) {
        Start-Sleep -Seconds 1
        if (Test-MediaMtxApi) { $ready = $true; break }
    }
    if (-not $ready) {
        $errorLog = Join-Path $mediaDir "mediamtx.stderr.log"
        $detail = if (Test-Path -LiteralPath $errorLog) { (Get-Content -LiteralPath $errorLog -Tail 20) -join "`n" } else { "No MediaMTX error log was created." }
        throw "MediaMTX did not become ready on http://127.0.0.1:9997. $detail"
    }
}

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Server virtual environment not found: $python" }
& $python -m uvicorn server.main:app --host 0.0.0.0 --port 8000
