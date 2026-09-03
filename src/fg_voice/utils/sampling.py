"""Deterministic sampling primitives.

The QA queue's "sample ~5% of submitted reports" invariant needs to
survive Twilio's at-least-once webhook delivery — the same report_id
must land on the same sample decision every time the sink writes it,
or a retry could flip a report into or out of the queue.

`should_sample_for_qa(report_id, rate)` gives a stable per-id decision:
- Same (report_id, rate) → same boolean, always.
- Distribution across many report_ids matches `rate` closely (BLAKE2b
  is uniform over uuid4 inputs).

Why not `random.random() < rate` at write time: retries would reroll
and disagree with the first pass. Even seeding on report_id would work
but seeding + reading `random` state has global side-effects that
are painful in async code — a pure hash is simpler and identical
across runs.
"""

from __future__ import annotations

import hashlib
from typing import Final
from uuid import UUID

# Salt keeps sampling stable within one deploy without exposing a
# uniform hash over public report_ids (defence-in-depth against
# anyone reverse-engineering the sample from a leaked short_ref).
# Not a secret — the sampling decision isn't sensitive; the salt
# just decorrelates from other hash-based sampling in the codebase.
_QA_SALT: Final[bytes] = b"fg_voice.qa.sample.v1"

# Sample space size — bigger = finer-grained rate. 10_000 lets us
# express rates down to 0.01% cleanly.
_SAMPLE_SPACE: Final[int] = 10_000


def should_sample_for_qa(report_id: UUID, rate: float) -> bool:
    """Return True iff this report should be flagged for QA review.

    Distribution converges to `rate` in the limit; per-id decision is
    fully deterministic. `rate=0` disables sampling entirely (safe
    dev default); `rate=1.0` samples every report."""
    if rate <= 0.0:
        return False
    if rate >= 1.0:
        return True
    # BLAKE2b keyed with our salt is faster than sha256 and still
    # cryptographically uniform. Take a 16-bit slice → mod into the
    # sample space → compare against the integer cutoff for `rate`.
    digest = hashlib.blake2b(report_id.bytes, key=_QA_SALT, digest_size=4).digest()
    bucket = int.from_bytes(digest, "big") % _SAMPLE_SPACE
    cutoff = int(rate * _SAMPLE_SPACE)
    return bucket < cutoff


__all__ = ["should_sample_for_qa"]
