# Boots the whole calling agent: mini-redis -> API -> ngrok tunnel.
#
# Usage:   .\start_agent.ps1
#          .\start_agent.ps1 -Stop      (shuts everything down)
#
# Safe to re-run: it stops anything already holding the ports first.

param([switch]$Stop)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$py   = Join-Path $root ".venv\Scripts\python.exe"

function Stop-Port($port, $label) {
    $lines = netstat -ano | Select-String ":$port\s" | Select-String "LISTENING"
    foreach ($l in $lines) {
        $procId = ($l -split '\s+')[-1]
        if ($procId -match '^\d+$') {
            Write-Host "  stopping $label (pid $procId)"
            taskkill /F /PID $procId 2>&1 | Out-Null
        }
    }
}

Write-Host "`n== stopping existing processes ==" -ForegroundColor Yellow
Stop-Port 6379 "mini-redis"
Stop-Port 8080 "api"
Get-Process ngrok -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2

if ($Stop) { Write-Host "`nAll stopped.`n" -ForegroundColor Green; exit 0 }

# ngrok must serve exactly the domain the app hands to Twilio, or Twilio
# gets a TwiML URL it cannot reach (Twilio error 11200).
$domain = (Select-String -Path (Join-Path $root ".env") -Pattern '^PUBLIC_WSS_BASE=wss?://(.+)$').Matches.Groups[1].Value.Trim()
if (-not $domain) { throw "PUBLIC_WSS_BASE missing from .env" }

Write-Host "`n== starting mini-redis ==" -ForegroundColor Cyan
Start-Process -FilePath $py -ArgumentList "scripts\run_fake_redis.py" `
    -WorkingDirectory $root -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $root "fakeredis.log") `
    -RedirectStandardError  (Join-Path $root "fakeredis.err.log")
Start-Sleep -Seconds 2

Write-Host "== starting API on :8080 ==" -ForegroundColor Cyan
Start-Process -FilePath $py -ArgumentList "-m","uvicorn","fg_voice.main:app","--host","0.0.0.0","--port","8080" `
    -WorkingDirectory $root -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $root "uvicorn_run.log") `
    -RedirectStandardError  (Join-Path $root "uvicorn.err.log")

# Migrations + prompt-bank validation run at boot, so give it real time.
# curl.exe rather than Invoke-WebRequest: the latter is unreliable here on
# PowerShell 5.1 and reported the API as down while it was serving 200s.
$curl = "$env:SystemRoot\System32\curl.exe"
$ready = $false
foreach ($i in 1..40) {
    Start-Sleep -Seconds 1
    $code = & $curl -s -o NUL -w "%{http_code}" --max-time 3 "http://localhost:8080/healthz" 2>$null
    if ($code -eq "200") { $ready = $true; break }
}
if (-not $ready) { throw "API did not come up - check uvicorn.err.log" }
Write-Host "  API healthy" -ForegroundColor Green

Write-Host "== starting ngrok on $domain ==" -ForegroundColor Cyan
Start-Process -FilePath "ngrok" -ArgumentList "http","8080","--url=https://$domain" `
    -WorkingDirectory $root -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $root "ngrok_run.log") `
    -RedirectStandardError  (Join-Path $root "ngrok.err.log")

$tunnel = $null
foreach ($i in 1..25) {
    Start-Sleep -Seconds 1
    $body = & $curl -s --max-time 3 "http://127.0.0.1:4040/api/tunnels" 2>$null
    if ($body) {
        try { $tunnel = ($body | ConvertFrom-Json).tunnels[0].public_url } catch { }
        if ($tunnel) { break }
    }
}
if (-not $tunnel) { throw "ngrok did not come up - check ngrok.err.log" }

Write-Host "`n=====================================================" -ForegroundColor Green
Write-Host " Calling agent is UP" -ForegroundColor Green
Write-Host "=====================================================" -ForegroundColor Green
Write-Host " Dial pad : $tunnel/dial"
Write-Host " Login    : sentinel / sentinel123"
Write-Host " Reports  : $tunnel/api/v1/console/"
Write-Host " Health   : $tunnel/healthz"
Write-Host "=====================================================`n" -ForegroundColor Green
