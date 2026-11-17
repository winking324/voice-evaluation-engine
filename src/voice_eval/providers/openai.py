"""OpenAI transcription API and Realtime transcription adapters."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
from pydantic import SecretStr

from voice_eval.domain.contracts import (
    AudioFrame,
    AudioInput,
    StreamingTranscriptEvent,
    StreamingTranscriptionOptions,
    TranscriptionOptions,
    TranscriptionResult,
    TranscriptionSegment,
)
from voice_eval.providers._http import map_connection_error, map_http_error
from voice_eval.providers.audio import AudioLoader
from voice_eval.providers.errors import ProviderError, ProviderErrorCode
from voice_eval.providers.protocols import BatchSpeechToTextProvider, StreamingSpeechToTextProvider

_BASE_URL = "https://api.openai.com/v1"
_REALTIME_URL = "wss://api.openai.com/v1/realtime?intent=transcription"
_BATCH_MODEL = "gpt-transcribe"
_STREAM_MODEL = "gpt-live-transcribe"
_AUDIO_SUFFIXES = {
    "audio/flac": ".flac",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/mpga": ".mpga",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/webm": ".webm",
    "audio/x-m4a": ".m4a",
    "audio/x-wav": ".wav",
}


class OpenAIProvider(BatchSpeechToTextProvider):
    """File transcription; pin the desired model in TranscriptionOptions."""

    provider_id = "openai"

    def __init__(
        self,
        api_key: SecretStr,
        audio_loader: AudioLoader,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._api_key = api_key.get_secret_value()
        self._audio_loader = audio_loader
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None

    async def transcribe(
        self, audio: AudioInput, options: TranscriptionOptions
    ) -> TranscriptionResult:
        model = options.model or _BATCH_MODEL
        if options.timestamps and model != "whisper-1":
            raise ProviderError(
                ProviderErrorCode.UNSUPPORTED_FEATURE,
                "OpenAI batch timestamps are not exposed for the selected transcription model",
                retryable=False,
                provider_id=self.provider_id,
            )
        media_type = audio.media_type.partition(";")[0].strip().lower()
        suffix = _AUDIO_SUFFIXES.get(media_type)
        if suffix is None:
            raise ProviderError(
                ProviderErrorCode.INVALID_INPUT,
                f"Unsupported OpenAI audio media type: {media_type}",
                retryable=False,
                provider_id=self.provider_id,
            )
        if options.locale and not options.language:
            raise ProviderError(
                ProviderErrorCode.UNSUPPORTED_FEATURE,
                "OpenAI transcription accepts a language code, not a locale-only value",
                retryable=False,
                provider_id=self.provider_id,
            )
        raw = await self._audio_loader(audio)
        data: dict[str, Any] = {}
        for key, value in options.provider_options.items():
            if isinstance(value, bool):
                data[key] = str(value).lower()
            elif isinstance(value, str | int | float):
                data[key] = str(value)
            elif isinstance(value, list) and all(
                isinstance(item, str | int | float | bool) for item in value
            ):
                data[key] = [
                    str(item).lower() if isinstance(item, bool) else str(item) for item in value
                ]
            else:
                raise ProviderError(
                    ProviderErrorCode.INVALID_INPUT,
                    f"Unsupported OpenAI option value for {key}",
                    retryable=False,
                    provider_id=self.provider_id,
                )
        reserved = {"model", "file", "language", "response_format", "timestamp_granularities[]"}
        if reserved.intersection(data):
            raise ProviderError(
                ProviderErrorCode.INVALID_INPUT,
                "provider_options cannot override reserved OpenAI transcription fields",
                retryable=False,
                provider_id=self.provider_id,
            )
        data["model"] = model
        data["response_format"] = "verbose_json" if options.timestamps else "json"
        if options.timestamps:
            data["timestamp_granularities[]"] = "segment"
        language = options.language
        if language:
            data["language"] = language
        try:
            response = await self._client.post(
                f"{_BASE_URL}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                data=data,
                files={"file": (f"audio{suffix}", raw, audio.media_type)},
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise map_http_error(self.provider_id, exc) from exc
        except (ValueError, TypeError) as exc:
            raise ProviderError(
                ProviderErrorCode.UNKNOWN,
                "OpenAI returned an invalid transcription response",
                retryable=False,
                provider_id=self.provider_id,
            ) from exc
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str):
            raise ProviderError(
                ProviderErrorCode.UNKNOWN,
                "OpenAI transcription response did not contain text",
                retryable=False,
                provider_id=self.provider_id,
            )
        segments = []
        if options.timestamps:
            raw_segments = payload.get("segments", [])
            if isinstance(raw_segments, list):
                for segment in raw_segments:
                    if not isinstance(segment, dict):
                        continue
                    start, end = segment.get("start"), segment.get("end")
                    segment_text = segment.get("text", "")
                    if (
                        isinstance(start, int | float)
                        and isinstance(end, int | float)
                        and end >= start
                        and isinstance(segment_text, str)
                    ):
                        segments.append(
                            TranscriptionSegment(
                                text=segment_text,
                                start_ms=round(start * 1000),
                                end_ms=round(end * 1000),
                            )
                        )
        return TranscriptionResult(
            provider_id=self.provider_id,
            model_id=model,
            text=text,
            language=payload.get("language"),
            segments=segments,
            request_id=response.headers.get("x-request-id"),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class OpenAIRealtimeProvider(StreamingSpeechToTextProvider):
    """Realtime transcription via dedicated sessions and partial transcript deltas."""

    provider_id = "openai"

    def __init__(
        self,
        api_key: SecretStr,
        *,
        connect: Callable[..., Any] | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._api_key = api_key.get_secret_value()
        self._connect = connect
        self._timeout_seconds = timeout_seconds

    async def open_session(self, options: StreamingTranscriptionOptions) -> OpenAIRealtimeSession:
        if (
            options.encoding.lower() not in {"pcm16", "linear16"}
            or options.sample_rate_hz != 24000
            or options.channels != 1
        ):
            raise ProviderError(
                ProviderErrorCode.UNSUPPORTED_FEATURE,
                "OpenAI Realtime transcription requires mono PCM16 audio at 24000 Hz",
                retryable=False,
                provider_id=self.provider_id,
            )
        model = options.model or _STREAM_MODEL
        if options.locale and not options.language:
            raise ProviderError(
                ProviderErrorCode.UNSUPPORTED_FEATURE,
                "OpenAI Realtime transcription accepts a language code, not a locale-only value",
                retryable=False,
                provider_id=self.provider_id,
            )
        if self._connect is None:
            from websockets.asyncio.client import connect

            connector: Callable[..., Any] = connect
        else:
            connector = self._connect
        try:
            socket = await connector(
                _REALTIME_URL,
                additional_headers={"Authorization": f"Bearer {self._api_key}"},
                open_timeout=self._timeout_seconds,
            )
        except Exception as exc:
            raise map_connection_error(self.provider_id, exc) from exc
        session = OpenAIRealtimeSession(socket, interim_results=options.interim_results)
        config: dict[str, Any] = {
            "type": "transcription",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {"model": model},
                    "turn_detection": None,
                }
            },
        }
        language = options.language
        if language:
            config["audio"]["input"]["transcription"]["languages"] = [language]
        options_map = dict(options.provider_options)
        for key in ("prompt", "keywords", "delay"):
            if key in options_map:
                value = options_map.pop(key)
                valid = (
                    isinstance(value, str)
                    if key in {"prompt", "delay"}
                    else isinstance(value, list) and all(isinstance(item, str) for item in value)
                )
                if not valid:
                    await session.aclose()
                    raise ProviderError(
                        ProviderErrorCode.INVALID_INPUT,
                        f"Invalid OpenAI Realtime option value for {key}",
                        retryable=False,
                        provider_id=self.provider_id,
                    )
                config["audio"]["input"]["transcription"][key] = value
        if options_map:
            await session.aclose()
            raise ProviderError(
                ProviderErrorCode.INVALID_INPUT,
                "Unsupported OpenAI Realtime provider_options",
                retryable=False,
                provider_id=self.provider_id,
            )
        await session._send({"type": "session.update", "session": config})
        try:
            while True:
                event = json.loads(await asyncio.wait_for(socket.recv(), self._timeout_seconds))
                if event.get("type") == "session.updated":
                    return session
                if event.get("type") == "error":
                    await session.aclose()
                    raise ProviderError(
                        ProviderErrorCode.INVALID_INPUT,
                        "OpenAI rejected the Realtime transcription session configuration",
                        retryable=False,
                        provider_id=self.provider_id,
                    )
        except TimeoutError as exc:
            await session.aclose()
            raise ProviderError(
                ProviderErrorCode.TIMEOUT,
                "OpenAI Realtime session setup timed out",
                retryable=True,
                provider_id=self.provider_id,
            ) from exc


class OpenAIRealtimeSession:
    def __init__(self, socket: Any, *, interim_results: bool) -> None:
        self._socket = socket
        self._interim_results = interim_results
        self._closed = False
        self._finish_requested = False

    async def _send(self, payload: dict[str, Any]) -> None:
        try:
            await self._socket.send(json.dumps(payload))
        except Exception as exc:
            raise ProviderError(
                ProviderErrorCode.UNKNOWN,
                "OpenAI Realtime send failed",
                retryable=True,
                provider_id="openai",
            ) from exc

    async def send_audio(self, frame: AudioFrame) -> None:
        await self._send(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(frame.data).decode("ascii"),
            }
        )

    async def finish_audio(self) -> None:
        self._finish_requested = True
        try:
            await self._send({"type": "input_audio_buffer.commit"})
        except Exception:
            self._finish_requested = False
            raise

    async def events(self) -> AsyncIterator[StreamingTranscriptEvent]:
        async for message in self._socket:
            event = json.loads(message)
            event_type = event.get("type")
            if event_type == "conversation.item.input_audio_transcription.delta":
                if not self._interim_results:
                    continue
                yield StreamingTranscriptEvent(
                    segment_id=event.get("item_id"), text=event.get("delta", ""), is_final=False
                )
            elif event_type == "conversation.item.input_audio_transcription.completed":
                yield StreamingTranscriptEvent(
                    segment_id=event.get("item_id"),
                    text=event.get("transcript", ""),
                    is_final=True,
                    is_terminal=self._finish_requested,
                )
            elif event_type in {"error", "conversation.item.input_audio_transcription.failed"}:
                raise ProviderError(
                    ProviderErrorCode.UNKNOWN,
                    "OpenAI Realtime transcription failed",
                    retryable=True,
                    provider_id="openai",
                )

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._socket.close()
