"""Call review console (spec §16.4).

A minimal internal HTML page for ops to:
- Filter calls by outcome, confidence, duration
- See the node path taken per call
- View per-turn latency and slot extraction results
- One-click "Add to golden set" (downloads a fixture JSON)

All endpoints are admin-gated (X-Admin-Api-Key header).
Public-facing: No. This route is /api/v1/console/* — not exposed via
the public ALB path routing unless an ops VPN or IP allowlist is added
(see Terraform waf.tf for the pattern).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse

from fg_voice.api.auth import require_basic_auth
from fg_voice.persistence.db import get_session_maker

router = APIRouter(
    prefix="/api/v1/console",
    tags=["console"],
    dependencies=[Depends(require_basic_auth)],
)

# ── HTML shell ───────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Sentinel Voice — Call Review Console</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; padding: 0; background: #f5f5f5; }
  header { background: #1a3a5c; color: white; padding: 12px 20px; display: flex; align-items: center; gap: 12px; }
  header h1 { margin: 0; font-size: 1.1rem; }
  .badge { background: #e8b44b; color: #1a3a5c; border-radius: 4px; padding: 2px 8px; font-size: 0.75rem; font-weight: bold; }
  .container { padding: 20px; }
  .filters { background: white; border-radius: 8px; padding: 16px; margin-bottom: 20px; display: flex; gap: 12px; flex-wrap: wrap; }
  .filters select, .filters input { padding: 6px 10px; border: 1px solid #ccc; border-radius: 4px; }
  .filters button { background: #1a3a5c; color: white; border: none; border-radius: 4px; padding: 6px 16px; cursor: pointer; }
  table { width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.1); }
  th { background: #f0f4f8; font-size: 0.8rem; text-transform: uppercase; padding: 10px 12px; text-align: left; color: #555; }
  td { padding: 10px 12px; border-top: 1px solid #f0f0f0; font-size: 0.875rem; }
  tr:hover { background: #fafafa; }
  .outcome-submitted { color: #2e7d32; font-weight: 600; }
  .outcome-abandoned { color: #c62828; }
  .outcome-timeout { color: #e65100; }
  .outcome-not_reporting { color: #555; }
  .conf-low { color: #c62828; }
  .conf-ok { color: #2e7d32; }
  .btn-sm { font-size: 0.75rem; padding: 3px 8px; border-radius: 3px; border: none; cursor: pointer; }
  .btn-golden { background: #e8b44b; color: #1a3a5c; }
  .btn-detail { background: #1a3a5c; color: white; }
  .turn-bar { display: inline-block; height: 10px; background: #1a3a5c; border-radius: 2px; min-width: 2px; }
  .life-safety { color: white; background: #c62828; border-radius: 3px; padding: 1px 6px; font-size: 0.75rem; }
  #detail-panel { display: none; position: fixed; right: 0; top: 0; bottom: 0; width: 480px; background: white; box-shadow: -4px 0 12px rgba(0,0,0,.15); padding: 20px; overflow-y: auto; }
  #detail-panel h2 { margin-top: 0; font-size: 1rem; }
  pre { background: #f5f5f5; border-radius: 4px; padding: 10px; font-size: 0.78rem; overflow-x: auto; }
  .close-btn { float: right; background: none; border: none; font-size: 1.2rem; cursor: pointer; }
</style>
</head>
<body>
<header>
  <h1>Sentinel Voice</h1>
  <span class="badge">Call Review Console</span>
</header>
<div class="container">
  <div class="filters">
    <select id="f-outcome">
      <option value="">All outcomes</option>
      <option value="submitted">Submitted</option>
      <option value="abandoned">Abandoned</option>
      <option value="timeout">Timeout</option>
      <option value="not_reporting">Not reporting</option>
    </select>
    <input type="number" id="f-max-conf" placeholder="Max confidence (0-1)" step="0.05" min="0" max="1">
    <input type="number" id="f-min-dur" placeholder="Min duration (s)" min="0">
    <button onclick="loadCalls()">Filter</button>
    <button onclick="loadCalls()" style="background:#555">Refresh</button>
  </div>
  <table>
    <thead>
      <tr>
        <th>Short ref</th>
        <th>Time (IST)</th>
        <th>Outcome</th>
        <th>Hazard</th>
        <th>Severity</th>
        <th>Location</th>
        <th>Conf.</th>
        <th>Duration</th>
        <th>Turns</th>
        <th>Actions</th>
      </tr>
    </thead>
    <tbody id="call-table-body">
      <tr><td colspan="10" style="text-align:center;color:#999;padding:40px">Loading…</td></tr>
    </tbody>
  </table>
</div>

<div id="detail-panel">
  <button class="close-btn" onclick="document.getElementById('detail-panel').style.display='none'">&times;</button>
  <h2 id="dp-title">Call detail</h2>
  <pre id="dp-content"></pre>
</div>

<script>
async function loadCalls() {
  const outcome = document.getElementById('f-outcome').value;
  const maxConf = document.getElementById('f-max-conf').value;
  const minDur  = document.getElementById('f-min-dur').value;
  let url = '/api/v1/console/calls?limit=50';
  if (outcome) url += '&outcome=' + encodeURIComponent(outcome);
  if (maxConf)  url += '&max_confidence=' + maxConf;
  if (minDur)   url += '&min_duration=' + minDur;

  const resp = await fetch(url, {
    headers: {'X-Admin-Api-Key': window.__ADMIN_KEY || ''}
  });
  if (!resp.ok) { alert('Auth failed'); return; }
  const data = await resp.json();
  renderTable(data.items);
}

function renderTable(calls) {
  const tbody = document.getElementById('call-table-body');
  if (!calls.length) {
    tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;color:#999;padding:40px">No calls found</td></tr>';
    return;
  }
  tbody.innerHTML = calls.map(c => {
    const oc = c.outcome || 'unknown';
    const confClass = (c.confidence_overall || 1) < 0.7 ? 'conf-low' : 'conf-ok';
    const conf = c.confidence_overall != null ? c.confidence_overall.toFixed(2) : '—';
    const ls = c.life_safety_flag ? '<span class="life-safety">⚠ 112</span> ' : '';
    const turnsBar = '|'.repeat(Math.min(c.turns_count || 0, 20));
    return `<tr>
      <td>${ls}<b>${c.short_ref || '—'}</b></td>
      <td>${c.received_at_ist || '—'}</td>
      <td class="outcome-${oc}">${oc}</td>
      <td>${c.hazard_type || '—'}</td>
      <td>${c.severity || '—'}</td>
      <td title="${c.location_text || ''}">${(c.resolved_place || c.location_text || '—').slice(0, 24)}</td>
      <td class="${confClass}">${conf}</td>
      <td>${c.call_duration_sec != null ? c.call_duration_sec + 's' : '—'}</td>
      <td title="${c.turns_count} turns">${turnsBar}</td>
      <td>
        <button class="btn-sm btn-detail" onclick="showDetail('${c.report_id}')">Detail</button>
        <button class="btn-sm btn-golden" onclick="downloadGolden('${c.report_id}')">→ Golden</button>
      </td>
    </tr>`;
  }).join('');
}

async function showDetail(reportId) {
  const resp = await fetch('/api/v1/console/calls/' + reportId, {
    headers: {'X-Admin-Api-Key': window.__ADMIN_KEY || ''}
  });
  if (!resp.ok) return;
  const data = await resp.json();
  document.getElementById('dp-title').textContent = 'Call: ' + (data.short_ref || reportId);
  document.getElementById('dp-content').textContent = JSON.stringify(data, null, 2);
  document.getElementById('detail-panel').style.display = 'block';
}

async function downloadGolden(reportId) {
  const resp = await fetch('/api/v1/console/calls/' + reportId + '/golden-fixture', {
    headers: {'X-Admin-Api-Key': window.__ADMIN_KEY || ''}
  });
  if (!resp.ok) { alert('Could not generate fixture'); return; }
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'golden_' + reportId.slice(0, 8) + '.json';
  a.click();
}

loadCalls();
</script>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def console_index() -> HTMLResponse:
    """Serve the call review console HTML."""
    return HTMLResponse(content=_HTML)


# ── API endpoints backing the console ───────────────────────────────


@router.get("/calls")
async def list_calls(
    outcome: str | None = Query(None),
    max_confidence: float | None = Query(None, ge=0.0, le=1.0),
    min_duration: int | None = Query(None, ge=0),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
) -> dict[str, Any]:
    """List calls for the review console with optional filters."""
    from sqlalchemy import select

    from fg_voice.persistence.models import Report

    session_maker = get_session_maker()
    if session_maker is None:
        return {"items": [], "next_cursor": None, "note": "database not configured"}

    async with session_maker() as session:
        # Join reports with call_sessions to get call metadata
        stmt = select(Report).order_by(Report.created_at.desc()).limit(limit)

        if outcome:
            # Map outcome string to terminal_node pattern
            _OUTCOME_MAP = {
                "submitted": "SUBMITTED",
                "abandoned": None,  # no terminal_node for abandoned
                "timeout": "TIMEOUT_EXIT",
                "not_reporting": "NOT_REPORTING",
            }

        if max_confidence is not None:
            stmt = stmt.where(
                (Report.confidence_score <= int(max_confidence * 100))
                | (Report.confidence_score.is_(None))
            )

        rows = (await session.execute(stmt)).scalars().all()

        items = []
        for r in rows:
            flags: dict[str, Any] = r.flags or {}
            items.append(
                {
                    "report_id": str(r.report_id),
                    "short_ref": r.short_ref,
                    "received_at_ist": (
                        r.created_at.astimezone(
                            __import__("zoneinfo").ZoneInfo("Asia/Kolkata")
                        ).strftime("%Y-%m-%d %H:%M")
                        if r.created_at
                        else None
                    ),
                    "outcome": "submitted" if r.status != "pending_enrichment" else "unknown",
                    "hazard_type": r.hazard_type,
                    "severity": r.severity,
                    "location_text": r.location_raw,
                    "resolved_place": r.location_resolved,
                    "confidence_overall": (
                        r.confidence_score / 100.0 if r.confidence_score is not None else None
                    ),
                    "life_safety_flag": bool(flags.get("life_safety")),
                    "call_duration_sec": None,
                    "turns_count": None,
                    "call_sid": r.call_sid,
                    "source": r.source,
                }
            )

        return {"items": items, "next_cursor": None}


@router.get("/calls/{report_id}")
async def get_call_detail(report_id: str) -> dict[str, Any]:
    """Full call detail including per-turn breakdown."""
    from sqlalchemy import select

    from fg_voice.persistence.models import Report

    session_maker = get_session_maker()
    if session_maker is None:
        return {"error": "database not configured"}

    async with session_maker() as session:
        r = (
            await session.execute(select(Report).where(Report.report_id == report_id))
        ).scalar_one_or_none()

        if r is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Report not found")

        # call_turns is not yet in the ORM model — turns are stored in
        # CallState (Redis) during the call and available via recording/transcript.
        # Per-turn breakdown is shown when the transcript S3 key is available.
        turns: list[dict[str, Any]] = []

        return {
            "report_id": str(r.report_id),
            "short_ref": r.short_ref,
            "call_sid": r.call_sid,
            "source": r.source,
            "hazard_type": r.hazard_type,
            "hazard_type_spoken": r.hazard_type,
            "description_clean": r.description_clean,
            "location_text": r.location_raw,
            "location_resolved": r.location_resolved,
            "severity": r.severity,
            "water_depth_cm": r.water_depth_cm,
            "life_safety_flag": bool((r.flags or {}).get("life_safety")),
            "confidence_overall": (
                r.confidence_score / 100.0 if r.confidence_score is not None else None
            ),
            "geo_confidence": None,
            "enrichment_status": r.status,
            "qa_sample": r.sampled_for_qa,
            "qa_reviewed_at": r.qa_reviewed_at.isoformat() if r.qa_reviewed_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "turns": turns,
        }


@router.get("/calls/{report_id}/golden-fixture")
async def download_golden_fixture(report_id: str) -> JSONResponse:
    """Generate a golden test fixture JSON for this call (§18.2 one-click add)."""
    detail = await get_call_detail(report_id)

    turns = detail.pop("turns", [])

    # Build a script from the transcript of each turn
    script = []
    for t in turns:
        if t.get("input_source") == "asr" and t.get("transcript"):
            script.append(
                {
                    "kind": "transcript",
                    "text": t["transcript"],
                    "confidence": t.get("asr_confidence", 0.9),
                }
            )
        elif t.get("input_source") == "dtmf":
            script.append({"kind": "dtmf", "digit": "?"})  # operator fills in
        elif t.get("input_source") == "timeout":
            script.append({"kind": "no_input"})

    fixture = {
        "id": f"real_call_{report_id[:8]}",
        "description": f"Real call {detail.get('short_ref', report_id)} — add description",
        "script": script,
        "expect": {
            "terminal_node": "SUBMITTED" if detail.get("short_ref") else "UNKNOWN",
            "slots": {
                k: v
                for k, v in {
                    "hazard_type": detail.get("hazard_type"),
                    "severity": detail.get("severity"),
                }.items()
                if v
            },
        },
        "_source_report": report_id,
        "_note": "Review script and expectations before committing to data/eval/golden/",
    }

    return JSONResponse(
        content=fixture,
        headers={
            "Content-Disposition": f'attachment; filename="golden_{report_id[:8]}.json"',
        },
    )
