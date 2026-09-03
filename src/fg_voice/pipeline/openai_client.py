"""OpenAI Realtime API (GA `gpt-realtime`) client for Twilio Media Streams.

Handles STT, conversational reasoning, and TTS in one bidirectional
websocket session — replaces the Deepgram/Cartesia pipeline for calls
routed through /ws/media."""

import asyncio
import contextlib
import inspect
import json
from uuid import UUID

import websockets
from fastapi import WebSocket, WebSocketDisconnect

from fg_voice.config import get_settings
from fg_voice.conversation.sql_report_sink import SqlReportSink
from fg_voice.conversation.state import CallState, Slot, SlotValue
from fg_voice.obs.logging import get_logger
from fg_voice.persistence.db import get_session_maker
from fg_voice.telephony.twilio_stream import MediaEvent, StopEvent, UnknownEventError, build_media, parse_inbound

log = get_logger(__name__)

REALTIME_MODEL = "gpt-realtime"

_INSTRUCTIONS = """You are Sentinel, an AI voice agent for a coastal disaster hotline in India. \
Callers report hazards such as floods, tsunamis, storms, oil/sludge spills, abnormal tides, \
and coastal erosion. Speak clearly, calmly, and concisely — this may be an emergency.

Collect exactly these four things, one at a time, in order:
1) Hazard type — ask "What kind of hazard is it — floods, a tsunami, storm damage, sludge or \
oil, unusual tides, or something else?" and map the answer to one of: flood, tsunami, storm, \
sludge_oil, abnormal_tide, erosion, other.
2) Location — a beach, landmark, town, or village name.
3) Severity — light, moderate, or extreme.
4) A short description of what the caller is seeing.

If the caller says anyone is hurt or trapped, immediately tell them to hang up and call 112, \
then continue collecting the report if they stay on the line.

Once you have all four items, call save_report with the collected fields, thank the caller, \
give them their reference, and say goodbye."""

_TOOLS = [
    {
        "type": "function",
        "name": "save_report",
        "description": "Save the collected hazard report to the database.",
        "parameters": {
            "type": "object",
            "properties": {
                "hazard_type": {
                    "type": "string",
                    "enum": ["flood", "tsunami", "storm", "sludge_oil", "abnormal_tide", "erosion", "other"],
                },
                "location": {"type": "string"},
                "severity": {"type": "string", "enum": ["light", "moderate", "extreme"]},
                "description": {"type": "string"},
            },
            "required": ["hazard_type", "location", "severity"],
        },
    }
]


