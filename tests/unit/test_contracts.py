from typing import cast

import pytest
from pydantic import JsonValue, ValidationError

from voice_eval.domain.contracts import (
    AudioFrame,
    AudioInput,
    StreamingTranscriptEvent,
    StreamingTranscriptionOptions,
    TranscriptionOptions,
    TranscriptionResult,
    TranscriptionSegment,
)
from voice_eval.providers.errors import ProviderError, ProviderErrorCode


def test_audio_input_requires_content_hash_and_positive_duration() -> None:
    audio = AudioInput(
        storage_key="dataset/sample.wav",
        sha256="a" * 64,
        duration_ms=1000,
        media_type="audio/wav",
    )

    assert audio.duration_ms == 1000

    with pytest.raises(ValidationError):
        AudioInput(
            storage_key="dataset/sample.wav",
            sha256="not-a-checksum",
            duration_ms=0,
            media_type="audio/wav",
        )


@pytest.mark.parametrize(
    "storage_key",
    [
        "https://example.com/audio.wav",
        "/etc/passwd",
        "../secret.wav",
        "clips/%2e%2e/secret.wav",
        "%2Fetc/passwd",
        "foo%5c..%5csecret",
        "%252Fetc/passwd",
        "foo%255c%252e%252e%255csecret",
    ],
)
def test_audio_input_rejects_urls_absolute_paths_and_encoded_traversal(storage_key: str) -> None:
    with pytest.raises(ValidationError):
        AudioInput(
            storage_key=storage_key,
            sha256="a" * 64,
            duration_ms=1000,
            media_type="audio/wav",
        )


def test_audio_input_returns_canonical_storage_key() -> None:
    audio = AudioInput(
        storage_key="dataset/hello%20world.wav",
        sha256="a" * 64,
        duration_ms=1000,
        media_type="audio/wav",
    )

    assert audio.storage_key == "dataset/hello world.wav"


def test_provider_options_accept_nested_json_values() -> None:
    batch_options = TranscriptionOptions(
        model="model-v1",
        provider_options={"keyterms": ["OpenAI", "Deepgram"], "prompt": {"domain": "dictation"}},
    )
    streaming_options = StreamingTranscriptionOptions(
        model="stream-v1",
        sample_rate_hz=16000,
        encoding="linear16",
        provider_options={"keyterm": ["OpenAI", "Deepgram"]},
    )

    assert batch_options.provider_options["keyterms"] == ["OpenAI", "Deepgram"]
    assert streaming_options.provider_options["keyterm"] == ["OpenAI", "Deepgram"]


def test_provider_options_reject_non_json_values() -> None:
    with pytest.raises(ValidationError):
        TranscriptionOptions(
            model="model-v1", provider_options={"callback": cast(JsonValue, object())}
        )


def test_transcription_result_preserves_raw_text() -> None:
    result = TranscriptionResult(
        provider_id="example",
        model_id="model-v1",
        text="May fifth, 2026.",
    )

    assert result.text == "May fifth, 2026."
    assert result.segments == []
    assert result.usage.billed_audio_duration_ms is None


def test_segments_reject_negative_or_reversed_times() -> None:
    with pytest.raises(ValidationError):
        TranscriptionSegment(text="hello", start_ms=10, end_ms=5)


def test_stream_event_keeps_interim_and_speech_final_semantics_separate() -> None:
    interim = StreamingTranscriptEvent(text="hello", is_final=False)
    utterance_end = StreamingTranscriptEvent(text="hello there", is_final=True, speech_final=True)
    sample_complete = StreamingTranscriptEvent(
        text="hello there", is_final=True, speech_final=True, is_terminal=True
    )

    assert interim.is_final is False
    assert interim.is_terminal is False
    assert utterance_end.is_final is True
    assert utterance_end.speech_final is True
    assert utterance_end.is_terminal is False
    assert sample_complete.is_terminal is True


def test_audio_frame_requires_data_and_duration() -> None:
    frame = AudioFrame(sequence=0, data=b"\x00\x01", duration_ms=20)

    assert frame.sequence == 0
    with pytest.raises(ValidationError):
        AudioFrame(sequence=0, data=b"", duration_ms=20)


def test_provider_error_exposes_retryability_and_request_correlation() -> None:
    error = ProviderError(
        ProviderErrorCode.RATE_LIMIT,
        "rate limited",
        retryable=True,
        provider_id="example",
        request_id="req-123",
    )

    assert error.retryable is True
    assert error.provider_id == "example"
    assert error.request_id == "req-123"
