"""Caller-hash: never reversible, always the same for the same input."""

from __future__ import annotations

from fg_voice.utils.hashing import hash_msisdn


def test_deterministic() -> None:
    assert hash_msisdn("+919876543210", "pepper") == hash_msisdn("+919876543210", "pepper")


def test_pepper_matters() -> None:
    assert hash_msisdn("+919876543210", "p1") != hash_msisdn("+919876543210", "p2")


def test_msisdn_matters() -> None:
    assert hash_msisdn("+919876543210", "p") != hash_msisdn("+919999999999", "p")


def test_empty_maps_to_sentinel() -> None:
    assert hash_msisdn("", "p") == "<none>"


def test_never_contains_raw_number() -> None:
    h = hash_msisdn("+919876543210", "pepper")
    assert "9876543210" not in h
    assert len(h) == 64  # SHA-256 hex
