"""Outbound call initiator — dev/ops helper page.

GET  /dial         → simple HTML form to enter a phone number
POST /dial/call    → triggers a Twilio outbound call to that number;
                     Twilio then webhooks /voice/inbound to start the
                     conversation graph as normal.

Not admin-gated so it works in dev without ADMIN_API_KEY. In production
this should sit behind a VPN or be removed from the public ALB routing.
"""

from __future__ import annotations

import re
import time
from collections import deque

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from twilio.rest import Client  # type: ignore[import-untyped]

from fg_voice.config import get_settings
from fg_voice.obs.logging import get_logger

log = get_logger(__name__)

# UNAUTHENTICATED BY DELIBERATE CHOICE, BUT GUARDED.
#
# The dial pad is hosted publicly (GitHub Pages) and must work with no
# login, so there is no Basic-auth on this router. Instead /dial/call is
# protected by two layers that do not depend on the Twilio account tier:
#   1. DIAL_ALLOWED_NUMBERS — only listed E.164 numbers can be dialed;
#      empty list refuses everything (fail closed).
#   2. Sliding-window rate limits: per client IP, per destination number,
#      and a global hourly cap as a backstop against spoofed IPs.
router = APIRouter(
    prefix="/dial",
    tags=["dial"],
)

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")
_WINDOW_SHORT = 600.0  # 10 min
_WINDOW_HOUR = 3600.0


class _SlidingWindow:
    """Timestamps of recent events per key; prunes on every check."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}

    def retry_after(self, key: str, limit: int, window: float, now: float) -> int:
        """0 if another event is allowed, else seconds until one would be."""
        q = self._hits.get(key)
        if q is None:
            return 0
        while q and now - q[0] >= window:
            q.popleft()
        if not q:
            del self._hits[key]
            return 0
        if len(q) < limit:
            return 0
        return max(1, int(window - (now - q[0])) + 1)

    def record(self, key: str, now: float) -> None:
        self._hits.setdefault(key, deque()).append(now)


_by_ip = _SlidingWindow()
_by_number = _SlidingWindow()
_global = _SlidingWindow()


def _reset_limits() -> None:
    """Test hook: clear all rate-limit state."""
    global _by_ip, _by_number, _global
    _by_ip, _by_number, _global = _SlidingWindow(), _SlidingWindow(), _SlidingWindow()


def _allowed_numbers(raw: str) -> set[str]:
    return {n for n in (_to_e164(p) for p in raw.split(",")) if n}


def _client_ip(request: Request) -> str:
    """Last X-Forwarded-For hop (the one our own proxy — ngrok/ALB — added;
    earlier hops are client-supplied and spoofable), else the socket peer."""
    xff = request.headers.get("x-forwarded-for", "")
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    if hops:
        return hops[-1]
    return request.client.host if request.client else "unknown"

_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FloodGuard — Initiate Call</title>
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
      <div class="logo-text">FloodGuard</div>
      <div class="logo-sub">Voice Agent</div>
    </div>
  </div>
  <h2>Initiate a Call</h2>
  <p>Enter the phone number to call. The AI agent will answer and collect the flood report.</p>
  <form id="form">
    <label for="phone">Phone number</label>
    <input id="phone" name="phone" type="tel" value="+91 "
           placeholder="+91 98765 43210" required autocomplete="tel" autofocus>
    <div class="hint">Spaces are fine &mdash; e.g. +91 98765 43210</div>
    <button id="btn" type="submit">&#128222; Call now</button>
  </form>
  <div id="status"></div>
  <a class="reports-link" href="/api/v1/console/">&#128202; View Call Reports Dashboard</a>
</div>
<script>
  const form   = document.getElementById('form');

  // Put the caret after the "+91 " default rather than before it.
  const phoneEl = document.getElementById('phone');
  phoneEl.addEventListener('focus', () => {
    if (phoneEl.selectionStart === 0) {
      const end = phoneEl.value.length;
      phoneEl.setSelectionRange(end, end);
    }
  });
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


def _to_e164(raw: str) -> str:
    """Strip the separators people naturally type.

    Twilio requires strict E.164 ("+919493579019") and rejects anything with
    spaces, dashes or brackets, so "+91 94935 79019" would fail. Normalising
    here rather than only in the page means every caller of this endpoint
    benefits — the hosted dial pad, the built-in /dial page, and curl.

    Only separators are removed; digits and the leading "+" are untouched, so
    an already-valid number passes through unchanged and an invalid one still
    fails at Twilio rather than being silently "fixed" into a wrong number."""
    return "".join(ch for ch in raw.strip() if ch.isdigit() or ch == "+")


@router.post("/call")
async def initiate_call(
    request: Request,
    phone: str = Form(...),
) -> JSONResponse:
    settings = get_settings()
    phone = _to_e164(phone)

    if not settings.twilio_account_sid or not settings.twilio_auth_token.get_secret_value():
        return JSONResponse({"detail": "Twilio credentials not configured"}, status_code=503)
    if not settings.twilio_phone_number:
        return JSONResponse({"detail": "TWILIO_PHONE_NUMBER not configured"}, status_code=503)

    # Guard 1: allow-list (fail closed). Checked before rate limits so a
    # refused number never consumes quota. The reply is the same whether the
    # list is empty or just doesn't contain the number — it never reveals it.
    if not _E164.match(phone) or phone not in _allowed_numbers(settings.dial_allowed_numbers):
        log.warning("dial.refused_not_allowed", to=phone[:6] + "****", ip=_client_ip(request))
        return JSONResponse(
            {"detail": "This number is not authorised for calls from this dial pad."},
            status_code=403,
        )

    # Guard 2: rate limits. Checked together, recorded only if all pass.
    now = time.monotonic()
    ip = _client_ip(request)
    waits = (
        _by_ip.retry_after(ip, settings.dial_limit_per_ip, _WINDOW_SHORT, now),
        _by_number.retry_after(phone, settings.dial_limit_per_number, _WINDOW_SHORT, now),
        _global.retry_after("all", settings.dial_limit_global_per_hour, _WINDOW_HOUR, now),
    )
    if any(waits):
        wait = max(waits)
        log.warning("dial.rate_limited", to=phone[:6] + "****", ip=ip, retry_after=wait)
        return JSONResponse(
            {"detail": f"Too many call requests. Try again in {wait} seconds."},
            status_code=429,
            headers={"Retry-After": str(wait)},
        )
    _by_ip.record(ip, now)
    _by_number.record(phone, now)
    _global.record("all", now)

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
