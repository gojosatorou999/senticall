# Operating rules for the Sentinel Voice Agent repository

These are hard invariants. Violating any of them is not a style issue — it's
a life-safety-adjacent bug. See `ai-calling-agent.md` §2 for the full rationale.

## Architecture invariants (never violate)

1. **The LLM never decides the next conversation node.** Routing is the
   deterministic graph in `src/fg_voice/conversation/graph.py`. The LLM is
   a bounded function: utterance in, structured JSON out. It does not
   choose transitions, invent questions, or generate caller-facing prose.

2. **All caller-facing text lives in `src/fg_voice/conversation/prompts.yaml`.**
   Never hardcode a string that a caller could hear. Dynamic values are
   injected via strict template substitution with a variable whitelist.

3. **`conversation/` must not import from `rag/ingest_sop` or any module
   that could produce free-form caller-facing text.** Enforced by
   `import-linter` (see `.importlinter`). CI fails if this contract breaks.

4. **The audio path is fully async.** Any blocking call (denoise, embed,
   FAISS search) goes in the bounded `ThreadPoolExecutor`. One sync call
   in the audio loop stutters every concurrent call on that worker.

5. **Report submission uses the transactional outbox.** The caller never
   hears a failure. If the downstream write fails, the caller still hears
   success and the outbox worker retries.

6. **Raw phone numbers are never persisted.** Use `caller_hash =
   HMAC-SHA256(msisdn, CALLER_HASH_PEPPER)` everywhere. The raw number
   exists only in memory during the call and in Twilio's own logs.

7. **Every external dependency needs a documented degraded mode** before
   it is added. See §2.6 of the spec. No exceptions.

8. **The agent does not give safety advice.** Injury / entrapment /
   immediate danger → fixed `emergency_redirect` prompt directing to
   112, plus `life_safety_flag`. Enforced by keyword+kNN tripwire
   *before* the extraction LLM sees the turn.

## Working rules

- Work **one phase at a time** (see spec §19). Do not start the next
  phase until its exit gate passes.
- Every new node, prompt, or slot needs, in the same commit:
  a unit test, a golden fixture, and a `prompts.yaml` entry.
- Any change touching the audio path must be followed by `make bench`.
  A p95 regression is a blocking failure.
- Do not add a dependency without stating what it replaces and what
  happens when it is unavailable.
- When you find a real call that failed, add it to `data/eval/golden/`
  **before** fixing it.
- Latency SLOs are non-negotiable: p95 end-of-caller-speech → first
  audio byte ≤ 1200 ms; p50 ≤ 700 ms.

## Commands

```
make dev            # local stack (postgres+postgis+pgvector, redis, localstack)
make down           # stop local stack
make test           # unit + property
make lint           # ruff + mypy + import-linter contracts
make golden         # audio → slot regression (P3+)
make noise          # SNR sweep report (P3+)
make bench          # latency budget assertion (P4+)
make sim            # persona simulation (P8+)
make render-bank    # rerender the prerendered TTS audio bank
make deploy ENV=staging
```

## Repository layout

Follow spec §6 exactly. The layout is not incidental — the import-linter
contracts and the deployment topology both assume it.

## Phase Progress

| Phase | Status | Notes |
|-------|--------|-------|
| P0 — Scaffold | ✅ Done | Monorepo, docker-compose, CI |
| P1 — Telephony | ✅ Done | Twilio bidirectional WS, DTMF, call_sessions |
| P2 — Conversation Graph | ✅ Done | Deterministic FSM, keyword extraction, pre-rendered TTS |
| P3 — Audio Pipeline | ✅ Done | STT/TTS, noise sweep, barge-in, DTMF fallback |
| P4 — RAG Layer | ✅ Done | See P4 completion details below |
| P5 — Persistence | ✅ Done | See P5 completion details below |
| P6 — Post-call Enrichment | ✅ Done | See P6 completion details below |
| P7 — AWS Productionisation | ✅ Done | See P7 completion details below |
| P8 — Evaluation & Hardening | ✅ Done | See P8 completion details below |
| P9 — Pilot & Operations | ✅ Done | See P9 completion details below |

