"""Decode bounded audio input into mono PCM16 frames for controlled replay."""

from __future__ import annotations

from importlib import import_module
from io import BytesIO

from voice_eval.domain.contracts import AudioFrame

MAX_STREAM_AUDIO_DURATION_MS = 10 * 60 * 1000


def decode_pcm16_frames(
    raw: bytes,
    *,
    media_type: str,
    target_sample_rate_hz: int,
    chunk_duration_ms: int,
    max_duration_ms: int = MAX_STREAM_AUDIO_DURATION_MS,
) -> list[AudioFrame]:
    """Decode WAV/FLAC and resample to mono PCM16, rejecting oversized inputs."""

    if target_sample_rate_hz <= 0 or chunk_duration_ms <= 0:
        raise ValueError("target sample rate and chunk duration must be positive")
    if media_type.partition(";")[0].strip().lower() not in {
        "audio/flac",
        "audio/wav",
        "audio/x-wav",
    }:
        raise ValueError(f"stream replay supports WAV and FLAC inputs, got {media_type!r}")
    if len(raw) > 100 * 1024 * 1024:
        raise ValueError("stream audio file exceeds the 100 MiB decode limit")
    try:
        np = import_module("numpy")
        sf = import_module("soundfile")
        soxr = import_module("soxr")
    except ImportError as error:
        raise ValueError("stream audio decoding requires the optional 'audio' extra") from error

    try:
        info = sf.info(BytesIO(raw))
        if info.samplerate <= 0 or info.frames <= 0:
            raise ValueError("audio stream metadata is invalid")
        if info.frames * 1000 > info.samplerate * max_duration_ms:
            raise ValueError(f"stream audio exceeds the {max_duration_ms} ms duration limit")
        audio, source_rate = sf.read(
            BytesIO(raw),
            dtype="float32",
            always_2d=True,
            frames=info.frames,
        )
    except Exception as error:
        raise ValueError(f"unsupported or invalid audio format ({media_type}): {error}") from error
    if len(audio) == 0:
        raise ValueError("stream audio file contains no samples")
    duration_ms = len(audio) * 1000 / source_rate
    if duration_ms > max_duration_ms:
        raise ValueError(f"stream audio exceeds the {max_duration_ms} ms duration limit")

    mono = audio.mean(axis=1) if audio.shape[1] > 1 else audio[:, 0]
    if source_rate != target_sample_rate_hz:
        mono = soxr.resample(mono, source_rate, target_sample_rate_hz, quality="HQ")
    pcm16 = (np.clip(mono, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    bytes_per_ms = target_sample_rate_hz * 2 / 1000
    frames: list[AudioFrame] = []
    chunk_bytes = max(2, int(target_sample_rate_hz * 2 * chunk_duration_ms / 1000))
    chunk_bytes -= chunk_bytes % 2
    for sequence, offset in enumerate(range(0, len(pcm16), chunk_bytes)):
        data = pcm16[offset : offset + chunk_bytes]
        frames.append(
            AudioFrame(
                sequence=sequence,
                data=data,
                duration_ms=max(1, round(len(data) / bytes_per_ms)),
            )
        )
    return frames
