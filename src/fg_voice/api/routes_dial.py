"""Outbound call initiator — dev/ops helper page.

GET  /dial         → simple HTML form to enter a phone number
POST /dial/call    → triggers a Twilio outbound call to that number;
                     Twilio then webhooks /voice/inbound to start the
                     conversation graph as normal.

Not admin-gated so it works in dev without ADMIN_API_KEY. In production
this should sit behind a VPN or be removed from the public ALB routing.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, JSONResponse
from twilio.rest import Client  # type: ignore[import-untyped]

from fg_voice.api.auth import require_basic_auth
from fg_voice.config import get_settings
from fg_voice.obs.logging import get_logger

log = get_logger(__name__)

router = APIRouter(
    prefix="/dial",
    tags=["dial"],
    dependencies=[Depends(require_basic_auth)],
)

_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sentinel — Initiate Call</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; }
    body {
      margin: 0; min-height: 100vh;
      display: flex; align-items: center; justify-content: center;
      background: #eef2f7;
      font-family: system-ui, sans-serif;
    }
    .card {
      background: white; border-radius: 12px;
      box-shadow: 0 4px 24px rgba(0,0,0,.10);
      padding: 40px 36px; width: 380px;
    }
    .logo { display: flex; align-items: center; gap: 10px; margin-bottom: 28px; }
    .logo-icon {
      width: 40px; height: 40px; border-radius: 10px;
      background: #1a3a5c; display: flex; align-items: center;
      justify-content: center; font-size: 20px;
    }
    .logo-text { font-size: 1rem; font-weight: 700; color: #1a3a5c; }
    .logo-sub  { font-size: 0.75rem; color: #888; }
    h2 { margin: 0 0 6px; font-size: 1.15rem; color: #1a3a5c; }
    p  { margin: 0 0 24px; font-size: 0.85rem; color: #666; }
    label { display: block; font-size: 0.8rem; font-weight: 600;
            color: #444; margin-bottom: 6px; }
    input[type=tel] {
      width: 100%; padding: 10px 14px;
      border: 1px solid #d0d7de; border-radius: 8px;
      font-size: 1rem; outline: none; transition: border .2s;
    }
    input[type=tel]:focus { border-color: #1a3a5c; }
    .hint { font-size: 0.75rem; color: #999; margin: 6px 0 20px; }
    button {
      width: 100%; padding: 12px;
      background: #1a3a5c; color: white;
      border: none; border-radius: 8px;
      font-size: 0.95rem; font-weight: 600;
      cursor: pointer; transition: background .2s;
    }
    button:hover { background: #14304d; }
    button:disabled { background: #aaa; cursor: not-allowed; }
    #status {
      margin-top: 18px; padding: 12px 14px;
      border-radius: 8px; font-size: 0.85rem;
      display: none;
    }
    #status.ok  { background: #e6f4ea; color: #1e6e3a; border: 1px solid #a8d5b5; }
    #status.err { background: #fdecea; color: #a01c1c; border: 1px solid #f5aea8; }
    .reports-link {
      display: block; margin-top: 20px; text-align: center;
      padding: 10px; border: 1px solid #d0d7de;
      border-radius: 8px; text-decoration: none;
      color: #1a3a5c; font-size: 0.85rem; font-weight: 600;
      transition: background .2s;
    }
    .reports-link:hover { background: #f6f8fa; }
  </style>
</head>
<body>
<div class="card">
  <div class="logo">
    <div class="logo-icon">&#127754;</div>
    <div>
      <div class="logo-text">Sentinel</div>
      <div class="logo-sub">Voice Agent</div>
    </div>
  </div>
  <h2>Initiate a Call</h2>
  <p>Enter the phone number to call. The AI agent will answer and collect the flood report.</p>
  <form id="form">
    <label for="phone">Phone number</label>
    <input id="phone" name="phone" type="tel"
           placeholder="+91 98765 43210" required autocomplete="tel">
    <div class="hint">Include country code, e.g. +91 for India</div>
    <button id="btn" type="submit">&#128222; Call now</button>
  </form>
  <div id="status"></div>
  <a class="reports-link" href="/api/v1/console/">&#128202; View Call Reports Dashboard</a>
</div>
<script>
  const form   = document.getElementById('form');
  const btn    = document.getElementById('btn');
  const status = document.getElementById('status');

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    btn.disabled = true;
    btn.textContent = 'Calling…';
    status.style.display = 'none';

    const body = new URLSearchParams({ phone: document.getElementById('phone').value });
    try {
      const res  = await fetch('/dial/call', { method: 'POST', body });
      const data = await res.json();
      if (res.ok) {
        status.className = 'ok';
        status.textContent = '✓ Call initiated — SID: ' + data.call_sid;
      } else {
        status.className = 'err';
        status.textContent = '✗ ' + (data.detail || 'Unknown error');
      }
    } catch (err) {
      status.className = 'err';
      status.textContent = '✗ Network error: ' + err.message;
    }
    status.style.display = 'block';
    btn.disabled = false;
    btn.textContent = '☎️ Call now';
  });
</script>
</body>
</html>
"""


@router.get("", response_class=HTMLResponse)
async def dial_page() -> HTMLResponse:
    return HTMLResponse(_HTML)


@router.post("/call")
async def initiate_call(
    phone: str = Form(...),
) -> JSONResponse:
    settings = get_settings()

    if not settings.twilio_account_sid or not settings.twilio_auth_token.get_secret_value():
        return JSONResponse({"detail": "Twilio credentials not configured"}, status_code=503)
    if not settings.twilio_phone_number:
        return JSONResponse({"detail": "TWILIO_PHONE_NUMBER not configured"}, status_code=503)

    # Build the webhook URL from PUBLIC_WSS_BASE (wss://... → https://...)
    # so Twilio always gets the public ngrok/prod URL, not localhost.
    public_base = settings.public_wss_base.replace("wss://", "https://").replace("ws://", "http://").rstrip("/")
    # In runner_mode we skip the /voice/inbound redirect and point Twilio
    # straight at the gather flow — one fewer round trip, one fewer thing
    # to fail on a flaky dev tunnel.
    if settings.runner_mode:
        webhook_url = f"{public_base}/voice/gather/start"
    else:
        webhook_url = f"{public_base}/voice/inbound"

    try:
        client = Client(
            settings.twilio_account_sid,
            settings.twilio_auth_token.get_secret_value(),
        )
        call = client.calls.create(
            to=phone,
            from_=settings.twilio_phone_number,
            url=webhook_url,
        )
        log.info("dial.call_initiated", to=phone[:6] + "****", call_sid=call.sid)
        return JSONResponse({"call_sid": call.sid, "status": call.status})
    except Exception as exc:
        log.warning("dial.call_failed", error=str(exc))
        return JSONResponse({"detail": str(exc)}, status_code=500)
