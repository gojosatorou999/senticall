"""CsvProjectorDispatcher — file layout + append semantics."""

from __future__ import annotations

import csv

import pytest

from fg_voice.persistence.csv_projector import (
    COLUMNS,
    SCHEMA_VERSION,
    CsvProjectorDispatcher,
)
from fg_voice.persistence.models import OutboxEntry


def _entry(**payload_overrides) -> OutboxEntry:
    payload = {
        "report_id": "aaaaaaaa-0000-0000-0000-000000000001",
        "short_ref": "FG-ABCD",
        "source": "voice",
        "call_sid": "CA_001",
        "caller_hash": "hashedhash",
        "hazard_type": "storm",
        "severity": "extreme",
        "water_depth_cm": 90,
        "description": "waves crashed onto the road",
        "location_raw": "RK Beach",
        "flags": ["life_safety"],
        "created_at": "2026-08-15T10:30:00+00:00",
        **payload_overrides,
    }
    entry = OutboxEntry(event_type="report.submitted", payload=payload)
    entry.id = 1
    return entry


def _read_csv(path):
    """Return (header, rows). Strips the UTF-8 BOM before parsing."""
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")  # 'sig' variant eats the BOM
    reader = csv.reader(text.splitlines())
    header = next(reader)
    return header, list(reader)


@pytest.mark.asyncio
async def test_first_write_creates_file_with_bom_and_header(tmp_path):
    dispatcher = CsvProjectorDispatcher(path=tmp_path / "reports.csv")
    await dispatcher.dispatch(_entry())

    csv_path = tmp_path / "reports.csv"
    raw = csv_path.read_bytes()
    # BOM present exactly once, at the very start.
    assert raw.startswith(b"\xef\xbb\xbf")

    header, rows = _read_csv(csv_path)
    assert tuple(header) == COLUMNS
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_subsequent_writes_append_without_re_writing_header(tmp_path):
    dispatcher = CsvProjectorDispatcher(path=tmp_path / "reports.csv")
    await dispatcher.dispatch(_entry(short_ref="FG-ONE"))
    await dispatcher.dispatch(_entry(short_ref="FG-TWO"))
    await dispatcher.dispatch(_entry(short_ref="FG-THR"))

    header, rows = _read_csv(tmp_path / "reports.csv")
    assert tuple(header) == COLUMNS
    assert len(rows) == 3
    short_refs = [r[header.index("short_ref")] for r in rows]
    assert short_refs == ["FG-ONE", "FG-TWO", "FG-THR"]


@pytest.mark.asyncio
async def test_row_values_match_payload_projection(tmp_path):
    dispatcher = CsvProjectorDispatcher(path=tmp_path / "reports.csv", agent_version="test-vX")
    await dispatcher.dispatch(_entry())

    header, rows = _read_csv(tmp_path / "reports.csv")
    row = dict(zip(header, rows[0], strict=True))

    assert row["short_ref"] == "FG-ABCD"
    assert row["source"] == "voice"
    assert row["caller_hash"] == "hashedhash"
    assert row["hazard_type"] == "storm"
    assert row["severity"] == "extreme"
    assert row["severity_score"] == "95"  # nominal mapping
    assert row["water_depth_cm"] == "90"
    assert row["description_raw"] == "waves crashed onto the road"
    assert row["location_text"] == "RK Beach"
    assert row["life_safety_flag"] == "true"
    assert row["call_sid"] == "CA_001"
    assert row["received_at_utc"] == "2026-08-15T10:30:00Z"
    assert row["received_at_ist"] == "2026-08-15 16:00:00"
    assert row["agent_version"] == "test-vX"
    assert row["schema_version"] == str(SCHEMA_VERSION)
    # Enrichment fields are present but empty (P4/P6 populate later).
    for field in ("resolved_place", "district", "latitude", "longitude", "dedupe_group_id"):
        assert row[field] == ""