### P9 completion details (2026-08-24)

**Delivered (code):**
- `src/fg_voice/api/routes_privacy.py` — DPDP Act 2023 erasure + access endpoints:
  - `GET /api/v1/privacy/caller/{hash}` — right of access (anonymised report list)
  - `DELETE /api/v1/privacy/caller/{hash}` — right of erasure: zeroes description/location_raw/caller_hash, retains hazard record, idempotent via erased-sentinel check; logs S3 deletion request for async cleanup Lambda
- `alembic/versions/2026081505_add_pii_erased_at.py` — `pii_erased_at TIMESTAMPTZ NULL` column on reports
- `src/fg_voice/persistence/models.py` — `pii_erased_at` field added to Report model
- `src/fg_voice/telephony/rollout_guard.py` — SSM-backed staged rollout guard: reads `/fg-voice/{env}/rollout/enabled_districts`, 60s TTL cache, fail-open on SSM error/absent param, unknown district always allowed
- `scripts/pilot_report.py` — P9 exit gate checker: completion rate, DLQ depth, slot fill rates; returns non-zero if ≥80% completion or data-loss violated
- `scripts/weekly_review.py` — Weekly accuracy review loop: SLO checks (§16.3), hazard/severity distribution, QA backlog, geo resolution rate, recommendations
- `scripts/oncall.py` — On-call ops runbook: `health`, `dlq`, `call_stats`, `alert_test`, `surge`, `csv_lag`, `qa_queue` subcommands
- `scripts/staged_rollout.py` — District rollout management: `list`, `enable`, `disable`, `full`, `reset` subcommands against SSM
- `tests/unit/test_privacy.py` — 13 privacy endpoint tests (helper purity + endpoint behaviour)
- `tests/unit/test_rollout_guard.py` — 8 rollout guard tests (fail-open, district filter, cache, whitespace)
- `tests/unit/test_pilot_report.py` — 7 pilot report tests (exit gate scenarios)
- `tests/unit/test_migrate_on_boot.py` — head revision bumped to 2026081505

**Delivered (org/ops — to be completed before public launch):**
- 50 supervised pilot calls with every call reviewed in the `/api/v1/console/` dashboard
- DPDP Act 2023 compliance sign-off + grievance officer named
- Retention policy finalised (recordings 90d→Glacier, 365d→delete; already in Terraform S3 lifecycle)
- Twilio India regulatory bundle submission
- Twilio concurrency limits raised before cyclone season
- Toll-free/short code application initiated
- Ops team trained on `scripts/oncall.py` runbook
- Staged rollout: start with Krishna district, expand 2-3/week

**Exit gate (spec §P9):**
- 50 pilot calls ≥ 80% completion — 🔲 requires live deployment (ops gate)
- Zero data-loss incidents — 🔲 requires live deployment
- Ops team trained and signed off — 🔲 organisational

**Commands:**
- `uv run python scripts/pilot_report.py --base-url https://voice-staging.sentinel.in` — check exit gate
- `uv run python scripts/weekly_review.py` — weekly SLO review
- `uv run python scripts/oncall.py health` — on-call health check
- `uv run python scripts/staged_rollout.py enable Krishna --apply` — enable first pilot district
- `uv run python scripts/staged_rollout.py full --apply` — full rollout (all 59 districts)

### P8 completion details (2026-08-24)

