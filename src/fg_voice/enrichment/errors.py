"""Error taxonomy for the P6 enrichment DAG.

Two categories so the relay knows how to react:

- `TransientEnrichmentError` — the task should be retried by the relay
  (network blip, LLM 5xx, external geocoder throttle). Raised so the
  relay's normal retry-count bump kicks in.
- `PermanentEnrichmentError` — the task will never succeed no matter
  how many retries (malformed data, hard schema mismatch). The relay
  still bumps retry_count and eventually dead-letters; a permanent
  error just carries a clearer message.

Both derive from the base so callers can catch either category.
`EnrichmentError` is deliberately not the same class as the relay's
`DispatchError` — the enrichment layer owns its own taxonomy, and the
dispatcher (`enrichment/dispatcher.py`) is the seam that translates
between the two."""

from __future__ import annotations


class EnrichmentError(Exception):
    """Base for anything raised inside the enrichment DAG."""


class TransientEnrichmentError(EnrichmentError):
    """Retriable — the relay will re-attempt on the next poll."""


class PermanentEnrichmentError(EnrichmentError):
    """Non-retriable — data will never make it through. Logged and
    counted; the row still cycles through retries until dead-lettered
    so the retry-count → DLQ ladder stays uniform across dispatcher
    types."""


__all__ = ["EnrichmentError", "PermanentEnrichmentError", "TransientEnrichmentError"]
