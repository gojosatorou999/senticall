"""Post-call SMS pin-drop offer (spec §11 + §7.3 ladder attempt 4).

If a caller trailed off on location or hit the reprompt-ladder timeout,
we text them a link to a lightweight web form where they can drop a
map pin. The SMS + web-form pair let a caller finish a report they
couldn't complete on-call — the alternative is losing the report
entirely.

Trigger conditions (evaluated on `/voice/status` `completed`):
- The last CallState in Redis shows `current_node == TIMEOUT_EXIT`, OR
- The LOCATION slot is missing, OR its confidence is below the
  gazetteer accept threshold (spec §9.4 — 0.85 by default).

Suppression:
- Life-safety flag → suppressed. The caller was redirected to 112;
  an SMS with a report-form link is noise on top of an emergency.
- No `short_ref` yet → suppressed. Without a ref there's nothing for
  the web form to attach the caller's pin to. (This happens when
  the call ended before SUBMIT.)
- SMS globally disabled (`SMS_PIN_OFFER_ENABLED=false`) → suppressed.

Phone-number handling (CLAUDE.md invariant #6):
The raw MSISDN is NEVER persisted. It arrives in Twilio's
`/voice/status` webhook as `From=...` and is passed straight into
`SmsPinOfferService.maybe_send(to=...)` — the number lives in that
function's stack frame only, never on the CallState, never in the DB.

Sender failure handling:
`SmsSender.send()` failures are caught + logged. The webhook still
returns 204; a failed SMS should NEVER cause Twilio to retry the
status callback (which would double-submit the report to enrichment
via a re-fire path). The failure is auditable via the structured log
entry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from fg_voice.conversation.state import CallState, NodeId, Slot
from fg_voice.conversation.state_store import CallStateStore
from fg_voice.obs.logging import get_logger
from fg_voice.telephony.twilio_sms import SmsSender, TwilioSmsError

log = get_logger(__name__)

# Spec §7.3 wording (also mirrored in `prompts.yaml::sms_pin_offer`).
# The SMS body is deliberately short — one carrier SMS segment (160
# GSM-7 chars) so a low-signal delivery doesn't get chunked.
_SMS_BODY_TEMPLATE: Final[str] = (
    "Sentinel: your report {short_ref} — drop a pin on the exact spot here: {url}"
)


@dataclass(slots=True)
class SmsPinOfferService:
    """Post-call SMS orchestrator. Callers construct one at boot and
    pass it to the status webhook handler."""

    sender: SmsSender
    state_store: CallStateStore
    from_number: str
    web_base_url: str
    location_confidence_threshold: float = 0.85

    async def maybe_send(self, *, call_sid: str, to_number: str) -> bool:
        """Load the CallState, decide, send. Returns True if an SMS was
        sent, False if the decision was to skip. Never raises: sender
        failures are logged, decision inputs missing → False."""
        if not to_number:
            log.info("sms_pin_offer.skip", call_sid=call_sid, reason="no_to_number")
            return False
        state = await self.state_store.load(call_sid)
        if state is None:
            log.info("sms_pin_offer.skip", call_sid=call_sid, reason="no_call_state")
            return False
        reason = self._suppression_reason(state)
        if reason is not None:
            log.info(
                "sms_pin_offer.skip",
                call_sid=call_sid,
                reason=reason,
                short_ref=state.short_ref,
            )
            return False
        trigger = self._trigger_reason(state)
        if trigger is None:
            log.info(
                "sms_pin_offer.skip",
                call_sid=call_sid,
                reason="no_trigger",
                short_ref=state.short_ref,
            )
            return False
        assert state.short_ref is not None  # narrowed by _suppression_reason
        body = _SMS_BODY_TEMPLATE.format(
            short_ref=state.short_ref,
            url=self._build_pin_url(state.short_ref),
        )
        try:
            sid = await self.sender.send(to=to_number, from_=self.from_number, body=body)
        except TwilioSmsError as exc:
            log.warning(
                "sms_pin_offer.send_failed",
                call_sid=call_sid,
                short_ref=state.short_ref,
                trigger=trigger,
                error=str(exc),
            )
            return False
        log.info(
            "sms_pin_offer.sent",
            call_sid=call_sid,
            short_ref=state.short_ref,
            trigger=trigger,
            message_sid=sid,
        )
        return True

    def _build_pin_url(self, short_ref: str) -> str:
        """`{base}/pin/{short_ref}` — trailing slash on base tolerated."""
        base = self.web_base_url.rstrip("/")
        return f"{base}/pin/{short_ref}"

    def _suppression_reason(self, state: CallState) -> str | None:
        """Reasons to abort BEFORE evaluating trigger — hard skips."""
        if "life_safety" in state.flags:
            return "life_safety_flag"
        if not state.short_ref:
            return "no_short_ref"
        return None

    def _trigger_reason(self, state: CallState) -> str | None:
        """Reasons to send. First match wins (for the log). Returns
        None when the call had a resolved location + no timeout — no
        need to SMS."""
        if state.current_node == NodeId.TIMEOUT_EXIT:
            return "timeout_exit"
        location = state.slots.get(Slot.LOCATION)
        if location is None:
            return "location_missing"
        if location.confidence < self.location_confidence_threshold:
            return "location_low_confidence"
        return None


__all__ = ["SmsPinOfferService"]
