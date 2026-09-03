"""SnsAlertBackend + _sns_subject packaging.

We don't hit real SNS — the AWS client is stubbed at the aioboto3
Session level. What we DO assert is the shape of the outgoing
`publish()` call: topic ARN, subject, JSON body, and the two
MessageAttributes (`trigger`, `short_ref`) that a paging Lambda
filters on.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fg_voice.persistence.alerts import SnsAlertBackend, _sns_subject

# ─── Subject packaging ──────────────────────────────────────────────


def test_subject_life_safety_wins_over_severity() -> None:
    alert = {
        "life_safety_flag": True,
        "severity": "extreme",
        "short_ref": "FG-ABCD",
        "hazard_type": "storm",
    }
    subj = _sns_subject(alert)
    assert "LIFE-SAFETY" in subj
    assert "SEVERITY-EXTREME" not in subj


def test_subject_falls_back_to_severity_extreme() -> None:
    alert = {"life_safety_flag": False, "short_ref": "FG-XXXX", "hazard_type": "abnormal_tide"}
    subj = _sns_subject(alert)
    assert "SEVERITY-EXTREME" in subj
    assert "FG-XXXX" in subj
    assert "abnormal_tide" in subj


def test_subject_capped_at_100_chars() -> None:
    alert = {
        "life_safety_flag": True,
        "short_ref": "FG-" + "X" * 200,
        "hazard_type": "a" * 200,
    }
    subj = _sns_subject(alert)
    assert len(subj) <= 100


def test_subject_handles_missing_fields() -> None:
    subj = _sns_subject({})
    assert "SEVERITY-EXTREME" in subj  # default trigger label
    assert "??" in subj  # unknown short_ref placeholder
    assert "unknown" in subj  # unknown hazard placeholder


# ─── Publish call shape ─────────────────────────────────────────────


class _FakeSnsClient:
    """Async context-manager returning `self`. Records publish calls."""

    def __init__(self) -> None:
        self.publish_calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> _FakeSnsClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def publish(self, **kwargs: Any) -> dict[str, str]:
        self.publish_calls.append(kwargs)
        return {"MessageId": "fake-msg-id"}


@pytest.mark.asyncio
async def test_send_publishes_expected_payload() -> None:
    fake_client = _FakeSnsClient()

    # aioboto3.Session().client(...) — patch both hops. The Session
    # constructor is sync so a plain MagicMock does; `client()` is
    # sync-returning-async-cm.
    fake_session = MagicMock()
    fake_session.client = MagicMock(return_value=fake_client)

    backend = SnsAlertBackend(
        topic_arn="arn:aws:sns:ap-south-1:123456789012:fg-voice-alerts",
        region_name="ap-south-1",
    )
    alert = {
        "trigger": "life_safety",
        "short_ref": "FG-9999",
        "severity": "extreme",
        "life_safety_flag": True,
        "hazard_type": "storm",
        "call_sid": "CA_X",
    }

    with patch("aioboto3.Session", return_value=fake_session):
        await backend.send(alert)

    assert len(fake_client.publish_calls) == 1
    call = fake_client.publish_calls[0]
    assert call["TopicArn"] == "arn:aws:sns:ap-south-1:123456789012:fg-voice-alerts"
    assert "FG-9999" in call["Subject"]
    body = json.loads(call["Message"])
    assert body["short_ref"] == "FG-9999"
    assert body["trigger"] == "life_safety"

    attrs = call["MessageAttributes"]
    assert attrs["trigger"]["StringValue"] == "life_safety"
    assert attrs["short_ref"]["StringValue"] == "FG-9999"


@pytest.mark.asyncio
async def test_send_propagates_client_error() -> None:
    """A publish failure MUST raise — AlertDispatcher then bumps the
    outbox retry_count."""

    class _RaisingClient(_FakeSnsClient):
        async def publish(self, **kwargs: Any) -> dict[str, str]:
            raise RuntimeError("boom")

    fake_session = MagicMock()
    fake_session.client = MagicMock(return_value=_RaisingClient())

    backend = SnsAlertBackend(topic_arn="arn:aws:sns:ap-south-1:x:t")
    with (
        patch("aioboto3.Session", return_value=fake_session),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await backend.send({"trigger": "severity_extreme", "short_ref": "FG-Y"})


# Keep the async helper referenced so pyright/ruff don't dead-strip it.
_ = AsyncMock
