"""P6 enrichment tasks. Each task is a pure async callable that takes
an `EnrichmentContext` and mutates its `result` field. Tasks live in
their own modules so a real implementation (LLM extractor, external
geocoder, embedding dedupe) can land as a swap without touching the
orchestrator."""
