"""Capability-based contracts implemented by STT provider adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from voice_eval.domain.contracts import (
    AudioFrame,
    AudioInput,
    StreamingTranscriptEvent,
    StreamingTranscriptionOptions,
    TranscriptionOptions,
    TranscriptionResult,
)


class BatchSpeechToTextProvider(Protocol):
    """One-shot file transcription capability."""

    @property
    def provider_id(self) -> str: ...

    async def transcribe(
        self, audio: AudioInput, options: TranscriptionOptions
    ) -> TranscriptionResult:
        """Transcribe audio; raise the normalized ProviderError for provider failures."""
        ...


class StreamingSpeechToTextSession(Protocol):
    """A single streaming attempt. The runner stamps receive times on yielded events."""

    async def send_audio(self, frame: AudioFrame) -> None:
        """Send the next frame; raise ProviderError for normalized failures."""
        ...

    async def finish_audio(self) -> None:
        """Signal that no more frames will be sent."""
        ...

    def events(self) -> AsyncIterator[StreamingTranscriptEvent]: ...

    async def aclose(self) -> None: ...


class StreamingSpeechToTextProvider(Protocol):
    """Streaming transcription capability, independent of the batch interface."""

    @property
    def provider_id(self) -> str: ...

    async def open_session(
        self, options: StreamingTranscriptionOptions
    ) -> StreamingSpeechToTextSession: ...
