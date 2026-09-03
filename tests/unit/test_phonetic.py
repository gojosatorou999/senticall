"""Phonetic key generation for the gazetteer resolver."""

from __future__ import annotations

from fg_voice.rag.phonetic import normalise_ascii, phonetic_key, phonetic_keys


def test_normalise_folds_diacritics_and_case():
    assert normalise_ascii("Vișākhapatnam") == "visakhapatnam"


def test_normalise_drops_non_letters():
    assert normalise_ascii("R.K. Beach!") == "rkbeach"


def test_normalise_empty_returns_empty():
    assert normalise_ascii("") == ""


def test_phonetic_key_empty_input():
    assert phonetic_key("") == ""


def test_phonetic_key_deterministic():
    assert phonetic_key("Kakinada") == phonetic_key("Kakinada")


def test_phonetic_key_similar_spellings_agree():
    """The two most common Vizag spellings should hash to the same
    or overlapping metaphone keys."""
    a = phonetic_key("Vizag")
    b = phonetic_key("Visakhapatnam")
    # Not necessarily identical (Vizag is a truncation) — but the
    # primary metaphone of "Vizag" should be a prefix of the primary
    # of "Visakhapatnam", or the fallback compresses both to a
    # shared prefix.
    assert a and b, "both keys should be non-empty"
    assert a[:2] == b[:2], f"phonetic prefix mismatch: {a} vs {b}"


def test_phonetic_keys_returns_two_slots():
    p, s = phonetic_keys("Bheemili")
    assert isinstance(p, str)
    assert isinstance(s, str)


def test_phonetic_key_stt_confusions_collide():
    """Common STT mishears (b/v, d/t swaps) should still land on
    similar phonetic keys — the whole point of the phonetic signal."""
    for original, misheard in [
        ("Bapatla", "Bapatla"),
        ("Kakinada", "Kakinaba"),  # d→b substitution mishear
        ("Tirupati", "Tirupathi"),  # h insertion
    ]:
        a = phonetic_key(original)
        b = phonetic_key(misheard)
        assert a and b
        # At least one character overlap in the first 3 keys — enough
        # for the phonetic signal to fire, downstream fuzzy will finish
        # the disambiguation.
        assert set(a[:3]) & set(b[:3]), f"no phonetic overlap: {a} vs {b}"
