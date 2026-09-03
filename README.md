# Sentinel Voice Agent

Real-time, interruptible, noise-robust English voice agent for telephone-based
coastal hazard reporting. Twilio → streaming perception → deterministic
conversation DAG → RAG-grounded slot resolution → durable report →
live CSV + `app.sentinel.in` feed.

The full design document is [`ai-calling-agent.md`](./ai-calling-agent.md).
The operating rules for anyone (human or agent) touching this repo are in
[`CLAUDE.md`](./CLAUDE.md). **Read both before writing a line of code.**

## Status 

| Phase | State |
|---|---|
| P0 Foundations | **in progress** |
| P1 Telephony spine | pending |
| P2 Conversation DAG | pending |
| P3 Perception hardening | pending |
| P4 RAG layer | pending |
| P5 Persistence & CSV | pending |
| P6 Enrichment DAG | pending |
| P7 AWS productionisation | pending |
| P8 Evaluation & hardening | pending |
| P9 Pilot | pending |

## Quick start

```
# 1. install (Python 3.12, uv, Docker required)
make install

# 2. boot the local stack (postgres+postgis+pgvector, redis, localstack)
make dev

# 3. run tests
make test

# 4. run the API locally
make api      # → http://localhost:8080/healthz
```

See `make help` for the full command list.

## Layout

Follows §6 of the design document exactly. The tree is not incidental — the
`import-linter` contracts, the deployment topology, and the "conversation
never imports from RAG SOP" invariant all assume this layout.

```
src/fg_voice/
  telephony/     # Twilio (and future Exotel) adapters
  audio/         # μ-law codec, front-end DSP, denoise, prerendered bank
  pipeline/      # Pipecat pipeline: STT / TTS / LLM / interrupt / backchannel
  conversation/  # THE deterministic graph + prompts.yaml
  rag/           # gazetteer, hazard taxonomy, SOP (offline only)
  extraction/    # Pydantic slot schemas + LLM extractor
  persistence/   # SQLAlchemy models, outbox relay, CSV projector
  enrichment/    # post-call Prefect flow
  api/           # webhook routes, /api/v1/reports, SSE
  obs/           # OpenTelemetry, structlog, metrics
```

## The invariants you cannot break

1. LLM never controls flow — the graph does.
2. All caller-facing text is in `conversation/prompts.yaml`.
3. Submission is durable (transactional outbox); the caller never hears "failed".
4. The agent does not give safety advice — only the `emergency_redirect` prompt.
5. Latency p95 ≤ 1200 ms is a blocking CI gate.
6. Raw phone numbers are never persisted.

Full rationale: `ai-calling-agent.md` §2.
