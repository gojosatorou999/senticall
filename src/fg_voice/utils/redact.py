"""PII scrubber for caller transcripts.

Applied to the `description` field before it lands on any outbound
artifact (SSE frame, CSV row, webhook alert body). The raw description
stays on `reports.description` for admin review; downstream consumers
get `description_clean` — the redacted twin.

Scope: pattern-based only. Names / addresses / locations need NER +
gazetteer resolution, which are P4/P6 concerns; a naive regex would
either miss most PII or scrub half the text. What's here is the
minimum set that satisfies the DPDP spirit:

- Indian phone numbers (all common shapes)
- Email addresses
- Aadhaar numbers (12 digits, typically space-separated 4/4/4)

Lives under `utils/` — not `enrichment/` per §6 — because the
layered import contract puts `conversation` below `enrichment` and
`SqlReportSink` (in conversation/) is the write-time caller.
`utils/` is the shared bottom layer both can reach."""

from __future__ import annotations

import re
from typing import Final

# ── Phone: Indian mobile shapes. One mega-regex trying to cover every
#    grouping (2+2 prefix, 5+5 body, 3+3+4 body, no separators) is a
#    maintenance nightmare — split into a small ordered list of patterns
#    instead. Each pattern is anchored by (?<!\d)/(?!\d) so it can't
#    slice into a longer digit run.
_PHONE_RES: Final[tuple[re.Pattern[str], ...]] = (
    # +91 XXXXX XXXXX  or  +91-XXXXX-XXXXX
    re.compile(r"(?<!\d)\+?91[\s-][6-9]\d{4}[\s-]\d{5}(?!\d)"),
    # +91XXXXXXXXXX (no separators after the country code)
    re.compile(r"(?<!\d)\+?91[6-9]\d{9}(?!\d)"),
    # 0XXXXXXXXXX (local-prefix variant) or bare 10-digit mobile
    re.compile(r"(?<!\d)0?[6-9]\d{9}(?!\d)"),
    # XXXXX XXXXX / XXXXX-XXXXX (no country prefix)
    re.compile(r"(?<!\d)[6-9]\d{4}[\s-]\d{5}(?!\d)"),
)

# ── Email: RFC-5322-lite. Strict enough to skip obvious non-emails
#    (no `@`) without pulling in a heavyweight parser.
_EMAIL_RE: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
)

# ── Aadhaar: 12 digits grouped 4/4/4 with space or hyphen separators.
#    We DELIBERATELY don't match an unseparated 12-digit run — those
#    are indistinguishable from order ids / transaction refs, and a
#    false positive there would over-scrub ordinary text. The standard
#    Aadhaar rendering (on cards, letters, forms) is always 4-4-4
#    separated, so this covers the practical case.
_AADHAAR_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<!\d)\d{4}[\s-]\d{4}[\s-]\d{4}(?!\d)",
)


PHONE_SENTINEL: Final[str] = "[phone]"
EMAIL_SENTINEL: Final[str] = "[email]"
AADHAAR_SENTINEL: Final[str] = "[aadhaar]"


def redact_pii(text: str | None) -> str:
    """Return `text` with PII patterns replaced by sentinels. None or
    empty in → empty string out.

    Order matters: Aadhaar (4-4-4 separated 12 digits) runs BEFORE
    phone so a real Aadhaar isn't half-caught by the phone regex on
    its last 10 digits."""
    if not text:
        return ""
    out = _AADHAAR_RE.sub(AADHAAR_SENTINEL, text)
    for phone_re in _PHONE_RES:
        out = phone_re.sub(PHONE_SENTINEL, out)
    out = _EMAIL_RE.sub(EMAIL_SENTINEL, out)
    return out


__all__ = [
    "AADHAAR_SENTINEL",
    "EMAIL_SENTINEL",
    "PHONE_SENTINEL",
    "redact_pii",
]
