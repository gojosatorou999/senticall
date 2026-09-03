"""PII scrubber — regex coverage + sink integration."""

from __future__ import annotations

import pytest

from fg_voice.utils.redact import (
    AADHAAR_SENTINEL,
    EMAIL_SENTINEL,
    PHONE_SENTINEL,
    redact_pii,
)

# ─── Phone patterns ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        "+91 98765 43210",
        "+91-98765-43210",
        "+919876543210",
        "919876543210",
        "09876543210",
        "9876543210",
        "8123456789",
        "7000000000",
        "6111111111",
    ],
)
def test_redact_phone_variants(raw):
    text = f"call back on {raw} thanks"
    out = redact_pii(text)
    assert PHONE_SENTINEL in out
    assert raw not in out


def test_short_numbers_are_not_phones():
    """5-digit order id, 6-digit OTP, etc. must not be scrubbed."""
    text = "order id 12345 was cancelled at 09:15"
    out = redact_pii(text)
    assert "12345" in out


def test_landline_or_us_number_not_matched():
    """Regex is Indian-mobile-shaped (leading 6-9); a US-shaped number
    should not match."""
    text = "call the office at 415-555-0142"
    out = redact_pii(text)
    assert "415-555-0142" in out or "415" in out


# ─── Email patterns ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        "alice@example.com",
        "alice.the-second@sub.example.co.in",
        "user+tag@sentinel.in",
    ],
)
def test_redact_email(raw):
    text = f"contact {raw} for followup"
    out = redact_pii(text)
    assert EMAIL_SENTINEL in out
    assert raw not in out


def test_bare_at_symbol_not_matched():
    """`@` used as a word (twitter handle, price @) shouldn't fire."""
    text = "the road is closed @ Main St, price @ 500"
    out = redact_pii(text)
    assert EMAIL_SENTINEL not in out


# ─── Aadhaar patterns ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        "1234 5678 9012",
        "1234-5678-9012",
    ],
)
def test_redact_aadhaar_when_separated(raw):
    text = f"my number is {raw} please note"
    out = redact_pii(text)
    assert AADHAAR_SENTINEL in out
    assert raw not in out


def test_unseparated_12_digits_left_alone():
    """Deliberate design choice — an unseparated 12-digit run is
    indistinguishable from an order id / transaction ref, and a
    false positive there would over-scrub ordinary text. Real Aadhaar
    is always rendered 4-4-4 separated in caller-facing contexts."""
    text = "reference 123456789012 was cancelled"
    out = redact_pii(text)
    assert "123456789012" in out


def test_11_digit_or_13_digit_never_matched():
    """Guard: adjacent lengths shouldn't match — otherwise every long
    order id gets scrubbed."""
    text = "reference 12345678901 and 1234567890123 stayed as-is"
    out = redact_pii(text)
    assert "12345678901" in out
    assert "1234567890123" in out


# ─── Ordering / edge cases ───────────────────────────────────────────


def test_none_or_empty_returns_empty_string():
    assert redact_pii(None) == ""
    assert redact_pii("") == ""


def test_multiple_pii_in_one_text():
    text = "call +91 98765 43210 or email me at alice@example.com about aadhaar 1234 5678 9012"
    out = redact_pii(text)
    assert PHONE_SENTINEL in out
    assert EMAIL_SENTINEL in out
    assert AADHAAR_SENTINEL in out
    assert "98765" not in out
    assert "alice" not in out
    assert "1234 5678" not in out


def test_ordinary_hazard_description_untouched():
    """No false positives on typical caller utterances."""
    text = "waves crashing onto the road at RK Beach, water is knee deep now"
    assert redact_pii(text) == text


def test_deterministic():
    """Same input → same output. Redaction is a pure function."""
    text = "call +91 98765 43210 for details"
    assert redact_pii(text) == redact_pii(text)


# ─── SqlReportSink integration ───────────────────────────────────────


