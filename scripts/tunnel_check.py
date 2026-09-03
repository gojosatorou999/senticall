"""Diagnose the Twilio -> agent reachability chain.

Twilio reports every break in this chain the same way: error 11200,
"cannot reach the TwiML server", and the caller hears an application
error. This script tells you *which* link is broken.

The chain, in the order it fails in practice:

1. **API up** — uvicorn listening on :8080 (`make api`). Without it the
   tunnel forwards into a closed port and ngrok returns 502.
2. **Tunnel running** — the local ngrok agent (`make tunnel`). Its own
   API on :4040 is the cheapest way to confirm it. A dead agent makes
   the public host return the ngrok "offline" page (ERR_NGROK_3200).
3. **Tunnel host matches PUBLIC_WSS_BASE** — every webhook URL Twilio is
   handed (`routes_dial.initiate_call`) and every Gather `action`
   (`routes_gather._next_action_path`) is built from that setting, so a
   tunnel on a *different* host means Twilio is pointed somewhere real
   but empty. This is the failure that survives a restart and looks
   like a code bug.
4. **Public URL returns TwiML** — POST the real entry webhook with
   Twilio's User-Agent and confirm a 200 carrying `<Response>`. A 200
   of HTML here is the ngrok free-tier browser interstitial, not TwiML.

Read-only: sends one POST to the agent's own entry webhook, which
creates a throwaway session row and places no call.

    uv run python scripts/tunnel_check.py
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"
NGROK_API = "http://127.0.0.1:4040/api/tunnels"
LOCAL_PORT = 8080
TIMEOUT = 15


def _public_base() -> str:
    """PUBLIC_WSS_BASE as an https:// origin — the same wss->https
    rewrite `routes_dial` and `routes_voice` do before handing a URL to
    Twilio. Env wins over .env so a shell override is honoured."""
    raw = os.environ.get("PUBLIC_WSS_BASE", "")
    if not raw and ENV_FILE.exists():
        text = ENV_FILE.read_text(encoding="utf-8-sig")
        match = re.search(r"^PUBLIC_WSS_BASE=(\S+)", text, re.MULTILINE)
        raw = match.group(1) if match else ""
    return raw.replace("wss://", "https://").replace("ws://", "http://").rstrip("/")


def _fail(step: str, detail: str, fix: str) -> int:
    print(f"FAIL {step}\n     {detail}\n     fix: {fix}")
    return 1


def main() -> int:
    base = _public_base()
    if not base:
        return _fail("config", "PUBLIC_WSS_BASE is unset", "set it in .env")
    host = base.split("://", 1)[1]
    print(f"PUBLIC_WSS_BASE -> {base}")

    # 1. local API
    with socket.socket() as sock:
        sock.settimeout(3)
        if sock.connect_ex(("127.0.0.1", LOCAL_PORT)) != 0:
            return _fail(
                "api", f"nothing listening on 127.0.0.1:{LOCAL_PORT}", "run 'make api'"
            )
    print(f"OK   api listening on :{LOCAL_PORT}")

    # 2 + 3. ngrok agent, and whether it serves the host Twilio is sent to
    try:
        with urllib.request.urlopen(NGROK_API, timeout=5) as resp:
            tunnels = json.load(resp).get("tunnels", [])
    except (urllib.error.URLError, TimeoutError, OSError):
        return _fail(
            "tunnel",
            "ngrok agent API on :4040 is not responding",
            "run 'make tunnel'",
        )
    live = [t.get("public_url", "") for t in tunnels]
    if not any(host in url for url in live):
        return _fail(
            "tunnel",
            f"ngrok is running but serves {live or '[]'}, not {host}",
            f"restart it as 'ngrok http {LOCAL_PORT} --url={host}', "
            "or point PUBLIC_WSS_BASE at the live tunnel",
        )
    print(f"OK   tunnel serving {host} -> :{LOCAL_PORT}")

    # 4. the request Twilio actually makes
    req = urllib.request.Request(
        f"{base}/voice/gather/start",
        data=b"CallSid=CAtunnelcheck&From=%2B910000000000&CallStatus=in-progress",
        headers={
            "User-Agent": "TwilioProxy/1.1",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            status, body = resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return _fail(
            "twiml",
            f"POST {base}/voice/gather/start returned HTTP {exc.code}",
            "check the api logs — the webhook itself is erroring",
        )
    except (urllib.error.URLError, TimeoutError) as exc:
        return _fail("twiml", f"POST {base}/voice/gather/start failed: {exc}", "check the tunnel")

    if status != 200:
        return _fail("twiml", f"HTTP {status} from the entry webhook", "check the api logs")
    if "<Response>" not in body:
        return _fail(
            "twiml",
            "200 but the body is not TwiML (likely the ngrok free interstitial)",
            "use a reserved ngrok domain, or a paid tunnel",
        )

    print(f"OK   TwiML served from {base}/voice/gather/start")
    print("\nTwilio can reach the agent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
