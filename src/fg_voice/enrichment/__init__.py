"""P6 enrichment: async post-call DAG that runs off the outbox.

Public surface:
- `EnrichmentFlow` — the composed pipeline (assemble → deep_extract →
  geocode → dedupe → score → persist).
- `EnrichmentDispatcher` — plugs the flow into the outbox relay.

Boundaries (LLM extractor, geocoder, dedupe strategy) are Protocols
with No-Op defaults so the flow runs end-to-end without any external
dependency configured. Real implementations get injected at wire-time
in `main.py`.
"""

from fg_voice.enrichment.dispatcher import EnrichmentDispatcher
from fg_voice.enrichment.flow import EnrichmentFlow

__all__ = ["EnrichmentDispatcher", "EnrichmentFlow"]
