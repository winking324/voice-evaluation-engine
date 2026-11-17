"""Reference-based word and character error metrics for ASR transcripts."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import jiwer

from voice_eval.evaluation.normalization import (
    ENGLISH_LEXICAL_V1,
    NormalizationProfile,
    normalize_text,
)


@dataclass(frozen=True, slots=True)
class EditCounts:
    substitutions: int
    deletions: int
    insertions: int
    reference_units: int
    hypothesis_units: int

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions


@dataclass(frozen=True, slots=True)
class TranscriptScore:
    profile_id: str
    profile_version: str
    raw_reference: str
    raw_hypothesis: str
    normalized_reference: str
    normalized_hypothesis: str
    words: EditCounts
    wer: float | None
    characters: EditCounts
    cer: float | None


@dataclass(frozen=True, slots=True)
class AggregateScore:
    profile_id: str | None
    profile_version: str | None
    sample_count: int
    words: EditCounts
    wer: float | None
    characters: EditCounts
    cer: float | None


def score_transcript(
    reference: str,
    hypothesis: str,
    profile: NormalizationProfile = ENGLISH_LEXICAL_V1,
) -> TranscriptScore:
    """Score one hypothesis against its reference using the same profile.

    WER/CER are `None` when their reference denominator is zero. Insertions on
    an empty-reference sample are still counted, so corpus aggregation can
    penalize hallucinated text on silence when other samples have references.
    """

    if not isinstance(reference, str):
        raise TypeError(f"reference must be str, got {type(reference).__name__}")
    if not isinstance(hypothesis, str):
        raise TypeError(f"hypothesis must be str, got {type(hypothesis).__name__}")

    normalized_reference = normalize_text(reference, profile)
    normalized_hypothesis = normalize_text(hypothesis, profile)
    word_counts, wer = _score_words(normalized_reference, normalized_hypothesis)
    char_counts, cer = _score_characters(normalized_reference, normalized_hypothesis)

    return TranscriptScore(
        profile_id=profile.profile_id,
        profile_version=profile.version,
        raw_reference=reference,
        raw_hypothesis=hypothesis,
        normalized_reference=normalized_reference,
        normalized_hypothesis=normalized_hypothesis,
        words=word_counts,
        wer=wer,
        characters=char_counts,
        cer=cer,
    )


def aggregate_scores(
    scores: Iterable[TranscriptScore],
    profile: NormalizationProfile | None = None,
) -> AggregateScore:
    """Aggregate corpus counts, weighting every sample by its reference size.

    Empty-reference samples contribute insertions to the corpus numerator but
    zero words to the denominator. Scores from different normalization
    profiles cannot be mixed. For an empty collection, profile metadata is
    unknown unless the caller supplies the run's configured profile.
    """

    items = tuple(scores)
    if not items:
        return AggregateScore(
            profile_id=profile.profile_id if profile is not None else None,
            profile_version=profile.version if profile is not None else None,
            sample_count=0,
            words=EditCounts(0, 0, 0, 0, 0),
            wer=None,
            characters=EditCounts(0, 0, 0, 0, 0),
            cer=None,
        )

    score_profile = (items[0].profile_id, items[0].profile_version)
    if any((item.profile_id, item.profile_version) != score_profile for item in items[1:]):
        raise ValueError("cannot aggregate scores from different normalization profiles")
    if profile is not None and (profile.profile_id, profile.version) != score_profile:
        raise ValueError("provided profile does not match transcript scores")

    words = _sum_counts(item.words for item in items)
    characters = _sum_counts(item.characters for item in items)
    return AggregateScore(
        profile_id=score_profile[0],
        profile_version=score_profile[1],
        sample_count=len(items),
        words=words,
        wer=_rate(words.errors, words.reference_units),
        characters=characters,
        cer=_rate(characters.errors, characters.reference_units),
    )


def _score_words(reference: str, hypothesis: str) -> tuple[EditCounts, float | None]:
    reference_tokens = reference.split()
    hypothesis_tokens = hypothesis.split()
    if not reference_tokens:
        counts = EditCounts(
            substitutions=0,
            deletions=0,
            insertions=len(hypothesis_tokens),
            reference_units=0,
            hypothesis_units=len(hypothesis_tokens),
        )
        return counts, None

    output = jiwer.process_words(reference, hypothesis)
    counts = EditCounts(
        substitutions=output.substitutions,
        deletions=output.deletions,
        insertions=output.insertions,
        reference_units=len(reference_tokens),
        hypothesis_units=len(hypothesis_tokens),
    )
    return counts, counts.errors / counts.reference_units


def _score_characters(reference: str, hypothesis: str) -> tuple[EditCounts, float | None]:
    # JiWER's character transform strips and collapses whitespace. Do so
    # explicitly here to keep the reported denominator consistent, including
    # when the raw-text profile is used for punctuation/case-sensitive scoring.
    reference = " ".join(reference.split())
    hypothesis = " ".join(hypothesis.split())
    reference_units = len(reference)
    hypothesis_units = len(hypothesis)
    if reference_units == 0:
        return (
            EditCounts(
                substitutions=0,
                deletions=0,
                insertions=hypothesis_units,
                reference_units=0,
                hypothesis_units=hypothesis_units,
            ),
            None,
        )

    output = jiwer.process_characters(reference, hypothesis)
    counts = EditCounts(
        substitutions=output.substitutions,
        deletions=output.deletions,
        insertions=output.insertions,
        reference_units=reference_units,
        hypothesis_units=hypothesis_units,
    )
    return counts, counts.errors / counts.reference_units


def _sum_counts(counts: Iterable[EditCounts]) -> EditCounts:
    items = tuple(counts)
    return EditCounts(
        substitutions=sum(item.substitutions for item in items),
        deletions=sum(item.deletions for item in items),
        insertions=sum(item.insertions for item in items),
        reference_units=sum(item.reference_units for item in items),
        hypothesis_units=sum(item.hypothesis_units for item in items),
    )


def _rate(errors: int, reference_units: int) -> float | None:
    if reference_units == 0:
        return None
    return errors / reference_units
