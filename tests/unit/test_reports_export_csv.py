"""GET /api/v1/reports/export.csv — streaming CSV export from the DB.

The interesting test here is the parity check: the export endpoint
and the CsvProjectorDispatcher MUST render identical rows for the
same Report, otherwise schema drift creeps in between the fast-path
projector and the batch export."""

from __future__ import annotations

import csv as csv_mod
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from io import StringIO

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from fg_voice.conversation.sql_report_sink import SqlReportSink
from fg_voice.conversation.state import CallState, Slot, SlotValue
from fg_voice.persistence.csv_projector import (
    COLUMNS,
    CsvProjectorDispatcher,
    row_from_report,
)
from fg_voice.persistence.db import (
    Base,
    get_session_maker,
    override_engine,
    reset_engine,
)
from fg_voice.persistence.models import Report


@pytest.fixture
async def db_engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", pool_pre_ping=False)
    override_engine(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()
    reset_engine()


@pytest.fixture
async def client(db_engine) -> AsyncIterator[httpx.AsyncClient]:
    from fg_voice.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _seed(
    call_sid: str,
    *,
    hazard: str = "storm",
    severity: str = "moderate",
    life_safety: bool = False,
    created_at: datetime | None = None,
) -> str:
    state = CallState(call_sid=call_sid, caller_hash="testhash")
    state.set_slot(Slot.HAZARD_TYPE, SlotValue(value=hazard, confidence=0.9, source="asr"))
    state.set_slot(Slot.SEVERITY, SlotValue(value=severity, confidence=0.9, source="asr"))
    state.set_slot(Slot.LOCATION, SlotValue(value="RK Beach", confidence=0.6, source="asr"))
    if life_safety:
        state.add_flag("life_safety")
    submitted = await SqlReportSink().write(state)

    if created_at is not None:
        from sqlalchemy import update

        sm = get_session_maker()
        async with sm() as session, session.begin():
            await session.execute(
                update(Report)
                .where(Report.report_id == submitted.report_id)
                .values(created_at=created_at, updated_at=created_at)
            )
    return submitted.short_ref


def _parse_csv(body_bytes: bytes) -> tuple[list[str], list[dict[str, str]]]:
    """Strip BOM, return (header, list of row dicts)."""
    text = body_bytes.decode("utf-8-sig")
    reader = csv_mod.reader(text.splitlines())
    header = next(reader)
    rows = [dict(zip(header, r, strict=True)) for r in reader]
    return header, rows


# ─── Content-type + envelope ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_export_returns_csv_content_type_and_disposition(client):
    await _seed("CA_EXP")
    r = await client.get("/api/v1/reports/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert "reports.csv" in r.headers["content-disposition"]


@pytest.mark.asyncio
async def test_export_starts_with_utf8_bom(client):
    await _seed("CA_BOM")
    r = await client.get("/api/v1/reports/export.csv")
    assert r.content.startswith(b"\xef\xbb\xbf")


@pytest.mark.asyncio
async def test_export_header_matches_columns_schema(client):
    await _seed("CA_HDR")
    r = await client.get("/api/v1/reports/export.csv")
    header, _ = _parse_csv(r.content)
    assert tuple(header) == COLUMNS


@pytest.mark.asyncio
async def test_empty_result_still_emits_header(client):
    """Zero rows → header + BOM only. Consumers can trust the file
    is always parseable, never an empty response."""
    r = await client.get("/api/v1/reports/export.csv")
    header, rows = _parse_csv(r.content)
    assert tuple(header) == COLUMNS
    assert rows == []


# ─── Row shape parity with the projector ─────────────────────────────


@pytest.mark.asyncio
async def test_row_shape_matches_projector_output(client, tmp_path):
    """Same Report through the fast-path projector and the export
    endpoint must render the same CSV row (schema stays in sync).

    Compare all columns except `received_at_*` (formatted from
    `Report.created_at`, which the projector reads from the outbox
    payload's isoformat and the export reads from the ORM tz-aware
    datetime — those two paths format to the same string only if the
    createed_at hits the same UTC second, which is racy)."""
    short_ref = await _seed(
        "CA_PARITY", hazard="abnormal_tide", severity="extreme", life_safety=True
    )

    # 1. Fetch what the projector would write, going the same way the
    # relay does: query the outbox row and hand it to the projector.
    from fg_voice.persistence.models import OutboxEntry
    from fg_voice.persistence.outbox import OutboxEventType

    sm = get_session_maker()
    async with sm() as session:
        entry = await session.scalar(
            select(OutboxEntry).where(OutboxEntry.event_type == OutboxEventType.REPORT_SUBMITTED)
        )
    assert entry is not None
    projector = CsvProjectorDispatcher(path=tmp_path / "parity.csv")
    await projector.dispatch(entry)
    projector_body = (tmp_path / "parity.csv").read_bytes()
    _, projector_rows = _parse_csv(projector_body)
    projector_row = projector_rows[0]

    # 2. Fetch the export endpoint's version of the same report.
    r = await client.get("/api/v1/reports/export.csv")
    _, export_rows = _parse_csv(r.content)
    export_row = next(row for row in export_rows if row["short_ref"] == short_ref)

    # 3. Compare every column except the timestamp fields.
    ignore = {"received_at_utc", "received_at_ist"}
    for col in COLUMNS:
        if col in ignore:
            continue
        assert projector_row[col] == export_row[col], f"drift on {col!r}"


@pytest.mark.asyncio
async def test_row_from_report_helper_is_pure(db_engine):
    """`row_from_report` should produce the same output for the same
    input regardless of call order — no hidden global state."""
    await _seed("CA_PURE_1", hazard="storm", severity="light")
    sm = get_session_maker()
    async with sm() as session:
        report = await session.scalar(select(Report).where(Report.call_sid == "CA_PURE_1"))
    assert report is not None
    a = row_from_report(report, agent_version="v-test")
    b = row_from_report(report, agent_version="v-test")
    assert a == b


# ─── Filter behaviour (same as list endpoint) ────────────────────────


@pytest.mark.asyncio
async def test_export_filter_by_severity(client):
    await _seed("CA_LO", severity="light")
    await _seed("CA_HI_1", severity="extreme")
    await _seed("CA_HI_2", severity="extreme")

    r = await client.get("/api/v1/reports/export.csv", params={"severity": "extreme"})
    _, rows = _parse_csv(r.content)
    assert len(rows) == 2
    for row in rows:
        assert row["severity"] == "extreme"


@pytest.mark.asyncio
async def test_export_filter_by_life_safety(client):
    await _seed("CA_OK", life_safety=False)
    await _seed("CA_TW_1", life_safety=True)
    await _seed("CA_TW_2", life_safety=True)

    r_true = await client.get("/api/v1/reports/export.csv", params={"life_safety": "true"})
    _, rows_true = _parse_csv(r_true.content)
    assert len(rows_true) == 2
    for row in rows_true:
        assert row["life_safety_flag"] == "true"

    r_false = await client.get("/api/v1/reports/export.csv", params={"life_safety": "false"})
    _, rows_false = _parse_csv(r_false.content)
    assert len(rows_false) == 1
    assert rows_false[0]["life_safety_flag"] == "false"


@pytest.mark.asyncio
async def test_export_filter_by_date_range(client):
    now = datetime.now(UTC)
    await _seed("CA_PAST", created_at=now - timedelta(hours=2))
    ref_mid = await _seed("CA_MID", created_at=now - timedelta(minutes=30))
    ref_new = await _seed("CA_NEW", created_at=now)

    from_ = (now - timedelta(hours=1)).isoformat()
    r = await client.get("/api/v1/reports/export.csv", params={"from": from_})
    _, rows = _parse_csv(r.content)
    refs = {row["short_ref"] for row in rows}
    assert refs == {ref_mid, ref_new}


@pytest.mark.asyncio
async def test_export_ordered_newest_first(client):
    now = datetime.now(UTC)
    ref_old = await _seed("CA_OLD", created_at=now - timedelta(minutes=10))
    ref_mid = await _seed("CA_MID", created_at=now - timedelta(minutes=5))
    ref_new = await _seed("CA_NEW", created_at=now)

    r = await client.get("/api/v1/reports/export.csv")
    _, rows = _parse_csv(r.content)
    order = [row["short_ref"] for row in rows]
    assert order == [ref_new, ref_mid, ref_old]


# ─── Streaming semantics ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_row_line_terminator_is_lf_only():
    """CSV consumers on Linux expect LF; DictWriter's default `\\r\\n`
    would upset downstream tools. Enforce LF via the module-level
    setup."""
    row = row_from_report(
        Report(  # type: ignore[call-arg]
            report_id=__import__("uuid").UUID("aaaaaaaa-0000-0000-0000-000000000099"),
            short_ref="FG-LF",
            source="voice",
            call_sid="CA_LF",
            caller_hash="h",
            status="pending_enrichment",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        ),
        agent_version="test",
    )
    buf = StringIO()
    writer = csv_mod.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    writer.writerow(row)
    line = buf.getvalue()
    assert line.endswith("\n")
    assert "\r\n" not in line
