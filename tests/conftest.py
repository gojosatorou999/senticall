"""Shared pytest fixtures for fg_voice.

The default fixture set is intentionally small — new fixtures land in
tests/{layer}/conftest.py when they are actually needed."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    """Every test starts with a fresh Settings instance so env-var edits
    inside one test don't leak into the next."""
    from fg_voice.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def dev_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Minimal environment for tests that need Settings to load."""
    monkeypatch.setenv("FG_ENV", "dev")
    monkeypatch.setenv("FG_AGENT_VERSION", "test")
    monkeypatch.setenv("CALLER_HASH_PEPPER", "test-pepper")


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip all FG_/TWILIO_/DEEPGRAM_ vars so validators see defaults."""
    for k in list(os.environ):
        if k.startswith(("FG_", "TWILIO_", "DEEPGRAM_", "TTS_", "LLM_", "AWS_", "STT_")):
            monkeypatch.delenv(k, raising=False)
