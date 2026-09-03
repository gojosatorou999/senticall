"""Phonetic key generation for the gazetteer resolver — spec §10.1.

The gazetteer matches spoken fragments to canonical place names by
combining exact + substring + phonetic + fuzzy signals. This module
owns the phonetic side: given "Kakinada" and "Kakanada" (a common
STT mishear), produce keys that hash to the same value so exact-key
matching finds the candidate before fuzzy matching has to.

Implementation: **Double Metaphone** when the `metaphone` package is
importable (the `[rag]` extras dep). Fallback: a simple Soundex-style
compression that keeps consonant skeletons + drops vowels, which
catches the majority of common Indian-English STT confusions
(vowel substitution, "v" vs "w", "th" vs "d/t") without an external
dep. Both paths return a normalised uppercase ASCII string suitable
as a dict key.

The fallback is deliberately small — it's the safety net, not the
production choice. The `[rag]` extras must be installed for
production-quality matching."""

from __future__ import annotations

import re
import unicodedata
from typing import Final

try:
    from metaphone import doublemetaphone

    _HAVE_METAPHONE = True
except ImportError:  # pragma: no cover — depends on optional extras
    _HAVE_METAPHONE = False


# Regex to strip anything that isn't ASCII letters. Applied AFTER
# unicode normalisation so "Vișākhapatnam" → "Visakhapatnam" (the
# umlaut and diacritics get folded to their base letters).
_NON_LETTER = re.compile(r"[^a-z]", flags=re.ASCII)

# Consonant compression map for the fallback path. Groups letters
# that Indian-English speakers often confuse in fast speech. Any bug
# here bites match recall, not precision — a wrong match still has
# to clear the fuzzy-score threshold downstream.
_CONSONANT_GROUPS: Final[dict[str, str]] = {
    "b": "B",
    "p": "B",
    "v": "B",
    "f": "B",
    "w": "B",  # labials
    "c": "K",
    "g": "K",
    "j": "K",
    "k": "K",
    "q": "K",
    "x": "K",  # velars
    "d": "T",
    "t": "T",
    "z": "T",
    "s": "T",  # dentals + sibilants
    "l": "L",
    "r": "L",  # liquids (Indian retroflex/lateral confusion)
    "m": "N",
    "n": "N",  # nasals
    "h": "H",
    "y": "Y",
}


def normalise_ascii(text: str) -> str:
    """Fold Unicode diacritics, lowercase, drop non-letter chars.
    Shared entry point for both phonetic paths so any downstream
    comparison is on the same alphabet."""
    if not text:
        return ""
    # NFKD split: 'ā' → 'a' + combining macron; encode+decode drops
    # the combining chars via 'ignore'. Fast + stdlib-only.
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return _NON_LETTER.sub("", folded.lower())


def phonetic_key(text: str) -> str:
    """Return a phonetic hash of `text`. Empty input → empty string.

    When `metaphone` is available, returns the primary Double Metaphone
    key (uppercase ASCII, typically 4-8 chars). Falls back to the
    consonant-compression rule when the dep is absent — good enough
    for smoke tests + dev deploys, meaningfully weaker on real STT
    output than the real metaphone package."""
    normalised = normalise_ascii(text)
    if not normalised:
        return ""
    if _HAVE_METAPHONE:
        primary, _secondary = doublemetaphone(normalised)
        return primary or normalised.upper()
    return _fallback_consonant_compress(normalised)


def _fallback_consonant_compress(text: str) -> str:
    """Consonant-only compression with the group map above. Drops
    doubled consonants (`kakinada` → `KKNT` → `KNT`). Deliberately
    lossy: fewer collisions than a raw drop-vowels would produce."""
    parts: list[str] = []
    prev: str | None = None
    for ch in text:
        mapped = _CONSONANT_GROUPS.get(ch, "")
        if not mapped or mapped == prev:
            continue
        parts.append(mapped)
        prev = mapped
    return "".join(parts)


def phonetic_keys(text: str) -> tuple[str, str]:
    """Return both the primary and secondary Double Metaphone keys
    when available; otherwise (fallback_key, "") so the caller can
    treat the tuple uniformly and the secondary is just empty."""
    normalised = normalise_ascii(text)
    if not normalised:
        return ("", "")
    if _HAVE_METAPHONE:
        p, s = doublemetaphone(normalised)
        return (p or normalised.upper(), s or "")
    return (_fallback_consonant_compress(normalised), "")


__all__ = [
    "normalise_ascii",
    "phonetic_key",
    "phonetic_keys",
]
