"""/ws/media — the Twilio Media Streams WebSocket endpoint."""

from __future__ import annotations

import asyncio
import contextlib
import math
import struct

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status, Request

from fg_voice.config import get_settings
from fg_voice.obs.logging import get_logger
from fg_voice.telephony.twilio_stream import (
    MediaEvent,
    StartEvent,
    StopEvent,
    UnknownEventError,
    build_clear,
    envelope_kind,
    parse_inbound,
    DtmfEvent,
)
from fg_voice.pipeline.openai_client import run_openai_realtime_session

log = get_logger(__name__)
router = APIRouter(tags=["media"])


@router.websocket("/ws/media")
async def media_ws(ws: WebSocket) -> None:
    """Twilio Media Streams handler — full call lifecycle."""
    try:
        await ws.accept(subprotocol="audio.twilio.com")
        stream_sid: str | None = None
        
        # Wait for the StartEvent from Twilio before kicking off OpenAI
        while True:
            raw = await ws.receive_text()
            try:
                evt = parse_inbound(raw)
            except UnknownEventError:
                log.debug("media.unknown_event", kind=envelope_kind(raw))
                continue

            if isinstance(evt, StartEvent):
                stream_sid = evt.stream_sid
                log.info(
                    "media.start",
                    stream_sid=stream_sid,
                    call_sid=evt.call_sid,
                    encoding=evt.media_format_encoding,
                    sample_rate=evt.media_format_sample_rate,
                )
                
                # Fetch params passed in the connect TwiML
                caller_hash = evt.custom_parameters.get("caller_hash", "unknown")
                report_id = evt.custom_parameters.get("report_id", "unknown")

                # Connect to OpenAI Realtime and block until call is done
                await run_openai_realtime_session(
                    ws_twilio=ws,
                    stream_sid=evt.stream_sid,
                    call_sid=evt.call_sid,
                    report_id=report_id,
                    caller_hash=caller_hash,
                )
                break

    except WebSocketDisconnect:
        log.info(
            "media.disconnect",
            stream_sid=stream_sid,
        )
    except Exception as exc:
        log.error("media.fatal_exception", error=str(exc))
        raise
    finally:
        if stream_sid is not None:
            with contextlib.suppress(Exception):
                await ws.send_text(build_clear(stream_sid))

