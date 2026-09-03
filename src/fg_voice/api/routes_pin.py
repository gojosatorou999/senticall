"""Pin-drop landing page for the post-call SMS.

The SMS body in `SmsPinOfferService` points at
`{SMS_PIN_OFFER_BASE_URL}/pin/{short_ref}`. This module serves that
URL: a minimal HTML page confirming the report ref, then asking the
caller to drop a pin on a Leaflet map. The submission POSTs back to
`/pin/{short_ref}` with `lat`/`lng`, which updates the row's
`location_raw`/`location_resolved` in a single atomic UPDATE (kept
tight — the caller is on a flaky cellular link, one round-trip is
all we get).

Not gated by admin auth: the URL is delivered directly to the caller
via SMS, so the `short_ref` acts as the capability token. Enumeration
is bounded by the alphabet (FG-XXXX, 32^4 ≈ 1M values, 24-bit) — an
attacker guessing a random ref only sees a pin form for it and can
overwrite its location. That's acceptable for MVP: we log every
write, ops can revert; a signed HMAC token can layer on top in P7 if
abuse is observed. Documented in the module docstring so a future
reviewer doesn't mistake the missing auth for an oversight.

Kept in `api/` (per the layered contract), imports the persistence
model directly."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Final

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fg_voice.obs.logging import get_logger
from fg_voice.persistence.db import get_session_maker
from fg_voice.persistence.models import Report

log = get_logger(__name__)
router = APIRouter(prefix="/pin", tags=["pin"])


async def _session_dep() -> AsyncIterator[AsyncSession]:
    async with get_session_maker()() as session:
        yield session


# Absolute minimum viable form — Leaflet + OSM tiles, no framework,
# no JS bundle, no CDN auth. Loads in <300 KB on a rural 2G link.
_HTML_TEMPLATE: Final[str] = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sentinel: report {short_ref}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
      integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">
<style>
  body {{ font-family: system-ui, sans-serif; margin: 0; padding: 12px; }}
  #map {{ height: 60vh; margin: 12px 0; border: 1px solid #ccc; }}
  h1 {{ font-size: 1.1rem; }}
  button {{ font-size: 1rem; padding: 10px 16px; }}
  .ref {{ font-family: monospace; background: #f4f4f4; padding: 2px 6px; }}
</style>
</head><body>
<h1>Drop a pin on the flood location for <span class="ref">{short_ref}</span></h1>
<p>Tap the map where the flooding is. Then press <b>Save pin</b>.</p>
<div id="map"></div>
<form method="post" action="/pin/{short_ref}" id="pinform">
  <input type="hidden" name="lat" id="lat">
  <input type="hidden" name="lng" id="lng">
  <button type="submit" disabled id="save">Save pin</button>
</form>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
        integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
<script>
  var map = L.map('map').setView([16.5, 80.0], 7);
  L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
    attribution: '&copy; OpenStreetMap'
  }}).addTo(map);
  var marker = null;
  map.on('click', function(e) {{
    if (marker) map.removeLayer(marker);
    marker = L.marker(e.latlng).addTo(map);
    document.getElementById('lat').value = e.latlng.lat.toFixed(6);
    document.getElementById('lng').value = e.latlng.lng.toFixed(6);
    document.getElementById('save').disabled = false;
  }});
</script>
</body></html>"""

_THANKYOU_HTML: Final[str] = """<!doctype html>
<html><head><meta charset="utf-8"><title>Sentinel</title></head>
<body style="font-family: system-ui, sans-serif; padding: 24px;">
<h1>Pin saved</h1>
<p>Thanks — we've attached the exact location to report
<span style="font-family: monospace;">{short_ref}</span>.</p>
</body></html>"""


@router.get("/{short_ref}", response_class=HTMLResponse)
async def pin_form(short_ref: str, session: AsyncSession = Depends(_session_dep)) -> Response:
    """Render the pin-drop form for a known report. Unknown refs return
    404 — the SMS obviously wasn't for this deploy."""
    row = await session.scalar(select(Report).where(Report.short_ref == short_ref))
    if row is None:
        raise HTTPException(status_code=404, detail="report not found")
    html = _HTML_TEMPLATE.format(short_ref=short_ref)
    return HTMLResponse(content=html)


@router.post("/{short_ref}", response_class=HTMLResponse)
async def submit_pin(
    short_ref: str,
    lat: float = Form(...),
    lng: float = Form(...),
    session: AsyncSession = Depends(_session_dep),
) -> Response:
    """Store the caller's picked coordinates. Overwrites any previous
    pin — a caller who tapped the wrong spot can just re-open the SMS
    link and drop again. Bounded to India's rough bbox so a copy-paste
    error into a scripted client can't plant a marker in the Pacific."""
    if not (6.0 <= lat <= 38.0) or not (68.0 <= lng <= 98.0):
        raise HTTPException(status_code=422, detail="coordinates outside India bounds")
    async with session.begin():
        row = await session.scalar(select(Report).where(Report.short_ref == short_ref))
        if row is None:
            raise HTTPException(status_code=404, detail="report not found")
        # Overwrite `location_resolved` with a `"lat,lng"` string. The
        # column is a plain VARCHAR (spec §12.1 keeps the schema PostGIS-
        # free at the ORM level; the geo-typed column arrives with the P7
        # PostGIS migration). Marker stamp so ops can tell caller-supplied
        # pins apart from RAG-resolved names.
        row.location_resolved = f"pin:{lat:.6f},{lng:.6f}"
        row.updated_at = datetime.now(UTC)
        log.info(
            "pin.saved",
            short_ref=short_ref,
            lat=lat,
            lng=lng,
        )
    return HTMLResponse(content=_THANKYOU_HTML.format(short_ref=short_ref))


__all__ = ["router"]
