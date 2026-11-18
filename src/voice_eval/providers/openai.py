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
from voice_eval.providers._http import (
    map_connection_error,
    map_http_error,
    websocket_request_id,
)
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


def _realtime_setup_error(event: dict[str, Any], request_id: str | None) -> ProviderError:
    """Keep useful provider diagnostics while bounding and classifying them."""
    error = event.get("error")
    error = error if isinstance(error, dict) else {}
    provider_code = error.get("code")
    provider_code = provider_code[:80] if isinstance(provider_code, str) else None
    provider_message = error.get("message")
    provider_message = provider_message[:300] if isinstance(provider_message, str) else None
    provider_param = error.get("param")
    provider_param = provider_param[:120] if isinstance(provider_param, str) else None

    if provider_code == "invalid_api_key":
        code = ProviderErrorCode.AUTHENTICATION
        retryable = False
    elif provider_code in {"rate_limit_exceeded", "too_many_requests"}:
        code = ProviderErrorCode.RATE_LIMIT
        retryable = True
    elif provider_code in {"server_error", "internal_error", "overloaded_error"}:
        code = ProviderErrorCode.TRANSIENT_SERVER
        retryable = True
    else:
        code = ProviderErrorCode.INVALID_INPUT
        retryable = False

    details = [f"code={provider_code}" if provider_code else None]
    details.append(f"param={provider_param}" if provider_param else None)
    details.append(provider_message)
    summary = "; ".join(part for part in details if part)
    message = "OpenAI Realtime returned an error"
    if summary:
        message = f"{message}: {summary}"
    return ProviderError(
        code,
        message,
        retryable=retryable,
        provider_id="openai",
        request_id=request_id,
    )


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
        session = OpenAIRealtimeSession(
            socket,
            interim_results=options.interim_results,
            request_id=websocket_request_id(socket, "x-request-id"),
        )
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
        try:
            await session._send({"type": "session.update", "session": config})
            while True:
                raw_event = await asyncio.wait_for(socket.recv(), self._timeout_seconds)
                try:
                    event = json.loads(raw_event)
                except (json.JSONDecodeError, TypeError) as exc:
                    raise ProviderError(
                        ProviderErrorCode.UNKNOWN,
                        "OpenAI Realtime returned malformed setup data",
                        retryable=False,
                        provider_id=self.provider_id,
                    ) from exc
                if not isinstance(event, dict):
                    raise ProviderError(
                        ProviderErrorCode.UNKNOWN,
                        "OpenAI Realtime returned an invalid setup event",
                        retryable=False,
                        provider_id=self.provider_id,
                    )
                if event.get("type") == "session.updated":
                    return session
                if event.get("type") == "error":
                    raise _realtime_setup_error(
                        event, event.get("request_id") or session._request_id
                    )
        except asyncio.CancelledError:
            try:
                await session.aclose()
            except Exception:
                pass
            raise
        except Exception as exc:
            try:
                await session.aclose()
            except Exception:
                pass
            if isinstance(exc, ProviderError):
                if exc.request_id is not None or session._request_id is None:
                    raise
                raise ProviderError(
                    exc.code,
                    str(exc),
                    retryable=exc.retryable,
                    provider_id=self.provider_id,
                    request_id=session._request_id,
                ) from exc
            mapped = map_connection_error(self.provider_id, exc)
            raise ProviderError(
                mapped.code,
                str(mapped),
                retryable=mapped.retryable,
                provider_id=self.provider_id,
                request_id=session._request_id or mapped.request_id,
            ) from exc


class OpenAIRealtimeSession:
    def __init__(
        self, socket: Any, *, interim_results: bool, request_id: str | None = None
    ) -> None:
        self._socket = socket
        self._interim_results = interim_results
        self._closed = False
        self._finish_requested = False
        self._request_id = request_id

    async def _send(self, payload: dict[str, Any]) -> None:
        try:
            await self._socket.send(json.dumps(payload))
        except Exception as exc:
            raise ProviderError(
                ProviderErrorCode.UNKNOWN,
                "OpenAI Realtime send failed",
                retryable=True,
                provider_id="openai",
                request_id=self._request_id,
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
        try:
            async for message in self._socket:
                try:
                    event = json.loads(message)
                except (json.JSONDecodeError, TypeError) as exc:
                    raise ProviderError(
                        ProviderErrorCode.UNKNOWN,
                        "OpenAI Realtime returned malformed event data",
                        retryable=False,
                        provider_id="openai",
                        request_id=self._request_id,
                    ) from exc
                if not isinstance(event, dict):
                    raise ProviderError(
                        ProviderErrorCode.UNKNOWN,
                        "OpenAI Realtime returned an invalid event",
                        retryable=False,
                        provider_id="openai",
                        request_id=self._request_id,
                    )
                request_id = event.get("request_id")
                if isinstance(request_id, str):
                    self._request_id = request_id
                event_type = event.get("type")
                if event_type == "conversation.item.input_audio_transcription.delta":
                    if not self._interim_results:
                        continue
                    yield StreamingTranscriptEvent(
                        segment_id=event.get("item_id"), text=event.get("delta", ""), is_final=False
                    )
                elif event_type == "conversation.item.input_audio_transcription.completed":
                    terminal = self._finish_requested
                    yield StreamingTranscriptEvent(
                        segment_id=event.get("item_id"),
                        text=event.get("transcript", ""),
                        is_final=True,
                        is_terminal=terminal,
                        request_id=self._request_id if terminal else None,
                    )
                    if terminal:
                        return
                elif event_type in {"error", "conversation.item.input_audio_transcription.failed"}:
                    raise ProviderError(
                        ProviderErrorCode.UNKNOWN,
                        "OpenAI Realtime transcription failed",
                        retryable=True,
                        provider_id="openai",
                        request_id=self._request_id,
                    )
            raise ProviderError(
                ProviderErrorCode.UNKNOWN,
                "OpenAI Realtime stream closed before terminal transcription",
                retryable=True,
                provider_id="openai",
                request_id=self._request_id,
            )
        except ProviderError:
            raise
        except Exception as exc:
            mapped = map_connection_error("openai", exc)
            raise ProviderError(
                mapped.code,
                str(mapped),
                retryable=mapped.retryable,
                provider_id="openai",
                request_id=self._request_id or mapped.request_id,
            ) from exc

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._socket.close()
