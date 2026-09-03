"""Runtime configuration for the Sentinel Voice Agent.

Fails at boot on missing required env vars — never at runtime, and never
with a caller on the line. See CLAUDE.md invariant #7.

Loaded once at process start via `get_settings()`. The result is cached
so imports remain cheap and the object is safely shared across async tasks.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEV = "dev"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """All runtime settings. Field names mirror .env.example exactly."""

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Runtime ──────────────────────────────────────────────
    fg_env: Environment = Environment.DEV
    fg_agent_version: str = "dev-local"
    fg_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # Optional on-disk copy of the JSON log stream. Empty = stdout only.
    fg_log_file: str = ""
    fg_region: str = "ap-south-1"

    # ── Telephony ────────────────────────────────────────────
    telephony_provider: Literal["twilio", "exotel"] = "twilio"
    twilio_account_sid: str = ""
    twilio_auth_token: SecretStr = SecretStr("")
    twilio_phone_number: str = ""
    twilio_media_region: str = "sg1"
    public_wss_base: str = "wss://voice.sentinel.in"
    # Dev-only escape hatch. Skips X-Twilio-Signature verification on
    # /voice/* webhooks — necessary when a free-tier tunnel (ngrok free,
    # cloudflared) strips or interposes on request headers. NEVER enable
    # in production: it lets anyone forge webhook events.
    twilio_signature_validation_enabled: bool = True

    # ── OpenAI ────────────────────────────────────────────────────────
    openai_api_key: SecretStr = SecretStr("")

    # ── STT ──────────────────────────────────────────────────
    deepgram_api_key: SecretStr = SecretStr("")
    stt_model: str = "flux-general-en"
    stt_fallback_model: str = "nova-3"
    stt_eot_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    stt_eager_eot_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
    stt_eot_timeout_ms: int = Field(default=1200, ge=100, le=10_000)
    # STT_PRIMARY_PROVIDER: which provider is tried first.
    # "deepgram" = Deepgram Flux (default, proven on Indian English).
    # "nemotron" = NVIDIA Nemotron ASR Streaming (requires NVIDIA_API_KEY).
    # The other provider becomes the automatic fallback.
    stt_primary_provider: Literal["deepgram", "nemotron"] = "deepgram"
    # STT chain circuit-breaker: open after this many consecutive failures.
    stt_circuit_open_after: int = Field(default=3, ge=1, le=20)
    # Seconds to wait before half-opening the circuit for a recovery probe.
    stt_circuit_reset_sec: float = Field(default=30.0, ge=5.0, le=300.0)
    # Hard timeout for STT stream connect (seconds). On exceed: switch provider.
    stt_connect_timeout_sec: float = Field(default=1.5, ge=0.5, le=10.0)

    # ── TTS ──────────────────────────────────────────────────
    tts_provider: Literal["cartesia", "deepgram", "polly", "magpie"] = "cartesia"
    tts_api_key: SecretStr = SecretStr("")
    tts_voice_id: str = ""
    tts_cache_ttl_sec: int = 604_800
    audio_bank_path: Path = Path("./prompts/audio_bank/en-IN")
    # TTS_PRIMARY_PROVIDER: which live-synthesis provider is used after
    # an audio-bank miss. "magpie" = NVIDIA Magpie TTS (requires
    # NVIDIA_API_KEY). "cartesia" / "deepgram" = existing integrations.
    tts_primary_provider: Literal["cartesia", "deepgram", "magpie"] = "cartesia"
    # Hard timeout for TTS first-byte (ms). On exceed: play pre-rendered
    # backchannel while streaming continues.
    tts_first_byte_timeout_ms: int = Field(default=1200, ge=200, le=5000)
    # Retry count for TTS DEADLINE_EXCEEDED (NVCF transient routing).
    tts_deadline_exceeded_retries: int = Field(default=2, ge=0, le=5)

    # ── NVIDIA (Nemotron ASR + Magpie TTS) ─────────────────────────────
    # API key for hosted NVIDIA NVCF endpoints (no GPU, no Docker).
    # Required when stt_primary_provider="nemotron" or
    # tts_primary_provider="magpie".
    nvidia_api_key: SecretStr = SecretStr("")
    # Nemotron ASR Streaming gRPC endpoint (NVCF hosted).
    nvidia_asr_endpoint: str = "grpc.nvcf.nvidia.com:443"
    # Magpie TTS REST endpoint (NVCF hosted).
    nvidia_tts_endpoint: str = "https://api.nvcf.nvidia.com/v2/nvcf/pexec/functions"
    # Magpie TTS voice preset. Check NVIDIA's published voice list.
    nvidia_tts_voice: str = "English-US.Female-1"

    # ── LLM ──────────────────────────────────────────────────
    llm_provider: Literal["bedrock", "anthropic"] = "bedrock"
    bedrock_region: str = "ap-south-1"
    llm_model_id: str = ""
    llm_fallback_api_key: SecretStr = SecretStr("")
    llm_timeout_ms: int = Field(default=800, ge=100, le=10_000)
    llm_max_tokens: int = Field(default=100, ge=10, le=1000)

    # ── Noise suppression ────────────────────────────────────
    denoise_provider: Literal["krisp", "rnnoise", "none"] = "rnnoise"
    krisp_license_key: SecretStr = SecretStr("")

    # ── Dual recording (§9.1 tee) ────────────────────────────
    # S3_RECORDING_ENABLED=true wires the DualRecorder into the
    # audio path — every call ships a raw μ-law + a clean PCM16 stream
    # to `s3_recordings_bucket` on call end. Off by default so tests +
    # dev deploys don't require S3 creds. Ops flips it on when the
    # bucket lifecycle policy is in place (30-day retention per §17).
    s3_recording_enabled: bool = False

    # ── Data ─────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://fg_voice:fg_voice@localhost:55432/fg_voice"
    redis_url: str = "redis://localhost:56379/0"
    caller_hash_pepper: SecretStr = SecretStr("dev-only-do-not-use-in-prod")
    s3_recordings_bucket: str = "fg-voice-recordings"
    s3_transcripts_bucket: str = "fg-voice-transcripts"
    s3_reports_bucket: str = "fg-reports"
    s3_rag_bucket: str = "fg-voice-rag"
    efs_csv_path: Path = Path("./data/csv")
    csv_schema_version: int = 1

    # ── AWS ──────────────────────────────────────────────────
    aws_endpoint_url: str | None = None  # set to localstack URL in dev
    aws_access_key_id: str | None = None
    aws_secret_access_key: SecretStr | None = None
    aws_default_region: str = "ap-south-1"

    # ── RAG ──────────────────────────────────────────────────
    gazetteer_snapshot_version: str = "latest"
    taxonomy_version: str = "latest"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    geo_accept_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    geo_confirm_threshold: float = Field(default=0.60, ge=0.0, le=1.0)
    geo_margin_threshold: float = Field(default=0.10, ge=0.0, le=1.0)

    # ── Conversation ─────────────────────────────────────────
    max_call_duration_sec: int = Field(default=300, ge=30, le=1800)
    no_input_timeout_ms: int = Field(default=6000, ge=1000, le=30_000)
    max_attempts_per_slot: int = Field(default=3, ge=1, le=5)
    emergency_number: str = "112"
    # RUNNER_MODE=true → /voice/inbound redirects to the Gather-based
    # HTTP flow instead of the Media Streams path. Provides a keyless
    # bring-up path for the P2 exit gate (Twilio does the STT itself);
    # Media Streams comes back for P3 once Deepgram is wired.
    runner_mode: bool = False
    # TURN_MODE=true → /voice/inbound redirects to the turn-based
    # <Play>/<Record> flow in `api/routes_turn.py`, which uses OpenAI for
    # both TTS and STT. Required on Twilio accounts without Media Streams
    # (trial tier): `<Connect><Stream>` is silently refused there, so the
    # realtime path in `pipeline/openai_client.py` never receives audio.
    # Set false once the account is upgraded to get full-duplex realtime.
    turn_mode: bool = False
    # RELAY_ENABLED=false disables the outbox relay background task
    # (useful in tests + in workers that only serve HTTP with no DB
    # writes). Defaults to true so a fresh deploy drains its outbox
    # without any extra config.
    relay_enabled: bool = True
    relay_poll_interval_sec: float = Field(default=1.0, ge=0.05, le=60.0)
    # CSV_ENABLED=true adds the CSV projector to the relay's dispatch
    # chain. Off by default so tests + CI don't accumulate files on
    # disk; on in staging/prod where downstream tooling consumes it.
    csv_enabled: bool = False
    csv_path: Path = Path("./data/reports.csv")
    # ALERTS_ENABLED=true adds the alert fan-out dispatcher. The
    # log-backend always fires when enabled; the webhook-backend only
    # when ALERT_WEBHOOK_URL is set. Off by default because a bad
    # webhook config is worse than no alerts (alerts still hit the log).
    alerts_enabled: bool = False
    alert_webhook_url: str = ""
    alert_webhook_timeout_sec: float = Field(default=5.0, ge=0.5, le=30.0)
    # ENRICHMENT_ENABLED=true adds the P6 enrichment dispatcher to the
    # relay chain — every `report.submitted` outbox row triggers the
    # enrichment DAG (assemble → deep_extract → geocode → dedupe →
    # score → persist). Off by default because the LLM extractor, RAG
    # geocoder, and dedupe strategy all default to No-Op — so enabling
    # it in prod with no impls injected would just burn cycles doing
    # nothing. Flip on once real impls are wired in `main.py`.
    enrichment_enabled: bool = False
    # EXTRACTOR_TYPE selects which LLMExtractor implementation the
    # enrichment DAG uses for deep_extract. `noop` is the safe default
    # (no external dep, no API cost); `claude` requires ANTHROPIC_API_KEY
    # and the optional `[llm]` install extras.
    extractor_type: Literal["noop", "claude"] = "noop"
    # Anthropic Claude API key for the ClaudeExtractor. SecretStr so it
    # doesn't leak in logs or repr(). `require_production_secrets`
    # rejects a blank key in prod when `extractor_type=claude`.
    anthropic_api_key: SecretStr = SecretStr("")
    # Model id for the Claude extractor. Defaults to Opus 4.7. Operators
    # who prioritise throughput / cost over intelligence can flip to
    # `claude-haiku-4-5` here once real-call data justifies it.
    claude_extractor_model: str = "claude-opus-4-7"
    # GEOCODER_TYPE selects which Geocoder implementation the
    # enrichment DAG uses. `noop` is the safe default; `json_gazetteer`
    # loads the JSON at GAZETTEER_PATH and fuzzy-matches via rapidfuzz
    # (requires the `[rag]` extras).
    geocoder_type: Literal["noop", "json_gazetteer"] = "noop"
    gazetteer_path: Path = Path("./data/gazetteer/districts.json")
    # Optional mandal (sub-district) gazetteer. When set AND the file
    # exists, the geocoder loads it alongside the district list — a
    # caller saying "Anaparthi" resolves to
    # "Anaparthi, East Godavari, Andhra Pradesh" instead of falling
    # through to fuzzy over districts alone. If the path is empty or
    # missing, mandal matching is silently disabled (district-only
    # matching remains functional).
    mandal_gazetteer_path: Path | None = Path("./data/gazetteer/mandals.json")
    # Fuzzy-match cutoff in [0, 100]. Below this, no match is returned
    # (safer to leave location_resolved NULL than write a wrong district
    # onto the row). 80 catches "Vishakapatnam" / "Anantapuram"; below
    # 70 false positives start dominating.
    gazetteer_min_score: int = Field(default=80, ge=0, le=100)
    # DEDUPE_TYPE selects which DedupeStrategy the enrichment DAG uses.
    # `noop` leaves every report as its own singleton; `text_window`
    # groups reports on hazard_type + district + time window + text
    # similarity (rapidfuzz, part of `[rag]` extras).
    dedupe_type: Literal["noop", "text_window"] = "noop"
    # Rolling window for `text_window` dedupe. Spec §11 default is 3h.
    dedupe_window_hours: int = Field(default=3, ge=1, le=48)
    # WRatio cutoff for text similarity — spec's embedding cosine
    # cutoff is 0.82, which tracks similarly at WRatio 82 on the
    # eval corpus. Tune per real-call calibration.
    dedupe_text_threshold: int = Field(default=82, ge=0, le=100)
    # DLQ_MONITOR_ENABLED=true spawns a background task that logs the
    # outbox DLQ depth every DLQ_MONITOR_INTERVAL_SEC seconds. Default
    # on because it's cheap (one COUNT per interval) and gives ops the
    # only ongoing visibility into stuck-row accumulation.
    dlq_monitor_enabled: bool = True
    dlq_monitor_interval_sec: float = Field(default=60.0, ge=1.0, le=3600.0)
    # First-crossing WARNING trips at this depth. 1 = fire on any
    # stuck row; tune upwards if a small standing DLQ is expected.
    dlq_alert_threshold: int = Field(default=1, ge=1, le=1000)
    # QA sampling rate — fraction of submitted reports flagged for
    # human review. 0.05 (spec §11.11) is enough to catch systematic
    # drift without swamping the review queue. Set to 0.0 to disable
    # sampling entirely (dev / smoke-test deploys).
    qa_sampling_rate: float = Field(default=0.05, ge=0.0, le=1.0)
    # /readyz per-check hard timeout. Kept short so a stuck dep never
    # hangs an ALB health poll; tune upwards only if a P50-slow dep is
    # deliberately in the path.
    readyz_timeout_sec: float = Field(default=1.5, ge=0.1, le=30.0)
    # /readyz outbox-depth threshold. Above this, /readyz reports
    # `degraded` (200 stays 200 for outbox alone; ALB still routes,
    # but ops sees the relay-behind signal in the response body).
    # Only trips 503 if a HARD dep (DB/Redis/relay task) also fails.
    readyz_outbox_max_depth: int = Field(default=1000, ge=1)
    # Analogous DLQ-depth threshold. Above this, `degraded` again.
    readyz_dlq_max_depth: int = Field(default=10, ge=1)

    # Admin API key for the /api/v1/reports* endpoints. Empty means
    # auth is disabled (dev bypass); production boot logs a warning
    # AND `require_production_secrets` refuses to boot without it set.
    admin_api_key: SecretStr = SecretStr("")
    # MIGRATE_ON_BOOT=true runs `alembic upgrade head` in main.py's
    # lifespan before the relay starts. Off by default because
    # multi-node prod deploys typically apply schema in an init
    # container / deploy step (avoids N pods racing on the same
    # upgrade). Dev + single-node deploys can flip it on.
    migrate_on_boot: bool = False

    # ── SMS pin-drop offer (spec §7.3 ladder attempt 4 + §11) ───
    # SMS_PIN_OFFER_ENABLED=true fires an SMS from /voice/status when
    # the last CallState shows a timeout-exit or a low-confidence
    # location. Off by default because it needs real Twilio SMS creds
    # and a public-facing base URL; a broken config is worse than no
    # SMS (spec §2.6). The web form is served regardless — the SMS
    # sender is what's optional.
    sms_pin_offer_enabled: bool = False
    # Public base URL that the SMS body links to. In dev this can be
    # the ngrok URL; in prod it's https://voice.sentinel.in.
    # Trailing slashes tolerated. Empty → disables the sender at boot
    # even if `sms_pin_offer_enabled=true` (loud warning in main.py).
    sms_pin_offer_base_url: str = ""
    # Threshold below which we treat the LOCATION slot as too
    # uncertain to skip the SMS. Defaults to `geo_accept_threshold`
    # per §9.4; kept as its own field so ops can tune SMS aggressiveness
    # without changing the in-call acceptance behaviour.
    sms_pin_offer_location_min_conf: float = Field(default=0.85, ge=0.0, le=1.0)

    # ── Ops ──────────────────────────────────────────────────
    alert_sns_topic_arn: str | None = None
    otel_exporter_otlp_endpoint: str | None = None
    surge_mode_min_tasks: int = 10

    # ── validators ───────────────────────────────────────────
    @field_validator("caller_hash_pepper")
    @classmethod
    def _reject_dev_pepper_in_prod(cls, v: SecretStr, info) -> SecretStr:  # type: ignore[no-untyped-def]
        env = info.data.get("fg_env")
        if env == Environment.PRODUCTION and v.get_secret_value().startswith("dev-only"):
            raise ValueError(
                "CALLER_HASH_PEPPER cannot use the dev placeholder in production. "
                "Rotate via Secrets Manager before boot."
            )
        return v

    def is_production(self) -> bool:
        return self.fg_env == Environment.PRODUCTION

    def require_production_secrets(self) -> None:
        """Called from main.py at startup when fg_env=production. Any missing
        production-only secret raises here, so a broken deploy never accepts
        traffic."""
        if not self.is_production():
            return
        required: list[tuple[str, str]] = [
            ("twilio_account_sid", self.twilio_account_sid),
            ("twilio_auth_token", self.twilio_auth_token.get_secret_value()),
            ("twilio_phone_number", self.twilio_phone_number),
            ("admin_api_key", self.admin_api_key.get_secret_value()),
            ("deepgram_api_key", self.deepgram_api_key.get_secret_value()),
            ("tts_api_key", self.tts_api_key.get_secret_value()),
            ("tts_voice_id", self.tts_voice_id),
            ("llm_model_id", self.llm_model_id),
            ("alert_sns_topic_arn", self.alert_sns_topic_arn or ""),
        ]
        # ClaudeExtractor needs an API key when enabled — conditional
        # so ops running enrichment with the No-Op extractor aren't
        # forced to provision an Anthropic key.
        if self.extractor_type == "claude":
            required.append(("anthropic_api_key", self.anthropic_api_key.get_secret_value()))
        missing = [name for name, value in required if not value]
        if missing:
            raise RuntimeError(f"Production boot missing required settings: {', '.join(missing)}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor. Import this, never instantiate Settings directly."""
    return Settings()
