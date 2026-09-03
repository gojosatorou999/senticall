"""QA sampling — deterministic per-report_id decision.

Covers:
- Same report_id + same rate → same boolean (idempotent under retries)
- Different report_ids at rate=0.5 → mixed True/False (not degenerate)
- rate=0.0 → always False
- rate=1.0 → always True
- Distribution converges to `rate` over N samples (statistical smoke)
- Different rates give different decisions for the same id (not a
  boolean-only cache)
"""

from __future__ import annotations

from uuid import UUID, uuid4

from fg_voice.utils.sampling import should_sample_for_qa


def test_same_id_same_rate_is_stable():
    """The load-bearing property — Twilio retries reusing the same
    report_id must land on the same decision."""
    rid = uuid4()
    calls = [should_sample_for_qa(rid, 0.5) for _ in range(20)]
    assert len(set(calls)) == 1


def test_rate_zero_never_samples():
    for _ in range(50):
        assert should_sample_for_qa(uuid4(), 0.0) is False


def test_rate_one_always_samples():
    for _ in range(50):
        assert should_sample_for_qa(uuid4(), 1.0) is True


def test_rate_zero_beats_hash_signal():
    """Even for an id that hashes into the lowest bucket, rate=0
    short-circuits without evaluating the hash."""
    assert should_sample_for_qa(UUID(int=0), 0.0) is False


def test_distribution_converges_at_five_percent():
    """5% ± ~2% window over 2000 samples. Not a proof — a smoke test
    that the sampler isn't wildly off. Fixed seed via deterministic
    UUIDs so the CI never flakes."""
    n = 2000
    hits = sum(should_sample_for_qa(UUID(int=i), 0.05) for i in range(n))
    fraction = hits / n
    assert 0.03 < fraction < 0.07, f"observed {fraction:.4f}, expected ~0.05"


def test_distribution_converges_at_ten_percent():
    n = 2000
    hits = sum(should_sample_for_qa(UUID(int=i), 0.10) for i in range(n))
    fraction = hits / n
    assert 0.08 < fraction < 0.12, f"observed {fraction:.4f}, expected ~0.10"


def test_different_rates_disagree_for_same_id():
    """A report at the bottom of the 5% cutoff should still be sampled
    at 50%. Not just "rate ↑ → hits ↑" — the SAME id may flip based
    on rate alone, which is the intended per-rate cutoff behaviour."""
    # Find an id that samples at rate=0.5 but NOT at rate=0.01.
    found_flip = False
    for i in range(200):
        rid = UUID(int=i)
        if should_sample_for_qa(rid, 0.5) and not should_sample_for_qa(rid, 0.01):
            found_flip = True
            break
    assert found_flip
