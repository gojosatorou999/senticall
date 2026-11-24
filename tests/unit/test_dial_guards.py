"""/dial/call guards: number allow-list (fail closed) + sliding-window rate limits.

Twilio is faked — no network, no real calls.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fg_voice.api import routes_dial

ALLOWED = "+919030762494"
OTHER = "+919999999999"


class _Secret:
    def get_secret_value(self) -> str:
        return "tok"


def _settings(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
        twilio_account_sid="ACxxx",
        twilio_auth_token=_Secret(),
        twilio_phone_number="+17372212163",
        public_wss_base="wss://example.test",
        runner_mode=True,
        dial_allowed_numbers=ALLOWED,
        dial_limit_per_ip=5,
        dial_limit_per_number=3,
        dial_limit_global_per_hour=20,
    )
    base.update(over)
    return SimpleNamespace(**base)


class _FakeCalls:
    def __init__(self, sink: list[dict[str, Any]]) -> None:
        self._sink = sink

    def create(self, **kw: Any) -> SimpleNamespace:
        self._sink.append(kw)
        return SimpleNamespace(sid="CAfake", status="queued")


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    placed: list[dict[str, Any]] = []
    cfg = {"s": _settings()}
    monkeypatch.setattr(routes_dial, "get_settings", lambda: cfg["s"])
    monkeypatch.setattr(
        routes_dial, "Client", lambda *a, **k: SimpleNamespace(calls=_FakeCalls(placed))
    )
    routes_dial._reset_limits()
    app = FastAPI()
    app.include_router(routes_dial.router)
    yield TestClient(app), placed, cfg
    routes_dial._reset_limits()


def _post(client: TestClient, phone: str, ip: str = "1.1.1.1"):
    return client.post("/dial/call", data={"phone": phone}, headers={"X-Forwarded-For": ip})


def test_allowed_number_places_call(env) -> None:
    client, placed, _ = env
    r = _post(client, ALLOWED)
    assert r.status_code == 200
    assert len(placed) == 1 and placed[0]["to"] == ALLOWED


def test_formatting_is_normalised_before_allow_list_check(env) -> None:
    client, placed, _ = env
    assert _post(client, "+91 90307 62494").status_code == 200
    assert len(placed) == 1


def test_unlisted_number_refused_and_no_call(env) -> None:
    client, placed, _ = env
    r = _post(client, OTHER)
    assert r.status_code == 403
    assert placed == []
    assert OTHER not in r.text and ALLOWED not in r.text  # never leaks the list


def test_empty_allow_list_fails_closed(env) -> None:
    client, placed, cfg = env
    cfg["s"] = _settings(dial_allowed_numbers="")
    assert _post(client, ALLOWED).status_code == 403
    assert placed == []


def test_malformed_number_refused(env) -> None:
    client, placed, cfg = env
    cfg["s"] = _settings(dial_allowed_numbers="abc, ")
    assert _post(client, "abc").status_code == 403
    assert placed == []


def test_multiple_allowed_numbers_with_spaces(env) -> None:
    client, placed, cfg = env
    cfg["s"] = _settings(dial_allowed_numbers=f" {ALLOWED} , {OTHER} ")
    assert _post(client, OTHER).status_code == 200


def test_per_number_limit(env) -> None:
    client, placed, _ = env
    for ip in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
        assert _post(client, ALLOWED, ip).status_code == 200
    r = _post(client, ALLOWED, "4.4.4.4")
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 0
    assert len(placed) == 3


def test_per_ip_limit(env) -> None:
    client, placed, cfg = env
    cfg["s"] = _settings(
        dial_allowed_numbers=f"{ALLOWED},{OTHER}", dial_limit_per_ip=2, dial_limit_per_number=99
    )
    assert _post(client, ALLOWED).status_code == 200
    assert _post(client, OTHER).status_code == 200
    assert _post(client, ALLOWED).status_code == 429
    assert _post(client, ALLOWED, ip="9.9.9.9").status_code == 200  # other IP unaffected


def test_global_cap_stops_spoofed_ip_rotation(env) -> None:
    client, placed, cfg = env
    cfg["s"] = _settings(dial_limit_global_per_hour=2, dial_limit_per_number=99)
    assert _post(client, ALLOWED, "1.1.1.1").status_code == 200
    assert _post(client, ALLOWED, "2.2.2.2").status_code == 200
    assert _post(client, ALLOWED, "3.3.3.3").status_code == 429


def test_refused_numbers_do_not_consume_quota(env) -> None:
    client, placed, cfg = env
    cfg["s"] = _settings(dial_limit_per_ip=1)
    for _ in range(10):
        assert _post(client, OTHER).status_code == 403
    assert _post(client, ALLOWED).status_code == 200


def test_client_supplied_xff_prefix_is_ignored(env) -> None:
    """Only the LAST hop (added by our proxy) identifies the client."""
    client, placed, cfg = env
    cfg["s"] = _settings(dial_limit_per_ip=1, dial_limit_per_number=99)
    h1 = {"X-Forwarded-For": "6.6.6.6, 8.8.8.8"}
    h2 = {"X-Forwarded-For": "7.7.7.7, 8.8.8.8"}
    assert client.post("/dial/call", data={"phone": ALLOWED}, headers=h1).status_code == 200
    assert client.post("/dial/call", data={"phone": ALLOWED}, headers=h2).status_code == 429


def test_window_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    w = routes_dial._SlidingWindow()
    w.record("k", 0.0)
    assert w.retry_after("k", 1, 600.0, 10.0) > 0
    assert w.retry_after("k", 1, 600.0, 601.0) == 0
