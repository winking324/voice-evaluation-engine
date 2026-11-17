"""Deepgram prerecorded and Nova-3 live transcription adapters."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any, cast
from urllib.parse import urlencode

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

_BASE_URL = "https://api.deepgram.com/v1"
_LISTEN_WS_URL = "wss://api.deepgram.com/v1/listen"
_BATCH_MODEL = "nova-3"


def _segments(payload: dict[str, Any]) -> tuple[str, float | None, list[TranscriptionSegment]]:
    results = payload.get("results")
    channels = results.get("channels") if isinstance(results, dict) else None
    channel = channels[0] if isinstance(channels, list) and channels else None
    alternatives = channel.get("alternatives") if isinstance(channel, dict) else None
    alternative = alternatives[0] if isinstance(alternatives, list) and alternatives else None
    if not isinstance(alternative, dict):
        raise ValueError("Deepgram response has no transcript alternative")
    text = alternative.get("transcript", "")
    if not isinstance(text, str):
        raise ValueError("Deepgram transcript must be a string")
    confidence = alternative.get("confidence")
    words = alternative.get("words", [])
    segments = [
        TranscriptionSegment(
            text=word["word"],
            start_ms=round(word["start"] * 1000),
            end_ms=round(word["end"] * 1000),
            confidence=word.get("confidence"),
        )
        for word in words
        if isinstance(word.get("word"), str)
        and isinstance(word.get("start"), int | float)
        and isinstance(word.get("end"), int | float)
    ]
    return text, confidence, segments


class DeepgramProvider(BatchSpeechToTextProvider):
    """Prerecorded transcription through Deepgram's Nova-3 endpoint."""

    provider_id = "deepgram"

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
        raw = await self._audio_loader(audio)
        params: list[tuple[str, str]] = [("model", options.model or _BATCH_MODEL)]
        if options.language or options.locale:
            params.append(("language", options.language or options.locale or "en"))
        if options.timestamps:
            params.append(("utterances", "true"))
        reserved = {"model", "language", "utterances"}
        for key, value in options.provider_options.items():
            if key in reserved:
                raise ProviderError(
                    ProviderErrorCode.INVALID_INPUT,
                    "provider_options cannot override reserved Deepgram parameters",
                    retryable=False,
                    provider_id=self.provider_id,
                )
            if isinstance(value, bool):
                params.append((key, str(value).lower()))
            elif isinstance(value, str | int | float):
                params.append((key, str(value)))
            elif isinstance(value, list) and all(
                isinstance(item, str | int | float) for item in value
            ):
                params.extend((key, str(item)) for item in value)
            else:
                raise ProviderError(
                    ProviderErrorCode.INVALID_INPUT,
                    f"Unsupported Deepgram option value for {key}",
                    retryable=False,
                    provider_id=self.provider_id,
                )
        try:
            response = await self._client.post(
                f"{_BASE_URL}/listen?{urlencode(params)}",
                headers={
                    "Authorization": f"Token {self._api_key}",
                    "Content-Type": audio.media_type,
                },
                content=raw,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise map_http_error(self.provider_id, exc) from exc
        except (ValueError, TypeError) as exc:
            raise ProviderError(
                ProviderErrorCode.UNKNOWN,
                "Deepgram returned an invalid transcription response",
                retryable=False,
                provider_id=self.provider_id,
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderError(
                ProviderErrorCode.UNKNOWN,
                "Deepgram transcription response was not an object",
                retryable=False,
                provider_id=self.provider_id,
            )
        try:
            text, confidence, segments = _segments(payload)
        except (TypeError, ValueError, IndexError) as exc:
            raise ProviderError(
                ProviderErrorCode.UNKNOWN,
                "Deepgram response did not contain a valid transcript",
                retryable=False,
                provider_id=self.provider_id,
                request_id=response.headers.get("dg-request-id"),
            ) from exc
        metadata = payload.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        return TranscriptionResult(
            provider_id=self.provider_id,
            model_id=options.model or _BATCH_MODEL,
            text=text,
            confidence=confidence,
            segments=segments if options.timestamps else [],
            request_id=response.headers.get("dg-request-id") or metadata.get("request_id"),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class DeepgramStreamingProvider(StreamingSpeechToTextProvider):
    """Deepgram `/v1/listen` WebSocket using Nova-3 interim/final results."""

    provider_id = "deepgram"

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

    async def open_session(
        self, options: StreamingTranscriptionOptions
    ) -> DeepgramStreamingSession:
        model = options.model or _BATCH_MODEL
        params: list[tuple[str, str]] = [
            ("model", model),
            ("channels", str(options.channels)),
            ("sample_rate", str(options.sample_rate_hz)),
            ("encoding", options.encoding),
            ("interim_results", str(options.interim_results).lower()),
        ]
        if options.language or options.locale:
            params.append(("language", options.language or options.locale or "en"))
        if options.endpointing_ms is not None:
            params.append(("endpointing", str(options.endpointing_ms)))
        for key, value in options.provider_options.items():
            if key in {
                "model",
                "channels",
                "sample_rate",
                "encoding",
                "interim_results",
                "language",
                "endpointing",
            }:
                raise ProviderError(
                    ProviderErrorCode.INVALID_INPUT,
                    "provider_options cannot override reserved Deepgram stream parameters",
                    retryable=False,
                    provider_id=self.provider_id,
                )
            if isinstance(value, bool):
                params.append((key, str(value).lower()))
            elif isinstance(value, str | int | float):
                params.append((key, str(value)))
            elif isinstance(value, list) and all(isinstance(item, str) for item in value):
                params.extend((key, item) for item in cast(list[str], value))
            else:
                raise ProviderError(
                    ProviderErrorCode.INVALID_INPUT,
                    f"Unsupported Deepgram stream option value for {key}",
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
                f"{_LISTEN_WS_URL}?{urlencode(params)}",
                additional_headers={"Authorization": f"Token {self._api_key}"},
                open_timeout=self._timeout_seconds,
            )
        except Exception as exc:
            raise map_connection_error(self.provider_id, exc) from exc
        return DeepgramStreamingSession(socket)


class DeepgramStreamingSession:
    def __init__(self, socket: Any, request_id: str | None = None) -> None:
        self._socket = socket
        self._closed = False
        self._finish_requested = False
        self._completion_metadata_received = False
        self._request_id = request_id or websocket_request_id(socket, "dg-request-id")

    async def send_audio(self, frame: AudioFrame) -> None:
        try:
            await self._socket.send(frame.data)
        except Exception as exc:
            raise ProviderError(
                ProviderErrorCode.UNKNOWN,
                "Deepgram streaming send failed",
                retryable=True,
                provider_id="deepgram",
                request_id=self._request_id,
            ) from exc

    async def finish_audio(self) -> None:
        self._finish_requested = True
        try:
            await self._socket.send(json.dumps({"type": "CloseStream"}))
        except Exception as exc:
            self._finish_requested = False
            raise ProviderError(
                ProviderErrorCode.UNKNOWN,
                "Deepgram could not finalize the audio stream",
                retryable=True,
                provider_id="deepgram",
                request_id=self._request_id,
            ) from exc

    async def events(self) -> AsyncIterator[StreamingTranscriptEvent]:
        try:
            async for message in self._socket:
                if not isinstance(message, str):
                    raise ProviderError(
                        ProviderErrorCode.UNKNOWN,
                        "Deepgram returned a non-text control event",
                        retryable=False,
                        provider_id="deepgram",
                        request_id=self._request_id,
                    )
                try:
                    payload = json.loads(message)
                except (json.JSONDecodeError, TypeError) as exc:
                    raise ProviderError(
                        ProviderErrorCode.UNKNOWN,
                        "Deepgram returned malformed event data",
                        retryable=False,
                        provider_id="deepgram",
                        request_id=self._request_id,
                    ) from exc
                if not isinstance(payload, dict):
                    raise ProviderError(
                        ProviderErrorCode.UNKNOWN,
                        "Deepgram returned an invalid event",
                        retryable=False,
                        provider_id="deepgram",
                        request_id=self._request_id,
                    )
                request_id = payload.get("request_id")
                if isinstance(request_id, str):
                    self._request_id = request_id
                if payload.get("type") == "Metadata":
                    if self._finish_requested:
                        self._completion_metadata_received = True
                    continue
                if payload.get("type") == "Error":
                    raise ProviderError(
                        ProviderErrorCode.UNKNOWN,
                        "Deepgram streaming transcription failed",
                        retryable=True,
                        provider_id="deepgram",
                        request_id=self._request_id,
                    )
                if payload.get("type") != "Results":
                    continue
                channel_data = payload.get("channel")
                alternatives = (
                    channel_data.get("alternatives") if isinstance(channel_data, dict) else None
                )
                channel = (
                    alternatives[0] if isinstance(alternatives, list) and alternatives else None
                )
                if not isinstance(channel, dict) or not isinstance(
                    channel.get("transcript", ""), str
                ):
                    raise ProviderError(
                        ProviderErrorCode.UNKNOWN,
                        "Deepgram stream returned a malformed transcript event",
                        retryable=False,
                        provider_id="deepgram",
                        request_id=self._request_id,
                    )
                is_final = payload.get("is_final")
                speech_final = payload.get("speech_final")
                if not isinstance(is_final, bool) or (
                    speech_final is not None and not isinstance(speech_final, bool)
                ):
                    raise ProviderError(
                        ProviderErrorCode.UNKNOWN,
                        "Deepgram stream returned invalid finalization flags",
                        retryable=False,
                        provider_id="deepgram",
                        request_id=self._request_id,
                    )
                start = payload.get("start")
                duration = payload.get("duration")
                start_ms = round(start * 1000) if isinstance(start, int | float) else None
                end_ms = (
                    round((start + duration) * 1000)
                    if isinstance(start, int | float) and isinstance(duration, int | float)
                    else None
                )
                yield StreamingTranscriptEvent(
                    text=channel.get("transcript", ""),
                    is_final=is_final,
                    speech_final=speech_final,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    confidence=(
                        channel.get("confidence")
                        if isinstance(channel.get("confidence"), int | float)
                        else None
                    ),
                )
            if not self._finish_requested:
                raise ProviderError(
                    ProviderErrorCode.UNKNOWN,
                    "Deepgram closed the stream before input was finalized",
                    retryable=True,
                    provider_id="deepgram",
                    request_id=self._request_id,
                )
            if not self._completion_metadata_received:
                raise ProviderError(
                    ProviderErrorCode.UNKNOWN,
                    "Deepgram stream closed without completion metadata",
                    retryable=True,
                    provider_id="deepgram",
                    request_id=self._request_id,
                )
            yield StreamingTranscriptEvent(
                text="", is_final=False, is_terminal=True, request_id=self._request_id
            )
        except ProviderError:
            raise
        except Exception as exc:
            mapped = map_connection_error("deepgram", exc)
            raise ProviderError(
                mapped.code,
                str(mapped),
                retryable=mapped.retryable,
                provider_id="deepgram",
                request_id=self._request_id or mapped.request_id,
            ) from exc

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._socket.close()
