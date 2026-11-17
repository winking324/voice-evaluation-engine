from voice_eval.evaluation.normalization import (
    ENGLISH_LEXICAL_V1,
    RAW_TEXT_V1,
    normalize_text,
)


def test_english_profile_ignores_case_and_punctuation() -> None:
    assert normalize_text("Hello, WORLD!", ENGLISH_LEXICAL_V1) == "hello world"


def test_english_profile_normalizes_unicode_and_whitespace() -> None:
    assert normalize_text("  ＣＡＦÉ\t  works.  ", ENGLISH_LEXICAL_V1) == "café works"


def test_english_profile_does_not_rewrite_numbers_or_dates() -> None:
    assert normalize_text("$1,234 on 05/06/2026", ENGLISH_LEXICAL_V1) == "$1 234 on 05 06 2026"


def test_raw_profile_preserves_text_exactly() -> None:
    raw = "  Hello,  WORLD!\n"
    assert normalize_text(raw, RAW_TEXT_V1) == raw


def test_normalization_rejects_non_text_input() -> None:
    try:
        normalize_text(None)  # type: ignore[arg-type]
    except TypeError as error:
        assert "transcript must be str" in str(error)
    else:
        raise AssertionError("expected a TypeError")
