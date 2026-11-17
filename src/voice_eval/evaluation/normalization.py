"""Versioned transcript normalization profiles used by ASR scoring."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NormalizationProfile:
    """A named, immutable set of text transformations.

    Profile changes that affect scores must use a new version. Number, date,
    currency, filler-word, and locale-specific rewrites are intentionally not
    part of the initial English lexical profile.
    """

    profile_id: str
    version: str
    unicode_form: str | None = "NFKC"
    case_fold: bool = True
    strip_punctuation: bool = True
    normalize_whitespace: bool = True


ENGLISH_LEXICAL_V1 = NormalizationProfile(
    profile_id="english-lexical",
    version="1",
)

RAW_TEXT_V1 = NormalizationProfile(
    profile_id="raw-text",
    version="1",
    unicode_form=None,
    case_fold=False,
    strip_punctuation=False,
    normalize_whitespace=False,
)


def normalize_text(text: str, profile: NormalizationProfile = ENGLISH_LEXICAL_V1) -> str:
    """Apply a profile symmetrically to a reference or hypothesis.

    Lexical punctuation is treated as a separator; apostrophes are removed so
    curly and straight apostrophe presentation does not affect WER. Numeric
    strings and symbols are otherwise preserved rather than guessed.
    """

    if not isinstance(text, str):
        raise TypeError(f"transcript must be str, got {type(text).__name__}")

    normalized = text
    if profile.unicode_form is not None:
        normalized = unicodedata.normalize(profile.unicode_form, normalized)
    if profile.case_fold:
        normalized = normalized.casefold()
    if profile.strip_punctuation:
        chars: list[str] = []
        for char in normalized:
            if char in {"'", "’", "‘", "ʼ"}:
                continue
            if unicodedata.category(char).startswith("P"):
                chars.append(" ")
            else:
                chars.append(char)
        normalized = "".join(chars)

    if profile.normalize_whitespace:
        return " ".join(normalized.split())
    return normalized