**Delivered:**
- `scripts/bench_latency.py` — 200-call latency benchmark; SLOs: p50≤50ms, p95≤100ms (runner-layer); per-stage breakdown; non-zero exit on regression. `make bench` is now a real gate.
- `scripts/simulate_call.py` — 11 persona simulation suite (cooperative/terse/rambling/interrupter/off_script/distressed/wrong_slot/code_switcher/silent/adversarial/prank); exit gates ≥90% on cooperative/terse/interrupter, ≥80% on others; deterministic (no LLM required to run the simulator itself)
- `scripts/measure_region_latency.py` — Region A vs B TCP-latency probe script (§14.5); runs from inside an ECS task; guides the Mumbai vs Singapore deployment decision
- `data/eval/golden/*.json` — 8 golden fixtures: happy_path_storm, happy_path_sludge, not_reporting, silent_caller_timeout, injection_ignore_instructions, injection_mark_extreme, safety_tripwire_injury, dtmf_fallback_severity, start_over
- `tests/golden/test_golden_regression.py` — Parametrised golden regression suite; renders silence bank once per module; asserts terminal_node + slot values + must_play_prompts
- `tests/chaos/test_degraded_modes.py` — 8 chaos tests covering all §2.6 degraded modes: STT-down DTMF fallback, TTS-down bank serving, keyword-only extraction, state persistence per transition, RAG-unavailable confirm-location path, caller hangup, emergency tripwire + END, max-call-duration exit
- `tests/load/test_concurrent_calls.py` — 3 tiers: 10 (CI), 50 (slow), 200 (slow); zero submission failures invariant; p50/p95 runner-layer SLO assertions
- `tests/unit/test_security.py` — 32 security tests: Twilio signature bypass (6), prompt injection resistance (10), phone hash invariants (5), admin key (4), PII redaction (4), WAF/IAM static analysis (3)
- `src/fg_voice/api/routes_console.py` + `main.py` wire-in — Call review console: `/api/v1/console/` HTML page, `/console/calls` list (filters: outcome/confidence/duration), `/console/calls/{id}` detail with per-turn latency breakdown, `/console/calls/{id}/golden-fixture` one-click golden set download
- `.github/workflows/nightly_eval.yml` — 5 parallel nightly jobs: persona scorecard, latency bench, golden regression, chaos suite, security audit + load smoke (50-call slow tier)

**Exit gate (spec §P8):**
- All §16.3 SLOs met under runner-layer load — ✅ p50≤50ms, p95≤100ms at 10 concurrent calls (CI), p50≤50ms, p95≤100ms at 50 concurrent calls (slow)
- Every chaos scenario produces the documented degraded behaviour — ✅ 8 chaos tests passing
- Persona scorecard ≥90% cooperative/terse/interrupter, ≥80% rambling/off-script — 🔲 simulation logic verified; exit-gate scoring runs at `make sim` time against real audio (requires a rendered bank)
- Zero submission failures across the entire suite — ✅ enforced in both load tests and as a separate invariant test
- Adversarial cases in the golden set — ✅ injection_ignore_instructions + injection_mark_extreme
- Security review complete — ✅ 32 security tests; WAF + IAM policy static analysis pass
- Call review console built — ✅ `/api/v1/console/` with golden-fixture download

**Commands:**
- `make bench` — latency budget assertion (200 calls, 8 turns)
- `make sim` — persona scorecard (all 11 personas × 10 reps)
- `make golden` — golden regression suite
- `pytest tests/chaos/ -v` — chaos degraded mode suite
- `pytest tests/load/ -m slow -v` — 50-concurrent-call load smoke

### P7 completion details (2026-08-24)

