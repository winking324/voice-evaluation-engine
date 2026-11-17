import pytest

from voice_eval.evaluation.normalization import RAW_TEXT_V1
from voice_eval.evaluation.scoring import aggregate_scores, score_transcript


def test_equal_text_after_normalization_has_zero_error() -> None:
    score = score_transcript("Hello, world!", "HELLO WORLD")

    assert score.normalized_reference == "hello world"
    assert score.normalized_hypothesis == "hello world"
    assert score.wer == 0
    assert score.words.errors == 0
    assert score.cer == 0


def test_different_text_reports_edit_counts() -> None:
    score = score_transcript("the cat sat", "the dog")

    assert (score.words.substitutions, score.words.deletions, score.words.insertions) == (1, 1, 0)
    assert score.words.reference_units == 3
    assert score.words.hypothesis_units == 2
    assert score.wer == pytest.approx(2 / 3)


def test_wer_is_not_clamped_when_insertions_exceed_reference() -> None:
    score = score_transcript("yes", "one two three")

    assert score.words.insertions == 2
    assert score.wer == pytest.approx(3.0)


def test_corpus_wer_uses_summed_edit_counts_not_mean_of_sample_rates() -> None:
    short = score_transcript("yes", "no")
    longer = score_transcript("one two three four", "one two three")

    aggregate = aggregate_scores([short, longer])

    assert aggregate.sample_count == 2
    assert aggregate.words.substitutions == 1
    assert aggregate.words.deletions == 1
    assert aggregate.words.reference_units == 5
    assert aggregate.wer == pytest.approx(2 / 5)
    assert aggregate.wer != pytest.approx((short.wer + longer.wer) / 2)


def test_cer_reports_character_edits() -> None:
    score = score_transcript("cat", "cut")

    assert score.characters.substitutions == 1
    assert score.characters.reference_units == 3
    assert score.cer == pytest.approx(1 / 3)


def test_empty_reference_has_undefined_sample_rate_and_counts_hallucinated_text() -> None:
    empty_output = score_transcript("", "")
    hallucination = score_transcript("", "invented text")

    assert empty_output.wer is None
    assert empty_output.cer is None
    assert hallucination.wer is None
    assert hallucination.words.insertions == 2
    assert hallucination.characters.insertions == len("invented text")


def test_empty_reference_insertions_are_included_when_aggregated_with_reference_data() -> None:
    ordinary = score_transcript("one two", "one two")
    hallucination = score_transcript("", "extra words")

    aggregate = aggregate_scores([ordinary, hallucination])

    assert aggregate.words.insertions == 2
    assert aggregate.words.reference_units == 2
    assert aggregate.wer == pytest.approx(1.0)


def test_raw_profile_is_available_for_format_sensitive_comparison() -> None:
    normalized = score_transcript("Hello, world!", "hello world")
    raw = score_transcript("Hello, world!", "hello world", profile=RAW_TEXT_V1)

    assert normalized.wer == 0
    assert raw.wer is not None and raw.wer > 0
    assert raw.raw_reference == "Hello, world!"
    assert raw.raw_hypothesis == "hello world"


def test_raw_character_counts_match_jiwer_whitespace_normalization() -> None:
    score = score_transcript("  Hello,  world!  ", "hello world", profile=RAW_TEXT_V1)

    assert score.raw_reference == "  Hello,  world!  "
    assert score.characters.reference_units == len("Hello, world!")
    assert score.cer is not None


def test_cannot_aggregate_different_profiles() -> None:
    normalized = score_transcript("A", "a")
    raw = score_transcript("A", "a", profile=RAW_TEXT_V1)

    with pytest.raises(ValueError, match="different normalization profiles"):
        aggregate_scores([normalized, raw])


def test_empty_aggregate_is_explicitly_undefined() -> None:
    aggregate = aggregate_scores([])

    assert aggregate.sample_count == 0
    assert aggregate.wer is None
    assert aggregate.cer is None


def test_profile_is_applied_symmetrically_to_reference_and_hypothesis() -> None:
    score = score_transcript("CAFÉ.", "cafe\u0301")

    assert score.normalized_reference == score.normalized_hypothesis
    assert score.wer == 0