@pytest.mark.asyncio
async def test_sink_stores_raw_and_clean_separately(tmp_path):
    """Report.description stays raw; Report.description_clean has PII
    replaced. Outbox payload carries both so downstream consumers can
    pick the right one."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine

    from fg_voice.conversation.sql_report_sink import SqlReportSink
    from fg_voice.conversation.state import CallState, Slot, SlotValue
    from fg_voice.persistence.db import (
        Base,
        get_session_maker,
        override_engine,
        reset_engine,
    )
    from fg_voice.persistence.models import OutboxEntry, Report
    from fg_voice.persistence.outbox import OutboxEventType

    eng = create_async_engine("sqlite+aiosqlite:///:memory:", pool_pre_ping=False)
    override_engine(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        state = CallState(call_sid="CA_REDACT", caller_hash="hh")
        raw_desc = "call me on +91 98765 43210, email alice@example.com"
        state.set_slot(Slot.DESCRIPTION, SlotValue(value=raw_desc, confidence=0.6, source="asr"))
        state.set_slot(Slot.HAZARD_TYPE, SlotValue(value="storm", confidence=0.9, source="asr"))
        state.set_slot(Slot.SEVERITY, SlotValue(value="moderate", confidence=0.9, source="asr"))

        await SqlReportSink().write(state)

        async with get_session_maker()() as session:
            row = await session.get(Report, state.report_id)
            assert row is not None
            # Raw description stays raw — admin can review the original.
            assert row.description == raw_desc
            assert "98765" in row.description
            # Clean twin has the PII scrubbed.
            assert row.description_clean is not None
            assert "98765" not in row.description_clean
            assert "alice" not in row.description_clean

            outbox = await session.scalar(
                select(OutboxEntry).where(
                    OutboxEntry.event_type == OutboxEventType.REPORT_SUBMITTED
                )
            )
            assert outbox is not None
            assert outbox.payload["description"] == raw_desc
            assert outbox.payload["description_clean"] == row.description_clean
    finally:
        await eng.dispose()
        reset_engine()


@pytest.mark.asyncio
async def test_sink_null_description_leaves_both_null(tmp_path):
    """No description slot filled → both raw + clean stay NULL, not
    empty string (empty string in DB is a real footgun)."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from fg_voice.conversation.sql_report_sink import SqlReportSink
    from fg_voice.conversation.state import CallState, Slot, SlotValue
    from fg_voice.persistence.db import (
        Base,
        get_session_maker,
        override_engine,
        reset_engine,
    )
    from fg_voice.persistence.models import Report

    eng = create_async_engine("sqlite+aiosqlite:///:memory:", pool_pre_ping=False)
    override_engine(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        state = CallState(call_sid="CA_NULL_DESC", caller_hash="h")
        state.set_slot(Slot.HAZARD_TYPE, SlotValue(value="storm", confidence=0.9, source="asr"))
        await SqlReportSink().write(state)

        async with get_session_maker()() as session:
            row = await session.get(Report, state.report_id)
            assert row is not None
            assert row.description is None
            assert row.description_clean is None
    finally:
        await eng.dispose()
        reset_engine()


# ─── CSV column parity ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_csv_projector_populates_description_clean(tmp_path):
    """The CSV row's `description_clean` column carries the scrubbed
    text, not empty. Projector path + export path stay identical."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine

    from fg_voice.conversation.sql_report_sink import SqlReportSink
    from fg_voice.conversation.state import CallState, Slot, SlotValue
    from fg_voice.persistence.csv_projector import (
        COLUMNS,
        CsvProjectorDispatcher,
    )
    from fg_voice.persistence.db import (
        Base,
        get_session_maker,
        override_engine,
        reset_engine,
    )
    from fg_voice.persistence.models import OutboxEntry
    from fg_voice.persistence.outbox import OutboxEventType

    eng = create_async_engine("sqlite+aiosqlite:///:memory:", pool_pre_ping=False)
    override_engine(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        state = CallState(call_sid="CA_CSV_CLEAN", caller_hash="h")
        state.set_slot(
            Slot.DESCRIPTION,
            SlotValue(value="ring me on 9876543210 asap", confidence=0.6, source="asr"),
        )
        state.set_slot(Slot.HAZARD_TYPE, SlotValue(value="storm", confidence=0.9, source="asr"))
        await SqlReportSink().write(state)

        async with get_session_maker()() as session:
            entry = await session.scalar(
                select(OutboxEntry).where(
                    OutboxEntry.event_type == OutboxEventType.REPORT_SUBMITTED
                )
            )
        assert entry is not None

        csv_path = tmp_path / "with_clean.csv"
        await CsvProjectorDispatcher(path=csv_path).dispatch(entry)

        text = csv_path.read_text(encoding="utf-8-sig")
        header, first_row = text.strip().split("\n")[0], text.strip().split("\n")[1]
        cols = header.split(",")
        vals = first_row.split(",")
        row = dict(zip(cols, vals, strict=True))
        assert tuple(cols) == COLUMNS
        assert row["description_raw"] == "ring me on 9876543210 asap"
        assert row["description_clean"] == "ring me on [phone] asap"
        assert "9876543210" not in row["description_clean"]
    finally:
        await eng.dispose()
        reset_engine()


# ─── Alert payload ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_alert_payload_uses_clean_not_raw(tmp_path):
    """Slack/PagerDuty bodies must NOT carry raw PII. The alert
    dispatcher builds its payload from `description_clean`."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from fg_voice.conversation.sql_report_sink import SqlReportSink
    from fg_voice.conversation.state import CallState, Slot, SlotValue
    from fg_voice.persistence.alerts import AlertDispatcher
    from fg_voice.persistence.db import (
        Base,
        override_engine,
        reset_engine,
    )
    from fg_voice.persistence.relay import OutboxRelay

    # Small local recording backend so we don't pull in test_alerts.py's.
    class _Recorder:
        def __init__(self):
            self.sent = []

        async def send(self, alert):
            self.sent.append(alert)

    eng = create_async_engine("sqlite+aiosqlite:///:memory:", pool_pre_ping=False)
    override_engine(eng)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        state = CallState(call_sid="CA_ALRT_PII", caller_hash="h")
        state.set_slot(
            Slot.DESCRIPTION,
            SlotValue(
                value="my son is trapped, call me on +91 98765 43210",
                confidence=0.6,
                source="asr",
            ),
        )
        state.set_slot(Slot.SEVERITY, SlotValue(value="extreme", confidence=0.9, source="asr"))
        state.add_flag("life_safety")
        await SqlReportSink().write(state)

        rec = _Recorder()
        relay = OutboxRelay(dispatcher=AlertDispatcher(backends=[rec]))
        await relay.drain_once()

        assert len(rec.sent) == 1
        alert = rec.sent[0]
        # description_clean is present and scrubbed…
        assert alert["description_clean"] is not None
        assert "98765" not in alert["description_clean"]
        # …and the raw description was NOT copied in.
        assert "description" not in alert or "98765" not in str(alert.get("description", ""))
    finally:
        await eng.dispose()
        reset_engine()