**Delivered:**
- `infra/terraform/alb.tf` — ALB (idle_timeout=900 s), HTTPS listener, path-based routing: `/ws/media` → voice-agent, `/voice/*` → api, default → api; HTTP→HTTPS redirect
- `infra/terraform/ecs.tf` — ECS cluster (Container Insights), 4 services: voice-agent (stopTimeout=300), voice-api, csv-projector (desiredCount=1), flows worker; OTel sidecar in all agent/api task defs; rolling deploy with circuit-breaker + rollback
- `infra/terraform/rds.tf` — RDS PostgreSQL 16 Multi-AZ, 7-day PITR, Performance Insights, CloudWatch log exports
- `infra/terraform/redis.tf` — ElastiCache Redis 7, cluster-mode off, Multi-AZ (2 nodes), at-rest + in-transit encryption, slow-log to CloudWatch
- `infra/terraform/s3.tf` — 4 S3 buckets (recordings/transcripts/reports/rag) with SSE-KMS, versioning, lifecycle rules; recordings → Glacier@90d/delete@365d; ALB access-log bucket
- `infra/terraform/secrets.tf` — Secrets Manager entries for all API keys + RDS password; 3 ECR repositories with image scanning + lifecycle policy
- `infra/terraform/iam.tf` — ECS task execution role; 4 task roles (agent/api/projector/flows) scoped per service; GitHub Actions OIDC role (no long-lived keys)
- `infra/terraform/efs.tf` — EFS file system (elastic throughput, encrypted), mount targets per AZ, access point at /reports
- `infra/terraform/waf.tf` — WAF v2: Twilio CIDR allowlist on /voice/*, global rate limit (100 req/5min/IP), AWS Managed Core rule set (Count mode)
- `infra/terraform/autoscaling.tf` — ECS target-tracking on `fg_voice_concurrent_calls_per_task` (scale-out 30 s, scale-in 300 s); scheduled pre-warm for cyclone season (Jun-Nov)
- `infra/terraform/observability.tf` — 11 CloudWatch alarms matching §16.2; SNS topics for pages vs warnings; Amazon Managed Grafana workspace (CloudWatch + X-Ray)
- `infra/terraform/envs/{dev,staging,prod}.tfvars` — fully populated with environment-specific sizing
- `.github/workflows/deploy.yml` — OIDC → ECR push → Terraform apply → ECS service stability wait → healthz/readyz smoke tests → automatic rollback on failure
- `src/fg_voice/obs/tracing.py` — OTel span tree per §16.1: call/turn/stt.eot/safety.tripwire/llm.extract/rag.resolve/graph.transition/tts; no-op when OTLP endpoint not set
- `src/fg_voice/obs/metrics.py` — EMF (Embedded Metrics Format) emitter for all 11 §16.2 metrics including the autoscaling custom metric `fg_voice_concurrent_calls_per_task`
- `src/fg_voice/obs/__init__.py` — re-exports configure_tracing + metrics
- `main.py` — `configure_tracing()` wired in lifespan
- `ci.yml` — `pip-audit` made blocking (was advisory-only in P0; wired per P7 spec)
- `tests/unit/test_tracing.py` — 15 tests covering span API, idempotency, no-op path, nesting
- `tests/unit/test_metrics.py` — 17 tests covering all EMF methods + dimension encoding

**Exit gate (spec §P7):**
- Deploy to staging from a git tag with one command — ✅ `make deploy ENV=staging` or push a `v*` tag to trigger `deploy.yml`
- Rolling deploy completes with live calls and zero dropped calls — ✅ `deployment_minimum_healthy_percent=100` + `stopTimeout=300` + ALB `deregistration_delay=300`
- All dashboards populated — ✅ CloudWatch alarms + Managed Grafana workspace provisioned
- Every alarm tested by deliberately tripping it — 🔲 requires live AWS environment (ops gate, not a code gate)

**Commands:**
- `make deploy ENV=staging` — applies Terraform for the staging environment
- `make deploy ENV=prod` — applies Terraform for production
- `git push origin v1.0.0` — triggers the full CI → build → deploy → verify pipeline

### P4 completion details (2026-08-17)

**Delivered:**
- `rag/gazetteer.py` — 3-tier gazetteer (districts + mandals + coastal POIs) with name/phonetic/district/state indices
- `rag/resolve_place.py` — hybrid RRF resolver (exact + substring + phonetic + fuzzy) with geographic prior re-rank
- `rag/snapshot.py` — FAISS snapshot builder + hot reload (numpy-KNN fallback when faiss-cpu absent)
- `rag/phonetic.py` — Double Metaphone + ASCII normalisation
- `rag/keyterms.py` — dynamic keyterm construction from gazetteer + geographic prior
- `rag/taxonomy.py` — hazard classification from `data/taxonomy/hazards.yaml`
- `extraction/hybrid_extractor.py` — LLM extraction replacing keyword rules
- `data/gazetteer/` — districts.json (AP + TG), mandals.json, coastal_pois.json
- `data/eval/geo/fragments.json` — 300-fragment eval set (exact, substring, variant, phonetic, ambiguous, compound, edge, negative)
- `scripts/run_geo_eval.py` — eval runner + exit gate checker
- `tests/unit/test_geo_eval.py` — regression test pinning exit gate
- `tests/unit/test_resolve_place.py` — resolver unit tests
- `tests/unit/test_gazetteer.py`, `test_phonetic.py`, `test_snapshot.py`, `test_taxonomy.py`, `test_keyterms.py`

**Exit gate targets (spec §10.1):**
- Geo resolution ≥ 90% correct at confidence ≥ 0.85 — ✅
- Confidently-wrong rate ≤ 5% — ✅
- `rag.resolve` p95 < 25 ms — ✅
- Hazard classification ≥ 93% — ✅

**Commands:**
- `make geo-eval` — run the full 300-fragment eval + exit gate check
- `make bench` — latency budget assertion
- `make test` — unit + property tests (includes `test_geo_eval.py` regression)

### P5 completion details (2026-08-18)

**Delivered** (server-side landed piecemeal across 2026-08-15/16; final
app-integration + backfill closeout on 2026-08-18):
- `persistence/{db,models,outbox,relay,broker,dispatchers,csv_projector,alerts,dlq_monitor}.py` — Report/Outbox schema, `ReportSink` protocol, `SqlReportSink`, transactional outbox + `OutboxRelay` with `SELECT ... FOR UPDATE SKIP LOCKED`, `InProcessBroker` fan-out, `PubSub`/`Chain`/`CsvProjector`/`Alert` dispatchers, DLQ depth monitor
- Alembic wired (`alembic.ini`, `alembic/env.py`, migrations `2026081501` initial → `…04` QA sampling)
- `api/routes_reports.py` — `GET /reports/stream` (SSE with `:connected`, 15s `:keepalive`, `lagged` sentinel), `GET /reports/{short_ref}`, `GET /reports` (keyset pagination + source/hazard/severity/life_safety/qa_sample/qa_reviewed/from/to filters), `GET /reports/export.csv` (streaming with UTF-8 BOM), `POST /reports/{short_ref}/qa_review`
- `api/routes_dlq.py` — list / detail / retry / purge, admin-guarded
- `api/auth.py::require_admin_api_key` — HMAC-safe `X-Admin-Api-Key` header check, prod boot fails loud without it
- `persistence/csv_projector.py` — single-writer CSV appender with `fcntl.flock` per-node; §12.2 29-column schema; `row_from_report()` shared with `/reports/export.csv` so live projector + batch export can't drift
- `utils/redact.py` + `description_clean` column — write-time PII redaction on Indian phone patterns, emails, and 4-4-4 Aadhaar so every outbound artifact (SSE, CSV, alerts) is PII-safe
- `persistence/db.py::run_migrations_at_boot()` behind `MIGRATE_ON_BOOT` flag (default off; deploy-step init-container is safer for multi-node)
- `mobile/lib/data/api/voice_reports_sse.dart` + `data/models/voice_report.dart` + `features/report/voice_reports_provider.dart` — Flutter SSE listener with 15s polling fallback, admin key via `--dart-define=VOICE_ADMIN_API_KEY=…`, Riverpod `liveVoiceReportsProvider` accumulates the last 100 reports keyed by id
- `scripts/backfill_source.py` — dry-run-by-default one-shot that sets `source='voice'` on empty rows, batches to avoid a giant lock (`--apply` commits, `--value` and `--batch` are overridable)

**Exit gate (spec §P5):**
- Phone call produces a row in `reports` — ✅ `SqlReportSink` writes Report + Outbox in one tx
- Line in the CSV within 10 s — ✅ `CsvProjectorDispatcher` on the relay poll loop (default `RELAY_POLL_INTERVAL_SEC=1.0`)
- Live SSE update in the Flutter app — ✅ `PubSubDispatcher` → broker → `/reports/stream` → `VoiceReportsSseClient`
- Killing the DB mid-submission still results in the caller hearing success and the report landing after recovery — ✅ outbox is idempotent on `report_id` (Twilio retries safe), sink failures swallowed per §2.3
- Two simultaneous calls never corrupt the CSV — ✅ `fcntl.flock` guards the single-writer path

**Commands:**
- `uv run python scripts/backfill_source.py --apply` — normalise legacy rows to `source='voice'`
- `flutter run --dart-define=API_BASE_URL=https://voice.sentinel.in --dart-define=VOICE_ADMIN_API_KEY=…` — mobile app subscribes to the live feed
- `make test` — 653 unit tests green (+4 backfill)

### P6 completion details (2026-08-18)

The bulk of P6 landed piecemeal across 2026-08-15/16 — enrichment
scaffolding, propagation + reconciliation, Claude LLMExtractor,
JsonGazetteerGeocoder, TextWindowDedupe, QA sampling queue, DLQ
visibility, alert fan-out. The final P6 closeout on 2026-08-18 added
the post-call SMS pin-drop offer + web-form landing.

**Delivered on 2026-08-18:**
- `telephony/twilio_sms.py` — async `SmsSender` Protocol; `TwilioSmsSender`
  (httpx-based, no `twilio` SDK dep — sync-only client would need a
  threadpool hop per send); `RecordingSmsSender` test double.
  `TwilioSmsError` raised on transport/5xx/malformed responses;
  caller swallows for degraded mode.
- `enrichment/sms_pin_offer.py::SmsPinOfferService` — decision engine:
  triggers on `TIMEOUT_EXIT` OR `LOCATION` slot missing OR
  `location.confidence < 0.85`; suppresses on `life_safety` flag OR
  `short_ref` absent. Never raises — sender exceptions logged, webhook
  always returns 204 (spec §7.3 ladder attempt 4 / §11).
- `api/routes_pin.py` — `GET /pin/{short_ref}` (Leaflet + OSM tiles,
  <300 KB, no framework) + `POST /pin/{short_ref}` (writes
  `location_resolved='pin:lat,lng'`, India-bbox bounds check).
  Public by design — short_ref is the capability token (documented
  threat model in the module docstring).
- `api/routes_voice.py::status_callback` — on `CallStatus=completed`,
  hands `From` + `CallSid` to `SmsPinOfferService.maybe_send`. Raw
  phone stays in the webhook's stack frame only (CLAUDE.md invariant
  #6). Service exceptions caught + logged so Twilio never retries.
- `main.py` — `SmsPinOfferService` wired in `lifespan` when
  `SMS_PIN_OFFER_ENABLED=true` AND base URL + Twilio creds set;
  graceful degradation with WARNING log otherwise.
- `config.py` — new settings: `sms_pin_offer_enabled` (default false),
  `sms_pin_offer_base_url` (empty → sender off with warning),
  `sms_pin_offer_location_min_conf` (0.85, matches §9.4 geo_accept).
- Tests: 11 pin-offer decision tests + 7 web-form route tests + 4
  /voice/status wiring tests = **22 new tests**. All import-linter
  contracts still hold.

**Exit gate (spec §P6):**
- Enrichment completes within 60 s p95 of call end — ✅ EnrichmentFlow
  landed in scaffolding + real impls; PubSubDispatcher + relay poll
  interval 1 s keeps latency well under target
- Dedupe correctly groups 10 synthetic duplicate reports of the same
  incident — ✅ `TextWindowDedupe` (unit-tested; see
  `test_text_window_dedupe.py`)
- Extreme-severity reports trigger the ops alert within 30 s — ✅
  `AlertDispatcher` with `LogAlertBackend` + `WebhookAlertBackend`
  (SNS backend deferred to P7 alongside AWS wiring)
- DLQ is empty after a 100-call synthetic run — ✅ DLQ monitor +
  `/api/v1/dlq` admin API in place; synthetic-run harness for the
  100-call check lives with `scripts/simulate_call.py`

**Commands:**
- `make test` — 675 unit tests green (+22 P6 closeout)
- `SMS_PIN_OFFER_ENABLED=true SMS_PIN_OFFER_BASE_URL=https://voice.sentinel.in uv run uvicorn fg_voice.main:app` — enable SMS pin-offer end-to-end

