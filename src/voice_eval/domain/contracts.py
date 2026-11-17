"""Provider-neutral values shared by the evaluation engine and adapters."""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


class ContractModel(BaseModel):
    """Base for validated internal contracts; unknown fields fail fast."""

    model_config = ConfigDict(extra="forbid")


class AudioInput(ContractModel):
    """A canonical relative key for trusted storage; never an input URL or host path.

    Storage adapters must resolve the key beneath their configured root and verify
    the resolved path remains inside that root before reading it.
    """

    storage_key: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    duration_ms: int = Field(gt=0)
    media_type: str = Field(min_length=1)
    sample_rate_hz: int | None = Field(default=None, gt=0)
    channels: int | None = Field(default=None, gt=0)

    @field_validator("storage_key")
    @classmethod
    def validate_storage_key(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("storage_key must not have surrounding whitespace")

        decoded = value
        for _ in range(8):
            next_value = unquote(decoded, errors="strict")
            if next_value == decoded:
                break
            decoded = next_value
        else:
            raise ValueError("storage_key has too many nested URL-encoding layers")

        parts = decoded.split("/")
        if (
            decoded.startswith(("/", "\\"))
            or "\\" in decoded
            or urlsplit(decoded).scheme
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError("storage_key must be a canonical relative key, not a URL or path")
        return decoded


class TranscriptionOptions(ContractModel):
    """Options common to one-shot transcription requests."""

    model: str = Field(min_length=1)
    language: str | None = None
    locale: str | None = None
    timestamps: bool = False
    provider_options: dict[str, JsonValue] = Field(default_factory=dict)


class TranscriptionSegment(ContractModel):
    """An optional provider segment with timing in milliseconds."""

    text: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_time_order(self) -> TranscriptionSegment:
        if self.end_ms < self.start_ms:
            raise ValueError("end_ms must be greater than or equal to start_ms")
        return self


class TranscriptionUsage(ContractModel):
    """Usage reported by a provider; absent values remain unknown, not zero."""

    billed_audio_duration_ms: int | None = Field(default=None, ge=0)


class TranscriptionResult(ContractModel):
    """Raw provider output. Normalization and scoring happen downstream."""

    provider_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    text: str
    language: str | None = None
    segments: list[TranscriptionSegment] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)
    usage: TranscriptionUsage = Field(default_factory=TranscriptionUsage)
    request_id: str | None = None
    provider_duration_ms: int | None = Field(default=None, ge=0)


class StreamingTranscriptionOptions(ContractModel):
    """Options for an audio-frame streaming session."""

    model: str = Field(min_length=1)
    sample_rate_hz: int = Field(gt=0)
    channels: int = Field(default=1, gt=0)
    encoding: str = Field(min_length=1)
    language: str | None = None
    locale: str | None = None
    interim_results: bool = True
    endpointing_ms: int | None = Field(default=None, gt=0)
    provider_options: dict[str, JsonValue] = Field(default_factory=dict)


class AudioFrame(ContractModel):
    """A chunk of source audio sent over one streaming attempt."""

    sequence: int = Field(ge=0)
    data: bytes = Field(min_length=1)
    duration_ms: int = Field(gt=0)


class StreamingTranscriptEvent(ContractModel):
    """A provider-neutral transcript event; receive timing is stamped by the runner."""

    sequence: int | None = Field(default=None, ge=0)
    segment_id: str | None = None
    text: str
    is_final: bool
    speech_final: bool | None = None
    start_ms: int | None = Field(default=None, ge=0)
    end_ms: int | None = Field(default=None, ge=0)
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_time_order(self) -> StreamingTranscriptEvent:
        if self.start_ms is not None and self.end_ms is not None and self.end_ms < self.start_ms:
            raise ValueError("end_ms must be greater than or equal to start_ms")
        return self
