"""DTMF capture from Twilio Media Streams `dtmf` events + digit→slot
mapping.

Twilio surfaces each keypress as a single-digit event on the same WS
that carries audio. This module keeps a per-call buffer (short, since
we only ever consult it when a reprompt has explicitly enabled DTMF)
and translates a completed digit sequence into a canonical slot value
via the `dtmf_map` on the current prompt."""

from __future__ import annotations

from dataclasses import dataclass, field

# Twilio only emits 0-9 * # on this event; kept permissive so an
# unexpected value logs cleanly rather than crashing the audio loop.
_VALID_DIGITS: frozenset[str] = frozenset("0123456789*#")


class DtmfError(ValueError):
    """Raised when a digit is not one of 0-9 * #."""


@dataclass(slots=True)
class DtmfBuffer:
    """Per-call keypress buffer. Cleared when the runner consumes the
    result. Kept tiny (max 8 digits) — v1 uses single-digit categorical
    maps, but leaving headroom for a future PIN-entry flow."""

    max_length: int = 8
    _digits: list[str] = field(default_factory=list)

    def push(self, digit: str) -> None:
        if digit not in _VALID_DIGITS:
            raise DtmfError(f"invalid DTMF digit: {digit!r}")
        if len(self._digits) >= self.max_length:
            # Overflow: drop the oldest, keep the caller's most recent
            # intent. A caller who fat-fingers ten digits still gets
            # heard, they don't get an error.
            self._digits.pop(0)
        self._digits.append(digit)

    def take(self) -> str:
        """Return the buffered digits and clear the buffer."""
        joined = "".join(self._digits)
        self._digits.clear()
        return joined

    def peek(self) -> str:
        return "".join(self._digits)

    def clear(self) -> None:
        self._digits.clear()

    def __len__(self) -> int:
        return len(self._digits)


def map_digit(digit: str, dtmf_map: dict[str, str] | None) -> str | None:
    """Return the canonical slot value for `digit`, or None if the map
    is absent (DTMF not armed) or the digit is not in the map."""
    if dtmf_map is None:
        return None
    return dtmf_map.get(digit)


__all__ = ["DtmfBuffer", "DtmfError", "map_digit"]