async def run_openai_realtime_session(
    ws_twilio: WebSocket, stream_sid: str, call_sid: str, report_id: str, caller_hash: str
) -> None:
    settings = get_settings()
    api_key = settings.openai_api_key.get_secret_value()

    if not api_key:
        log.error("openai.missing_api_key")
        await ws_twilio.close()
        return

    url = f"wss://api.openai.com/v1/realtime?model={REALTIME_MODEL}"
    headers = {"Authorization": f"Bearer {api_key}"}

    log.info("openai.connecting", model=REALTIME_MODEL)
    try:
        # `additional_headers` (websockets >=13 asyncio impl) vs
        # `extra_headers` (older legacy impl) — support whichever this
        # environment's installed websockets version expects.
        headers_kwarg = (
            "additional_headers"
            if "additional_headers" in inspect.signature(websockets.connect).parameters
            else "extra_headers"
        )
        async with websockets.connect(url, **{headers_kwarg: headers}) as ws_openai:
            log.info("openai.connected")

            session_update = {
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "model": REALTIME_MODEL,
                    "output_modalities": ["audio"],
                    "instructions": _INSTRUCTIONS,
                    "audio": {
                        "input": {
                            "format": {"type": "audio/g711_ulaw"},
                            "turn_detection": {"type": "semantic_vad"},
                        },
                        "output": {
                            "format": {"type": "audio/g711_ulaw"},
                            "voice": "marin",
                        },
                    },
                    "tools": _TOOLS,
                    "tool_choice": "auto",
                },
            }
            await ws_openai.send(json.dumps(session_update))
            await ws_openai.send(json.dumps({"type": "response.create"}))

            twilio_to_openai = asyncio.create_task(_relay_twilio_to_openai(ws_twilio, ws_openai))
            openai_to_twilio = asyncio.create_task(
                _relay_openai_to_twilio(ws_openai, ws_twilio, stream_sid, report_id, call_sid, caller_hash)
            )

            done, pending = await asyncio.wait(
                [twilio_to_openai, openai_to_twilio],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            for task in done:
                exc = task.exception()
                if exc:
                    log.error("openai.relay_task_error", error=str(exc))
    except Exception as exc:
        log.error("openai.fatal_error", error=str(exc))


async def _relay_twilio_to_openai(ws_twilio: WebSocket, ws_openai: websockets.WebSocketClientProtocol) -> None:
    try:
        while True:
            raw = await ws_twilio.receive_text()
            try:
                evt = parse_inbound(raw)
            except UnknownEventError:
                continue

            if isinstance(evt, MediaEvent):
                await ws_openai.send(json.dumps({"type": "input_audio_buffer.append", "audio": evt.payload}))
            elif isinstance(evt, StopEvent):
                break
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.error("relay.twilio_to_openai_error", error=str(exc))


async def _relay_openai_to_twilio(
    ws_openai: websockets.WebSocketClientProtocol,
    ws_twilio: WebSocket,
    stream_sid: str,
    report_id: str,
    call_sid: str,
    caller_hash: str,
) -> None:
    # item_id -> {"call_id": ..., "name": ...}, populated when a function-call
    # item is opened so `response.function_call_arguments.done` (which only
    # reliably carries item_id) can be resolved back to a call_id.
    pending_calls: dict[str, dict[str, str]] = {}
    try:
        async for raw_msg in ws_openai:
            msg = json.loads(raw_msg)
            msg_type = msg.get("type")

            if msg_type == "response.output_audio.delta":
                b64_audio = msg.get("delta")
                if b64_audio:
                    await ws_twilio.send_text(build_media(stream_sid, b64_audio))

            elif msg_type == "response.output_item.added":
                item = msg.get("item") or {}
                if item.get("type") == "function_call" and item.get("id"):
                    pending_calls[item["id"]] = {
                        "call_id": item.get("call_id", ""),
                        "name": item.get("name", ""),
                    }

            elif msg_type == "response.function_call_arguments.done":
                item_id = msg.get("item_id", "")
                info = pending_calls.pop(item_id, None)
                call_id = (info or {}).get("call_id") or msg.get("call_id", "")
                name = (info or {}).get("name") or msg.get("name", "")

                if name == "save_report" and call_id:
                    args = json.loads(msg.get("arguments", "{}"))
                    log.info("openai.save_report", args=args)

                    try:
                        state = _build_call_state(report_id, call_sid, caller_hash, args)
                        submitted = await SqlReportSink(get_session_maker()).write(state)
                        log.info("openai.report_saved", short_ref=submitted.short_ref)
                    except Exception:
                        log.exception("openai.report_save_failed")

                    await ws_openai.send(
                        json.dumps(
                            {
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "function_call_output",
                                    "call_id": call_id,
                                    "output": '{"success": true}',
                                },
                            }
                        )
                    )
                    await ws_openai.send(json.dumps({"type": "response.create"}))

            elif msg_type == "error":
                log.error("openai.server_error", detail=msg.get("error"))

    except websockets.ConnectionClosed:
        pass
    except Exception as exc:
        log.error("relay.openai_to_twilio_error", error=str(exc))


def _build_call_state(report_id: str, call_sid: str, caller_hash: str, args: dict) -> CallState:
    """Project the `save_report` tool-call arguments onto a minimal
    `CallState` so `SqlReportSink.write` can persist it the same way
    the DTMF/ASR-driven graph does."""
    state = CallState(
        call_sid=call_sid,
        report_id=UUID(report_id),
        caller_hash=caller_hash,
        direction="inbound",
    )
    if args.get("hazard_type"):
        state.set_slot(
            Slot.HAZARD_TYPE, SlotValue(value=str(args["hazard_type"]), confidence=1.0, source="llm")
        )
    if args.get("location"):
        state.set_slot(Slot.LOCATION, SlotValue(value=str(args["location"]), confidence=1.0, source="llm"))
    if args.get("severity"):
        state.set_slot(Slot.SEVERITY, SlotValue(value=str(args["severity"]), confidence=1.0, source="llm"))
    if args.get("description"):
        state.set_slot(
            Slot.DESCRIPTION, SlotValue(value=str(args["description"]), confidence=1.0, source="llm")
        )
    return state
