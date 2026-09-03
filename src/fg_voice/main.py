"""ASGI entrypoint. Wires the FastAPI app together — routes come from
the per-phase modules; the lifespan hook starts + stops the outbox
relay background task."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from fg_voice import __version__
from fg_voice.api import routes_reports, routes_voice
from fg_voice.api.routes_console import router as console_router
from fg_voice.api.routes_dial import router as dial_router
from fg_voice.api.routes_dlq import router as dlq_router
from fg_voice.api.routes_gather import router as gather_router
from fg_voice.api.routes_health import router as health_router
from fg_voice.api.routes_media import router as media_router
from fg_voice.api.routes_pin import router as pin_router
from fg_voice.api.routes_privacy import router as privacy_router
from fg_voice.api.routes_reports import router as reports_router
from fg_voice.api.routes_turn import router as turn_router
from fg_voice.api.routes_voice import router as voice_router
from fg_voice.config import Settings, get_settings
from fg_voice.conversation import graph_patch as _graph_patch
from fg_voice.conversation.state_store import get_call_state_store
from fg_voice.enrichment import EnrichmentDispatcher, EnrichmentFlow
from fg_voice.enrichment.sms_pin_offer import SmsPinOfferService
from fg_voice.enrichment.tasks.dedupe import DedupeStrategy
from fg_voice.enrichment.tasks.extract import LLMExtractor
from fg_voice.enrichment.tasks.geocode import Geocoder
from fg_voice.obs.logging import configure_logging, get_logger
from fg_voice.obs.tracing import configure_tracing
from fg_voice.persistence.alerts import (
    AlertBackend,
    AlertDispatcher,
    LogAlertBackend,
    WebhookAlertBackend,
)
from fg_voice.persistence.broker import InProcessBroker
from fg_voice.persistence.csv_projector import CsvProjectorDispatcher
from fg_voice.persistence.db import run_migrations_at_boot
from fg_voice.persistence.dispatchers import ChainDispatcher, PubSubDispatcher
from fg_voice.persistence.dlq_monitor import DlqMonitor
from fg_voice.persistence.relay import Dispatcher, LogDispatcher, OutboxRelay
from fg_voice.telephony.twilio_sms import TwilioSmsSender
from fg_voice.utils.drain import mark_draining

log = get_logger(__name__)

# Bound to the FastAPI `app.state` at lifespan entry; kept as
# module-level references so tests can peek at the running relay.
_relay_task: asyncio.Task[None] | None = None
_dlq_monitor_task: asyncio.Task[None] | None = None
_shutdown_event: asyncio.Event | None = None
_broker: InProcessBroker | None = None

# Graceful shutdown budget for the relay — enough for one final poll
# to finish, not so long that the ASGI server stalls on SIGTERM.
_RELAY_SHUTDOWN_TIMEOUT_SEC = 5.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _relay_task, _dlq_monitor_task, _shutdown_event, _broker

    settings = get_settings()
    configure_logging(
        settings.fg_log_level, service="fg_voice", log_file=settings.fg_log_file or None
    )
    configure_tracing(
        service_name="fg_voice",
        environment=settings.fg_env,
        agent_version=settings.fg_agent_version,
    )
    settings.require_production_secrets()
    log.info(
        "fg_voice.starting",
        version=__version__,
        env=settings.fg_env,
        region=settings.fg_region,
        agent_version=settings.fg_agent_version,
        runner_mode=settings.runner_mode,
        relay_enabled=settings.relay_enabled,
    )
    if not settings.admin_api_key.get_secret_value():
        # `require_production_secrets` already refuses to boot in
        # production without the key, so this branch is only hit in
        # dev/staging. Warn loudly so an operator doesn't accidentally
        # expose /reports to the internet.
        log.warning(
            "fg_voice.admin_auth_disabled",
            note="ADMIN_API_KEY is empty; /api/v1/reports* are unauthenticated",
        )

    # ── Graceful drain on SIGTERM (spec §14.3) ───────────────────
    # ECS sends SIGTERM to give the task a chance to drain in-flight
    # calls before SIGKILL (stopTimeout: 300). We flip a process-wide
    # flag; /voice/inbound consults it and returns the fallback TwiML
    # instead of connecting the caller to a stream that's about to
    # die. In-flight WebSockets are unaffected and complete naturally.
    #
    # Only bind when running under an asyncio loop that supports
    # add_signal_handler (POSIX). On Windows / non-signal contexts
    # (some tests) this is a no-op — the flag can be flipped manually
    # via `mark_draining()`.
    try:
        loop = asyncio.get_running_loop()

        def _on_sigterm() -> None:
            log.warning("fg_voice.sigterm.received", note="draining new-call intake")
            mark_draining()

        loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
        loop.add_signal_handler(signal.SIGINT, _on_sigterm)
    except Exception:  # noqa: BLE001
        # Windows: signal.SIGTERM may not exist (AttributeError) and
        # ProactorEventLoop doesn't support add_signal_handler
        # (NotImplementedError). Not fatal — surge-mode test paths flip
        # the flag directly.
        pass

    if settings.migrate_on_boot:
        # Run before the relay starts so the relay's first drain never
        # hits a missing table. Any migration failure aborts the boot
        # (the lifespan re-raises), which is the right behaviour —
        # serving traffic against a stale schema silently corrupts data.
        try:
            revision = await run_migrations_at_boot()
            log.info("fg_voice.migrations.applied", revision=revision)
        except Exception as exc:
            log.exception("fg_voice.migrations.failed", error=str(exc))
            raise

    # Clear any leftover state from a previous lifespan entry so a
    # re-entry (mostly tests) doesn't inherit stale task references.
    app.state.relay_task = None
    app.state.relay = None
    app.state.broker = None
    app.state.dlq_monitor_task = None
    app.state.dlq_monitor = None

    if settings.relay_enabled:
        _broker = InProcessBroker()
        routes_reports.set_broker(_broker)
        # PubSubDispatcher fans events into the SSE broker for live
        # consumers. CsvProjectorDispatcher optionally lands a row in
        # a shared CSV file (§12.3 fast path). Composed via
        # ChainDispatcher so both run per event; if either raises the
        # relay bumps retry_count and re-attempts.
        dispatchers: list[Dispatcher] = [PubSubDispatcher(broker=_broker)]
        if settings.csv_enabled:
            dispatchers.append(
                CsvProjectorDispatcher(
                    path=settings.csv_path,
                    agent_version=settings.fg_agent_version,
                )
            )
        if settings.alerts_enabled:
            backends: list[AlertBackend] = [LogAlertBackend()]
            if settings.alert_webhook_url:
                backends.append(
                    WebhookAlertBackend(
                        url=settings.alert_webhook_url,
                        timeout_sec=settings.alert_webhook_timeout_sec,
                    )
                )
            dispatchers.append(AlertDispatcher(backends=backends))
        if settings.enrichment_enabled:
            # Enrichment runs LAST in the chain so a slow LLM call
            # can't delay the fast-path SSE/CSV/alert side-effects.
            # Geocoder + dedupe still default to No-Op — real impls
            # land with P4 RAG + PostGIS. The extractor swap is
            # controlled by EXTRACTOR_TYPE (noop | claude); import is
            # lazy so a `noop` deploy never touches the anthropic SDK.
            extractor = _build_extractor(settings)
            geocoder = _build_geocoder(settings)
            dedupe_strategy = _build_dedupe(settings)
            flow = EnrichmentFlow(
                extractor=extractor,
                geocoder=geocoder,
                dedupe_strategy=dedupe_strategy,
            )
            dispatchers.append(EnrichmentDispatcher(flow=flow))
            log.info(
                "fg_voice.enrichment.wired",
                extractor=settings.extractor_type,
                geocoder=settings.geocoder_type,
                dedupe=settings.dedupe_type,
                model=(
                    settings.claude_extractor_model if settings.extractor_type == "claude" else None
                ),
            )
        dispatcher: Dispatcher = (
            dispatchers[0] if len(dispatchers) == 1 else ChainDispatcher(dispatchers)
        )
        relay = OutboxRelay(
            dispatcher=dispatcher,
            poll_interval_sec=settings.relay_poll_interval_sec,
        )
        _shutdown_event = asyncio.Event()
        _relay_task = asyncio.create_task(relay.run(_shutdown_event), name="fg_voice.outbox_relay")
        # Keep for observability + testability.
        app.state.broker = _broker
        app.state.relay = relay
        app.state.relay_task = _relay_task

        # DLQ depth monitor — shares the shutdown_event so both tasks
        # drain on SIGTERM together. Cheap (one COUNT per interval);
        # gives ops the only ongoing visibility into stuck-row
        # accumulation.
        if settings.dlq_monitor_enabled:
            monitor = DlqMonitor(
                interval_sec=settings.dlq_monitor_interval_sec,
                alert_threshold=settings.dlq_alert_threshold,
            )
            _dlq_monitor_task = asyncio.create_task(
                monitor.run(_shutdown_event), name="fg_voice.dlq_monitor"
            )
            app.state.dlq_monitor = monitor
            app.state.dlq_monitor_task = _dlq_monitor_task
        # LogDispatcher isn't wired in the chain today because the
        # relay-side logs already record dispatched entries; adding
        # log-in-chain would double-log every event.
        _ = LogDispatcher  # keep the import warm so the linter is quiet

    # ── SMS pin-drop offer ────────────────────────────────────────
    # Independent of the relay — wired straight into the /voice/status
    # handler. Off unless enabled AND a base URL is set (a broken
    # config is worse than no SMS per §2.6).
    if settings.sms_pin_offer_enabled:
        if not settings.sms_pin_offer_base_url:
            log.warning(
                "fg_voice.sms_pin_offer.disabled",
                reason="SMS_PIN_OFFER_BASE_URL empty; SMS sender not wired",
            )
        elif not settings.twilio_account_sid or not settings.twilio_auth_token.get_secret_value():
            log.warning(
                "fg_voice.sms_pin_offer.disabled",
                reason="Twilio credentials missing; SMS sender not wired",
            )
        else:
            sender = TwilioSmsSender(
                account_sid=settings.twilio_account_sid,
                auth_token=settings.twilio_auth_token.get_secret_value(),
            )
            state_store = await get_call_state_store()
            service = SmsPinOfferService(
                sender=sender,
                state_store=state_store,
                from_number=settings.twilio_phone_number,
                web_base_url=settings.sms_pin_offer_base_url,
                location_confidence_threshold=settings.sms_pin_offer_location_min_conf,
            )
            routes_voice.set_sms_pin_offer_service(service)
            app.state.sms_pin_offer_service = service
            log.info(
                "fg_voice.sms_pin_offer.wired",
                base_url=settings.sms_pin_offer_base_url,
                from_number=settings.twilio_phone_number,
            )

    try:
        yield
    finally:
        if _shutdown_event is not None and _relay_task is not None:
            log.info("fg_voice.stopping.relay")
            _shutdown_event.set()
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(_relay_task, timeout=_RELAY_SHUTDOWN_TIMEOUT_SEC)
            if not _relay_task.done():
                _relay_task.cancel()
                with contextlib.suppress(BaseException):
                    await _relay_task
            _relay_task = None
            # DLQ monitor shares the shutdown_event; wait for it too.
            if _dlq_monitor_task is not None:
                with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(_dlq_monitor_task, timeout=_RELAY_SHUTDOWN_TIMEOUT_SEC)
                if not _dlq_monitor_task.done():
                    _dlq_monitor_task.cancel()
                    with contextlib.suppress(BaseException):
                        await _dlq_monitor_task
                _dlq_monitor_task = None
                app.state.dlq_monitor_task = None
                app.state.dlq_monitor = None
            _shutdown_event = None
            app.state.relay_task = None
            app.state.relay = None
        routes_reports.set_broker(None)
        routes_voice.set_sms_pin_offer_service(None)
        app.state.broker = None
        app.state.sms_pin_offer_service = None
        _broker = None
        log.info("fg_voice.stopping")


def _build_extractor(settings: Settings) -> LLMExtractor:
    """Instantiate the LLMExtractor picked by `EXTRACTOR_TYPE`. Lazy
    imports so a `noop` deploy never loads the anthropic SDK — that
    keeps the base image small and the No-Op path dependency-free."""
    if settings.extractor_type == "claude":
        # Lazy import so `anthropic` stays truly optional at runtime.
        from fg_voice.enrichment.extractors.claude_llm import (
            build_claude_extractor,
        )

        api_key = settings.anthropic_api_key.get_secret_value()
        if not api_key:
            # Non-prod bypass — production already errors out in
            # `require_production_secrets`. Fall back to No-Op with
            # a loud warning so an operator notices before hitting
            # the enrichment path.
            log.warning(
                "fg_voice.extractor.claude_disabled",
                reason="ANTHROPIC_API_KEY empty; falling back to NoOpExtractor",
            )
            from fg_voice.enrichment.tasks.extract import NoOpExtractor

            return NoOpExtractor()
        return build_claude_extractor(api_key=api_key, model=settings.claude_extractor_model)
    # Default path.
    from fg_voice.enrichment.tasks.extract import NoOpExtractor

    return NoOpExtractor()


def _build_geocoder(settings: Settings) -> Geocoder:
    """Instantiate the Geocoder picked by `GEOCODER_TYPE`. Lazy import
    so a `noop` deploy never loads rapidfuzz — the `[rag]` extras
    stays optional at runtime."""
    if settings.geocoder_type == "json_gazetteer":
        from fg_voice.enrichment.geocoders.json_gazetteer import (
            build_gazetteer_geocoder_with_mandals,
        )

        path = settings.gazetteer_path
        if not path.exists():
            # Fail loud at boot — a running deploy pointed at a missing
            # gazetteer would silently return None for every location.
            raise RuntimeError(
                f"GEOCODER_TYPE=json_gazetteer but GAZETTEER_PATH does not exist: {path}"
            )
        # Mandals are optional — degrade gracefully to district-only if
        # the file isn't shipped in this deploy.
        mandals_path = settings.mandal_gazetteer_path
        effective_mandals = mandals_path if mandals_path and mandals_path.exists() else None
        if mandals_path and not effective_mandals:
            log.warning(
                "fg_voice.geocoder.mandals_disabled",
                configured_path=str(mandals_path),
                reason="MANDAL_GAZETTEER_PATH set but file missing; district-only matching",
            )
        return build_gazetteer_geocoder_with_mandals(
            districts_path=path,
            mandals_path=effective_mandals,
            min_score=settings.gazetteer_min_score,
        )
    from fg_voice.enrichment.tasks.geocode import NoOpGeocoder

    return NoOpGeocoder()


def _build_dedupe(settings: Settings) -> DedupeStrategy:
    """Instantiate the DedupeStrategy picked by `DEDUPE_TYPE`. Lazy
    import so `noop` deploys stay dependency-free."""
    if settings.dedupe_type == "text_window":
        from fg_voice.enrichment.dedupers.text_window import TextWindowDedupe

        return TextWindowDedupe(
            window_hours=settings.dedupe_window_hours,
            text_threshold=settings.dedupe_text_threshold,
        )
    from fg_voice.enrichment.tasks.dedupe import NoDedupeStrategy

    return NoDedupeStrategy()


app = FastAPI(
    title="Sentinel Voice Agent",
    version=__version__,
    lifespan=lifespan,
    docs_url="/docs" if get_settings().fg_env != "production" else None,
    redoc_url=None,
)

app.include_router(health_router)
app.include_router(voice_router)
app.include_router(media_router)
app.include_router(reports_router)
app.include_router(dlq_router)
app.include_router(pin_router)
app.include_router(console_router)
app.include_router(privacy_router)
app.include_router(dial_router)
app.include_router(turn_router)
app.include_router(gather_router)

# Widen the ASK_HAZARD_TYPE guard so `flood` and `tsunami` can transition.
# See conversation/graph_patch.py — defect in recovered bytecode.
_graph_patch.apply()
