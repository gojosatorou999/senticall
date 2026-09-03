"""Config must fail fast — a bad config is caught at boot, never at runtime
with a caller on the line. These tests pin that behaviour."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fg_voice.config import Environment, Settings, get_settings


def test_defaults_load_in_dev(dev_env: None) -> None:
    s = get_settings()
    assert s.fg_env == Environment.DEV
    assert s.fg_region == "ap-south-1"
    assert s.stt_model == "flux-general-en"
    assert s.max_call_duration_sec == 300


def test_thresholds_are_bounded() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, stt_eot_threshold=1.5)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, geo_accept_threshold=-0.1)  # type: ignore[call-arg]


def test_max_call_duration_bounded() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_call_duration_sec=10)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_call_duration_sec=3600)  # type: ignore[call-arg]


def test_production_rejects_dev_pepper() -> None:
    with pytest.raises(ValidationError, match="dev placeholder"):
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            fg_env=Environment.PRODUCTION,
            caller_hash_pepper="dev-only-anything",
        )


def test_production_requires_secrets() -> None:
    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        fg_env=Environment.PRODUCTION,
        caller_hash_pepper="real-secret-from-secrets-manager",
    )
    with pytest.raises(RuntimeError, match="Production boot missing"):
        s.require_production_secrets()


def test_production_secrets_satisfied() -> None:
    s = Settings(  # type: ignore[call-arg]
        _env_file=None,
        fg_env=Environment.PRODUCTION,
        caller_hash_pepper="real-secret",
        twilio_account_sid="AC123",
        twilio_auth_token="tok",
        twilio_phone_number="+911800XXXXXXX",
        admin_api_key="real-admin-key-rotated",
        deepgram_api_key="dg",
        tts_api_key="tts",
        tts_voice_id="voice",
        llm_model_id="model",
        alert_sns_topic_arn="arn:aws:sns:ap-south-1:123:alerts",
    )
    # Should not raise.
    s.require_production_secrets()


def test_settings_cached(dev_env: None) -> None:
    assert get_settings() is get_settings()