@pytest.mark.asyncio
async def test_missing_payload_fields_become_empty_string_not_none(tmp_path):
    """A literal 'None' in the CSV is a well-known downstream footgun.
    Missing keys must render as empty."""
    dispatcher = CsvProjectorDispatcher(path=tmp_path / "reports.csv")
    entry = OutboxEntry(event_type="report.submitted", payload={"short_ref": "FG-QQQQ"})
    entry.id = 2
    await dispatcher.dispatch(entry)

    header, rows = _read_csv(tmp_path / "reports.csv")
    row = dict(zip(header, rows[0], strict=True))
    assert row["hazard_type"] == ""
    assert row["severity"] == ""
    assert row["water_depth_cm"] == ""
    assert row["description_raw"] == ""
    assert row["life_safety_flag"] == "false"


@pytest.mark.asyncio
async def test_description_newlines_escaped_per_spec(tmp_path):
    """§12.2: newlines inside description_* are escaped to `\\n` so
    the CSV row stays one line for Excel."""
    dispatcher = CsvProjectorDispatcher(path=tmp_path / "reports.csv")
    await dispatcher.dispatch(_entry(description="line one\nline two\r\nline three"))

    header, rows = _read_csv(tmp_path / "reports.csv")
    row = dict(zip(header, rows[0], strict=True))
    assert "\n" not in row["description_raw"]
    assert "\r" not in row["description_raw"]
    assert row["description_raw"] == "line one\\nline two\\nline three"


@pytest.mark.asyncio
async def test_non_report_events_are_skipped(tmp_path):
    """CSV projector only cares about `report.*` events; a moderation
    or alert event on the same outbox must not land in the CSV."""
    dispatcher = CsvProjectorDispatcher(path=tmp_path / "reports.csv")
    entry = OutboxEntry(event_type="alert.paged", payload={"note": "hi"})
    entry.id = 3
    await dispatcher.dispatch(entry)

    # File wasn't even created.
    assert not (tmp_path / "reports.csv").exists()


@pytest.mark.asyncio
async def test_flags_as_dict_still_read_correctly(tmp_path):
    """The SqlReportSink writes flags as a JSON list; the Report model
    stores them as `{flag: True}`. Both shapes must work — a Twilio
    retry that hits the JSON dict path shouldn't lose the flag."""
    dispatcher = CsvProjectorDispatcher(path=tmp_path / "reports.csv")
    await dispatcher.dispatch(_entry(flags={"life_safety": True, "low_confidence": True}))

    header, rows = _read_csv(tmp_path / "reports.csv")
    row = dict(zip(header, rows[0], strict=True))
    assert row["life_safety_flag"] == "true"


@pytest.mark.asyncio
async def test_end_to_end_relay_writes_csv(tmp_path):
    """SqlReportSink → OutboxRelay(ChainDispatcher([PubSub, Csv])) →
    row lands in the file. Proves the whole pipeline composes."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from fg_voice.conversation.sql_report_sink import SqlReportSink
    from fg_voice.conversation.state import CallState, Slot, SlotValue
    from fg_voice.persistence.broker import InProcessBroker
    from fg_voice.persistence.db import Base, override_engine, reset_engine
    from fg_voice.persistence.dispatchers import ChainDispatcher, PubSubDispatcher
    from fg_voice.persistence.relay import OutboxRelay

    eng = create_async_engine("sqlite+aiosqlite:///:memory:", pool_pre_ping=False)
    override_engine(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        # 1. Write a report via the sink → outbox row lands
        state = CallState(call_sid="CA_e2e_csv", caller_hash="hh")
        state.set_slot(
            Slot.HAZARD_TYPE, SlotValue(value="abnormal_tide", confidence=0.9, source="asr")
        )
        state.set_slot(Slot.SEVERITY, SlotValue(value="moderate", confidence=0.9, source="asr"))
        await SqlReportSink().write(state)

        # 2. Drain via the chain (pubsub + csv)
        broker = InProcessBroker()
        csv_path = tmp_path / "reports.csv"
        chain = ChainDispatcher(
            dispatchers=[
                PubSubDispatcher(broker=broker),
                CsvProjectorDispatcher(path=csv_path),
            ]
        )
        relay = OutboxRelay(dispatcher=chain)
        await relay.drain_once()

        # 3. CSV file exists with the report row
        header, rows = _read_csv(csv_path)
        assert len(rows) == 1
        row = dict(zip(header, rows[0], strict=True))
        assert row["hazard_type"] == "abnormal_tide"
        assert row["severity"] == "moderate"
        assert row["caller_hash"] == "hh"
    finally:
        await eng.dispose()
        reset_engine()
